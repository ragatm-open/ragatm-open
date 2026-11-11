"""courriel déposé → pièces jointes indexées, retrouvées et citées.

Bout en bout par les voies de PRODUCTION : dépôt HTTP, extraction (processus isolé),
indexation (publication atomique), retrieval (`query_rag`), projection des citations
(`normalize_source_for_api`), suppression, réindexation, dédoublonnage, isolation.
Chaque pièce porte un MARQUEUR unique que l'on traque ensuite. Aucune donnée réelle.
"""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest

from apps.chat.services.chat_sources import normalize_source_for_api
from apps.rag_core.models import Chunk, Document
from apps.rag_core.services.query import numeric_evidence as NE
from apps.rag_core.services.query import query_rag

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import build_pptx, build_xlsx, make_docx, make_pdf_pages  # noqa: E402
from _email_builders import build_eml  # noqa: E402

pytestmark = pytest.mark.django_db

#: L'OCR d'image exige le binaire tesseract (présent sur les images de déploiement, absent
#: du runner CI) : sans lui, l'image jointe est hors du jeu et ses assertions sont sautées.
HAS_OCR = shutil.which("tesseract") is not None
needs_ocr = pytest.mark.skipif(not HAS_OCR, reason="binaire tesseract absent (runner CI)")

M_BODY = "CORPS-ORION-1180"
M_PDF = "PDFORION7731"
M_XLSX = "XLSXORION4402"
M_PPTX = "PPTXORION9915"
M_DOCX = "DOCXORION3307"
M_PNG = "IMAGE ORION"


def _pdf(marker: str, pages: int = 3) -> bytes:
    texts = []
    for i in range(1, pages + 1):
        text = f"Architecture cible du projet, page {i}."
        if i == 2:
            text = f"Section architecture : le code de reference {marker} decrit le bus de donnees."
        texts.append(text)
    return make_pdf_pages(texts)


def _png(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((20, 60), text, fill="black", font=ImageFont.load_default(size=56))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _full_eml(subject="Reunion de validation ORION") -> bytes:
    """Courriel `.eml` complet (édition Open : le conteneur Outlook `.msg` n'existe plus)."""
    return build_eml(
        subject=subject,
        message_id="<reunion-orion@example.invalid>",
        plain=(f"Veuillez trouver les documents de reference pour la reunion. Code {M_BODY}.\n\n"
               "Merci de valider le budget avant vendredi."),
        attachments=[
            ("rapport.pdf", _pdf(M_PDF), "application/pdf"),
            ("budget.xlsx", build_xlsx({"Previsions": [["Poste", "Montant", "Code"],
                                                        ["Etudes", 125000, M_XLSX]]}), _XLSX_MIME),
            ("presentation.pptx", build_pptx([
                {"title": "Introduction", "bullets": ["Contexte"]},
                {"title": f"Planning {M_PPTX}", "bullets": ["Jalon 1", "Jalon 2"]},
            ]), _PPTX_MIME),
            ("note.docx", make_docx(title=f"Note {M_DOCX}", paragraphs=("Synthese du comite.",)), _DOCX_MIME),
        ] + ([("schema.png", _png(M_PNG), "image/png")] if HAS_OCR else []),
    )


@pytest.fixture
def email_space(settings, rag_space):
    settings.DOCUMENT_EML_ENABLED = True
    settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = [str(rag_space.id)]
    return rag_space


def _upload(client, space, data: bytes, name: str):
    stream = io.BytesIO(data)
    stream.name = name
    return client.post("/api/v1/documents/upload/", data={"rag_space_id": str(space.id), "file": stream},
                       format="multipart")


def _ingest(client, space, data: bytes, name: str) -> Document:
    res = _upload(client, space, data, name)
    assert res.status_code == 200, res.data
    return Document.objects.get(id=res.data["document"]["id"])


def _attachments(doc: Document) -> dict:
    doc.refresh_from_db()
    return {a["filename"]: a for a in (doc.meta or {}).get("extraction", {}).get("attachments") or []}


def _cited(space, question: str, marker: str):
    """Première source dont l'extrait contient le marqueur, projetée comme l'API chat."""
    payload = query_rag(rag_space_id=space.id, question=question)
    for rank, src in enumerate(payload.get("sources") or [], 1):
        text = (src.get("text") or "") + " " + str(src.get("snippet") or "")
        if marker.lower() in text.lower():
            return normalize_source_for_api(src, rank)
    return None


class TestCourrielEtPiecesJointes:
    def test_pieces_indexees_retrouvables_et_citees(self, api_client, email_space):
        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        doc.refresh_from_db()
        assert doc.status == "READY"
        rows = _attachments(doc)
        names = ["rapport.pdf", "budget.xlsx", "presentation.pptx", "note.docx"] + (["schema.png"] if HAS_OCR else [])
        for name in names:
            assert rows[name]["status"] == "INDEXED", (name, rows[name])
            assert rows[name]["fragment_count"] >= 1

        # Recherche dans le CORPS : fragment de corps, pas de pièce.
        body = _cited(email_space, f"Que demande le message {M_BODY} ?", M_BODY)
        assert body and body["meta"].get("email_part") == "body"

        # Recherche dans chaque PIÈCE : bon parent, bonne pièce, bon repère interne.
        pdf = _cited(email_space, f"Quelle architecture decrit {M_PDF} ?", M_PDF)
        assert pdf["document_id"] == str(doc.id)
        assert pdf["meta"]["email_part"] == "attachment"
        assert pdf["meta"]["email_attachment_filename"] == "rapport.pdf"
        assert pdf["meta"]["email_subject"] == "Reunion de validation ORION"
        assert pdf["page_number"] == 2

        xlsx = _cited(email_space, f"Quel montant pour {M_XLSX} ?", M_XLSX)
        assert xlsx["meta"]["email_attachment_filename"] == "budget.xlsx"
        assert xlsx["meta"]["sheet_name"] == "Previsions" and xlsx["meta"].get("cell_range")

        pptx = _cited(email_space, f"Que montre le planning {M_PPTX} ?", M_PPTX)
        assert pptx["meta"]["email_attachment_filename"] == "presentation.pptx"
        assert pptx["meta"]["slide_number"] == 2

        docx = _cited(email_space, f"Que contient la note {M_DOCX} ?", M_DOCX)
        assert docx["meta"]["email_attachment_filename"] == "note.docx"

        if HAS_OCR:
            png = _cited(email_space, "Que montre l'image ORION ?", "ORION")
            assert png is not None
            image_chunks = Chunk.objects.filter(document=doc, meta__email_attachment_filename="schema.png")
            assert image_chunks.exists() and all(c.meta.get("source_is_ocr") is True for c in image_chunks)

        # Relation message ↔ pièces : une section dédiée nomme les pièces jointes.
        rel = Chunk.objects.filter(document=doc, meta__email_role="attachments")
        assert rel.exists() and "budget.xlsx" in rel.first().text

        # Aucun fragment indexé deux fois : pas de texte intégral EN PLUS des rails spécialisés.
        texts = list(Chunk.objects.filter(document=doc).values_list("text", flat=True))
        assert len(texts) == len(set(texts))
        assert sum(M_PDF in t for t in texts) == 1

    @needs_ocr
    def test_preuve_numerique_jamais_rehaussee_par_le_courriel(self, api_client, email_space):
        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        statuses = {}
        for ch in Chunk.objects.filter(document=doc, meta__email_part="attachment"):
            status, _ = NE.classify({"meta": ch.meta}, {})
            statuses.setdefault(ch.meta["email_attachment_filename"], set()).add(status)
        assert statuses["schema.png"] == {NE.OCR_OTHER}          # OCR reste OCR
        assert NE.NATIVE_TEXT in statuses["rapport.pdf"]          # texte natif reste natif

    def test_api_detail_expose_les_statuts_sans_contenu(self, api_client, email_space):
        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        res = api_client.get(f"/api/v1/documents/{doc.id}/")
        assert res.status_code == 200
        blob = str(res.data)
        assert "rapport.pdf" in blob and "INDEXED" in blob
        assert M_PDF not in blob and M_XLSX not in blob  # aucun contenu de pièce dans l'API


class TestAdmissionEtIsolation:
    def test_espace_non_admis_refuse_nommement(self, api_client, rag_space, settings):
        settings.DOCUMENT_EML_ENABLED = True
        settings.DOCUMENT_EMAIL_RAGSPACE_IDS = []
        res = _upload(api_client, rag_space, build_eml(), "m.eml")
        assert res.status_code == 400 and res.data["detail"] == "email_ingestion_not_enabled_for_space"
        assert not Document.objects.filter(rag_space=rag_space).exists()

    def test_eml_desactive_refus_nomme(self, api_client, email_space, settings):
        settings.DOCUMENT_EML_ENABLED = False
        res = _upload(api_client, email_space, build_eml(), "m.eml")
        assert res.status_code == 400 and res.data["detail"] == "eml_ingestion_disabled"

    def test_aucune_piece_visible_hors_de_son_espace(self, api_client, email_space, foreign_setup, settings):
        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        space_b = foreign_setup["rag_space"]
        payload = query_rag(rag_space_id=space_b.id, question=f"Quelle architecture decrit {M_PDF} ?")
        assert M_PDF not in str(payload.get("sources") or [])
        assert str(doc.id) not in str(payload.get("sources") or [])
        assert foreign_setup["client"].get(f"/api/v1/documents/{doc.id}/").status_code == 404
        assert foreign_setup["client"].get(f"/api/v1/documents/{doc.id}/download/").status_code == 404
        assert not Chunk.objects.filter(document=doc).exclude(rag_space_id=email_space.id).exists()


class TestCycleDeVie:
    def test_reimport_identique_dedoublonne(self, api_client, email_space):
        data = _full_eml()  # MÊMES octets (chaque génération de PDF/EML est horodatée)
        first = _ingest(api_client, email_space, data, "reunion.eml")
        n = Chunk.objects.filter(document=first).count()
        res = _upload(api_client, email_space, data, "reunion-copie.eml")
        assert res.status_code == 200 and res.data["document"]["id"] == str(first.id)
        assert Document.objects.filter(rag_space=email_space).count() == 1
        assert Chunk.objects.filter(document=first).count() == n

    def test_meme_piece_dans_deux_courriels_garde_ses_deux_provenances(self, api_client, email_space):
        shared = _pdf(M_PDF)
        a = _ingest(api_client, email_space, build_eml(subject="Premier envoi", message_id="<a@example.invalid>",
                                                       attachments=[("rapport.pdf", shared, "application/pdf")]),
                    "a.eml")
        b = _ingest(api_client, email_space, build_eml(subject="Second envoi", message_id="<b@example.invalid>",
                                                       attachments=[("rapport.pdf", shared, "application/pdf")]),
                    "b.eml")
        assert a.id != b.id
        for doc, subject in ((a, "Premier envoi"), (b, "Second envoi")):
            chunks = Chunk.objects.filter(document=doc, meta__email_attachment_filename="rapport.pdf")
            assert chunks.exists() and all(c.meta["email_subject"] == subject for c in chunks)

    def test_reindexation_idempotente(self, api_client, email_space):
        from apps.rag_core.services.ingest.indexing import extract_and_index_bytes

        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        before = sorted(Chunk.objects.filter(document=doc).values_list("text", flat=True))
        rows_before = _attachments(doc)
        raw = doc.storage_file.read()
        doc.storage_file.close()
        for _ in range(2):
            res = extract_and_index_bytes(doc=doc, space=doc.rag_space, file_bytes=raw, filename=doc.filename,
                                          content_type=doc.content_type)
            assert res.status in ("READY", "ready") or getattr(res, "ok", True)
        after = sorted(Chunk.objects.filter(document=doc).values_list("text", flat=True))
        assert before == after
        assert {k: (v["status"], v["fragment_count"]) for k, v in _attachments(doc).items()} == \
               {k: (v["status"], v["fragment_count"]) for k, v in rows_before.items()}

    def test_suppression_du_parent_emporte_les_fragments_des_pieces(self, api_client, email_space):
        doc = _ingest(api_client, email_space, _full_eml(), "reunion.eml")
        assert Chunk.objects.filter(document=doc, meta__email_part="attachment").exists()
        res = api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
        assert res.status_code == 200, res.data
        assert not Chunk.objects.filter(document_id=doc.id).exists()
        payload = query_rag(rag_space_id=email_space.id, question=f"Quelle architecture decrit {M_PDF} ?")
        assert M_PDF not in str(payload.get("sources") or [])

    def test_echec_partiel_preserve_le_reste(self, api_client, email_space):
        data = build_eml(attachments=[
            ("notes.txt", f"Compte rendu {M_DOCX} du comite.".encode(), "text/plain"),
            ("casse.pdf", b"%PDF-1.4\n%%EOF", "application/pdf"),
            ("archive.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip"),
        ])
        doc = _ingest(api_client, email_space, data, "partiel.eml")
        doc.refresh_from_db()
        assert doc.status == "READY"
        rows = _attachments(doc)
        assert rows["notes.txt"]["status"] == "INDEXED"
        assert rows["casse.pdf"]["status"] == "FAILED"
        assert rows["archive.zip"]["status"] in ("REJECTED", "UNSUPPORTED")
        assert doc.meta["extraction"]["email_status"] == "partial"
        assert _cited(email_space, f"Compte rendu {M_DOCX}", M_DOCX) is not None


@needs_ocr
def test_image_deposee_seule_reste_ocr_non_probante(api_client, rag_space):
    """Défaut préexistant corrigé dans ce lot : le texte OCR d'une IMAGE était classé
    « texte natif » (probant). Il doit rester OCR, déposé seul comme en pièce jointe."""
    res = _upload(api_client, rag_space, _png(M_PNG), "schema.png")
    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])
    chunks = list(Chunk.objects.filter(document=doc))
    assert chunks and all(c.meta.get("source_is_ocr") is True for c in chunks)
    assert {NE.classify({"meta": c.meta}, {})[0] for c in chunks} == {NE.OCR_OTHER}


class TestDisponibiliteGenerale:
    """mode général — tout espace, MAIS droits d'import inchangés."""

    @pytest.fixture
    def global_on(self, settings):
        settings.DOCUMENT_EML_ENABLED = True
        settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
        settings.DOCUMENT_EMAIL_RAGSPACE_IDS = []
        settings.DOCUMENT_EMAIL_GLOBAL_ENABLED = True
        return settings

    def test_utilisateur_autorise_accepte_hors_liste(self, api_client, rag_space, global_on):
        data = build_eml(attachments=[("notes.txt", f"Compte rendu {M_DOCX} du comite.".encode(), "text/plain")])
        doc = _ingest(api_client, rag_space, data, "m.eml")
        assert _attachments(doc)["notes.txt"]["status"] == "INDEXED"
        assert _cited(rag_space, f"Compte rendu {M_DOCX}", M_DOCX) is not None

    def test_utilisateur_sans_droit_refuse(self, rag_space, foreign_setup, global_on):
        # Membre d'un AUTRE workspace : le mode général n'ouvre aucun droit d'écriture.
        res = _upload(foreign_setup["client"], rag_space, build_eml(), "m.eml")
        assert res.status_code in (403, 404)
        assert not Document.objects.filter(rag_space=rag_space).exists()

    def test_pieces_isolees_entre_espaces(self, api_client, rag_space, foreign_setup, global_on):
        doc = _ingest(api_client, rag_space, build_eml(attachments=[
            ("notes.txt", f"Compte rendu {M_DOCX} du comite.".encode(), "text/plain")]), "m.eml")
        payload = query_rag(rag_space_id=foreign_setup["rag_space"].id, question=f"Compte rendu {M_DOCX}")
        assert M_DOCX not in str(payload.get("sources") or []) and str(doc.id) not in str(payload.get("sources") or [])
        assert foreign_setup["client"].get(f"/api/v1/documents/{doc.id}/").status_code == 404

    def test_mode_general_ferme_et_espace_hors_liste_refuse(self, api_client, rag_space, global_on):
        global_on.DOCUMENT_EMAIL_GLOBAL_ENABLED = False
        res = _upload(api_client, rag_space, build_eml(), "m.eml")
        assert res.status_code == 400 and res.data["detail"] == "email_ingestion_not_enabled_for_space"

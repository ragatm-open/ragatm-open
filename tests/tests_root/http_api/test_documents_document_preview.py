"""`GET /api/v1/documents/<id>/document-preview/` — contrat, permissions, sécurité.

Fil directeur, repris du tabulaire : **aucun refus ne doit ressembler à un succès**.
Ni bloc vide, ni passage approchant, ni repli sur le début du document.
"""
import sys
from pathlib import Path

import pytest
from django.core.files.base import ContentFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))

from _builders import make_docx_raw  # noqa: E402

from apps.rag_core.models.chunk import Chunk  # noqa: E402
from apps.rag_core.models.document import Document  # noqa: E402
from apps.rag_core.services.chunking import chunk_text_deterministic  # noqa: E402
from apps.rag_core.services.ingest.extraction import mime as M  # noqa: E402
from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.ingest.extraction.docx_extractor import (  # noqa: E402
    DocxExtractor,
)

pytestmark = pytest.mark.django_db

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text, style=None, num=False):
    ppr = ""
    if style or num:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if num:
            ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        ppr += "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _body(n_paras=40, *, with_titles=True, with_table=True, with_list=True):
    out = ""
    if with_titles:
        out += _p("Rapport annuel", "Heading1")
    for i in range(n_paras):
        if with_titles and i and i % 10 == 0:
            out += _p(f"Section {i // 10}", "Heading2")
        out += _p(f"Paragraphe numero {i} du document de test avec du contenu reel.")
    if with_list:
        out += _p("Premier point", num=True) + _p("Second point", num=True)
    if with_table:
        out += (
            "<w:tbl>"
            "<w:tr><w:tc><w:p><w:r><w:t>Poste</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Montant</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>Alpha</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>1000</w:t></w:r></w:p></w:tc></w:tr>"
            "</w:tbl>"
        )
    return out


def _docx(**kw) -> bytes:
    return make_docx_raw(
        f'<?xml version="1.0"?><w:document {W}><w:body>{_body(**kw)}</w:body></w:document>'
    )


def _ingest(rag_space, *, data=None, filename="rapport.docx", **kw):
    """Ingère RÉELLEMENT : extraction + chunks avec offsets, comme le pipeline."""
    import hashlib

    data = data if data is not None else _docx(**kw)
    result = DocxExtractor().extract(data, ExtractionContext(filename=filename))

    doc = Document.objects.create(
        rag_space=rag_space,
        filename=filename,
        content_type=M.DOCX,
        sha256=hashlib.sha256(data).hexdigest(),
        status="READY",
        meta={"extraction": dict(result.metadata)},
    )
    doc.storage_file.save(filename, ContentFile(data), save=True)

    specs = chunk_text_deterministic(
        result.text, source=filename, chunk_size=500, overlap=100
    )
    chunks = [
        Chunk.objects.create(
            rag_space=rag_space, document=doc, chunk_index=s.chunk_index, text=s.text,
            meta={
                "chunk_index": s.chunk_index, "page_number": None, "source": s.source,
                "char_start": s.char_start, "char_end": s.char_end,
            },
        )
        for s in specs
    ]
    return doc, chunks, result


def _url(doc, **params):
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return f"/api/v1/documents/{doc.id}/document-preview/" + (f"?{qs}" if qs else "")


# --------------------------------------------------------------------------- #
class TestPermissions:
    def test_utilisateur_autorise(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[2].id)))
        assert res.status_code == 200, res.data
        assert res.data["document"]["preview_kind"] == "document"

    def test_non_authentifie(self, rag_space):
        from rest_framework.test import APIClient

        doc, _, _ = _ingest(rag_space)
        assert APIClient().get(_url(doc)).status_code in (401, 403)

    def test_autre_workspace_404(self, rag_space, foreign_setup):
        doc, chunks, _ = _ingest(rag_space)
        res = foreign_setup["client"].get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 404

    def test_document_inexistant_indiscernable(self, api_client):
        import uuid

        res = api_client.get(f"/api/v1/documents/{uuid.uuid4()}/document-preview/")
        assert res.status_code == 404


class TestIDOR:
    def test_chunk_d_un_autre_document(self, api_client, rag_space):
        doc_a, _, _ = _ingest(rag_space, filename="a.docx")
        doc_b, chunks_b, _ = _ingest(rag_space, filename="b.docx", n_paras=30)
        res = api_client.get(_url(doc_a, chunk_id=str(chunks_b[0].id)))
        assert res.status_code == 404
        assert res.data["reason_code"] == "chunk_document_mismatch"

    def test_chunk_d_un_autre_ragspace(self, api_client, rag_space, foreign_setup):
        doc, _, _ = _ingest(rag_space)
        f_doc, f_chunks, _ = _ingest(foreign_setup["rag_space"], filename="f.docx")
        res = api_client.get(_url(doc, chunk_id=str(f_chunks[0].id)))
        assert res.status_code == 404
        assert res.data["reason_code"] == "chunk_document_mismatch"

    def test_chunk_supprime(self, api_client, rag_space):
        import uuid

        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(uuid.uuid4())))
        assert res.status_code == 409
        assert res.data["reason_code"] == "chunk_stale"


class TestLocalisation:
    def test_ouverture_sur_la_citation(self, api_client, rag_space):
        doc, chunks, result = _ingest(rag_space)
        chunk = chunks[3]
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200, res.data

        assert res.data["source"]["chunk_id"] == str(chunk.id)
        assert res.data["source"]["char_start"] == chunk.meta["char_start"]

        # Les fragments surlignés reconstruisent EXACTEMENT le texte du chunk.
        marked = [
            b["text"][b["highlight"]["start"] : b["highlight"]["end"]]
            for b in res.data["blocks"]
            if b["highlight"]
        ]
        assert "\n\n".join(marked).strip() == chunk.text

    def test_blocs_de_bordure_partiellement_surlignes(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[2].id)))
        marked = [b for b in res.data["blocks"] if b["highlight"]]
        assert len(marked) >= 3
        first, last = marked[0], marked[-1]
        # Au moins une bordure est partielle — un chunk coupe au caractère.
        partial = (first["highlight"]["start"] > 0) or (
            last["highlight"]["end"] < len(last["text"])
        )
        assert partial, "aucune bordure partielle : le surlignage est approximé"

    def test_contexte_avant_et_apres(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(
            _url(doc, chunk_id=str(chunks[3].id), context_before=2, context_after=3)
        )
        vp = res.data["viewport"]
        assert vp["first_block"] == max(0, vp["source_first_block"] - 2)
        assert vp["last_block"] <= vp["source_last_block"] + 3
        # Les blocs de contexte ne sont PAS surlignés.
        assert any(b["highlight"] is None for b in res.data["blocks"])

    def test_navigation_par_bloc_sans_chunk(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, first_block=5))
        assert res.status_code == 200
        assert res.data["source"] is None
        assert all(b["highlight"] is None for b in res.data["blocks"])

    def test_sans_chunk_ni_bloc(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc))
        assert res.status_code == 400
        assert res.data["reason_code"] == "invalid_block_range"

    def test_offsets_absents(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        c = chunks[0]
        c.meta = {"chunk_index": 0}          # ni char_start ni char_end
        c.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "invalid_chunk_offsets"

    def test_offsets_incoherents(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        c = chunks[0]
        c.meta = {**c.meta, "char_start": 900, "char_end": 100}
        c.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "invalid_chunk_offsets"

    def test_offsets_au_dela_du_texte(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        c = chunks[0]
        c.meta = {**c.meta, "char_start": 10, "char_end": 10_000_000}
        c.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"


class TestIntegrite:
    def test_compteurs_divergents_refuses(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {"extraction": {**doc.meta["extraction"], "chars": 999_999}}
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"

    def test_nombre_de_blocs_divergent_refuse(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {"extraction": {**doc.meta["extraction"], "blocks": 3}}
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409

    def test_version_d_extracteur_differente_refusee(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {
            "extraction": {**doc.meta["extraction"], "extractor_version": "docx-v0"}
        }
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"

    def test_document_historique_sans_version_reste_visualisable(
        self, api_client, rag_space
    ):
        """Sans `extractor_version`, les compteurs suffisent — pas de réindexation."""
        doc, chunks, _ = _ingest(rag_space)
        extraction = dict(doc.meta["extraction"])
        extraction.pop("extractor_version", None)
        doc.meta = {"extraction": extraction}
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200, res.data

    def test_sha256_attendu_different(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id), sha256="f" * 64))
        assert res.status_code == 409

    def test_aucune_metadonnee_extraction(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {}
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "preview_unavailable"


class TestFormatEtContenu:
    def test_pdf_refuse(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="r.pdf", content_type=M.PDF, status="READY"
        )
        doc.storage_file.save("r.pdf", ContentFile(b"%PDF-1.4"), save=True)
        res = api_client.get(_url(doc, first_block=0))
        assert res.status_code == 422
        assert res.data["reason_code"] == "unsupported_format"

    def test_document_corrompu_jamais_500(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="c.docx", content_type=M.DOCX,
            sha256="c" * 64, status="READY", meta={"extraction": {"chars": 1, "blocks": 1}},
        )
        doc.storage_file.save("c.docx", ContentFile(b"PK\x03\x04 pas un docx"), save=True)
        res = api_client.get(_url(doc, first_block=0))
        assert res.status_code == 422
        assert res.data["reason_code"] == "document_unreadable"

    def test_fichier_absent(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="v.docx", content_type=M.DOCX,
            status="READY", meta={"extraction": {"chars": 0, "blocks": 0}},
        )
        res = api_client.get(_url(doc, first_block=0))
        assert res.status_code == 404
        assert res.data["reason_code"] == "no_file_available"

    def test_document_vide_rend_200_et_non_une_erreur(self, api_client, rag_space):
        # Un document sans texte n'est pas une panne : c'est un fait, et il doit
        # se dire comme tel, sans quoi l'UI afficherait une erreur trompeuse.
        data = make_docx_raw(
            f'<?xml version="1.0"?><w:document {W}><w:body></w:body></w:document>'
        )
        doc, _, _ = _ingest(rag_space, data=data, filename="vide.docx")
        res = api_client.get(_url(doc, first_block=0))
        assert res.status_code == 200, res.data
        assert res.data["blocks"] == []
        assert res.data["viewport"]["total_blocks"] == 0


class TestContrat:
    def test_forme_de_la_reponse(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, chunk_id=str(chunks[1].id))).data

        assert d["schema_version"] == 1
        assert d["document"]["sha256"] == doc.sha256
        assert d["fidelity"]["extractor_version"] == "docx-v1"
        assert d["fidelity"]["simplified"] is True
        for w in ("images_not_extracted", "headers_footers_not_indexed",
                  "notes_not_indexed", "comments_not_indexed",
                  "lists_flattened", "tables_simplified",
                  "computed_pagination_unavailable"):
            assert w in d["fidelity"]["warnings"]
        for o in ("images", "headers", "footers", "footnotes", "comments",
                  "computed_pagination"):
            assert o in d["fidelity"]["omitted"]
        assert d["truncation"]["truncated"] is False

    def test_types_de_blocs_limites_au_mvp(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, first_block=0, context_after=100))
        kinds = {b["type"] for b in res.data["blocks"]}
        assert kinds <= {"heading", "paragraph", "list", "table", "unsupported"}
        # Aucun type que l'extracteur ne produit pas n'est déclaré.
        assert not (kinds & {"page_break", "image", "image_placeholder", "section"})

    def test_liste_declaree_aplatie(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, first_block=0, context_after=100))
        lists = [b for b in res.data["blocks"] if b["type"] == "list"]
        assert lists and all(b["list_fidelity"] == "flattened" for b in lists)

    def test_tableau_prefomate_et_signale(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, first_block=0, context_after=100))
        tables = [b for b in res.data["blocks"] if b["type"] == "table"]
        assert tables
        for t in tables:
            assert t["render_mode"] == "preformatted"
            assert "table_simplified" in t["warnings"]
            # Aucune structure de grille n'est fournie : la donnée ne la porte pas.
            assert "rows" not in t and "cells" not in t

    def test_sommaire_des_titres(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        heads = res.data["navigation"]["headings"]
        assert heads
        assert heads[0]["text"] == "Rapport annuel"
        assert all({"id", "block_index", "level", "text"} <= set(h) for h in heads)
        assert res.data["navigation"]["headings_truncated"] is False

    def test_document_sans_titre_signale(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space, with_titles=False, filename="sans.docx")
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.data["navigation"]["headings"] == []
        assert "headings_not_detected" in res.data["fidelity"]["warnings"]


class TestVolumetrie:
    def test_troncature_declaree(self, api_client, rag_space, settings):
        settings.DOCX_PREVIEW_MAX_BLOCKS = 5
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(
            _url(doc, chunk_id=str(chunks[0].id), context_after=100)
        )
        assert res.status_code == 200
        assert res.data["truncation"]["truncated"] is True
        assert res.data["truncation"]["reason"] == "max_blocks"
        assert len(res.data["blocks"]) <= 5

    def test_borne_cardinale_de_caracteres(self, api_client, rag_space, settings):
        settings.DOCX_PREVIEW_MAX_TOTAL_CHARS = 200
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id), context_after=100))
        assert res.data["truncation"]["truncated"] is True
        assert res.data["truncation"]["reason"] == "max_total_chars"

    def test_bloc_long_tronque_avec_longueur_reelle(self, api_client, rag_space, settings):
        settings.DOCX_PREVIEW_MAX_BLOCK_CHARS = 50
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        long_blocks = [b for b in res.data["blocks"] if b["truncated"]]
        assert long_blocks
        for b in long_blocks:
            assert len(b["text"]) == 50
            assert b["full_length"] > 50

    def test_document_trop_grand(self, api_client, rag_space, settings):
        settings.DOCX_PREVIEW_MAX_DOCUMENT_BLOCKS = 3
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 413
        assert res.data["reason_code"] == "document_too_large"

    @pytest.mark.parametrize(
        "params", [{"context_before": 10_000}, {"context_after": 10_000},
                   {"context_before": -1}]
    )
    def test_contexte_excessif(self, api_client, rag_space, params):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id), **params))
        assert res.status_code == 400
        assert res.data["reason_code"] == "context_limit_exceeded"

    def test_sommaire_borne(self, api_client, rag_space, settings):
        settings.DOCX_PREVIEW_MAX_HEADINGS = 2
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert len(res.data["navigation"]["headings"]) <= 2
        assert res.data["navigation"]["headings_truncated"] is True


class TestSecuriteContenu:
    def test_html_transporte_comme_texte(self, api_client, rag_space):
        body = (
            _p("&lt;script&gt;alert(1)&lt;/script&gt;")
            + _p("&lt;img src=x onerror=alert(1)&gt;")
            + _p("javascript:alert(1)")
        )
        data = make_docx_raw(
            f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>'
        )
        doc, chunks, _ = _ingest(rag_space, data=data, filename="xss.docx")
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200

        texts = [b["text"] for b in res.data["blocks"]]
        assert "<script>alert(1)</script>" in texts
        assert "javascript:alert(1)" in texts
        # Le backend transporte du TEXTE : aucun HTML n'est produit ici, c'est le
        # rendu React qui échappe.
        assert all(isinstance(t, str) for t in texts)

    def test_aucun_texte_de_document_dans_les_logs(self, api_client, rag_space, caplog):
        import logging

        caplog.set_level(logging.INFO)
        body = _p("CANARI-DOCX-SECRET-42") + _body(n_paras=5)
        data = make_docx_raw(
            f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>'
        )
        doc, chunks, _ = _ingest(rag_space, data=data, filename="canari.docx")
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200

        assert any("CANARI-DOCX-SECRET-42" in b["text"] for b in res.data["blocks"])
        assert "CANARI-DOCX-SECRET-42" not in caplog.text


class TestCache:
    def test_chunk_supprime_apres_mise_en_cache(self, api_client, rag_space):
        """Le cache ne doit JAMAIS survivre à la disparition du chunk."""
        from django.core.cache import cache

        cache.clear()
        doc, chunks, _ = _ingest(rag_space)
        chunk_id = chunks[0].id

        assert api_client.get(_url(doc, chunk_id=str(chunk_id))).status_code == 200

        Chunk.objects.filter(document=doc).delete()

        res = api_client.get(_url(doc, chunk_id=str(chunk_id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "chunk_stale"
        assert "blocks" not in res.data

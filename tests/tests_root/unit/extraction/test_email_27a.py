"""courriels.eml et CONTENU des pièces jointes (édition Open : .eml seul).

Couvre, sans base de données : la signature EML, le parseur MIME (corps, encodages,
pièces, messages imbriqués, défauts), les contenus Outlook transitoires, puis le
traitement des pièces par les extracteurs EXISTANTS — statuts, limites, sécurité.
Tous les messages sont fabriqués par les tests (`_email_builders`), sans donnée réelle.
"""
from __future__ import annotations

import uuid

import pytest

from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext, RejectedFile
from apps.rag_core.services.ingest.extraction.eml_email import EmlParseError, parse_eml_bytes
from apps.rag_core.services.ingest.extraction.eml_signature import has_eml_signature
from apps.rag_core.services.ingest.extraction.email_extractor import (
    ATT_DUPLICATE,
    ATT_FAILED,
    ATT_NOT_INGESTED,
    ATT_PENDING,
    ATT_REJECTED,
    ATT_SKIPPED_LIMIT,
    ATT_UNSUPPORTED,
    safe_attachment_name,
)
from apps.rag_core.services.ingest.extraction.validation import check_email_admission

from ._builders import build_pptx, build_xlsx, make_docx, make_pdf_pages
from ._email_builders import build_eml

SPACE = str(uuid.uuid4())
OTHER_SPACE = str(uuid.uuid4())


def _pdf(text: str, pages: int = 1) -> bytes:
    return make_pdf_pages([f"{text} page {i + 1}" for i in range(pages)])


@pytest.fixture
def email_on(settings):
    settings.DOCUMENT_EML_ENABLED = True
    settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = [SPACE]
    settings.DOCUMENT_OCR_ENABLED = False
    return settings


def _extract(data: bytes, name: str, space=SPACE):
    return extract_document(data, filename=name, rag_space_id=space)


def _by_name(res):
    return {e["filename"]: e for e in res.metadata["attachments"]}


# --- signature EML ------------------------------------------------------------------


def test_signature_eml_exige_extension_et_en_tetes():
    eml = build_eml()
    assert has_eml_signature(eml)
    assert M.detect_mime(eml, ExtractionContext(filename="m.eml")) == M.EML
    # Même contenu, autre extension : jamais reclassé (on n'accepte pas sur le contenu seul).
    assert M.detect_mime(eml, ExtractionContext(filename="m.txt")) != M.EML
    # mbox (« From  » sans deux-points) et fichier clé/valeur sans identité de message : non.
    assert not has_eml_signature(b"From someone Tue Nov  3 09:00:00 2026\nSubject: x\n\nbody")
    assert not has_eml_signature(b"Content-Type: text/plain\nDate: 2026-11-03\n\nbody")


def test_eml_ferme_par_defaut_refus_nomme(settings):
    settings.DOCUMENT_EML_ENABLED = False
    with pytest.raises(RejectedFile) as exc:
        extract_document(build_eml(), filename="m.eml")
    assert exc.value.code == "eml_ingestion_disabled"


def test_admission_par_espace(settings):
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = [SPACE]
    check_email_admission("eml", SPACE)
    check_email_admission("pdf", OTHER_SPACE)  # autres formats jamais concernés
    for space in (OTHER_SPACE, None, ""):
        with pytest.raises(RejectedFile) as exc:
            check_email_admission("eml", space)
        assert exc.value.code == "email_ingestion_not_enabled_for_space"
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = []
    with pytest.raises(RejectedFile):
        check_email_admission("eml", SPACE)  # liste vide = AUCUN espace
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = [SPACE, SPACE[:8]]
    with pytest.raises(RejectedFile):
        check_email_admission("eml", SPACE)  # une entrée invalide invalide toute la liste


# --- parseur MIME -------------------------------------------------------------------


def _parse(data: bytes, depth: int = 2):
    return parse_eml_bytes(data, max_recipients=50, max_attachments=50, max_depth=depth)


def test_eml_texte_seul_et_en_tetes():
    p = _parse(build_eml(subject="Transmission des previsions"))
    e = p.email
    assert e.subject == "Transmission des previsions"
    assert e.sender_address == "unite@example.invalid"
    assert e.to == ("Destinataire <dest@example.invalid>",)
    assert e.internet_message_id == "eml-27a@example.invalid"
    assert e.sent_at is not None and e.sent_at.tzinfo is not None
    assert e.body_plain and "budget" in e.body_plain and e.body_html is None
    assert p.payloads == [] and e.source_format == "eml"


def test_eml_alternative_un_seul_corps_indexe(email_on):
    html = "<p>Veuillez valider le <b>budget</b> du projet Orion avant vendredi, merci beaucoup.</p>"
    res = _extract(build_eml(html=html), "m.eml")
    assert res.metadata["body_kind"] == "html"
    body = " ".join(s["text"] for s in res.email_sections if s["role"] == "body")
    # Le texte et le HTML disent la même chose : il n'apparaît qu'UNE fois.
    assert body.count("avant vendredi") == 1


def test_eml_html_actif_neutralise(email_on):
    html = ("<p>Contenu utile du message pour la reunion de validation du projet.</p>"
            "<script>alert('x')</script><img src='https://tracker.invalid/p.png'>")
    res = _extract(build_eml(plain=None, html=html), "m.eml")
    text = res.text
    assert "alert" not in text and "tracker.invalid" not in text
    assert "active_content_removed" in res.warnings


def test_eml_encodages_qp_base64_et_charset():
    raw = (
        "From: Unite <unite@example.invalid>\r\nTo: dest@example.invalid\r\n"
        "Subject: =?utf-8?b?w4l0YXQgZHUgcHJvamV0?=\r\nDate: Tue, 03 Nov 2026 09:30:00 +0100\r\n"
        "Message-ID: <enc@example.invalid>\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=iso-8859-1\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
        "Le r=E9sultat pr=E9vu est d=E9finitif pour cette =E9tape du projet.\r\n"
    ).encode("ascii")
    e = _parse(raw).email
    assert e.subject == "État du projet"
    assert "résultat prévu" in e.body_plain


def test_eml_charset_inconnu_signale_sans_perte():
    raw = (b"From: a@example.invalid\r\nSubject: x\r\nMessage-ID: <c@example.invalid>\r\n"
           b"Content-Type: text/plain; charset=x-inexistant\r\n\r\nTexte lisible malgre le charset inconnu.\r\n")
    e = _parse(raw).email
    assert "Texte lisible" in (e.body_plain or "")
    assert "eml_charset_fallback" in e.warnings


def test_eml_pieces_rfc2231_inline_et_imbrique():
    inner = build_eml(subject="Message transfere", message_id="<inner@example.invalid>")
    data = build_eml(
        html="<p>Voir le schema ci-dessous <img src='cid:img1'></p>",
        attachments=[("Récapitulatif été.pdf", b"%PDF-1.4 x", "application/pdf")],
        inline_images=[("img1", b"\x89PNG\r\n\x1a\nxx", "image/png")],
        forwarded=inner,
    )
    p = _parse(data)
    names = [pl.attachment.filename for pl in p.payloads]
    assert "Récapitulatif été.pdf" in names  # nom RFC 2231 décodé
    inline = [pl for pl in p.payloads if pl.attachment.content_id == "img1"]
    assert inline and inline[0].attachment.is_inline and inline[0].data.startswith(b"\x89PNG")
    nested = [pl for pl in p.payloads if pl.embedded is not None]
    assert nested and nested[0].embedded.email.subject == "Message transfere"


def test_eml_profondeur_zero_n_ouvre_pas_les_imbriques():
    data = build_eml(forwarded=build_eml(subject="Inner", message_id="<i@example.invalid>"))
    p = _parse(data, depth=0)
    assert [pl.error_code for pl in p.payloads] == ["email_nesting_limit"]


def test_eml_malforme_leve_ou_reste_exploitable():
    with pytest.raises(EmlParseError):
        _parse(b"ceci n'est pas un courriel\n\nni un en-tete")
    # Multipart tronqué (frontière jamais fermée) : le corps lisible est conservé, défaut signalé.
    truncated = build_eml(attachments=[("a.txt", b"x" * 50, "text/plain")])
    truncated = truncated[: truncated.rfind(b"--")]
    p = _parse(truncated)
    assert "budget" in (p.email.body_plain or "")


# --- contenus Outlook ---------------------------------------------------------------




# --- traitement des pièces ------------------------------------------------------------




def test_eml_piece_corrompue_ninvalide_ni_le_corps_ni_les_autres(email_on):
    data = build_eml(attachments=[
        ("notes.txt", "Compte rendu ORION-TXT-9921 du comite.".encode(), "text/plain"),
        ("casse.pdf", b"%PDF-1.4\n%%EOF garbage", "application/pdf"),
    ])
    res = _extract(data, "m.eml")
    by = _by_name(res)
    assert by["notes.txt"]["status"] == ATT_PENDING
    assert by["casse.pdf"]["status"] == ATT_FAILED and by["casse.pdf"]["reason"]
    assert "budget" in res.text  # corps intact
    assert res.metadata["email_status"] == "partial"


def test_securite_type_reel_et_noms(email_on):
    data = build_eml(attachments=[
        ("../../etc/rapport.pdf", b"MZ\x90\x00" + b"\x00" * 60, "application/pdf"),   # exécutable déguisé
        ("photo.png", _pdf("Contenu PDF deguise en image"), "image/png"),            # MIME trompeur
        ("C:\\Users\\x\\evil\x07.txt", b"texte ordinaire et inoffensif du test", "text/plain"),
    ])
    res = _extract(data, "m.eml")
    rows = res.metadata["attachments"]
    names = [r["filename"] for r in rows]
    assert "rapport.pdf" in names and all("/" not in n and "\\" not in n for n in names)
    assert "evil.txt" in names  # caractère de contrôle retiré
    exe = next(r for r in rows if r["filename"] == "rapport.pdf")
    assert exe["status"] == ATT_REJECTED and exe["reason"] == "executable_refused"
    disguised = next(r for r in rows if r["filename"] == "photo.png")
    assert disguised["detected_format"] == "pdf"  # le contenu décide, jamais le MIME déclaré


def test_noms_homonymes_et_doublons_dans_un_meme_courriel(email_on):
    a = "Version A du rapport ORION-HOMO-1".encode()
    b = "Version B du rapport ORION-HOMO-2".encode()
    data = build_eml(attachments=[("rapport.txt", a, "text/plain"), ("rapport.txt", b, "text/plain"),
                                  ("copie.txt", a, "text/plain")])
    res = _extract(data, "m.eml")
    rows = res.metadata["attachments"]
    assert [r["filename"] for r in rows] == ["rapport.txt", "rapport.txt", "copie.txt"]
    assert rows[0]["sha256"] != rows[1]["sha256"] and rows[0]["path"] != rows[1]["path"]
    assert rows[2]["status"] == ATT_DUPLICATE and rows[2]["same_as"] == rows[0]["path"]
    assert len(res.email_attachment_results) == 2  # contenu identique indexé une seule fois


def test_limites_nombre_taille_volume(email_on):
    email_on.DOCUMENT_EMAIL_MAX_ATTACHMENTS = 2
    data = build_eml(attachments=[(f"n{i}.txt", f"note numero {i} du dossier".encode(), "text/plain")
                                  for i in range(4)])
    rows = _extract(data, "m.eml").metadata["attachments"]
    assert [r["status"] for r in rows].count(ATT_SKIPPED_LIMIT) == 2
    assert rows[-1]["reason"] == "email_attachment_count_limit"

    email_on.DOCUMENT_EMAIL_MAX_ATTACHMENTS = 20
    email_on.DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB = 0.1
    big = build_eml(attachments=[("gros.txt", b"a" * 200_000, "text/plain")])
    assert _by_name(_extract(big, "m.eml"))["gros.txt"]["reason"] == "email_attachment_too_large"

    email_on.DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB = 1
    email_on.DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB = 0.15
    two = build_eml(attachments=[("a.txt", b"a" * 100_000, "text/plain"), ("b.txt", b"b" * 100_000, "text/plain")])
    assert _by_name(_extract(two, "m.eml"))["b.txt"]["reason"] == "email_total_size_limit"


def test_profondeur_et_budget_partages_avec_les_imbriques(email_on):
    email_on.DOCUMENT_EMAIL_MAX_NESTING_DEPTH = 1
    level2 = build_eml(subject="Niveau 2", message_id="<n2@example.invalid>",
                       attachments=[("profond.txt", b"contenu du niveau deux", "text/plain")])
    level1 = build_eml(subject="Niveau 1", message_id="<n1@example.invalid>", forwarded=level2,
                       attachments=[("n1.txt", b"contenu du niveau un du fil", "text/plain")])
    res = _extract(build_eml(forwarded=level1), "m.eml")
    rows = res.metadata["attachments"]
    paths = {r["path"]: r for r in rows}
    assert paths["1"]["detected_format"] == "eml" and paths["1"]["status"] == ATT_PENDING
    nested_limit = [r for r in rows if r["reason"] == "email_nesting_limit"]
    assert nested_limit and nested_limit[0]["depth"] == 2
    assert any(r["filename"] == "n1.txt" and r["status"] == ATT_PENDING for r in rows)


def test_cycle_detecte(email_on):
    same = dict(subject="Boucle", message_id="<loop@example.invalid>")
    inner = build_eml(**same)
    res = _extract(build_eml(forwarded=inner, **same), "m.eml")
    assert any(r["reason"] == "email_cycle_detected" for r in res.metadata["attachments"])


def test_desactive_ou_espace_non_admis_inventaire_seul(email_on):
    data = build_eml(attachments=[("notes.txt", b"contenu des notes du projet", "text/plain")])
    email_on.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = False
    res = _extract(data, "m.eml")
    assert [r["status"] for r in res.metadata["attachments"]] == [ATT_NOT_INGESTED]
    assert res.email_attachment_results == [] and res.metadata["attachments_supported_in_this_release"] is False
    email_on.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
    res = _extract(data, "m.eml", space=OTHER_SPACE)
    assert [r["status"] for r in res.metadata["attachments"]] == [ATT_NOT_INGESTED]
    res = extract_document(data, filename="m.eml")  # corpus métier : aucune identité d'espace
    assert res.email_attachment_results == []


def test_format_a_worker_dedie_non_pris_en_charge_en_piece_jointe(email_on):
    email_on.DOCUMENT_IFC_ENABLED = True
    ifc = b"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    res = _extract(build_eml(attachments=[("maquette.ifc", ifc, "application/octet-stream")]), "m.eml")
    row = res.metadata["attachments"][0]
    assert row["status"] == ATT_UNSUPPORTED and row["reason"] == "email_attachment_requires_dedicated_worker"


def test_nom_sur_et_jamais_vide():
    assert safe_attachment_name("", index=3, extension="pdf") == "piece-jointe-3.pdf"
    assert safe_attachment_name("a\x00b\x1f.txt", index=1) == "ab.txt"
    assert len(safe_attachment_name("x" * 500, index=1)) == 200




# --- Statuts des pièces et disponibilité générale ---------------------------

import shutil  # noqa: E402

from apps.rag_core.services.ingest.extraction.email_extractor import ATT_NO_TEXT  # noqa: E402

_OCR = pytest.mark.skipif(shutil.which("tesseract") is None, reason="binaire tesseract absent (runner CI)")


def _blank_png() -> bytes:
    import io as _io

    from PIL import Image

    out = _io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(out, "PNG")
    return out.getvalue()


def _gif() -> bytes:
    import io as _io

    from PIL import Image

    out = _io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(out, "GIF")
    return out.getvalue()


@_OCR
def test_image_sans_texte_n_est_pas_un_echec(email_on):
    data = build_eml(inline_images=[("logo", _blank_png(), "image/png")],
                     html="<p>Corps du message avec un logo insere dans la signature du courriel.</p>")
    res = _extract(data, "m.eml")
    row = res.metadata["attachments"][0]
    assert row["status"] == ATT_NO_TEXT and row["reason"] == "ocr_no_text"
    assert res.email_attachment_results == []          # rien n'est indexé pour cette pièce
    assert res.metadata["email_status"] == "complete"   # le courriel n'est pas « partiel »


def test_gif_non_supporte_distingue_sans_rendre_partiel(email_on):
    res = _extract(build_eml(attachments=[("anim.gif", _gif(), "image/gif")]), "m.eml")
    row = res.metadata["attachments"][0]
    assert row["status"] == ATT_UNSUPPORTED and row["reason"] == "image_unsupported_format"
    assert res.metadata["email_status"] == "complete"


def test_image_corrompue_reste_un_vrai_echec(email_on):
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 50
    res = _extract(build_eml(attachments=[("casse.png", corrupt, "image/png")]), "m.eml")
    row = res.metadata["attachments"][0]
    assert row["status"] == ATT_FAILED and row["reason"] not in ("ocr_no_text", "empty_document")
    assert res.metadata["email_status"] == "partial"


def test_mode_general_admet_tout_espace_identifie(settings):
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = []
    settings.DOCUMENT_EMAIL_GLOBAL_ENABLED = True
    check_email_admission("eml", OTHER_SPACE)
    check_email_admission("eml", SPACE)
    for space in (None, "", "pas-un-uuid"):
        with pytest.raises(RejectedFile):
            check_email_admission("eml", space)  # identité d'espace toujours exigée
    settings.DOCUMENT_EMAIL_GLOBAL_ENABLED = False
    with pytest.raises(RejectedFile):
        check_email_admission("eml", OTHER_SPACE)   # fermé + hors liste : refusé
    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = [SPACE]
    check_email_admission("eml", SPACE)             # fermé + listé : admis


def test_mode_general_ouvre_les_pieces_de_tout_espace(settings):
    from apps.rag_core.services.ingest.extraction import config as C

    settings.DOCUMENT_EMAIL_RAGSPACE_IDS = []
    settings.DOCUMENT_EMAIL_GLOBAL_ENABLED = True
    settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = True
    assert C.email_attachments_enabled_for(OTHER_SPACE) is True
    assert C.email_attachments_enabled_for(None) is False   # corpus métier : jamais
    settings.DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = False
    assert C.email_attachments_enabled_for(OTHER_SPACE) is False


def test_lecture_des_courriels_anterieurs_normalisee_sans_mutation():
    from api.v1.documents.views_common import _attachment_rows, _email_status

    rows = [{"status": "FAILED", "reason": "ocr_no_text", "detected_format": "image"},
            {"status": "UNSUPPORTED", "reason": "image_unsupported_format"},
            {"status": "FAILED", "reason": "image_corrupted"}, {"status": "INDEXED"}]
    out = _attachment_rows(rows)
    assert [r["status"] for r in out] == ["NO_EXTRACTABLE_TEXT", "UNSUPPORTED", "FAILED", "INDEXED"]
    assert rows[0]["status"] == "FAILED"  # la donnée source n'est jamais modifiée
    ext = {"attachment_ingestion": "enabled", "email_status": "partial", "attachments": rows[:2] + rows[3:]}
    assert _email_status(ext) == "complete"
    assert _email_status({**ext, "attachments": rows}) == "partial"  # une vraie erreur reste visible


def test_pdf_illisible_et_scan_sans_texte_restent_des_echecs():
    from api.v1.documents.views_common import _attachment_rows
    from apps.rag_core.services.ingest.extraction.email_extractor import is_no_text_code

    assert is_no_text_code("ocr_no_text", "image") and is_no_text_code("empty_document", "txt")
    assert not is_no_text_code("ocr_no_text", "pdf")          # scan illisible : échec
    assert not is_no_text_code("image_corrupted", "image")
    legacy = _attachment_rows([{"status": "FAILED", "reason": "ocr_no_text", "detected_format": "pdf"},
                               {"status": "FAILED", "reason": "email_attachment_no_content"}])
    assert [r["status"] for r in legacy] == ["FAILED", "FAILED"]

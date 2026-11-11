"""Lot E0 — refus HTTP des formats Office hostiles / non supportés.

Deux familles de refus, à deux étages différents du pipeline :

1. REFUS AVANT CRÉATION (validation MIME) : XLSM, .xls hérité, ODS.
   -> HTTP 400, AUCUN Document créé, aucune tâche, aucun embedding.

2. REFUS PENDANT L'EXTRACTION (durcissement XML) : un DOCX piégé passe la
   validation MIME (c'est un DOCX authentique) et n'est refusé qu'au parsing.
   -> HTTP 200 (jamais 500), Document FAILED, 0 chunk, aucun embedding.
   C'est le comportement historique des échecs d'extraction (cf. DOCX corrompu) :
   on ne le change pas, on le vérifie.
"""
import io
import sys
from pathlib import Path

import pytest

from apps.rag_core.models import Chunk, Document

# réutilise les builders de fixtures unitaires (même motif que tests/tests_root/integration)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import (  # noqa: E402
    make_docx,
    make_docx_with_unsafe_xml,
    make_legacy_ole_header,
    make_ods_zip,
    make_xlsm_zip,
)

pytestmark = pytest.mark.django_db


def _upload(api_client, rag_space, data: bytes, name: str, content_type: str | None = None):
    f = io.BytesIO(data)
    f.name = name
    payload = {"rag_space_id": str(rag_space.id), "file": f}
    return api_client.post("/api/v1/documents/upload/", data=payload, format="multipart")


@pytest.fixture
def no_embeddings(monkeypatch):
    """Sentinelle : tout appel au provider d'embeddings fait échouer le test."""
    calls = []

    def _forbidden(*a, **kw):  # pragma: no cover - ne doit jamais être atteint
        calls.append(1)
        raise AssertionError("embed_texts ne doit PAS être appelé pour un fichier refusé")

    monkeypatch.setattr(
        "apps.rag_core.services.ingest.indexing.embed_texts", _forbidden, raising=True
    )
    return calls


# --------------------------------------------------------------------------- #
# 1) Refus AVANT création du Document
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("payload_fn", "name", "expected_code"),
    [
        (make_xlsm_zip, "budget.xlsm", "macro_enabled_workbook_refused"),
        # LE cas du lot E0 : un XLSM déguisé en XLSX.
        (make_xlsm_zip, "budget.xlsx", "macro_enabled_workbook_refused"),
        # NB : `.xlsx` SANS macro n'est plus dans cette liste — il est ACCEPTÉ depuis
        # le lot E2 (cf. tests/tests_root/http_api/test_documents_upload_excel.py).
        (make_legacy_ole_header, "budget.xls", "legacy_office_format"),
        (make_ods_zip, "budget.ods", "unsupported_spreadsheet_format"),
    ],
)
def test_office_formats_refused_before_any_document_is_created(
    api_client, rag_space, no_embeddings, payload_fn, name, expected_code
):
    res = _upload(api_client, rag_space, payload_fn(), name)

    assert res.status_code == 400, res.data
    assert res.data["detail"] == expected_code
    # Message utilisateur actionnable, en français, non technique.
    assert res.data["error"] and len(res.data["error"]) > 20

    # Rien n'est entré dans le système : ni document, ni chunk, ni embedding.
    assert not Document.objects.filter(rag_space=rag_space).exists()
    assert not Chunk.objects.filter(rag_space=rag_space).exists()
    assert no_embeddings == []


def test_legacy_xls_message_is_actionable(api_client, rag_space):
    res = _upload(api_client, rag_space, make_legacy_ole_header(), "budget.xls")
    assert res.status_code == 400
    assert ".xlsx" in res.data["error"] or "CSV" in res.data["error"].upper()


def test_ods_is_not_reported_as_a_generic_archive(api_client, rag_space):
    res = _upload(api_client, rag_space, make_ods_zip(), "budget.ods")
    assert res.data["detail"] == "unsupported_spreadsheet_format"
    assert "archive" not in res.data["error"].lower()


# --------------------------------------------------------------------------- #
# 2) Refus PENDANT l'extraction : DOCX à XML piégé
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["doctype", "entity", "billion_laughs"])
def test_docx_with_unsafe_xml_fails_cleanly_without_500(
    api_client, rag_space, no_embeddings, kind
):
    res = _upload(api_client, rag_space, make_docx_with_unsafe_xml(kind), "piege.docx")

    # Jamais de 500 : l'échec d'extraction est un échec MÉTIER, pas une erreur serveur.
    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])

    assert doc.status == "FAILED"
    assert "unsafe_xml" in doc.error_message
    assert Chunk.objects.filter(document=doc).count() == 0
    assert no_embeddings == []

    # Aucun contenu XML ni chemin système ne fuit dans le message utilisateur.
    assert "<" not in doc.error_message
    assert "/home" not in doc.error_message


def test_normal_docx_upload_is_unaffected(api_client, rag_space):
    """Non-régression : le DOCX sain reste indexé normalement."""
    res = _upload(api_client, rag_space, make_docx(paragraphs=("Contenu suffisant.",)), "ok.docx")

    assert res.status_code == 200, res.data
    doc = Document.objects.get(id=res.data["document"]["id"])
    assert doc.status == "READY"
    assert Chunk.objects.filter(document=doc).count() >= 1

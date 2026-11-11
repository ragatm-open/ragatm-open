"""Lot E2 — l'upload Excel vu depuis HTTP.

Stratégie d'activation retenue, et vérifiée ici :

- **backend privé OUVERT** : un `.xlsx` est accepté, extrait et indexé ;
- **async FORCÉ** : hors mode eager (tests), Excel ne s'exécute jamais dans le cycle
  d'une requête HTTP (E1 : 500 k cellules ≈ 7 s) ;
- **corpus métier FERMÉ** : son ingestion est synchrone, un classeur y bloquerait la
  requête. Refus net, avant même la création d'un document ;
- **frontend FERMÉ** : `UPLOAD_ACCEPT` n'annonce pas Excel (verrouillé côté frontend,
  cf. tests/frontend/lib/humanError.test.ts).
"""
import io
import sys
from pathlib import Path

import pytest

from apps.rag_core.models import Chunk, Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import build_xlsx, make_xlsm_zip  # noqa: E402

pytestmark = pytest.mark.django_db

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file(data: bytes, name: str):
    f = io.BytesIO(data)
    f.name = name
    return f


def _workbook():
    return build_xlsx({"Budget": [
        ["Projet", "Budget", "Realise"],
        ["Atlas", 250000, 220000],
        ["Orion", 180000, 195000],
    ]})


def _upload(api_client, rag_space, data, name):
    return api_client.post(
        "/api/v1/documents/upload/",
        data={"rag_space_id": str(rag_space.id), "file": _file(data, name)},
        format="multipart",
    )


class TestPrivateUpload:
    def test_xlsx_is_accepted_and_indexed(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _workbook(), "Budget_2026.xlsx")
        assert res.status_code == 200, res.data

        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.status == "READY"

        chunks = list(Chunk.objects.filter(document=doc))
        assert chunks
        assert all(c.page_number is None for c in chunks)      # feuille ≠ page
        assert "| Atlas | 250000 | 220000 |" in chunks[0].text
        assert chunks[0].meta["sheet_name"] == "Budget"
        assert chunks[0].meta["cell_range"] == "A2:C3"

    def test_extraction_view_exposes_excel_status(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _workbook(), "b.xlsx")
        ext = res.data["document"]["extraction"]
        assert ext["status"] == "success"
        assert ext["method"] == "excel:openpyxl"
        assert ext["type"] == "excel"

    def test_xlsm_is_still_refused(self, api_client, rag_space):
        """Même renommé `.xlsx` : la détection est par CONTENU (lot E0)."""
        res = _upload(api_client, rag_space, make_xlsm_zip(), "budget.xlsx")
        assert res.status_code == 400
        assert res.data["detail"] == "macro_enabled_workbook_refused"
        assert not Document.objects.filter(rag_space=rag_space).exists()

    def test_excel_disabled_falls_back_to_refusal(self, api_client, rag_space, settings):
        settings.DOCUMENT_EXCEL_ENABLED = False
        res = _upload(api_client, rag_space, _workbook(), "b.xlsx")
        assert res.status_code == 400
        assert res.data["detail"] == "unsupported_office_format"
        assert not Document.objects.filter(rag_space=rag_space).exists()


class TestAsyncIsForcedForExcel:
    def test_excel_forces_async_even_if_client_asks_sync(self, rag_space, settings):
        """Hors mode eager, un classeur part TOUJOURS en Celery — comme l'OCR.

        On teste la décision (`_should_async`) plutôt que l'enqueue : en tests, Celery
        est en mode eager, donc la vue exécute en sync explicite. C'est la décision qui
        doit être juste.
        """
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from api.v1.documents.views_common import _should_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = Request(APIRequestFactory().post("/api/v1/documents/upload/?async=false"))

        assert _should_async(req, format_key="excel") is True     # non surchargeable
        assert _should_async(req, format_key="pdf") is False       # inchangé


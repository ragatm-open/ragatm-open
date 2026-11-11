"""Lot E4 — ouverture CONTRÔLÉE de l'upload `.xlsx`.

« Contrôlée » veut dire trois choses, toutes vérifiées ici :

1. `.xlsx` est accepté sur le chemin PRIVÉ — et **toujours en asynchrone**, même si le
   client réclame explicitement le mode synchrone. Un classeur de 500 k cellules prend
   ~7 s (mesuré en E1) : il n'a rien à faire dans le cycle d'une requête HTTP ;
2. le **corpus métier reste fermé** — son ingestion est synchrone ;
3. `.xlsm`, `.xls`, `.ods` restent refusés, **par contenu** (le renommage ne sert à rien).
"""
import io
import sys
from pathlib import Path

import pytest

from apps.rag_core.models import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import (  # noqa: E402
    build_xlsx,
    make_legacy_ole_header,
    make_ods_zip,
    make_xlsm_zip,
)

pytestmark = pytest.mark.django_db


def _f(data: bytes, name: str):
    f = io.BytesIO(data)
    f.name = name
    return f


def _workbook():
    return build_xlsx({"Budget": [["Projet", "Budget"], ["Atlas", 250000]]})


def _upload(api_client, rag_space, data, name, **extra):
    payload = {"rag_space_id": str(rag_space.id), "file": _f(data, name), **extra}
    return api_client.post("/api/v1/documents/upload/", data=payload, format="multipart")


class TestPrivateUploadIsOpen:
    def test_xlsx_is_accepted(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _workbook(), "Budget_2026.xlsx")
        assert res.status_code == 200, res.data
        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.status == "READY"          # mode eager en tests

    def test_folder_import_metadata_is_preserved(self, api_client, rag_space):
        """L'import de dossier fonctionne pour un `.xlsx` comme pour le reste."""
        res = _upload(
            api_client, rag_space, _workbook(), "budget.xlsx",
            source_folder="Finance", relative_path="2026/budget.xlsx",
        )
        assert res.status_code == 200, res.data
        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.meta["import"] == {
            "source_folder": "Finance",
            "relative_path": "2026/budget.xlsx",
        }


class TestAsyncIsAlwaysForced:
    """Un classeur ne doit JAMAIS être extrait dans le thread HTTP de l'utilisateur."""

    @pytest.mark.parametrize("explicit", [None, "false", "0", False])
    def test_excel_forces_async_whatever_the_client_asks(self, rag_space, settings, explicit):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from api.v1.documents.views_common import _should_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        url = "/api/v1/documents/upload/"
        if explicit is not None:
            url += f"?async={explicit}"
        req = Request(APIRequestFactory().post(url))

        assert _should_async(req, format_key="excel") is True

    def test_a_sync_header_does_not_override_it_either(self, rag_space, settings):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from api.v1.documents.views_common import _should_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = Request(
            APIRequestFactory().post("/api/v1/documents/upload/", HTTP_X_RAG_ASYNC="false")
        )
        assert _should_async(req, format_key="excel") is True

    def test_other_formats_keep_their_behaviour(self, rag_space, settings):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from api.v1.documents.views_common import _should_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = Request(APIRequestFactory().post("/api/v1/documents/upload/?async=false"))
        assert _should_async(req, format_key="pdf") is False
        assert _should_async(req, format_key="docx") is False


class TestOtherSpreadsheetFormatsStayClosed:
    @pytest.mark.parametrize(("payload_fn", "name", "code"), [
        # Renommé `.xlsx` : la détection est par CONTENU (lot E0).
        (make_xlsm_zip, "budget.xlsx", "macro_enabled_workbook_refused"),
        (make_xlsm_zip, "budget.xlsm", "macro_enabled_workbook_refused"),
        (make_legacy_ole_header, "budget.xls", "legacy_office_format"),
        (make_ods_zip, "budget.ods", "unsupported_spreadsheet_format"),
    ])
    def test_refused_before_any_document_is_created(self, api_client, rag_space,
                                                    payload_fn, name, code):
        res = _upload(api_client, rag_space, payload_fn(), name)
        assert res.status_code == 400, res.data
        assert res.data["detail"] == code
        assert not Document.objects.filter(rag_space=rag_space).exists()

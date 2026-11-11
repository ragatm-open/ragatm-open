"""Lot P1 — ouverture CONTRÔLÉE de l'upload `.pptx`, au niveau HTTP.

« Contrôlée » veut dire, comme pour Excel en E4 :

1. `.pptx` est accepté sur le chemin PRIVÉ — et **toujours en asynchrone**, même si le
   client réclame explicitement le mode synchrone ;
2. le **corpus métier reste fermé** — son ingestion est synchrone ;
3. `.pptm`, `.ppt`, `.odp` restent refusés **par contenu** (renommer ne sert à rien) ;
4. les permissions ne bougent pas d'un pouce.
"""
import io
import sys
from pathlib import Path

import pytest
from apps.rag_core.models import Chunk, Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import (  # noqa: E402
    build_pptx,
    make_legacy_ole_header,
    make_odp_zip,
    make_pptm_zip,
)

pytestmark = pytest.mark.django_db

PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _f(data: bytes, name: str):
    f = io.BytesIO(data)
    f.name = name
    return f


def _deck():
    return build_pptx([
        {"title": "Architecture cible", "bullets": [("Frontend", 0), ("Backend", 0)]},
        {"title": "Ports", "table": [["Service", "Port"], ["AI Gateway", "8100"]]},
    ])


def _upload(api_client, rag_space, data, name, **extra):
    payload = {"rag_space_id": str(rag_space.id), "file": _f(data, name), **extra}
    return api_client.post("/api/v1/documents/upload/", data=payload, format="multipart")


class TestPrivateUploadIsOpen:
    def test_pptx_is_accepted(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _deck(), "Architecture.pptx")
        assert res.status_code == 200, res.data
        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.status == "READY"          # mode eager en tests

    def test_chunks_are_created_with_slide_provenance(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _deck(), "Architecture.pptx")
        doc_id = res.data["document"]["id"]
        chunks = list(Chunk.objects.filter(document_id=doc_id))
        assert len(chunks) >= 2
        assert sorted({c.meta["slide_number"] for c in chunks}) == [1, 2]

    def test_initial_status_and_extraction_meta(self, api_client, rag_space):
        res = _upload(api_client, rag_space, _deck(), "deck.pptx")
        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.meta["extraction"]["format"] == "pptx"

    def test_folder_import_metadata_is_preserved(self, api_client, rag_space):
        res = _upload(
            api_client, rag_space, _deck(), "deck.pptx",
            source_folder="Comités", relative_path="2026/deck.pptx",
        )
        assert res.status_code == 200, res.data
        doc = Document.objects.get(id=res.data["document"]["id"])
        assert doc.meta["import"] == {
            "source_folder": "Comités",
            "relative_path": "2026/deck.pptx",
        }

    def test_dedup_on_second_upload(self, api_client, rag_space):
        data = _deck()
        first = _upload(api_client, rag_space, data, "deck.pptx")
        second = _upload(api_client, rag_space, data, "deck.pptx")
        assert second.status_code == 200, second.data
        assert second.data.get("deduped") is True
        assert Document.objects.filter(rag_space=rag_space).count() == 1
        assert first.data["document"]["id"] == second.data["document"]["id"]


class TestAsyncIsAlwaysForced:
    """Une présentation ne doit JAMAIS être extraite dans le thread HTTP de l'utilisateur."""

    @pytest.mark.parametrize("explicit", [None, "false", "0", False])
    def test_pptx_forces_async_whatever_the_client_asks(self, rag_space, settings, explicit):
        from api.v1.documents.views_common import _should_async
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        url = "/api/v1/documents/upload/"
        if explicit is not None:
            url += f"?async={explicit}"
        req = Request(APIRequestFactory().post(url))

        assert _should_async(req, format_key="pptx") is True

    def test_a_sync_header_does_not_override_it_either(self, rag_space, settings):
        from api.v1.documents.views_common import _should_async
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = Request(
            APIRequestFactory().post("/api/v1/documents/upload/", HTTP_X_RAG_ASYNC="false")
        )
        assert _should_async(req, format_key="pptx") is True

    def test_other_formats_keep_their_behaviour(self, rag_space, settings):
        from api.v1.documents.views_common import _should_async
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        req = Request(APIRequestFactory().post("/api/v1/documents/upload/?async=false"))
        assert _should_async(req, format_key="pdf") is False
        assert _should_async(req, format_key="docx") is False


class TestOtherPresentationFormatsStayClosed:
    @pytest.mark.parametrize(("payload_fn", "name", "code"), [
        # Renommé `.pptx` : la détection est par CONTENU.
        (make_pptm_zip, "deck.pptx", "macro_enabled_presentation_refused"),
        (make_pptm_zip, "deck.pptm", "macro_enabled_presentation_refused"),
        (make_legacy_ole_header, "deck.ppt", "legacy_office_format"),
        (make_odp_zip, "deck.odp", "unsupported_presentation_format"),
    ])
    def test_refused_before_any_document_is_created(self, api_client, rag_space,
                                                    payload_fn, name, code):
        res = _upload(api_client, rag_space, payload_fn(), name)
        assert res.status_code == 400, res.data
        assert res.data["detail"] == code
        assert not Document.objects.filter(rag_space=rag_space).exists()


class TestCorruptedAndInvalid:
    def test_corrupted_pptx_never_reports_success(self, api_client, rag_space):
        """Un fichier corrompu produit un Document FAILED, jamais un faux READY."""
        data = _deck()
        res = _upload(api_client, rag_space, data[: len(data) // 3], "casse.pptx")
        # Une archive tronquée n'est plus détectable comme PPTX : refus net avant
        # création du Document, ou Document FAILED. Jamais 200 + READY, jamais 500.
        assert res.status_code in (400, 415, 200)
        assert res.status_code != 500
        if res.status_code == 200:
            doc = Document.objects.get(id=res.data["document"]["id"])
            assert doc.status == "FAILED"
            assert Chunk.objects.filter(document_id=doc.id).count() == 0

    def test_gate_closed_refuses_at_http_level(self, api_client, rag_space, settings):
        settings.DOCUMENT_PPTX_ENABLED = False
        res = _upload(api_client, rag_space, _deck(), "deck.pptx")
        assert res.status_code == 400, res.data
        assert res.data["detail"] == "unsupported_office_format"
        assert not Document.objects.filter(rag_space=rag_space).exists()

    def test_too_large_is_413_not_500(self, api_client, rag_space, settings):
        settings.UPLOAD_MAX_FILE_SIZE_MB = 0.001
        res = _upload(api_client, rag_space, _deck(), "deck.pptx")
        assert res.status_code == 413, res.data


class TestPermissionsAreUnchanged:
    def test_viewer_cannot_upload_a_pptx(self, viewer_setup):
        client = viewer_setup["client"]
        space = viewer_setup["rag_space"]
        res = client.post(
            "/api/v1/documents/upload/",
            data={"rag_space_id": str(space.id), "file": _f(_deck(), "deck.pptx")},
            format="multipart",
        )
        assert res.status_code in (403, 404)
        assert not Document.objects.filter(rag_space=space).exists()

    def test_user_cannot_upload_a_pptx_into_a_foreign_ragspace(self, api_client, foreign_setup):
        """L'ouverture d'un format ne doit ouvrir AUCUNE porte d'isolation."""
        space = foreign_setup["rag_space"]
        res = api_client.post(
            "/api/v1/documents/upload/",
            data={"rag_space_id": str(space.id), "file": _f(_deck(), "deck.pptx")},
            format="multipart",
        )
        assert res.status_code in (403, 404)
        assert not Document.objects.filter(rag_space=space).exists()

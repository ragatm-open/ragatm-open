"""Tests de l'API documents (list / ingest sync+async / dédup / ACL)."""
import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.services.ingest_facade import ingest_plain_text

pytestmark = pytest.mark.django_db


def test_list_requires_rag_space_id(api_client):
    res = api_client.get("/api/v1/documents/")
    assert res.status_code == 400


def test_list_documents(api_client, rag_space):
    ingest_plain_text(rag_space_id=rag_space.id, filename="a.txt", text="Bonjour le monde.")
    res = api_client.get(f"/api/v1/documents/?rag_space_id={rag_space.id}")
    assert res.status_code == 200
    assert res.data["total"] >= 1
    assert isinstance(res.data["items"], list)


def test_ingest_requires_rag_space_id(api_client):
    res = api_client.post("/api/v1/documents/ingest/", {"text": "hello"}, format="json")
    assert res.status_code == 400


def test_ingest_requires_text(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/ingest/", {"rag_space_id": str(rag_space.id)}, format="json"
    )
    assert res.status_code == 400


def test_ingest_sync_creates_ready_document_with_chunks(api_client, rag_space):
    res = api_client.post(
        "/api/v1/documents/ingest/",
        {"rag_space_id": str(rag_space.id), "text": "Paris est la capitale de la France.", "async": False},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["ok"] is True
    assert res.data["queued"] is False
    doc_id = res.data["document"]["id"]
    doc = Document.objects.get(id=doc_id)
    assert doc.status == "READY"
    assert Chunk.objects.filter(document=doc).count() >= 1


def test_ingest_sync_dedup(api_client, rag_space):
    body = {"rag_space_id": str(rag_space.id), "text": "Texte dédup HTTP.", "async": False}
    api_client.post("/api/v1/documents/ingest/", body, format="json")
    res2 = api_client.post("/api/v1/documents/ingest/", body, format="json")
    assert res2.status_code == 200
    assert res2.data.get("deduped") is True
    # un seul document pour ce contenu
    assert Document.objects.filter(rag_space=rag_space).count() == 1


def test_ingest_async_queues(api_client, rag_space, settings, monkeypatch):
    # L'async réel n'existe qu'en non-eager (en eager l'upload/ingest est routé
    # en sync explicite). On simule le mode async réel : broker dispo + .delay mocké.
    from api.v1.documents import views_common

    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(views_common, "broker_ready", lambda: True)
    monkeypatch.setattr(views_common.ingest_document_task, "delay", lambda **kw: None)

    res = api_client.post(
        "/api/v1/documents/ingest/",
        {"rag_space_id": str(rag_space.id), "text": "Contenu à indexer.", "async": True},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["ok"] is True
    assert res.data["queued"] is True


def test_ingest_async_in_eager_runs_sync(api_client, rag_space, settings):
    # En eager, ?async/"async":true est neutralisé en sync explicite : queued:false.
    settings.CELERY_TASK_ALWAYS_EAGER = True
    res = api_client.post(
        "/api/v1/documents/ingest/",
        {"rag_space_id": str(rag_space.id), "text": "Paris est la capitale.", "async": True},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["ok"] is True
    assert res.data["queued"] is False
    assert Document.objects.get(id=res.data["document"]["id"]).status == "READY"


def test_ingest_viewer_forbidden(viewer_setup):
    res = viewer_setup["client"].post(
        "/api/v1/documents/ingest/",
        {"rag_space_id": str(viewer_setup["rag_space"].id), "text": "x", "async": True},
        format="json",
    )
    assert res.status_code == 403


def test_get_document_not_found(api_client):
    res = api_client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000/")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Anti-énumération sur les endpoints par document_id (P0.2)
# Un document EXISTANT mais hors périmètre doit être indiscernable d'un inexistant
# (404 uniforme) afin de ne pas transformer l'API en oracle d'existence.
# ---------------------------------------------------------------------------
def _seed_document(rag_space, *, text="contenu secret"):
    ingest_plain_text(rag_space_id=rag_space.id, filename="secret.txt", text=text)
    return Document.objects.filter(rag_space=rag_space).first()


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("get", ""),
        ("get", "download/"),
        ("delete", "delete/"),
        ("post", "reindex/"),
    ],
)
def test_foreign_document_returns_404_anti_enumeration(api_client, foreign_setup, method, suffix):
    doc = _seed_document(foreign_setup["rag_space"])
    res = getattr(api_client, method)(f"/api/v1/documents/{doc.id}/{suffix}")
    assert res.status_code == 404
    # un appel hors périmètre ne doit jamais muter l'état (pas de suppression)
    assert Document.objects.filter(id=doc.id).exists()


def test_foreign_and_missing_document_are_indistinguishable(api_client, foreign_setup):
    doc = _seed_document(foreign_setup["rag_space"])
    missing = api_client.get("/api/v1/documents/00000000-0000-0000-0000-000000000000/")
    foreign = api_client.get(f"/api/v1/documents/{doc.id}/")
    assert missing.status_code == foreign.status_code == 404
    assert missing.data == foreign.data


def test_viewer_delete_own_document_is_403_not_404(viewer_setup):
    """Un viewer du MÊME workspace voit déjà le document (lecture OK) : un refus
    d'écriture reste un 403 honnête (il ne s'agit pas d'énumération)."""
    doc = _seed_document(viewer_setup["rag_space"])
    res = viewer_setup["client"].delete(f"/api/v1/documents/{doc.id}/delete/")
    assert res.status_code == 403
    assert Document.objects.filter(id=doc.id).exists()


def test_member_can_delete_own_document(api_client, rag_space):
    """Non-régression : le membre légitime supprime toujours son document."""
    doc = _seed_document(rag_space)
    res = api_client.delete(f"/api/v1/documents/{doc.id}/delete/")
    assert res.status_code == 200
    assert not Document.objects.filter(id=doc.id).exists()

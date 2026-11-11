"""Tests HTTP — PR 3 RagATM_Core (exposition de la façade Core).

Couvre :
  1. /core/health/ répond.
  2. /core/retrieve/ appelle core.retrieve.
  3. /core/retrieve/ refuse un utilisateur sans accès ragspace (403).
  4. /core/retrieve/ ne génère JAMAIS de réponse LLM.
  5. /core/documents/{id}/status/ respecte le scoping ragspace.
  8. Dimension embedding toujours 768 (via /core/health/).

NB : APPEND_SLASH=False => toutes les URLs finissent par '/'.
"""
import uuid

import pytest

from apps.rag_core import core


@pytest.mark.django_db
def test_core_health_responds(api_client):
    resp = api_client.get("/api/v1/core/health/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["embedding_dim"] == 768  # test 8
    assert "ai_gateway" in body["providers"]
    assert "ollama" in body["providers"]


@pytest.mark.django_db
def test_core_health_requires_auth(client):
    # Client non authentifié rejeté.
    assert client.get("/api/v1/core/health/").status_code in (401, 403)


@pytest.mark.django_db
def test_core_retrieve_calls_facade(api_client, rag_space, monkeypatch):
    # Stub la façade pour prouver que la vue l'appelle (et n'embarque pas de logique).
    sentinel = core.RetrievalResult(
        status="ok",
        sources=[{"chunk_id": "doc.txt#000", "score": 0.9}],
        prompt="PROMPT",
        log_id=None,
        answer_kind="ok",
        raw={"status": "ok"},
    )
    called = {}

    def _fake_retrieve(**kwargs):
        called.update(kwargs)
        return sentinel

    monkeypatch.setattr("api.v1.core.views.core.retrieve", _fake_retrieve)

    resp = api_client.post(
        "/api/v1/core/retrieve/",
        {"rag_space_id": str(rag_space.id), "question": "Quelle architecture ?"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sources"] == [{"chunk_id": "doc.txt#000", "score": 0.9}]
    # La vue a bien délégué à core.retrieve avec le bon scope.
    assert str(called.get("rag_space_id")) == str(rag_space.id)
    assert called.get("question") == "Quelle architecture ?"
    # Aucun champ "answer" : /core/retrieve ne génère pas.
    assert "answer" not in body


@pytest.mark.django_db
def test_core_retrieve_real_path_returns_sources_no_generation(api_client, rag_space):
    # Chemin réel (sans stub) : ingestion + retrieve. Le résultat contient des
    # passages/sources et AUCUN champ de réponse générée.
    core.ingest_text(
        rag_space_id=rag_space.id,
        filename="archi.txt",
        text="L'architecture du système repose sur PostgreSQL et pgvector.",
    )
    resp = api_client.post(
        "/api/v1/core/retrieve/",
        {"rag_space_id": str(rag_space.id), "question": "Quelle est l'architecture ?"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "no_answer")
    assert isinstance(body["sources"], list)
    assert "answer" not in body  # jamais de génération


@pytest.mark.django_db
def test_core_retrieve_never_calls_generate(api_client, rag_space, monkeypatch):
    # Garde-fou explicite (test 4) : aucune génération LLM ne doit être déclenchée.
    import apps.llm_router.router as router

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("generate_text must not be called by /core/retrieve")

    monkeypatch.setattr(router, "generate_text", _boom, raising=False)
    monkeypatch.setattr(router, "generate_text_stream", _boom, raising=False)

    core.ingest_text(rag_space_id=rag_space.id, filename="d.txt", text="contexte")
    resp = api_client.post(
        "/api/v1/core/retrieve/",
        {"rag_space_id": str(rag_space.id), "question": "contexte ?"},
        format="json",
    )
    assert resp.status_code == 200


@pytest.mark.django_db
def test_core_retrieve_denies_user_without_access(foreign_setup, rag_space):
    # foreign_setup.user n'est PAS membre du workspace de `rag_space` (test 3).
    client = foreign_setup["client"]
    resp = client.post(
        "/api/v1/core/retrieve/",
        {"rag_space_id": str(rag_space.id), "question": "secret ?"},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_core_retrieve_requires_auth(client, rag_space):
    resp = client.post(
        "/api/v1/core/retrieve/",
        {"rag_space_id": str(rag_space.id), "question": "x"},
        content_type="application/json",
    )
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_core_document_status_respects_scoping(api_client, rag_space):
    ing = core.ingest_text(
        rag_space_id=rag_space.id, filename="doc.txt", text="contenu indexable"
    )
    resp = api_client.get(
        f"/api/v1/core/documents/{ing.document_id}/status/",
        {"rag_space_id": str(rag_space.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["status"] == "READY"          # technique
    assert body["document_status"] == "active"  # métier
    assert body["chunks_count"] > 0


@pytest.mark.django_db
def test_core_document_status_requires_rag_space_id(api_client, rag_space):
    resp = api_client.get(f"/api/v1/core/documents/{uuid.uuid4()}/status/")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_core_document_status_denies_foreign_space(foreign_setup, api_client, rag_space):
    # Un document ingéré dans `rag_space` n'est pas accessible via le ragspace
    # étranger : require_rag_space_access du ragspace étranger échoue pour `user`.
    ing = core.ingest_text(rag_space_id=rag_space.id, filename="d.txt", text="x")
    foreign_rs = foreign_setup["rag_space"]
    # `api_client` (user) n'a pas accès au ragspace étranger => 403.
    resp = api_client.get(
        f"/api/v1/core/documents/{ing.document_id}/status/",
        {"rag_space_id": str(foreign_rs.id)},
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_core_document_status_unknown_doc_returns_404(api_client, rag_space):
    resp = api_client.get(
        f"/api/v1/core/documents/{uuid.uuid4()}/status/",
        {"rag_space_id": str(rag_space.id)},
    )
    assert resp.status_code == 404

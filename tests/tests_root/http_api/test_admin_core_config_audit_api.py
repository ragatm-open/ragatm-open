"""Tests PR-B — Admin RagATM_Core, audit lecture seule (superuser only).

GET /api/v1/admin/core-config/audit/ expose LlmSettingsChangeLog (aucun secret n'y
est stocké). Couvre : permissions, read-only, contenu, filtres, pagination, et
absence de PII (email) / secret dans la réponse.
"""
import pytest

AUDIT = "/api/v1/admin/core-config/audit/"


@pytest.fixture
def change_log(db, workspace, rag_space, user):
    from apps.audit.models import LlmSettingsChangeLog

    return LlmSettingsChangeLog.objects.create(
        user=user,
        workspace=workspace,
        rag_space=rag_space,
        old_provider="ollama",
        old_model="llama3.1:8b",
        new_provider="openai",
        new_model="gpt-4.1-mini",
        old_embedding_provider="ollama",
        old_embedding_model="nomic-embed-text",
        new_embedding_provider="openai",
        new_embedding_model="text-embedding-3-small",
    )


# --- permissions -----------------------------------------------------------

@pytest.mark.django_db
def test_audit_rejects_unauthenticated(client):
    assert client.get(AUDIT).status_code in (401, 403)


@pytest.mark.django_db
def test_audit_rejects_non_superuser(api_client):
    assert api_client.get(AUDIT).status_code == 403


@pytest.mark.django_db
def test_audit_rejects_workspace_owner(admin_client):
    assert admin_client.get(AUDIT).status_code == 403


@pytest.mark.django_db
def test_audit_accepts_superuser(superuser_client):
    assert superuser_client.get(AUDIT).status_code == 200


# --- read-only -------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("verb", ["post", "patch", "delete", "put"])
def test_audit_is_read_only(superuser_client, verb):
    assert getattr(superuser_client, verb)(AUDIT, {}, format="json").status_code == 405


# --- contenu ---------------------------------------------------------------

@pytest.mark.django_db
def test_audit_returns_change_event(superuser_client, change_log, rag_space, workspace, user):
    body = superuser_client.get(AUDIT).json()
    assert body["count"] == 1
    assert body["limit"] == 50 and body["offset"] == 0
    ev = body["results"][0]
    assert ev["id"] == str(change_log.id)
    assert ev["user"] == user.username
    assert ev["workspace_id"] == workspace.id
    assert ev["rag_space_id"] == str(rag_space.id)
    assert ev["chat"] == {
        "old_provider": "ollama", "old_model": "llama3.1:8b",
        "new_provider": "openai", "new_model": "gpt-4.1-mini",
    }
    assert ev["embeddings"]["new_model"] == "text-embedding-3-small"


@pytest.mark.django_db
def test_audit_empty_ok(superuser_client):
    body = superuser_client.get(AUDIT).json()
    assert body["count"] == 0
    assert body["results"] == []


# --- filtres + pagination --------------------------------------------------

@pytest.mark.django_db
def test_audit_invalid_workspace_id_returns_400(superuser_client):
    assert superuser_client.get(AUDIT, {"workspace_id": "abc"}).status_code == 400


@pytest.mark.django_db
def test_audit_filter_by_rag_space(superuser_client, change_log, rag_space, workspace):
    import uuid

    # même rag_space -> 1 résultat ; rag_space inconnu -> 0
    assert superuser_client.get(AUDIT, {"rag_space_id": str(rag_space.id)}).json()["count"] == 1
    other = superuser_client.get(AUDIT, {"rag_space_id": str(uuid.uuid4())}).json()
    assert other["count"] == 0


@pytest.mark.django_db
def test_audit_limit_is_capped(superuser_client):
    body = superuser_client.get(AUDIT, {"limit": "9999"}).json()
    assert body["limit"] == 200  # plafonné


# --- pas de PII / secret ---------------------------------------------------

@pytest.mark.django_db
def test_audit_does_not_leak_email(superuser_client, change_log, user_with_email):
    # l'email de l'utilisateur ne doit pas apparaître (username only)
    blob = str(superuser_client.get(AUDIT).json())
    assert user_with_email.email not in blob

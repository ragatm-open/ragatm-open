import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.workspaces.models import Workspace, Membership
from apps.rag_core.models import RagSpace

User = get_user_model()


@pytest.mark.django_db
def test_list_workspaces_returns_only_memberships():
    u1 = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    u2 = User.objects.create_user(username="u2", email="u2@example.com", password="pw")

    ws1 = Workspace.objects.create(name="WS1")
    ws2 = Workspace.objects.create(name="WS2")

    Membership.objects.create(workspace=ws1, user=u1, role=Membership.ROLE_MEMBER)
    Membership.objects.create(workspace=ws2, user=u2, role=Membership.ROLE_MEMBER)

    client = APIClient()
    client.force_authenticate(user=u1)

    res = client.get("/api/v1/workspaces/")
    assert res.status_code == 200
    assert isinstance(res.data, list)
    assert len(res.data) == 1
    assert res.data[0]["name"] == "WS1"
    assert res.data[0]["role"] == Membership.ROLE_MEMBER


@pytest.mark.django_db
def test_list_ragspaces_requires_workspace_access():
    u1 = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    u2 = User.objects.create_user(username="u2", email="u2@example.com", password="pw")

    ws1 = Workspace.objects.create(name="WS1")
    ws2 = Workspace.objects.create(name="WS2")

    Membership.objects.create(workspace=ws1, user=u1, role=Membership.ROLE_MEMBER)
    Membership.objects.create(workspace=ws2, user=u2, role=Membership.ROLE_MEMBER)

    rs1 = RagSpace.objects.create(name="RS1", workspace=ws1)
    RagSpace.objects.create(name="RS2", workspace=ws2)

    client = APIClient()
    client.force_authenticate(user=u1)

    # accès ok
    res_ok = client.get(f"/api/v1/rag-spaces/?workspace_id={ws1.id}")
    assert res_ok.status_code == 200
    assert [x["name"] for x in res_ok.data] == ["RS1"]
    assert str(res_ok.data[0]["id"]) == str(rs1.id)

    # pas d'accès au workspace ciblé -> REFUS explicite (403 not_a_member), pas
    # « 200 + [] » : la frontière d'autorisation doit être discernable d'un espace
    # accessible mais vide.
    res_no = client.get(f"/api/v1/rag-spaces/?workspace_id={ws2.id}")
    assert res_no.status_code == 403
    assert res_no.data["detail"] == "not_a_member"


_EXPECTED_KEYS = {
    "id",
    "name",
    "workspace_id",
    "llm_provider",
    "llm_model",
    "embedding_provider",
    "embedding_model",
    "allow_external_fallback",
    "reindex_required",
    # Champs d'accès effectif ajoutés (owner-scope / verrou config modèles). Présents
    # dans LES DEUX branches (scoped / global) → la parité de clés testée ici tient.
    "permission_level",
    "space_role",
    "is_owner_implicit",
    "can_edit_model_configuration",
    "audio_upload_enabled",
}


@pytest.mark.django_db
def test_ragspace_list_branches_have_same_keys_and_embedding_fields():
    """Les deux branches (scoped vs global) doivent renvoyer EXACTEMENT les mêmes
    clés essentielles, embeddings inclus (régression A4 : la branche globale les
    omettait, ce qui levait un 500 KeyError au niveau du serializer)."""
    user = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    ws = Workspace.objects.create(name="WS1")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    rs = RagSpace.objects.create(
        name="RS1",
        workspace=ws,
        llm_provider="ollama",
        llm_model="llama3",
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
    )

    client = APIClient()
    client.force_authenticate(user=user)

    scoped = client.get(f"/api/v1/rag-spaces/?workspace_id={ws.id}")
    glob = client.get("/api/v1/rag-spaces/")

    assert scoped.status_code == 200
    assert glob.status_code == 200
    assert len(scoped.data) == 1 and len(glob.data) == 1

    # parité des clés entre branches
    assert set(scoped.data[0].keys()) == _EXPECTED_KEYS
    assert set(glob.data[0].keys()) == _EXPECTED_KEYS

    # les valeurs reflètent bien l'espace dans les DEUX branches
    for item in (scoped.data[0], glob.data[0]):
        assert str(item["id"]) == str(rs.id)
        assert item["workspace_id"] == ws.id
        assert item["embedding_provider"] == "ollama"
        assert item["embedding_model"] == "nomic-embed-text"
        assert item["llm_provider"] == "ollama"


@pytest.mark.django_db
def test_ragspace_list_excludes_soft_deleted():
    user = User.objects.create_user(username="u1", email="u1@example.com", password="pw")
    ws = Workspace.objects.create(name="WS1")
    Membership.objects.create(workspace=ws, user=user, role=Membership.ROLE_MEMBER)
    RagSpace.objects.create(name="alive", workspace=ws)
    dead = RagSpace.objects.create(name="dead", workspace=ws)
    dead.soft_delete()

    client = APIClient()
    client.force_authenticate(user=user)

    for url in (f"/api/v1/rag-spaces/?workspace_id={ws.id}", "/api/v1/rag-spaces/"):
        res = client.get(url)
        assert res.status_code == 200
        names = [x["name"] for x in res.data]
        assert names == ["alive"]

"""Tests transverses de contrôle d'accès / isolation multi-workspace.

Vérifie qu'un utilisateur du workspace A ne peut pas atteindre les ressources
du workspace B (anti-énumération : 403, sauf endpoints chargeant la ressource
avant l'ACL qui renvoient 404).
"""
import pytest

pytestmark = pytest.mark.django_db


def test_unauthenticated_endpoints_return_401(db, rag_space, workspace):
    from rest_framework.test import APIClient

    c = APIClient()
    assert c.get("/api/v1/workspaces/").status_code == 401
    assert c.get(f"/api/v1/rag-spaces/?workspace_id={workspace.id}").status_code == 401
    assert c.get(f"/api/v1/documents/?rag_space_id={rag_space.id}").status_code == 401


def test_member_cannot_access_foreign_workspace_audit(api_client, foreign_setup):
    res = api_client.get(
        f"/api/v1/audit/rag-queries/?workspace_id={foreign_setup['workspace'].id}"
    )
    assert res.status_code == 403


# Occurrence unique : la copie identique `test_list_forbidden_foreign_ragspace` de
# test_documents_api.py a été retirée (doublon strict — même corps, mêmes fixtures).
def test_member_cannot_access_foreign_documents(api_client, foreign_setup):
    res = api_client.get(
        f"/api/v1/documents/?rag_space_id={foreign_setup['rag_space'].id}"
    )
    assert res.status_code == 403


# Occurrence unique : la copie identique `test_chat_forbidden_on_foreign_ragspace` de
# test_chat_api.py a été retirée (doublon strict — même corps, mêmes fixtures).
def test_member_cannot_chat_in_foreign_ragspace(api_client, foreign_setup):
    res = api_client.post(
        "/api/v1/chat/",
        {"rag_space_id": str(foreign_setup["rag_space"].id), "question": "x"},
        format="json",
    )
    assert res.status_code == 403


def test_member_cannot_list_foreign_members(api_client, foreign_setup):
    res = api_client.get(
        f"/api/v1/workspaces/{foreign_setup['workspace'].id}/members/"
    )
    assert res.status_code == 403


def test_foreign_user_cannot_see_my_threads(api_client, foreign_setup, rag_space):
    # création d'un thread par le user courant
    create = api_client.post(
        "/api/v1/chat/threads/", {"rag_space_id": str(rag_space.id)}, format="json"
    )
    thread_id = create.data["id"]
    # l'autre user ne peut pas lire les messages de ce thread
    res = foreign_setup["client"].get(f"/api/v1/chat/threads/{thread_id}/messages/")
    assert res.status_code in (403, 404)

"""Tests de l'API rag-spaces (list / patch / soft-delete / stats)."""
import pytest

from apps.rag_core.models import RagSpace

pytestmark = pytest.mark.django_db


def test_list_by_workspace(api_client, workspace, rag_space):
    res = api_client.get(f"/api/v1/rag-spaces/?workspace_id={workspace.id}")
    assert res.status_code == 200
    ids = [r["id"] for r in res.data]
    assert str(rag_space.id) in ids


def test_list_non_member_is_forbidden(api_client, foreign_setup):
    # Contrat : un utilisateur authentifié mais NON MEMBRE du workspace visé est
    # REFUSÉ (403 not_a_member) — et non « 200 + [] ». Le 403 matérialise la
    # frontière d'autorisation et évite de confondre « workspace inaccessible » avec
    # « workspace accessible mais sans RagSpace » (cf. cas membre-vide ci-dessous).
    res = api_client.get(f"/api/v1/rag-spaces/?workspace_id={foreign_setup['workspace'].id}")
    assert res.status_code == 403
    assert res.data["detail"] == "not_a_member"


def test_list_member_without_ragspace_returns_empty(api_client, workspace):
    # Distinction explicite : un MEMBRE autorisé d'un workspace SANS RagSpace obtient
    # bien 200 + [] (résultat vide légitime), à ne pas confondre avec le refus 403.
    res = api_client.get(f"/api/v1/rag-spaces/?workspace_id={workspace.id}")
    assert res.status_code == 200
    assert res.data == []


def test_patch_requires_admin(api_client, rag_space):
    # api_client est membre (pas owner/admin) -> 403
    res = api_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"name": "nouveau"}, format="json"
    )
    assert res.status_code == 403


def test_patch_as_admin(admin_client, rag_space):
    res = admin_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"name": "nouveau-nom"}, format="json"
    )
    assert res.status_code == 200
    assert res.data["name"] == "nouveau-nom"


def test_patch_empty_body_400(admin_client, rag_space):
    res = admin_client.patch(f"/api/v1/rag-spaces/{rag_space.id}/", {}, format="json")
    assert res.status_code == 400


def test_delete_is_a_permanent_purge_not_a_soft_delete(admin_client, rag_space):
    """DELETE = purge irréversible (contrat actuel), et NON un soft delete.

    Remplace `test_soft_delete_idempotent` : le soft delete derrière ce bouton a été
    retiré — il faisait disparaître l'espace de l'UI tout en conservant documents,
    chunks et fichiers, alors que l'interface promettait l'inverse.
    Le stockage est couvert par tests/tests_root/integration/test_ragspace_hard_delete.py.
    """
    from apps.rag_core.models import RagSpace

    space_id = rag_space.id
    r1 = admin_client.delete(f"/api/v1/rag-spaces/{space_id}/")
    assert r1.status_code == 204
    assert not RagSpace.objects.filter(id=space_id).exists(), "la ligne doit être SUPPRIMÉE"

    # Deuxième appel : l'espace n'existe plus -> 404 (et jamais un 500).
    r2 = admin_client.delete(f"/api/v1/rag-spaces/{space_id}/")
    assert r2.status_code == 404


def test_delete_unknown_id_is_404(admin_client):
    """Un identifiant jamais existé est une erreur d'appel, pas une suppression réussie."""
    res = admin_client.delete("/api/v1/rag-spaces/00000000-0000-0000-0000-000000000000/")
    assert res.status_code == 404


def test_stats(api_client, rag_space):
    res = api_client.get(f"/api/v1/rag-spaces/{rag_space.id}/stats/")
    assert res.status_code == 200
    assert "documents" in res.data
    assert "chunks_total" in res.data


def test_stats_counts_ready_documents(api_client, rag_space):
    from apps.rag_core.services.ingest_facade import ingest_plain_text

    ingest_plain_text(rag_space_id=rag_space.id, filename="a.txt", text="Paris capitale.")
    res = api_client.get(f"/api/v1/rag-spaces/{rag_space.id}/stats/")
    docs = res.data["documents"]
    # le doc READY est bien compté dans 'ready' (et non noyé dans 'other')
    assert docs["total"] == 1
    assert docs["ready"] == 1
    assert docs["other"] == 0
    assert res.data["chunks_total"] >= 1

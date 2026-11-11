"""Phase 1 — switch chat Ollama/OpenAI par RAG space.

Couvre :
- validation PATCH (provider/model + clé OpenAI requise) ;
- endpoint capacités (aucun secret exposé) ;
- endpoint test de connexion (Ollama/OpenAI mockés) ;
- audit log des changements provider/modèle ;
- permissions (membre non-admin refusé).
"""
import json

import pytest

from apps.audit.models import LlmSettingsChangeLog

pytestmark = pytest.mark.django_db


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# PATCH validation
# --------------------------------------------------------------------------- #





def test_patch_ollama_is_accepted(su_client, rag_space):
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/",
        {"llm_provider": "ollama", "llm_model": "llama3.1:8b"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["llm_provider"] == "ollama"


def test_patch_invalid_provider_is_rejected(su_client, rag_space):
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_provider": "anthropic"}, format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "invalid_llm_provider"


def test_patch_invalid_model_is_rejected(su_client, rag_space):
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_model": "bad model !!"}, format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "invalid_llm_model"


def test_patch_empty_model_is_allowed(su_client, rag_space):
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_model": ""}, format="json"
    )
    assert res.status_code == 200
    assert res.data["llm_model"] == ""


def test_member_cannot_patch_provider(api_client, rag_space):
    res = api_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_provider": "ollama"}, format="json"
    )
    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #

def test_provider_model_change_creates_audit_log(su_client, rag_space):
    # L'ancien modèle est le DÉFAUT DE L'ENVIRONNEMENT, pas un littéral : le
    # comparer à `settings.DEFAULT_RAGSPACE_CHAT_MODEL` fait que le test suit la
    # configuration au lieu de la figer. On PATCH vers un modèle différent pour
    # matérialiser un vrai changement audité.
    from django.conf import settings

    previous_model = settings.DEFAULT_RAGSPACE_CHAT_MODEL
    target = "llama3:latest"
    assert target != previous_model, "le test exige un CHANGEMENT effectif"

    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_model": target}, format="json"
    )
    assert res.status_code == 200
    logs = LlmSettingsChangeLog.objects.filter(rag_space=rag_space)
    assert logs.count() == 1
    log = logs.first()
    assert log.old_model == previous_model
    assert log.new_model == target
    assert log.new_provider == settings.DEFAULT_RAGSPACE_CHAT_PROVIDER


def test_no_audit_log_when_no_effective_change(admin_client, rag_space):
    # Espace mis en Ollama : re-PATCH des mêmes valeurs -> aucun changement -> pas de log.
    rag_space.llm_provider = "ollama"
    rag_space.embedding_provider = "ollama"
    rag_space.embedding_model = "nomic-embed-text"
    rag_space.save(update_fields=["llm_provider", "embedding_provider", "embedding_model"])
    admin_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/",
        {
            "llm_provider": "ollama",
            "llm_model": "llama3.1:8b",
            "embedding_provider": "ollama",
            "embedding_model": "nomic-embed-text",
        },
        format="json",
    )
    assert LlmSettingsChangeLog.objects.filter(rag_space=rag_space).count() == 0


def test_name_only_change_does_not_audit_llm(admin_client, rag_space):
    admin_client.patch(f"/api/v1/rag-spaces/{rag_space.id}/", {"name": "x"}, format="json")
    assert LlmSettingsChangeLog.objects.filter(rag_space=rag_space).count() == 0


# --------------------------------------------------------------------------- #
# Capabilities endpoint
# --------------------------------------------------------------------------- #





def test_capabilities_requires_owner_admin(api_client, rag_space):
    res = api_client.get(f"/api/v1/rag-spaces/{rag_space.id}/llm-settings/")
    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# Test-connection endpoint
# --------------------------------------------------------------------------- #



def test_connection_ollama_ok(admin_client, rag_space, monkeypatch):
    monkeypatch.setattr(
        "apps.llm_router.capabilities.requests.get", lambda *a, **k: _FakeResp(200)
    )
    res = admin_client.post(
        f"/api/v1/rag-spaces/{rag_space.id}/llm-settings/test/",
        {"provider": "ollama"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["ok"] is True


def test_connection_ollama_unreachable(admin_client, rag_space, monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr("apps.llm_router.capabilities.requests.get", _boom)
    res = admin_client.post(
        f"/api/v1/rag-spaces/{rag_space.id}/llm-settings/test/",
        {"provider": "ollama"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["ok"] is False
    assert res.data["code"] == "ollama_unreachable"




def test_connection_requires_owner_admin(api_client, rag_space):
    res = api_client.post(
        f"/api/v1/rag-spaces/{rag_space.id}/llm-settings/test/",
        {"provider": "ollama"},
        format="json",
    )
    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# Étape 1 — Chat/Embeddings séparés par RagSpace
# --------------------------------------------------------------------------- #

def test_defaults_are_local_ollama_for_chat_and_embeddings(db, workspace):
    """Un espace créé sans config explicite => IA locale (Ollama) pour le chat ET
    les embeddings, aux modèles PAR DÉFAUT DE L'ENVIRONNEMENT.

    Ce test affirmait `llm_model == "llama3.1:8b"`. C'est précisément ce qui a rendu
    invisible la divergence de production : il verrouillait le littéral au lieu de
    vérifier que la valeur suit `DEFAULT_RAGSPACE_*`. La couverture du suivi de
    configuration (avec deux environnements contrastés) vit dans
    tests/tests_root/http_api/test_ragspace_default_model_parity.py.
    """
    from django.conf import settings

    from apps.rag_core.models import RagSpace

    rs = RagSpace.objects.create(name="defaults", workspace=workspace)
    assert rs.llm_provider == settings.DEFAULT_RAGSPACE_CHAT_PROVIDER
    assert rs.llm_model == settings.DEFAULT_RAGSPACE_CHAT_MODEL
    assert rs.embedding_provider == settings.DEFAULT_RAGSPACE_EMBEDDING_PROVIDER
    assert rs.embedding_model == settings.DEFAULT_RAGSPACE_EMBEDDING_MODEL


def test_get_settings_returns_four_values(admin_client, rag_space):
    res = admin_client.get(f"/api/v1/rag-spaces/{rag_space.id}/llm-settings/")
    assert res.status_code == 200
    for key in ("llm_provider", "llm_model", "embedding_provider", "embedding_model"):
        assert key in res.data
    assert res.data["embeddings"]["configurable"] is True


def test_patch_chat_only_does_not_change_embeddings(su_client, rag_space):
    rag_space.embedding_provider = "ollama"
    rag_space.embedding_model = "nomic-embed-text"
    rag_space.llm_provider = "ollama"
    rag_space.save(update_fields=["embedding_provider", "embedding_model", "llm_provider"])

    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"llm_model": "llama3.1:8b"}, format="json"
    )
    assert res.status_code == 200
    rag_space.refresh_from_db()
    assert rag_space.llm_model == "llama3.1:8b"
    # Embeddings inchangés.
    assert rag_space.embedding_provider == "ollama"
    assert rag_space.embedding_model == "nomic-embed-text"


def test_patch_embeddings_only_does_not_change_chat(su_client, rag_space):
    rag_space.llm_provider = "ollama"
    rag_space.llm_model = "llama3:latest"
    rag_space.embedding_provider = "ollama"
    rag_space.save(update_fields=["llm_provider", "llm_model", "embedding_provider"])

    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/",
        {"embedding_model": "nomic-embed-text:v1.5"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["embeddings_reindex_required"] is True
    rag_space.refresh_from_db()
    assert rag_space.embedding_model == "nomic-embed-text:v1.5"
    # Chat inchangé.
    assert rag_space.llm_provider == "ollama"
    assert rag_space.llm_model == "llama3:latest"




def test_patch_invalid_embedding_provider_is_rejected(su_client, rag_space):
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/", {"embedding_provider": "cohere"}, format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "invalid_embedding_provider"


def test_embedding_change_is_audited(su_client, rag_space):
    # Défaut produit des embeddings = ollama / nomic-embed-text ; on PATCH vers un
    # modèle DIFFÉRENT pour matérialiser un vrai changement audité.
    rag_space.embedding_provider = "ollama"
    rag_space.save(update_fields=["embedding_provider"])
    res = su_client.patch(
        f"/api/v1/rag-spaces/{rag_space.id}/",
        {"embedding_model": "mxbai-embed-large"},
        format="json",
    )
    assert res.status_code == 200
    log = LlmSettingsChangeLog.objects.filter(rag_space=rag_space).first()
    assert log is not None
    assert log.new_embedding_model == "mxbai-embed-large"
    assert log.new_embedding_provider == "ollama"


# --------------------------------------------------------------------------- #
# Étape 2 — réindexation globale (jobs durables asynchrones)
#
# Contrat depuis la refonte en jobs durables : la requête HTTP CRÉE un job et
# répond 202 immédiatement. Aucune extraction/embedding n'a lieu dans la requête,
# quel que soit le mode Celery — il n'existe plus de branche « inline ».
# La machine à états du job elle-même est couverte par le fichier canonique
# tests/tests_root/http_api/test_ragspace_reindex_jobs.py.
# --------------------------------------------------------------------------- #

def test_reindex_creates_job_and_returns_202(admin_client, rag_space, ingest_text, monkeypatch):
    from apps.rag_core.models.reindex_job import (
        RagSpaceReindexJob,
        RagSpaceReindexJobItem,
        ReindexJobStatus,
    )

    # Le semis Celery est neutralisé : on teste la réponse HTTP, pas le broker.
    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )
    # Témoin : aucune extraction ne doit être déclenchée par la requête HTTP.
    extractions = {"n": 0}
    monkeypatch.setattr(
        "apps.rag_core.services.ingest_document.reindex_existing_document",
        lambda **k: extractions.__setitem__("n", extractions["n"] + 1),
    )

    ingest_text(rag_space, "Premier document de l'espace.")
    ingest_text(rag_space, "Second document de l'espace.", filename="doc2.txt")

    res = admin_client.post(f"/api/v1/rag-spaces/{rag_space.id}/reindex/", {}, format="json")
    assert res.status_code == 202
    assert res.data["status"] == "accepted"
    assert res.data["already_running"] is False
    assert res.data["total_documents"] == 2

    job = RagSpaceReindexJob.objects.get(id=res.data["job_id"])
    assert job.status == ReindexJobStatus.PENDING
    assert RagSpaceReindexJobItem.objects.filter(job=job).count() == 2
    # La réponse ne prétend RIEN sur un travail déjà accompli.
    assert extractions["n"] == 0


def test_reindex_empty_space_creates_empty_job(admin_client, rag_space, monkeypatch):
    from apps.rag_core.models.reindex_job import RagSpaceReindexJob, RagSpaceReindexJobItem

    monkeypatch.setattr(
        "apps.rag_core.services.ingest.reindex_jobs._enqueue_orchestrator", lambda job_id: None
    )

    res = admin_client.post(f"/api/v1/rag-spaces/{rag_space.id}/reindex/", {}, format="json")
    # Un espace vide n'est pas une erreur : c'est un job à zéro document.
    assert res.status_code == 202
    assert res.data["status"] == "accepted"
    assert res.data["total_documents"] == 0

    job = RagSpaceReindexJob.objects.get(id=res.data["job_id"])
    assert RagSpaceReindexJobItem.objects.filter(job=job).count() == 0


def test_reindex_requires_owner_admin(api_client, rag_space, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    res = api_client.post(f"/api/v1/rag-spaces/{rag_space.id}/reindex/", {}, format="json")
    assert res.status_code == 403

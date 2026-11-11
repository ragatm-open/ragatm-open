"""Tests du profil d'embeddings unifié (ai_gateway / legacy-768 / 768) et de ses gardes.

Couvre le bug d'audit : `RAG_HYBRID_ENABLED=True` était résolu à False par un
`== "1"` naïf, désactivant le leg vectoriel en silence.
"""
import os
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.rag_core.checks_embeddings import (
    ID_DIM,
    ID_HYBRID,
    ID_MODEL,
    ID_PROVIDER,
    check_embeddings_profile,
)
from config.env import env_bool, env_str


# --------------------------------------------------------------------------
# Parsing booléen
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["1", "true", "True", "TRUE", "yes", "on", " true ", '"1"'])
def test_env_bool_truthy(raw):
    with mock.patch.dict(os.environ, {"X_FLAG": raw}):
        assert env_bool("X_FLAG", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off", " off "])
def test_env_bool_falsy(raw):
    with mock.patch.dict(os.environ, {"X_FLAG": raw}):
        assert env_bool("X_FLAG", True) is False


def test_env_bool_regression_true_string_is_not_false():
    """Le bug prod : RAG_HYBRID_ENABLED=True ne doit PLUS résoudre à False."""
    with mock.patch.dict(os.environ, {"RAG_HYBRID_ENABLED": "True"}):
        assert env_bool("RAG_HYBRID_ENABLED", False) is True


@pytest.mark.parametrize("raw", ["banana", "2", "oui", "vrai", "-1"])
def test_env_bool_invalid_raises_not_silent_false(raw):
    with mock.patch.dict(os.environ, {"X_FLAG": raw}):
        with pytest.raises(ImproperlyConfigured):
            env_bool("X_FLAG", True)


def test_env_bool_absent_uses_default():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("X_ABSENT_FLAG", None)
        assert env_bool("X_ABSENT_FLAG", True) is True
        assert env_bool("X_ABSENT_FLAG", False) is False


def test_env_str_trims_and_defaults():
    with mock.patch.dict(os.environ, {"X_S": '  "ollama" '}):
        assert env_str("X_S", "zzz") == "ollama"
    with mock.patch.dict(os.environ, {"X_S": "  "}):
        assert env_str("X_S", "fallback") == "fallback"


# --------------------------------------------------------------------------
# Défauts du profil (source de vérité)
# --------------------------------------------------------------------------
def test_reference_profile_defaults(settings):
    assert settings.EMBEDDINGS_EXPECTED_PROVIDER == "ai_gateway"
    assert settings.EMBEDDINGS_EXPECTED_MODEL == "legacy-768"
    assert settings.RAG_EMBEDDING_DIM == 768


def test_min_score_default_is_calibrated_value():
    """Le défaut du CODE (hors .env) doit être le seuil calibré du profil 768 de référence."""
    import importlib

    from apps.rag_core.domain import policies

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RAG_MIN_SCORE", None)
        importlib.reload(policies)
        assert policies.DEFAULT_MIN_SCORE == 0.76


def test_governed_default_model_is_the_gateway_profile(settings):
    from apps.llm_router.embeddings import default_embedding_model_for_provider

    settings.EMBEDDINGS_MODEL = "default"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    assert default_embedding_model_for_provider("ai_gateway") == "legacy-768"


# --------------------------------------------------------------------------
# System checks : divergences
# --------------------------------------------------------------------------
def _ids(issues):
    return {i.id for i in issues}


def test_check_passes_on_reference_profile(settings):
    settings.DEBUG = False
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.EMBEDDINGS_MODEL = "default"  # -> legacy-768
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    assert check_embeddings_profile(None) == []


def test_check_errors_on_divergent_provider_in_prod(settings):
    settings.DEBUG = False
    settings.EMBEDDINGS_PROVIDER = "ollama"  # hors profil de référence (et sans chemin d'encodage)
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    issues = check_embeddings_profile(None)
    assert ID_PROVIDER in _ids(issues)
    assert all(i.level >= 40 for i in issues)  # ERROR bloquant


def test_check_errors_on_wrong_dimension_and_hybrid_off(settings):
    settings.DEBUG = False
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.RAG_EMBEDDING_DIM = 1536
    settings.RAG_HYBRID_ENABLED = False
    issues = check_embeddings_profile(None)
    assert {ID_DIM, ID_HYBRID} <= _ids(issues)


def test_check_errors_on_wrong_model(settings):
    settings.DEBUG = False
    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "fr-en-retrieval"
    issues = check_embeddings_profile(None)
    assert ID_MODEL in _ids(issues)


def test_check_is_warning_only_in_dev(settings):
    settings.DEBUG = True
    settings.EMBEDDINGS_PROVIDER = "ollama"
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    issues = check_embeddings_profile(None)
    assert ID_PROVIDER in _ids(issues)
    assert all(i.level < 40 for i in issues)  # WARNING non bloquant


def test_check_ignores_stub_escape_hatch(settings):
    settings.DEBUG = False
    settings.EMBEDDINGS_PROVIDER = "stub"
    assert check_embeddings_profile(None) == []


# --------------------------------------------------------------------------
# Commande d'audit (lecture seule) — codes de sortie
# --------------------------------------------------------------------------
@pytest.mark.django_db
def test_audit_exit_0_when_aligned_and_index_empty(settings):
    """Config conforme + index vide (aucun document) => aligné."""
    from django.core.management import call_command

    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.EMBEDDINGS_MODEL = "default"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    with pytest.raises(SystemExit) as exc:
        call_command("audit_embeddings_config")
    assert exc.value.code == 0


@pytest.mark.django_db
def test_audit_exit_1_on_config_divergence(settings):
    from django.core.management import call_command

    settings.EMBEDDINGS_PROVIDER = "ollama"  # hors profil de référence
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True
    with pytest.raises(SystemExit) as exc:
        call_command("audit_embeddings_config")
    assert exc.value.code == 1


@pytest.mark.django_db
def test_audit_exit_2_on_ready_documents_without_embeddings(settings):
    """Le cas exact constaté en staging : documents READY, 0 vecteur => index inerte."""
    from django.core.management import call_command

    from apps.rag_core.models import Chunk, Document, DocumentStatus, RagSpace
    from apps.workspaces.models import Workspace

    settings.EMBEDDINGS_PROVIDER = "ai_gateway"
    settings.EMBEDDINGS_MODEL = "default"
    settings.AI_GATEWAY_EMBEDDINGS_MODEL = "legacy-768"
    settings.RAG_EMBEDDING_DIM = 768
    settings.RAG_HYBRID_ENABLED = True

    ws = Workspace.objects.create(name="WAudit")
    sp = RagSpace.objects.create(name="audit-space", workspace=ws)
    doc = Document.objects.create(rag_space=sp, filename="a.pdf", status=DocumentStatus.READY)
    Chunk.objects.create(rag_space=sp, document=doc, chunk_index=0, text="t", meta={})

    with pytest.raises(SystemExit) as exc:
        call_command("audit_embeddings_config")
    assert exc.value.code == 2

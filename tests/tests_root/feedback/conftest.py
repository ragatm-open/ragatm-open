"""Fixtures communes aux tests de feedback."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.audit.models import RagQueryLog


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Vide le cache DRF de throttling entre les tests.

    Le throttle est porté par le cache Django (LocMem en test), qui SURVIT au
    rollback de base : sans ce nettoyage, les compteurs d'un test fuiraient dans
    le suivant, et l'ordre d'exécution deviendrait significatif.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _diagnostics_enabled_by_default(settings):
    """Ce paquet teste le dispositif DIAGNOSTIQUE : il est donc actif par défaut.

    La baseline globale force les cinq drapeaux à 0 (déterminisme quel que soit
    le `.env` du poste). Or ici, la plupart des tests ont besoin d'un snapshot
    pour exister. On active donc la capture pour ce paquet — et les tests qui
    vérifient précisément le comportement DÉSACTIVÉ posent leur propre
    `override_settings`, qui l'emporte (cf. test_diagnostics_gating.py).
    """
    settings.RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED = True
    return settings


@pytest.fixture
def query_log(db, user, workspace, rag_space):
    """Un log de requête RAG complet et réaliste (sources v3 comprises)."""
    return RagQueryLog.objects.create(
        user=user,
        workspace=workspace,
        rag_space=rag_space,
        question="what is Nordexia Conseil?",
        answer="I don't know.",
        status=RagQueryLog.STATUS_NO_ANSWER,
        policy="no_answer",
        prompt_hash="a" * 64,
        llm_provider="ollama",
        llm_model="qwen3:30b-instruct",
        sources=[
            {
                "schema_version": 3,
                "source_type": "workspace_document",
                "rank": 1,
                "chunk_id": "11111111-1111-1111-1111-111111111111",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "score": 0.71,
                "filename": "note-interne-2023.pdf",
                "page_number": 4,
                "snippet": "extrait",
            }
        ],
    )

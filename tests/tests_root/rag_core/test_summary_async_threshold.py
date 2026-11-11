"""Tests de la bascule async des résumés/rapports fondée sur une estimation de coût.

Couvre : estimation de tokens, décision `_should_run_async`, et l'aiguillage de
`summarize_document` (sync vs enqueue Celery) selon le seuil / eager / async off.
Mocks uniquement — aucune génération LLM ni broker réel.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.rag_core.services import summary_service as ss


# --------------------------------------------------------------- estimation

def test_estimate_generation_calls():
    assert ss._estimate_generation_calls([["a"]]) == 1          # mono-bloc
    assert ss._estimate_generation_calls([]) == 1               # vide => 1
    assert ss._estimate_generation_calls([["a"], ["b"]]) == 3   # 2 blocs => map+map+reduce
    assert ss._estimate_generation_calls([["a"], ["b"], ["c"]]) == 4


def test_estimate_output_tokens(settings):
    settings.RAG_REPORT_NUM_PREDICT = 2048
    assert ss._estimate_output_tokens([["a"]]) == 2048          # 1 × 2048
    assert ss._estimate_output_tokens([["a"], ["b"]]) == 3 * 2048


# --------------------------------------------------------------- décision brute

def test_should_run_async_long(settings):
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = False
    assert ss._should_run_async(True) is True                   # map-reduce
    assert ss._should_run_async(False, heavy=True) is True      # mono-bloc lourd
    assert ss._should_run_async(False, heavy=False) is False    # léger => sync


def test_should_run_async_eager_forces_sync(settings):
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = True
    assert ss._should_run_async(True, heavy=True) is False


def test_should_run_async_disabled(settings):
    settings.SUMMARY_ASYNC_ENABLED = False
    settings.CELERY_TASK_ALWAYS_EAGER = False
    assert ss._should_run_async(True, heavy=True) is False


def test_default_threshold_preserves_single_block_sync(settings):
    """Non-régression : au seuil par défaut, un mono-bloc rapport reste sync."""
    settings.RAG_REPORT_NUM_PREDICT = 2048
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 2048
    est = ss._estimate_output_tokens([["a"]])
    assert est == 2048
    assert (est > ss._async_max_sync_output_tokens()) is False   # pas "heavy"


# --------------------------------------------------------------- aiguillage e2e

SYNC = object()
ASYNC = object()


@pytest.fixture
def _patch_paths(monkeypatch):
    """Neutralise chunks/génération et marque le chemin pris (sync vs async)."""
    monkeypatch.setattr(
        ss, "_fetch_chunks",
        lambda document, scope, page: [SimpleNamespace(id=1, text="contenu", page_number=None, chunk_index=0)],
    )
    monkeypatch.setattr(ss, "_perform_generation", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(ss, "_finalize", lambda **k: SYNC)
    monkeypatch.setattr(ss, "_enqueue_async", lambda **k: ASYNC)


def _make_doc(rag_space):
    from apps.rag_core.models.document import Document
    return Document.objects.create(rag_space=rag_space, filename="rapport.txt", status="READY")


@pytest.mark.django_db
def test_summarize_sync_at_default_threshold(_patch_paths, settings, rag_space):
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.RAG_REPORT_NUM_PREDICT = 2048
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 2048   # mono-bloc (2048) NON > 2048
    doc = _make_doc(rag_space)
    out = ss.summarize_document(user=None, document=doc)
    assert out is SYNC


@pytest.mark.django_db
def test_summarize_async_when_threshold_lowered(_patch_paths, settings, rag_space):
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.RAG_REPORT_NUM_PREDICT = 2048
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 1024   # mono-bloc (2048) > 1024 => heavy
    doc = _make_doc(rag_space)
    out = ss.summarize_document(user=None, document=doc)
    assert out is ASYNC


@pytest.mark.django_db
def test_summarize_eager_stays_sync_even_if_heavy(_patch_paths, settings, rag_space):
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = True                # pas de broker => inline
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 1        # forcerait "heavy"
    doc = _make_doc(rag_space)
    out = ss.summarize_document(user=None, document=doc)
    assert out is SYNC


@pytest.mark.django_db
def test_summarize_async_disabled_stays_sync(_patch_paths, settings, rag_space):
    settings.SUMMARY_ASYNC_ENABLED = False
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 1
    doc = _make_doc(rag_space)
    out = ss.summarize_document(user=None, document=doc)
    assert out is SYNC


@pytest.mark.django_db
def test_enqueue_async_calls_celery_delay(monkeypatch, settings, rag_space):
    """Le chemin async crée une ligne PENDING et enqueue la tâche Celery (mockée)."""
    settings.SUMMARY_ASYNC_ENABLED = True
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = 1
    monkeypatch.setattr(
        ss, "_fetch_chunks",
        lambda document, scope, page: [SimpleNamespace(id=1, text="contenu", page_number=None, chunk_index=0)],
    )

    captured = {}
    import apps.rag_core.tasks as tasks_mod
    stub = SimpleNamespace(delay=lambda **k: captured.update(k))
    monkeypatch.setattr(tasks_mod, "generate_document_summary_task", stub, raising=True)

    doc = _make_doc(rag_space)
    out = ss.summarize_document(user=None, document=doc)
    assert out.status == "pending"
    assert "summary_id" in captured

    from apps.rag_core.models.summary import DocumentSummary
    assert DocumentSummary.objects.filter(document=doc, status="PENDING").exists()

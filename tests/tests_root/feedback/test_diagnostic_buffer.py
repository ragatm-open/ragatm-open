"""Tampon de capture — bornes, TTL, confidentialité, fail-open.

Le tampon est écrit pour 100 % des réponses. C'est ce qui rend ses invariants
non négociables : s'il contenait du texte documentaire, on aurait créé une
seconde copie du corpus dans Redis.
"""
from __future__ import annotations

import json

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.feedback.diagnostics import buffer, capture
from apps.feedback.diagnostics import reasons as R

pytestmark = pytest.mark.django_db


# --- Jeu de candidats représentatif ----------------------------------------
def _chunk(cid, doc="doc-1", **kw):
    base = {
        "chunk_id": cid,
        "source": {"document_id": doc, "filename": "SECRET_Rapport_interne.pdf"},
        "text": "Nordexia Conseil est un cabinet de conseil en organisation.",
        "score": 0.71,
        "fusion_score": 0.032,
        "text_rank": 3,
        "vector_rank": 7,
    }
    base.update(kw)
    return base


@pytest.fixture
def pool():
    return [_chunk(f"c{i}") for i in range(1, 9)]


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_buffer_contains_no_text_no_filename_no_path(pool):
    trace = capture.build_candidate_trace(
        pool=pool,
        selected_pre_rerank=pool[:4],
        selected_post_rerank=pool[:3],
        selected_final=pool[:2],
        top_k=4,
        rerank_applied=True,
        diversity_applied=False,
    )
    payload = capture.build_trace_payload(
        log_id="log-1",
        created_at="2026-07-28T10:00:00+00:00",
        embedding_model="nomic-embed-text",
        candidate_trace=trace,
        selection=capture.build_selection(
            selected_final=pool[:2],
            answerability_policy="ok",
            thresholds={"min_score": 0.76},
            budgets={"top_k": 4},
        ),
    )
    assert buffer.write_trace("log-1", payload) is True

    raw = cache.get(buffer.buffer_key("log-1"))
    assert raw, "la trace doit être écrite"

    # L'INVARIANT central : rien de documentaire ne doit se trouver là.
    for forbidden in (
        "Nordexia Conseil",
        "conseil en organisation",
        "SECRET_Rapport_interne.pdf",
        ".pdf",
        "/media/",
    ):
        assert forbidden not in raw, f"contenu interdit dans le tampon : {forbidden!r}"

    parsed = json.loads(raw)
    assert {c["chunk_id"] for c in parsed["candidates"]} == {f"c{i}" for i in range(1, 9)}


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TOP_K=3)
def test_buffer_is_bounded_to_top_k(pool):
    from apps.feedback.diagnostics.hook import capture_retrieval_trace

    capture_retrieval_trace(
        log_id="log-2",
        pool=pool,
        selected_pre_rerank=pool[:2],
        selected_post_rerank=pool[:2],
        selected_final=pool[:2],
        top_k=4,
        rerank_applied=False,
        diversity_applied=False,
        answerability_policy="ok",
    )
    parsed = json.loads(cache.get(buffer.buffer_key("log-2")))
    assert len(parsed["candidates"]) == 3
    assert parsed["pool_size"] == 8
    # La troncature est DITE, jamais silencieuse.
    assert parsed["truncated"] is True


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=False)
def test_flag_off_writes_nothing(pool):
    from apps.feedback.diagnostics.hook import capture_retrieval_trace

    assert (
        capture_retrieval_trace(
            log_id="log-3",
            pool=pool,
            selected_pre_rerank=[],
            selected_post_rerank=[],
            selected_final=[],
            top_k=4,
            rerank_applied=False,
            diversity_applied=False,
            answerability_policy="no_answer",
        )
        is False
    )
    assert cache.get(buffer.buffer_key("log-3")) is None


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS=900)
def test_ttl_is_applied(pool, monkeypatch):
    seen = {}

    real_set = cache.set

    def _spy(key, value, timeout=None, **kw):
        seen["timeout"] = timeout
        return real_set(key, value, timeout, **kw)

    monkeypatch.setattr(cache, "set", _spy)
    buffer.write_trace("log-4", {"candidates": []})
    assert seen["timeout"] == 900


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_redis_failure_never_breaks_the_caller(monkeypatch, pool):
    def _boom(*a, **kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "set", _boom)
    # Aucune exception ne doit sortir : une capture d'observabilité ne coûte
    # jamais une réponse à l'utilisateur.
    assert buffer.write_trace("log-5", {"candidates": []}) is False


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_oversized_payload_is_shrunk_and_flagged():
    payload = {
        "candidates": [{"chunk_id": f"c{i}", "padding": "x" * 400} for i in range(500)],
        "truncated": False,
    }
    assert buffer.write_trace("log-6", payload) is True
    raw = cache.get(buffer.buffer_key("log-6"))
    # On mesure le blob RÉELLEMENT stocké, pas une resérialisation.
    assert len(raw.encode("utf-8")) <= buffer.MAX_BUFFER_BYTES
    parsed = json.loads(raw)
    assert parsed["truncated"] is True
    assert len(parsed["candidates"]) < 500


def test_read_returns_none_when_absent():
    assert buffer.read_trace("jamais-ecrit") is None


# ---------------------------------------------------------------------------
# Raisons d'exclusion
# ---------------------------------------------------------------------------
def test_exclusion_reasons_are_deduced_not_guessed(pool):
    """Chaque raison provient d'une COMPARAISON entre états, pas d'une supposition."""
    trace = capture.build_candidate_trace(
        pool=pool,
        selected_pre_rerank=pool[:5],   # c1..c5 avant rerank
        selected_post_rerank=pool[:3],  # c1..c3 après rerank
        selected_final=pool[:2],        # c1..c2 après budget de prompt
        top_k=6,
        rerank_applied=True,
        diversity_applied=True,
    )
    by_id = {c["chunk_id"]: c for c in trace["candidates"]}

    assert by_id["c1"]["selected"] is True and by_id["c1"]["excluded_by"] is None
    assert by_id["c2"]["selected"] is True
    # Retenu par le ranking, évincé par le budget de caractères.
    assert by_id["c3"]["excluded_by"] == R.PROMPT_BUDGET
    # Présent avant rerank, absent après.
    assert by_id["c4"]["excluded_by"] == R.RERANK_CUT
    assert by_id["c5"]["excluded_by"] == R.RERANK_CUT
    # Dans le top_k du pool, jamais sélectionné, diversification active.
    assert by_id["c6"]["excluded_by"] == R.DIVERSITY_MAX_PER_GROUP
    # Au-delà du top_k.
    assert by_id["c7"]["excluded_by"] == R.NOT_SELECTED_TOP_K


def test_unknown_is_used_when_the_step_is_not_known(pool):
    """On ne fabrique pas une raison plausible : on dit qu'on ne sait pas."""
    trace = capture.build_candidate_trace(
        pool=pool,
        selected_pre_rerank=pool[:1],
        selected_post_rerank=pool[:1],
        selected_final=pool[:1],
        top_k=6,
        rerank_applied=False,
        diversity_applied=False,  # aucune étape identifiable n'explique l'exclusion
    )
    by_id = {c["chunk_id"]: c for c in trace["candidates"]}
    assert by_id["c2"]["excluded_by"] == R.UNKNOWN


def test_leg_is_derived_from_ranks():
    trace = capture.build_candidate_trace(
        pool=[
            _chunk("a", text_rank=1, vector_rank=None),
            _chunk("b", text_rank=None, vector_rank=2),
            _chunk("c", text_rank=1, vector_rank=2),
            _chunk("d", text_rank=None, vector_rank=None, retrieval_leg="temporal"),
        ],
        selected_pre_rerank=[],
        selected_post_rerank=[],
        selected_final=[],
        top_k=4,
        rerank_applied=False,
        diversity_applied=False,
    )
    legs = {c["chunk_id"]: c["leg"] for c in trace["candidates"]}
    assert legs == {"a": "fts", "b": "vector", "c": "hybrid", "d": "temporal"}


def test_non_finite_scores_are_neutralised():
    """Un NaN sérialisé casserait la relecture du tampon."""
    trace = capture.build_candidate_trace(
        pool=[_chunk("a", score=float("nan"), fusion_score=float("inf"))],
        selected_pre_rerank=[],
        selected_post_rerank=[],
        selected_final=[],
        top_k=1,
        rerank_applied=False,
        diversity_applied=False,
    )
    entry = trace["candidates"][0]
    assert entry["score"] is None and entry["fusion_score"] is None
    json.dumps(trace)  # doit être sérialisable

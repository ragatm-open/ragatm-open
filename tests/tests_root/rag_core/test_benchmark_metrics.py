"""Tests unitaires des métriques de benchmark (fonctions pures, sans DB)."""
import math

import pytest

from apps.rag_core.benchmark import metrics


def test_recall_at_k_hit_and_miss():
    retrieved = ["a.pdf", "b.pdf", "c.pdf"]
    assert metrics.recall_at_k(retrieved, ["a.pdf"], 1) == 1.0
    assert metrics.recall_at_k(retrieved, ["b.pdf"], 1) == 0.0
    assert metrics.recall_at_k(retrieved, ["b.pdf"], 3) == 1.0


def test_recall_at_k_multi_expected_is_fraction():
    retrieved = ["a.pdf", "x.pdf", "b.pdf"]
    # 2 attendus, 1 seul dans le top-2 -> 0.5
    assert metrics.recall_at_k(retrieved, ["a.pdf", "b.pdf"], 2) == 0.5
    assert metrics.recall_at_k(retrieved, ["a.pdf", "b.pdf"], 3) == 1.0


def test_recall_empty_expected_returns_zero():
    assert metrics.recall_at_k(["a"], [], 5) == 0.0


def test_precision_at_k():
    retrieved = ["a", "b", "c", "d"]
    assert metrics.precision_at_k(retrieved, ["a", "b"], 2) == 1.0
    assert metrics.precision_at_k(retrieved, ["a"], 4) == 0.25
    assert metrics.precision_at_k([], ["a"], 4) == 0.0


def test_reciprocal_rank():
    assert metrics.reciprocal_rank(["a", "b", "c"], ["a"]) == 1.0
    assert metrics.reciprocal_rank(["a", "b", "c"], ["b"]) == 0.5
    assert metrics.reciprocal_rank(["a", "b", "c"], ["c"]) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank(["a", "b"], ["z"]) == 0.0


def test_first_relevant_rank():
    assert metrics.first_relevant_rank(["a", "b"], ["b"]) == 2
    assert metrics.first_relevant_rank(["a", "b"], ["z"]) is None


def test_no_answer_correct():
    assert metrics.no_answer_correct(should_answer=True, status="ok") is True
    assert metrics.no_answer_correct(should_answer=True, status="no_answer") is False
    assert metrics.no_answer_correct(should_answer=False, status="no_answer") is True
    assert metrics.no_answer_correct(should_answer=False, status="ok") is False
    assert metrics.no_answer_correct(should_answer=True, status="error") is False


def test_citation_precision():
    assert metrics.citation_precision([], 3) == 1.0  # rien à invalider
    assert metrics.citation_precision([1, 2], 3) == 1.0
    assert metrics.citation_precision([1, 5], 3) == 0.5  # [5] invalide
    assert metrics.citation_precision([4], 3) == 0.0


def test_answer_contains_score():
    assert metrics.answer_contains_score("le mot de passe par email", ["email", "passe"]) == 1.0
    assert metrics.answer_contains_score("le mot de passe", ["email"]) == 0.0
    assert metrics.answer_contains_score("rien", []) == 1.0  # rien à vérifier


def test_percentile_and_mean():
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([10], 95) == 10
    assert metrics.percentile([0, 10], 50) == 5.0
    assert metrics.percentile([0, 100], 95) == pytest.approx(95.0)
    assert metrics.mean([]) is None
    assert metrics.mean([2, 4]) == 3.0


def test_dedup_preserve_order():
    assert metrics.dedup_preserve_order(["a", "b", "a", "c", None, "b"]) == ["a", "b", "c"]

"""Pondération des legs corpus dans la fusion RRF unifiée — spécification.

La fusion unifiée, activée telle quelle en production, a évincé quasi totalement les
documents privés (rétention 0,00 sur 3 questions cibles sur 4). Le levier correct n'est
pas un quota de provenance — qui réserverait des places indépendamment de la pertinence
— mais l'atténuation du VOTE des legs corpus dans le calcul de rang.

Ce fichier verrouille trois choses :
  1. le réglage est validé STRICTEMENT (jamais de repli silencieux à 1.0) ;
  2. α = 1.0 reproduit exactement la fusion non pondérée, et α < 1 réduit la seule
     contribution du corpus, sans toucher aux rangs privés ;
  3. la télémétrie distingue ce qui est SÉLECTIONNÉ de ce qui survit au budget de
     prompt — la seule étape qui décrive ce que le modèle a réellement lu.
"""
from __future__ import annotations

import math

import pytest
from django.test import override_settings

from apps.rag_core.services.query.candidate_retriever import (
    WORKSPACE_RRF_LEG_WEIGHT,
    corpus_rrf_leg_weight,
)
from apps.rag_core.services.query.contracts import RetrievalPolicy
from apps.rag_core.services.query.errors import RetrievalConfigurationError
from apps.rag_core.services.query.merge import (
    MODALITY_TEXT,
    MODALITY_VECTOR,
    FusionLeg,
    _rrf_k,
    merge_rrf,
)


def _policy(**kwargs) -> RetrievalPolicy:
    base = dict(
        fts_required=True, hybrid_enabled=True, vector_available=True,
        temporal_enabled=False, rerank_enabled=False, diversity_enabled=True,
        min_sources=1, min_text_rank=0.0, min_score=0.5,
        top_k=8, vector_top_k=8, candidate_k=24, lexical_weight=1.0,
    )
    base.update(kwargs)
    return RetrievalPolicy(**base)


# ===========================================================================
# Configuration — validation stricte, aucun repli silencieux
# ===========================================================================
def test_default_weight_is_neutral():
    """Le défaut reproduit la fusion non pondérée : livrer ne change rien."""
    assert corpus_rrf_leg_weight() == 1.0
    assert _policy().corpus_rrf_leg_weight == 1.0
    assert WORKSPACE_RRF_LEG_WEIGHT == 1.0


@pytest.mark.parametrize("value", [1.0, 0.75, 0.5, 0.35, 0.25, 0.01])
def test_valid_weights_are_accepted(value):
    assert _policy(corpus_rrf_leg_weight=value).corpus_rrf_leg_weight == value


@pytest.mark.parametrize("value", [0.0, -0.1, -1.0, 1.01, 2.0])
def test_out_of_range_weights_are_refused(value):
    """Hors ]0, 1] : refus explicite.

    Ramener la valeur à 1.0 exécuterait la fusion NON pondérée en croyant l'avoir
    atténuée — exactement l'erreur que ce lot cherche à rendre impossible.
    """
    with pytest.raises(RetrievalConfigurationError, match="corpus_rrf_leg_weight"):
        _policy(corpus_rrf_leg_weight=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_weights_are_refused(value):
    with pytest.raises(RetrievalConfigurationError, match="corpus_rrf_leg_weight"):
        _policy(corpus_rrf_leg_weight=value)


@override_settings(RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT="pas-un-nombre")
def test_unreadable_setting_becomes_a_refused_value_not_a_fallback():
    """Une valeur illisible ne devient pas 1.0 : elle devient une valeur REFUSÉE."""
    raw = corpus_rrf_leg_weight()
    assert math.isnan(raw)
    with pytest.raises(RetrievalConfigurationError):
        _policy(corpus_rrf_leg_weight=raw)


@override_settings(
    RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=False,
    RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT=0.25,
)
def test_weight_has_no_effect_when_unified_fusion_is_off():
    """Fusion OFF : le poids est lu, mais aucun leg corpus ne vote — donc aucun effet."""
    from apps.rag_core.services.query.candidate_retriever import unified_fusion_enabled

    assert unified_fusion_enabled() is False
    assert corpus_rrf_leg_weight() == 0.25


# ===========================================================================
# Fusion — le poids agit sur le VOTE, pas après coup
# ===========================================================================
def _legs(corpus_weight: float, lexical_weight: float = 1.0):
    private_text = [{"chunk_id": f"w{i}", "score": 0.5, "source": {"document_id": f"dw{i}"}}
                    for i in range(1, 4)]
    private_vector = [{"chunk_id": f"w{i}", "score": 0.8, "source": {"document_id": f"dw{i}"}}
                      for i in range(1, 4)]
    corpus_text = [{"chunk_id": f"c{i}", "score": 0.5, "source_type": "domain_corpus",
                    "source": {"document_id": f"dc{i}", "source_type": "domain_corpus"}}
                   for i in range(1, 4)]
    corpus_vector = [{"chunk_id": f"c{i}", "score": 0.8, "source_type": "domain_corpus",
                      "source": {"document_id": f"dc{i}", "source_type": "domain_corpus"}}
                     for i in range(1, 4)]
    return [
        FusionLeg(MODALITY_TEXT, private_text, weight=lexical_weight, origin="private"),
        FusionLeg(MODALITY_VECTOR, private_vector, weight=1.0, origin="private"),
        FusionLeg(MODALITY_TEXT, corpus_text, weight=lexical_weight * corpus_weight,
                  origin="business_corpus"),
        FusionLeg(MODALITY_VECTOR, corpus_vector, weight=corpus_weight,
                  origin="business_corpus"),
    ]


def _scores(rows):
    return {r["chunk_id"]: r["fusion_score"] for r in rows}


def test_weight_one_reproduces_unweighted_fusion():
    """α = 1.0 : privé et corpus obtiennent exactement le même score à rang égal."""
    scores = _scores(merge_rrf(legs=_legs(1.0), top_k=50))
    for i in range(1, 4):
        assert scores[f"c{i}"] == pytest.approx(scores[f"w{i}"])


@pytest.mark.parametrize("alpha", [0.75, 0.5, 0.35, 0.25])
def test_lower_weight_reduces_only_the_corpus_contribution(alpha):
    """α < 1 : le corpus vote moins fort ; les scores privés sont INCHANGÉS."""
    reference = _scores(merge_rrf(legs=_legs(1.0), top_k=50))
    weighted = _scores(merge_rrf(legs=_legs(alpha), top_k=50))
    for i in range(1, 4):
        assert weighted[f"w{i}"] == pytest.approx(reference[f"w{i}"]), "rangs privés intacts"
        assert weighted[f"c{i}"] == pytest.approx(reference[f"c{i}"] * alpha)
        assert weighted[f"c{i}"] < weighted[f"w{i}"]


def test_weight_is_applied_to_the_vote_not_after_fusion():
    """La contribution vaut exactement ``α / (K + rang)``, terme à terme."""
    alpha = 0.5
    scores = _scores(merge_rrf(legs=_legs(alpha), top_k=50))
    k = _rrf_k()
    # `c1` est rang 1 dans les DEUX legs corpus : deux votes pondérés.
    assert scores["c1"] == pytest.approx(2 * alpha / (k + 1))
    assert scores["w1"] == pytest.approx(2 * 1.0 / (k + 1))


def test_every_candidate_keeps_a_fusion_score_and_no_quota_is_applied():
    rows = merge_rrf(legs=_legs(0.25), top_k=50)
    assert all(float(r.get("fusion_score") or 0.0) > 0.0 for r in rows)
    assert len(rows) == 6, "aucun candidat n'est retiré ni réservé par provenance"
    assert len({r["chunk_id"] for r in rows}) == 6, "déduplication inchangée"


def test_ranking_is_stable_at_equal_scores():
    """Deux exécutions identiques rendent le même ordre : le départage reste total."""
    first = [r["chunk_id"] for r in merge_rrf(legs=_legs(0.5), top_k=50)]
    second = [r["chunk_id"] for r in merge_rrf(legs=_legs(0.5), top_k=50)]
    assert first == second


# ===========================================================================
# Télémétrie — sélection vs budget de prompt
# ===========================================================================
def test_trace_exposes_weights_and_stage_counts():
    from apps.rag_core.services.query.trace import RetrievalTraceBuilder, CorpusLegRecorder

    builder = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    recorder = CorpusLegRecorder()
    recorder.note_scope(associated=1, allowed=1)
    recorder.note_requested()
    recorder.note_ready_documents(12)
    recorder.note_leg_counts(text=5, vector=4)
    recorder.note_candidate_count(7)
    builder.record_corpus_state(
        recorder, unified_fusion=True, corpus_leg_weight=0.35, workspace_leg_weight=1.0
    )

    corpus = [{"chunk_id": f"c{i}", "source_type": "domain_corpus",
               "source": {"document_id": "dc", "corpus_id": "co", "source_type": "domain_corpus"}}
              for i in range(3)]
    private = [{"chunk_id": f"w{i}", "source_type": "workspace_document",
                "source": {"document_id": f"dw{i}", "source_type": "workspace_document"}}
               for i in range(4)]
    pool = private + corpus
    selected = private[:2] + corpus[:2]      # après diversification/rerank
    final = private[:1] + corpus[:1]         # après budget de prompt

    builder.record_bi_source_selection(
        pool=pool, pre_rerank=selected, post_rerank=selected, final=final,
        diversity_applied=True, rerank_applied=False,
    )
    builder.record_prompt_budget(before=selected, after=final)
    payload = builder.build(final_candidate_count=len(pool), source_count=len(final)).to_dict()
    bc = payload["business_corpus"]

    assert bc["corpus_rrf_leg_weight"] == 0.35
    assert bc["workspace_rrf_leg_weight"] == 1.0
    assert bc["rrf_k"] > 0
    assert bc["stage_counts"]["after_reranking"] == {"workspace": 2, "corpus": 2}
    assert bc["stage_counts"]["after_prompt_budget"] == {"workspace": 1, "corpus": 1}
    # Une source coupée par le budget n'est PAS comptée comme citation finale.
    assert bc["prompt_budget"] == {"observed": True, "dropped_workspace": 1, "dropped_corpus": 1}
    assert bc["first_rank"]["workspace_after_rrf"] == 1
    assert bc["first_rank"]["corpus_after_rrf"] == 5
    assert bc["distinct_documents_final"] == 2
    assert bc["distinct_corpora_final"] == 1


def test_trace_stays_free_of_sensitive_payload():
    import json

    from apps.rag_core.services.query.trace import RetrievalTraceBuilder, CorpusLegRecorder

    builder = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    builder.record_corpus_state(CorpusLegRecorder(), unified_fusion=True, corpus_leg_weight=0.5)
    secret = "TEXTE_DOCUMENTAIRE_CONFIDENTIEL"
    rows = [{"chunk_id": "c1", "text": secret, "source_type": "domain_corpus",
             "source": {"document_id": "d", "filename": "dossier-client.pdf", "text": secret}}]
    builder.record_bi_source_selection(
        pool=rows, pre_rerank=rows, post_rerank=rows, final=rows,
        diversity_applied=False, rerank_applied=False,
    )
    builder.record_prompt_budget(before=rows, after=[])
    blob = json.dumps(builder.build(final_candidate_count=1, source_count=0).to_dict())
    assert secret not in blob
    assert "dossier-client.pdf" not in blob


def test_old_traces_remain_readable_without_the_new_fields():
    """Compatibilité : une trace sans budget observé reste sérialisable et lisible."""
    from apps.rag_core.services.query.trace import RetrievalTraceBuilder, CorpusLegRecorder

    builder = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    builder.record_corpus_state(CorpusLegRecorder(), unified_fusion=False)
    bc = builder.build(final_candidate_count=0, source_count=0).to_dict()["business_corpus"]
    assert bc["prompt_budget"]["observed"] is False
    assert bc["corpus_rrf_leg_weight"] == 1.0
    assert bc["corpus_drop_stage"] == "not_associated"

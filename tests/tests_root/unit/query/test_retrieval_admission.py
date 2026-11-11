"""Lot 4 — politique d'admission du retrieval : cas aux bornes et par mode.

Le module de décision est PUR : ces tests couvrent donc exhaustivement les bornes
(seuil atteint, seuil frôlé, marge exacte), ce qu'un test d'intégration ne pourrait
pas faire sans fabriquer un corpus par cas.

L'invariant le plus important est vérifié par fuzzing : `legacy` doit reproduire
`should_answer` À L'IDENTIQUE. C'est ce qui fait du rollback un vrai retour arrière
et non une seconde implémentation « équivalente ».
"""
from __future__ import annotations

import random

import pytest

from apps.rag_core.domain.policies import RagPolicy, should_answer
from apps.rag_core.domain.retrieval_admission import (
    ACCEPT,
    POLICY_LEGACY,
    POLICY_SCORE_AND_MARGIN,
    POLICY_SCORE_ONLY,
    POLICY_TIERED,
    REASON_FOREIGN_PIPELINE,
    REASON_INSUFFICIENT_SOURCES,
    REASON_NO_RESULT,
    REASON_TEXT_ONLY_UNCORROBORATED,
    REASON_VECTOR_NO_SEPARATION,
    REJECT,
    TEXT_POLICY_COMBINED,
    TEXT_POLICY_DISABLED,
    TEXT_POLICY_INDEPENDENT,
    UNCERTAIN,
    AdmissionThresholds,
    RetrievalEvidence,
    build_evidence,
    decide,
)


def ev(**kw) -> RetrievalEvidence:
    kw.setdefault("source_count", max(1, kw.get("source_count", 1)))
    return RetrievalEvidence(**kw)


def th(**kw) -> AdmissionThresholds:
    kw.setdefault("policy", POLICY_SCORE_ONLY)
    kw.setdefault("min_score", 0.80)
    return AdmissionThresholds(**kw)


# =========================================================================
# Validation de la configuration
# =========================================================================
class TestThresholdValidation:
    def test_unknown_policy_is_refused(self):
        with pytest.raises(ValueError, match="RAG_ADMISSION_POLICY"):
            AdmissionThresholds(policy="bogus")

    def test_unknown_text_policy_is_refused(self):
        with pytest.raises(ValueError, match="RAG_TEXT_ADMISSION_POLICY"):
            AdmissionThresholds(text_policy="bogus")

    @pytest.mark.parametrize("field,value", [
        ("min_score", -0.1), ("min_score", 1.1),
        ("min_margin", -0.01), ("min_margin", 2.0),
        ("uncertain_score", 1.5),
    ])
    def test_out_of_bounds_is_refused(self, field, value):
        with pytest.raises(ValueError):
            AdmissionThresholds(**{field: value})

    def test_min_sources_must_be_positive(self):
        with pytest.raises(ValueError, match="RAG_MIN_SOURCES"):
            AdmissionThresholds(min_sources=0)

    def test_uncertain_above_min_score_is_refused(self):
        """Une zone d'incertitude au-dessus du seuil serait logiquement vide."""
        with pytest.raises(ValueError, match="RAG_UNCERTAIN_SCORE"):
            AdmissionThresholds(policy=POLICY_TIERED, min_score=0.70, uncertain_score=0.90)

    def test_uncertain_equal_to_min_score_is_accepted(self):
        AdmissionThresholds(policy=POLICY_TIERED, min_score=0.70, uncertain_score=0.70)

    def test_nan_is_refused(self):
        with pytest.raises(ValueError):
            AdmissionThresholds(min_score=float("nan"))


# =========================================================================
# Gardes structurelles — s'appliquent à TOUTES les politiques
# =========================================================================
class TestStructuralGuards:
    @pytest.mark.parametrize("policy", sorted(
        {POLICY_LEGACY, POLICY_SCORE_ONLY, POLICY_SCORE_AND_MARGIN, POLICY_TIERED}))
    def test_no_result_is_rejected(self, policy):
        d = decide(RetrievalEvidence(source_count=0), th(policy=policy))
        assert d.status == REJECT and d.reason == REASON_NO_RESULT

    @pytest.mark.parametrize("policy", sorted(
        {POLICY_LEGACY, POLICY_SCORE_ONLY, POLICY_SCORE_AND_MARGIN, POLICY_TIERED}))
    def test_insufficient_sources_is_rejected(self, policy):
        d = decide(ev(source_count=1, top_vector_score=0.99),
                   th(policy=policy, min_sources=3))
        assert d.status == REJECT and d.reason == REASON_INSUFFICIENT_SOURCES

    @pytest.mark.parametrize("policy", sorted(
        {POLICY_LEGACY, POLICY_SCORE_ONLY, POLICY_SCORE_AND_MARGIN, POLICY_TIERED}))
    def test_foreign_pipeline_is_rejected_whatever_the_score(self, policy):
        """Un vecteur d'un autre modèle n'est pas comparable, même excellent."""
        d = decide(ev(top_vector_score=0.99, pipeline_family="autre@ffffffff",
                      expected_pipeline_family="fr-en-retrieval@1a37926b"),
                   th(policy=policy))
        assert d.status == REJECT and d.reason == REASON_FOREIGN_PIPELINE

    def test_space_without_expected_family_is_not_judged(self):
        """Un espace historique ne doit pas être refusé faute d'identité."""
        d = decide(ev(top_vector_score=0.99, pipeline_family=""), th(min_score=0.5))
        assert d.status == ACCEPT


# =========================================================================
# Bornes de score et de marge
# =========================================================================
class TestBoundaries:
    def test_score_exactly_at_threshold_is_accepted(self):
        assert decide(ev(top_vector_score=0.80), th(min_score=0.80)).status == ACCEPT

    def test_score_just_below_threshold_is_rejected(self):
        assert decide(ev(top_vector_score=0.7999), th(min_score=0.80)).status == REJECT

    def test_margin_exactly_at_threshold_is_accepted(self):
        d = decide(ev(top_vector_score=0.90, second_vector_score=0.85),
                   th(policy=POLICY_SCORE_AND_MARGIN, min_score=0.80, min_margin=0.05))
        assert d.status == ACCEPT

    def test_margin_just_below_threshold_is_rejected(self):
        d = decide(ev(top_vector_score=0.90, second_vector_score=0.8501),
                   th(policy=POLICY_SCORE_AND_MARGIN, min_score=0.80, min_margin=0.05))
        assert d.status == REJECT and d.reason == REASON_VECTOR_NO_SEPARATION

    def test_uncertain_score_exactly_reached_is_uncertain(self):
        d = decide(ev(top_vector_score=0.70),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.70))
        assert d.status == UNCERTAIN

    def test_just_below_uncertain_score_is_rejected(self):
        d = decide(ev(top_vector_score=0.6999),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.70))
        assert d.status == REJECT

    def test_single_result_has_full_margin(self):
        """Sans concurrent, la séparation est totale — pas nulle.

        Rendre 0 ferait basculer en UNCERTAIN tout corpus ne contenant qu'un seul
        passage pertinent : un refus dû à la forme du corpus, pas à la réponse.
        """
        e = ev(top_vector_score=0.90, second_vector_score=0.0)
        assert e.vector_margin == pytest.approx(0.90)
        d = decide(e, th(policy=POLICY_SCORE_AND_MARGIN, min_score=0.80, min_margin=0.05))
        assert d.status == ACCEPT

    def test_identical_scores_give_zero_margin(self):
        e = ev(top_vector_score=0.90, second_vector_score=0.90)
        assert e.vector_margin == 0.0


# =========================================================================
# Modes de retrieval
# =========================================================================
class TestModes:
    def test_vector_accept(self):
        d = decide(ev(top_vector_score=0.90), th(policy=POLICY_TIERED, min_score=0.80,
                                                 uncertain_score=0.60))
        assert d.status == ACCEPT

    def test_vector_uncertain(self):
        d = decide(ev(top_vector_score=0.70), th(policy=POLICY_TIERED, min_score=0.80,
                                                 uncertain_score=0.60))
        assert d.status == UNCERTAIN

    def test_vector_reject(self):
        d = decide(ev(top_vector_score=0.40), th(policy=POLICY_TIERED, min_score=0.80,
                                                 uncertain_score=0.60))
        assert d.status == REJECT

    def test_text_accept_when_independent(self):
        d = decide(ev(top_text_rank=0.05),
                   th(policy=POLICY_TIERED, min_text_rank=0.02,
                      text_policy=TEXT_POLICY_INDEPENDENT))
        assert d.status == ACCEPT

    def test_text_alone_is_uncertain_when_combined(self):
        """Le point central du lot : un ts_rank élevé signale des MOTS, pas du sens."""
        d = decide(ev(top_text_rank=0.05),
                   th(policy=POLICY_TIERED, min_text_rank=0.02,
                      text_policy=TEXT_POLICY_COMBINED))
        assert d.status == UNCERTAIN and d.reason == REASON_TEXT_ONLY_UNCORROBORATED

    def test_text_too_weak_is_rejected(self):
        d = decide(ev(top_text_rank=0.001),
                   th(policy=POLICY_TIERED, min_text_rank=0.02))
        assert d.status == REJECT

    def test_hybrid_confirmed(self):
        d = decide(ev(top_vector_score=0.90, top_text_rank=0.05),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.60,
                      min_text_rank=0.02, text_policy=TEXT_POLICY_COMBINED))
        assert d.status == ACCEPT

    def test_hybrid_conflict_is_uncertain(self):
        """Le vectoriel affirme, le lexical ne corrobore pas."""
        d = decide(ev(top_vector_score=0.90, top_text_rank=0.001),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.60,
                      min_text_rank=0.02, text_policy=TEXT_POLICY_COMBINED))
        assert d.status == UNCERTAIN

    def test_hybrid_weak_is_rejected(self):
        d = decide(ev(top_vector_score=0.30, top_text_rank=0.001),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.60,
                      min_text_rank=0.02, text_policy=TEXT_POLICY_COMBINED))
        assert d.status == REJECT

    def test_text_disabled_ignores_lexical_signal(self):
        d = decide(ev(top_text_rank=0.9),
                   th(policy=POLICY_TIERED, min_text_rank=0.02,
                      text_policy=TEXT_POLICY_DISABLED))
        assert d.status == REJECT

    def test_score_only_does_not_let_fts_bypass_when_combined(self):
        """La correction du contournement, sur `score_only`."""
        d = decide(ev(top_vector_score=0.50, top_text_rank=0.9),
                   th(policy=POLICY_SCORE_ONLY, min_score=0.80, min_text_rank=0.02,
                      text_policy=TEXT_POLICY_COMBINED))
        assert d.status == REJECT

    def test_score_only_lets_fts_bypass_when_independent(self):
        d = decide(ev(top_vector_score=0.50, top_text_rank=0.9),
                   th(policy=POLICY_SCORE_ONLY, min_score=0.80, min_text_rank=0.02,
                      text_policy=TEXT_POLICY_INDEPENDENT))
        assert d.status == ACCEPT


# =========================================================================
# UNCERTAIN n'est jamais une acceptation
# =========================================================================
class TestUncertainSemantics:
    def test_uncertain_is_not_accept(self):
        d = decide(ev(top_vector_score=0.70),
                   th(policy=POLICY_TIERED, min_score=0.80, uncertain_score=0.60))
        assert d.status == UNCERTAIN
        assert d.is_accept is False
        assert d.is_answerable is True

    def test_reject_is_neither(self):
        d = decide(ev(top_vector_score=0.10), th(min_score=0.80))
        assert d.is_accept is False and d.is_answerable is False


# =========================================================================
# Scores invalides
# =========================================================================
class TestInvalidScores:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", None])
    def test_invalid_score_never_admits(self, bad):
        """Un NaN propagé rendrait les comparaisons fausses SANS lever."""
        e = build_evidence([{"vector_score": bad, "score": bad}])
        d = decide(e, th(min_score=0.80))
        assert d.status == REJECT


# =========================================================================
# build_evidence
# =========================================================================
class TestBuildEvidence:
    def test_vector_scores_are_sorted_not_taken_in_order(self):
        """Après fusion RRF, la liste est ordonnée par fusion_score, pas par vectoriel.

        Prendre `chunks[1]` donnerait une marge fausse — pire qu'une marge absente,
        parce qu'elle déciderait quand même.
        """
        e = build_evidence([
            {"vector_score": 0.50, "chunk_id": "a"},
            {"vector_score": 0.95, "chunk_id": "b"},
            {"vector_score": 0.80, "chunk_id": "c"},
        ])
        assert e.top_vector_score == pytest.approx(0.95)
        assert e.second_vector_score == pytest.approx(0.80)
        assert e.vector_margin == pytest.approx(0.15)

    def test_mode_detection(self):
        assert build_evidence([]).retrieval_mode == "empty"
        assert build_evidence([{"vector_score": 0.5}]).retrieval_mode == "vector"
        assert build_evidence([{"text_score": 0.05}]).retrieval_mode == "text"
        assert build_evidence(
            [{"vector_score": 0.5}, {"text_score": 0.05}]).retrieval_mode == "hybrid"

    def test_mixed_families_are_reported_and_refused(self):
        e = build_evidence(
            [{"vector_score": 0.9, "pipeline_family": "a@1"},
             {"vector_score": 0.8, "pipeline_family": "b@2"}],
            expected_pipeline_family="a@1")
        assert "|" in e.pipeline_family
        assert e.pipeline_is_consistent is False

    def test_distinct_documents_are_counted(self):
        e = build_evidence([
            {"vector_score": 0.9, "source": {"document_id": "d1"}},
            {"vector_score": 0.8, "source": {"document_id": "d1"}},
            {"vector_score": 0.7, "source": {"document_id": "d2"}},
        ])
        assert e.distinct_document_count == 2

    def test_log_fields_carry_no_content(self):
        e = build_evidence([{"vector_score": 0.9, "text": "contenu confidentiel",
                             "chunk_id": "c1"}])
        blob = str(e.to_log_fields())
        assert "confidentiel" not in blob and "c1" not in blob


# =========================================================================
# INVARIANT DE ROLLBACK : legacy == should_answer
# =========================================================================
class TestLegacyEquivalence:
    def test_legacy_matches_should_answer_on_random_inputs(self):
        """3 000 tirages : aucune divergence tolérée.

        C'est ce qui fait du rollback un vrai retour arrière plutôt qu'une seconde
        implémentation « équivalente » qui divergerait au premier cas limite.
        """
        rnd = random.Random(20260801)
        policy = RagPolicy(min_sources=1, min_score=0.76, min_text_rank=0.02)
        thresholds = AdmissionThresholds(policy=POLICY_LEGACY, min_sources=1,
                                         min_score=0.76, min_text_rank=0.02)
        divergences = []
        for i in range(3000):
            chunks = []
            for _ in range(rnd.randint(0, 4)):
                c: dict = {}
                if rnd.random() < 0.6:
                    c["text_score"] = round(rnd.uniform(0.0, 0.2), 4)
                if rnd.random() < 0.7:
                    c["vector_score"] = round(rnd.uniform(0.4, 1.0), 4)
                c["score"] = c.get("vector_score", c.get("text_score", 0.0))
                chunks.append(c)
            legacy = should_answer(chunks, policy)
            new = decide(build_evidence(chunks), thresholds).is_accept
            if legacy != new:
                divergences.append((i, chunks))
        assert not divergences, f"{len(divergences)} divergences, ex. {divergences[:2]}"

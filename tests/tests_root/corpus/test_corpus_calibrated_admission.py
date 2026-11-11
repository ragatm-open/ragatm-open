"""Porte d'admission calibrée des corpus métier — spécification exécutable.

Ce que ce fichier verrouille, dans l'ordre où le lot le construit :

1. **la configuration** : porte OFF par défaut, seuil validé strictement, aucun repli ;
2. **le signal** : la moyenne des dix meilleures similarités du leg vectoriel corpus,
   définie indépendamment de l'ordre reçu, et son indisponibilité ;
3. **la calibration** : la formule terme à terme, et le REFUS de toute calibration dont
   l'identité ne décrit plus l'espace vectoriel interrogé ;
4. **l'admission** : tout-ou-rien sur les DEUX legs, jamais de porte ouverte silencieuse,
   privé strictement inchangé ;
5. **l'observabilité** : la décision, ses entrées et son effet — sans contenu.

Le point le plus important est le 3. Un calibrateur entraîné sur une échelle de
similarités qui n'existe plus rendrait une probabilité **crédible et fausse** — le même
piège que la cascade de modèles d'embeddings globale, sous une autre forme.
"""
from __future__ import annotations

import math

import pytest
from django.test import override_settings

from apps.rag_core.domain.corpus_admission import (
    CALIBRATOR_LOGISTIC,
    ERROR_CALIBRATION_INVALID,
    ERROR_CALIBRATION_MISSING,
    ERROR_SIGNAL_UNAVAILABLE,
    REASON_CALIBRATION_INVALID,
    REASON_CALIBRATION_MISSING,
    REASON_FEATURE_DISABLED,
    REASON_NO_VECTOR_CANDIDATES,
    REASON_PROBABILITY_ABOVE_THRESHOLD,
    REASON_PROBABILITY_BELOW_THRESHOLD,
    REASON_SIGNAL_UNAVAILABLE,
    REASON_VECTOR_LEG_ERROR,
    SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
    SIGNAL_TOP_N,
    CorpusAdmissionCalibrationError,
    LogisticAdmissionCalibration,
    admission_signal_value,
    decide_admission,
)
from apps.rag_core.services.query.candidate_retriever import (
    calibrated_admission_enabled,
    corpus_admission_probability_threshold,
)
from apps.rag_core.services.query.contracts import RetrievalPolicy
from apps.rag_core.services.query.errors import RetrievalConfigurationError

PROVIDER = "ai_gateway"
MODEL = "legacy-768"
DIMENSION = 768


def _policy(**kwargs) -> RetrievalPolicy:
    base = dict(
        fts_required=True, hybrid_enabled=True, vector_available=True,
        temporal_enabled=False, rerank_enabled=False, diversity_enabled=True,
        min_sources=1, min_text_rank=0.0, min_score=0.5,
        top_k=8, vector_top_k=8, candidate_k=24, lexical_weight=1.0,
    )
    base.update(kwargs)
    return RetrievalPolicy(**base)


def _payload(**overrides) -> dict:
    payload = {
        "version": 1,
        "calibration_version": 3,
        "signal": SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
        "calibrator_type": CALIBRATOR_LOGISTIC,
        "embedding_provider": PROVIDER,
        "embedding_model": MODEL,
        "embedding_dimension": DIMENSION,
        "coefficients": {"intercept": -30.0, "top10_mean": 46.5},
    }
    payload.update(overrides)
    return payload


def _calibration(corpus_id="corpus-1", **overrides) -> LogisticAdmissionCalibration:
    return LogisticAdmissionCalibration.from_payload(_payload(**overrides), corpus_id=corpus_id)


def _candidates(scores, corpus_id="corpus-1"):
    return [
        {
            "chunk_id": f"c{i}", "score": s, "source_type": "domain_corpus",
            "source": {"source_type": "domain_corpus", "corpus_id": corpus_id,
                       "document_id": f"d{i}"},
        }
        for i, s in enumerate(scores)
    ]


def _decide(*, candidates=None, calibration=None, threshold=0.65, healthy=True, enabled=True):
    return decide_admission(
        enabled=enabled,
        threshold=threshold,
        vector_candidates=_candidates(candidates) if candidates is not None else [],
        vector_leg_healthy=healthy,
        calibration=calibration,
        corpus_id="corpus-1",
        embedding_provider=PROVIDER,
        embedding_model=MODEL,
        embedding_dimension=DIMENSION,
    )


# ===========================================================================
# 1-4. Configuration — validée strictement, jamais rattrapée
# ===========================================================================
def test_gate_is_off_by_default():
    """Livrer ce lot ne change rien : la porte est fermée à clé, pas ouverte."""
    assert calibrated_admission_enabled() is False
    assert _policy().calibrated_corpus_admission is False


def test_default_threshold_is_the_measured_one():
    assert corpus_admission_probability_threshold() == 0.65
    assert _policy().corpus_admission_probability_threshold == 0.65


@pytest.mark.parametrize("value", [0.0, 0.35, 0.5, 0.65, 0.9, 1.0])
def test_threshold_inside_the_unit_interval_is_accepted(value):
    assert _policy(corpus_admission_probability_threshold=value).corpus_admission_probability_threshold == value


@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0, -1.0])
def test_threshold_outside_the_unit_interval_is_refused(value):
    """Un seuil hors [0, 1] n'est pas une probabilité : il ne peut pas être « corrigé »."""
    with pytest.raises(RetrievalConfigurationError, match="corpus_admission_probability_threshold"):
        _policy(corpus_admission_probability_threshold=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_threshold_is_refused(value):
    with pytest.raises(RetrievalConfigurationError, match="corpus_admission_probability_threshold"):
        _policy(corpus_admission_probability_threshold=value)


@override_settings(RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD="pas-un-nombre")
def test_unreadable_threshold_becomes_a_refused_value_not_a_fallback():
    """Illisible ≠ défaut. Une valeur cassée doit faire échouer, pas filtrer au hasard."""
    raw = corpus_admission_probability_threshold()
    assert math.isnan(raw)
    with pytest.raises(RetrievalConfigurationError):
        _policy(corpus_admission_probability_threshold=raw)


@pytest.mark.parametrize("calibrator", ["isotonic", "quantile", "", "logistic-v2"])
def test_unknown_calibrator_is_refused(calibrator):
    """Seul le logistique est implémenté : un type inconnu ne doit pas être interprété."""
    with pytest.raises(CorpusAdmissionCalibrationError) as exc:
        _calibration(calibrator_type=calibrator)
    assert exc.value.code == ERROR_CALIBRATION_INVALID


# ===========================================================================
# 5-8. Le signal
# ===========================================================================
def test_signal_averages_the_ten_best_scores_whatever_the_input_order():
    """Défini comme « la moyenne des dix meilleures », donc indépendant de l'ordre reçu.

    Le leg arrive trié par le SQL, mais traverse `merge_candidates` (fusion multi-passes
    par chunk_id) qui ne garantit rien. Supposer l'ordre serait supposer une propriété
    que le pipeline ne promet pas.
    """
    best_ten = [0.90, 0.89, 0.88, 0.87, 0.86, 0.85, 0.84, 0.83, 0.82, 0.81]
    noise = [0.10, 0.20, 0.30]
    expected = sum(best_ten) / SIGNAL_TOP_N

    ordered = admission_signal_value(_candidates(best_ten + noise))
    shuffled = admission_signal_value(_candidates(noise + list(reversed(best_ten))))
    assert ordered == pytest.approx(expected)
    assert shuffled == pytest.approx(expected)


def test_signal_uses_at_most_ten_candidates():
    """Un onzième candidat, même excellent, ne change pas le signal."""
    ten = [0.9] * SIGNAL_TOP_N
    assert admission_signal_value(_candidates(ten)) == pytest.approx(0.9)
    assert admission_signal_value(_candidates(ten + [0.05])) == pytest.approx(0.9)


@pytest.mark.parametrize("count", [1, 3, 9])
def test_fewer_than_ten_candidates_are_averaged_as_they_are(count):
    """Le signal reste défini, simplement estimé sur moins d'observations."""
    scores = [0.7] * count
    assert admission_signal_value(_candidates(scores)) == pytest.approx(0.7)


def test_no_candidate_makes_the_signal_undefined():
    assert admission_signal_value([]) is None
    assert admission_signal_value(None) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "abc"])
def test_non_finite_score_is_refused_rather_than_averaged(bad):
    """Une moyenne contaminée produirait une probabilité absurde, mais plausible."""
    with pytest.raises(CorpusAdmissionCalibrationError) as exc:
        admission_signal_value(_candidates([0.9, bad, 0.8]))
    assert exc.value.code == ERROR_SIGNAL_UNAVAILABLE


def test_numeric_string_score_is_coerced_like_everywhere_else_in_the_pipeline():
    """`merge` et les boosts convertissent déjà `score` : diverger ici serait un piège."""
    assert admission_signal_value(_candidates(["0.8", 0.6])) == pytest.approx(0.7)


# ===========================================================================
# 9-15. La calibration
# ===========================================================================
def test_logistic_formula_is_verified_term_by_term():
    """``p = sigmoid(intercept + coefficient × signal)`` — sans raccourci ni magie."""
    calibration = _calibration(coefficients={"intercept": -3.0, "top10_mean": 5.0})
    for signal in (0.0, 0.2, 0.6, 0.645, 1.0):
        z = -3.0 + 5.0 * signal
        assert calibration.probability(signal) == pytest.approx(1 / (1 + math.exp(-z)))


def test_probability_is_monotonic_and_bounded():
    calibration = _calibration()
    values = [calibration.probability(x) for x in (0.4, 0.5, 0.6, 0.7, 0.8)]
    assert values == sorted(values)
    assert all(0.0 <= v <= 1.0 for v in values)
    # Bornage numérique : aucun débordement de `exp` sur des entrées extrêmes.
    assert calibration.probability(1e6) == pytest.approx(1.0)
    assert calibration.probability(-1e6) == pytest.approx(0.0)


def test_probability_exactly_at_threshold_admits():
    """Choix EXPLICITE : ``p >= seuil`` admet. L'égalité ne doit pas dépendre du hasard."""
    # sigmoid(0) = 0.5 exactement : le signal 0.5 place p pile au seuil.
    calibration = _calibration(coefficients={"intercept": -0.5, "top10_mean": 1.0})
    assert calibration.probability(0.5) == pytest.approx(0.5)
    decision = _decide(candidates=[0.5], calibration=calibration, threshold=0.5)
    assert decision.admitted is True
    assert decision.reason == REASON_PROBABILITY_ABOVE_THRESHOLD


def test_calibration_bound_to_another_corpus_is_refused():
    """Une calibration est une propriété du corpus, pas un réglage transposable."""
    calibration = _calibration(corpus_id="corpus-AUTRE")
    decision = _decide(candidates=[0.8], calibration=calibration)
    assert decision.admitted is False
    assert decision.reason == REASON_CALIBRATION_INVALID
    assert decision.error_code == ERROR_CALIBRATION_INVALID


@pytest.mark.parametrize(
    "override, label",
    [
        ({"embedding_provider": "ollama"}, "provider"),
        ({"embedding_model": "fr-en-retrieval"}, "modèle"),
        ({"embedding_dimension": 1536}, "dimension"),
    ],
)
def test_calibration_measured_in_another_vector_space_is_refused(override, label):
    """Le cœur du lot.

    L'échelle des similarités change avec le modèle d'embeddings : la calibration mesurée
    ailleurs rendrait une probabilité crédible et fausse. Elle est donc refusée AVANT
    tout calcul, comme l'est un corpus sans modèle explicite.
    """
    decision = _decide(candidates=[0.8], calibration=_calibration(**override))
    assert decision.admitted is False, label
    assert decision.error_code == ERROR_CALIBRATION_INVALID


@pytest.mark.parametrize("signal", ["corpus_fts_top10_mean", "corpus_vector_top1", ""])
def test_calibration_trained_on_another_signal_is_refused(signal):
    with pytest.raises(CorpusAdmissionCalibrationError) as exc:
        _calibration(signal=signal)
    assert exc.value.code == ERROR_CALIBRATION_INVALID


@pytest.mark.parametrize("version", [0, 2, 99, None])
def test_unknown_calibration_format_version_is_refused(version):
    with pytest.raises(CorpusAdmissionCalibrationError):
        _calibration(version=version)


@pytest.mark.parametrize(
    "coefficients",
    [
        {},
        {"intercept": -30.0},
        {"top10_mean": 46.5},
        {"intercept": float("nan"), "top10_mean": 46.5},
        {"intercept": -30.0, "top10_mean": float("inf")},
        {"intercept": None, "top10_mean": 46.5},
    ],
)
def test_absent_or_non_finite_coefficients_are_refused(coefficients):
    with pytest.raises(CorpusAdmissionCalibrationError) as exc:
        _calibration(coefficients=coefficients)
    assert exc.value.code == ERROR_CALIBRATION_INVALID


def test_calibration_error_message_carries_no_secret_or_content():
    with pytest.raises(CorpusAdmissionCalibrationError) as exc:
        _calibration(coefficients={})
    message = str(exc.value)
    assert ERROR_CALIBRATION_INVALID in message
    assert "sk-" not in message and "Bearer" not in message


# ===========================================================================
# 16-20. La décision
# ===========================================================================
def test_probability_above_threshold_admits():
    decision = _decide(candidates=[0.75] * 10, calibration=_calibration(), threshold=0.65)
    assert decision.admitted is True
    assert decision.reason == REASON_PROBABILITY_ABOVE_THRESHOLD
    assert decision.signal_name == SIGNAL_CORPUS_VECTOR_TOP10_MEAN
    assert decision.signal_value == pytest.approx(0.75)
    assert decision.calibration_version == 3


def test_probability_below_threshold_rejects():
    decision = _decide(candidates=[0.58] * 10, calibration=_calibration(), threshold=0.65)
    assert decision.admitted is False
    assert decision.reason == REASON_PROBABILITY_BELOW_THRESHOLD
    assert decision.error_code is None, "un rejet nominal n'est pas une erreur"


def test_vector_leg_in_error_rejects_the_corpus():
    """Le signal repose sur le leg vectoriel : sans lui, aucune décision n'est possible."""
    decision = _decide(candidates=[0.9] * 10, calibration=_calibration(), healthy=False)
    assert decision.admitted is False
    assert decision.reason == REASON_VECTOR_LEG_ERROR
    assert decision.error_code == ERROR_SIGNAL_UNAVAILABLE


def test_no_vector_candidate_rejects_the_corpus():
    decision = _decide(candidates=[], calibration=_calibration())
    assert decision.admitted is False
    assert decision.reason == REASON_NO_VECTOR_CANDIDATES


def test_missing_calibration_rejects_explicitly():
    """Jamais de porte ouverte silencieuse : non calibré ≠ réputé pertinent."""
    decision = _decide(candidates=[0.95] * 10, calibration=None)
    assert decision.admitted is False
    assert decision.reason == REASON_CALIBRATION_MISSING
    assert decision.error_code == ERROR_CALIBRATION_MISSING


def test_non_finite_signal_rejects_without_computing_a_probability():
    decision = _decide(candidates=[0.9, float("nan")], calibration=_calibration())
    assert decision.admitted is False
    assert decision.reason == REASON_SIGNAL_UNAVAILABLE
    assert decision.probability is None


def test_disabled_gate_admits_without_deciding_anything():
    decision = _decide(candidates=[], calibration=None, enabled=False)
    assert decision.admitted is True
    assert decision.reason == REASON_FEATURE_DISABLED
    assert decision.probability is None and decision.signal_value is None

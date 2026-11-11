"""Porte d'admission calibrée — comportement de bout en bout dans `query_rag`.

Le fichier unitaire voisin (`test_corpus_calibrated_admission.py`) verrouille la règle.
Celui-ci vérifie ce que le PIPELINE en fait : quelles sources finissent dans le prompt,
ce que la trace en dit, et surtout que le flag à 0 reproduit l'existant à l'identique.

Jeu de données synthétique, marqueurs disjoints : un corpus métier qui répond à la
question, et un document privé qui n'en parle pas.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.corpus.models import (
    BusinessDomain,
    DomainCorpus,
    DomainCorpusAdmissionCalibration,
    WorkspaceDomainCorpusAccess,
)
from apps.corpus.services.ingest import ingest_corpus_document_from_upload
from apps.rag_core.domain.corpus_admission import (
    CALIBRATOR_LOGISTIC,
    ERROR_CALIBRATION_MISSING,
    REASON_CALIBRATION_MISSING,
    REASON_FEATURE_DISABLED,
    REASON_PROBABILITY_ABOVE_THRESHOLD,
    REASON_PROBABILITY_BELOW_THRESHOLD,
    REASON_VECTOR_LEG_ERROR,
    SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
)
from apps.rag_core.models import RagSpace
from apps.rag_core.services.query import query_rag
from apps.workspaces.models import Membership, Workspace

pytestmark = pytest.mark.django_db

CORPUS_MARKER = "BUSINESS_CORPUS_MARKER_9A42"
PRIVATE_MARKER = "PRIVATE_WORKSPACE_MARKER_7F31"
QUESTION = "venture capital equity participation"

CORPUS_TEXT = (
    f"{CORPUS_MARKER}\n"
    "Venture capital is a form of equity capital provided to unlisted, privately held "
    "companies with high growth potential. The venture capital investor takes an equity "
    "participation in the company capital rather than lending money. Venture capital "
    "funds invest capital in unlisted companies and take equity participations."
).encode("utf-8")

PRIVATE_TEXT = (
    f"{PRIVATE_MARKER} The internal note reviews the quarterly meeting minutes, the "
    "travel policy, the office relocation schedule and the annual training plan. It "
    "mentions capital once, in passing, without any definition."
)

#: Calibrateur CONSTANT (coefficient nul) : la probabilité ne dépend plus du signal
#: mesuré, donc le test porte sur la POLITIQUE et non sur la valeur qu'un provider
#: d'embeddings de test produirait par accident.
ALWAYS = {"intercept": 40.0, "top10_mean": 0.0}      # p ≈ 1
NEVER = {"intercept": -40.0, "top10_mean": 0.0}      # p ≈ 0

GATE_ON = override_settings(RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED=True)
GATE_OFF = override_settings(RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED=False)


@pytest.fixture
def setup(db, ingest_text, settings):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="admission", password="pw", email="admission@example.test"
    )
    workspace = Workspace.objects.create(name="admission-workspace")
    Membership.objects.create(workspace=workspace, user=user, role=Membership.ROLE_MEMBER)
    rag_space = RagSpace.objects.create(name="admission-space", workspace=workspace)
    ingest_text(rag_space, PRIVATE_TEXT, filename="internal-note.txt")

    domain = BusinessDomain.objects.create(slug="vc", name="Venture Capital")
    corpus = DomainCorpus.objects.create(
        domain=domain, slug="vc-reference", name="VC Reference",
        embedding_provider="ai_gateway", embedding_model="legacy-768",
    )
    ingest_corpus_document_from_upload(
        corpus=corpus, filename="vc.txt", content_type="text/plain", file_bytes=CORPUS_TEXT,
    )
    WorkspaceDomainCorpusAccess.objects.create(workspace=workspace, corpus=corpus, enabled=True)
    return {"user": user, "workspace": workspace, "rag_space": rag_space, "corpus": corpus}


def _calibrate(corpus, coefficients, *, dimension=None, provider=None, model=None, version=1):
    from django.conf import settings as dj_settings

    return DomainCorpusAdmissionCalibration.objects.create(
        corpus=corpus,
        version=version,
        is_active=True,
        signal=SIGNAL_CORPUS_VECTOR_TOP10_MEAN,
        calibrator_type=CALIBRATOR_LOGISTIC,
        embedding_provider=provider or corpus.embedding_provider,
        embedding_model=model or corpus.embedding_model,
        embedding_dimension=dimension or int(dj_settings.RAG_EMBEDDING_DIM),
        coefficients=coefficients,
        training={"sample_count": 70, "positive_count": 35, "negative_count": 35},
        validation={"stratified": {"roc_auc": 0.96}},
        dataset_hash="a" * 64,
    )


def _run(setup, question=QUESTION):
    return query_rag(
        rag_space_id=setup["rag_space"].id,
        rag_space=setup["rag_space"],
        user=setup["user"],
        question=question,
    )


def _admission(result) -> dict:
    trace = result.get("retrieval_trace") or {}
    return (trace.get("business_corpus") or {}).get("admission") or {}


def _corpus_trace(result) -> dict:
    return (result.get("retrieval_trace") or {}).get("business_corpus") or {}


def _source_types(result) -> list[str]:
    return [s.get("source_type") for s in (result.get("sources") or [])]


# ===========================================================================
# 21-22. Flag OFF — l'existant, à l'identique
# ===========================================================================
@GATE_OFF
def test_gate_off_reproduces_the_existing_behaviour(setup):
    """Sans calibration ET sans porte : le corpus contribue comme avant."""
    result = _run(setup)
    assert "domain_corpus" in _source_types(result)
    assert _admission(result)["calibrated_admission_enabled"] is False
    assert _admission(result)["corpus_admitted"] is True
    assert _admission(result)["corpus_admission_reason"] is None


@GATE_OFF
def test_gate_off_ignores_even_a_rejecting_calibration(setup):
    """Le rollback par le flag est réel : la calibration présente n'est même pas lue."""
    _calibrate(setup["corpus"], NEVER)
    result = _run(setup)
    assert "domain_corpus" in _source_types(result)
    assert _admission(result)["corpus_admitted"] is True


@GATE_ON
def test_rejected_corpus_leaves_the_private_sources_untouched(setup):
    """Un corpus rejeté ne doit rien retirer au privé : la porte ne touche qu'au corpus."""
    _calibrate(setup["corpus"], NEVER)
    with GATE_OFF:
        reference = _source_types(_run(setup))
    rejected = _source_types(_run(setup))

    assert "domain_corpus" not in rejected
    assert [t for t in reference if t != "domain_corpus"] == rejected


# ===========================================================================
# 16-20. La décision, dans le pipeline
# ===========================================================================
@GATE_ON
def test_admitted_corpus_contributes_both_legs(setup):
    _calibrate(setup["corpus"], ALWAYS)
    result = _run(setup)
    assert "domain_corpus" in _source_types(result)
    admission = _admission(result)
    assert admission["corpus_admitted"] is True
    assert admission["corpus_admission_reason"] == REASON_PROBABILITY_ABOVE_THRESHOLD
    assert admission["admission_probability"] == pytest.approx(1.0, abs=1e-6)
    assert admission["corpus_candidate_count_after_admission"] == (
        admission["corpus_candidate_count_before_admission"]
    )


@GATE_ON
def test_rejected_corpus_contributes_nothing_at_all(setup):
    """Tout-ou-rien : le leg lexical ne survit pas au rejet du corpus."""
    _calibrate(setup["corpus"], NEVER)
    result = _run(setup)
    assert "domain_corpus" not in _source_types(result)
    admission = _admission(result)
    assert admission["corpus_admitted"] is False
    assert admission["corpus_admission_reason"] == REASON_PROBABILITY_BELOW_THRESHOLD
    assert admission["corpus_candidate_count_before_admission"] > 0
    assert admission["corpus_candidate_count_after_admission"] == 0


@GATE_ON
def test_lexical_leg_alone_cannot_bypass_the_gate(setup, monkeypatch):
    """Leg vectoriel muet + leg lexical fourni : le corpus reste dehors.

    C'est le contournement le plus tentant, et le plus nuisible : `corpus_fts` a été
    mesuré ANTI-corrélé à la pertinence (ROC-AUC 0.42). Le laisser entrer seul rendrait
    la porte pire qu'inutile.
    """
    from apps.corpus.services import retrieval as corpus_retrieval

    monkeypatch.setattr(
        corpus_retrieval, "search_corpus_chunks_vector", lambda **kwargs: []
    )
    _calibrate(setup["corpus"], ALWAYS)
    result = _run(setup)
    assert "domain_corpus" not in _source_types(result)
    assert _admission(result)["corpus_admitted"] is False


@GATE_ON
def test_vector_leg_error_rejects_the_corpus_with_a_named_reason(setup, monkeypatch):
    from apps.corpus.services import retrieval as corpus_retrieval

    def boom(**kwargs):
        raise TimeoutError("provider indisponible")

    monkeypatch.setattr(corpus_retrieval, "search_corpus_chunks_vector", boom)
    _calibrate(setup["corpus"], ALWAYS)
    result = _run(setup)
    assert "domain_corpus" not in _source_types(result)
    assert _admission(result)["corpus_admission_reason"] == REASON_VECTOR_LEG_ERROR


@GATE_ON
def test_missing_calibration_rejects_and_names_the_error(setup):
    """Le cas d'un corpus jamais calibré : rejet EXPLICITE, code d'erreur exploitable."""
    result = _run(setup)
    assert "domain_corpus" not in _source_types(result)
    admission = _admission(result)
    assert admission["corpus_admitted"] is False
    assert admission["corpus_admission_reason"] == REASON_CALIBRATION_MISSING
    assert admission["corpus_admission_error_code"] == ERROR_CALIBRATION_MISSING


@GATE_ON
def test_inactive_calibration_is_not_used(setup):
    """Désactiver une calibration la retire du chemin de décision, sans la détruire."""
    row = _calibrate(setup["corpus"], ALWAYS)
    DomainCorpusAdmissionCalibration.objects.filter(id=row.id).update(is_active=False)
    result = _run(setup)
    assert _admission(result)["corpus_admission_reason"] == REASON_CALIBRATION_MISSING
    assert DomainCorpusAdmissionCalibration.objects.filter(id=row.id).exists()


@GATE_ON
def test_calibration_from_another_embedding_model_is_refused_at_runtime(setup):
    """La calibration ne suit pas un changement de modèle : elle est refusée."""
    _calibrate(setup["corpus"], ALWAYS, model="fr-en-retrieval")
    result = _run(setup)
    assert "domain_corpus" not in _source_types(result)
    assert _admission(result)["corpus_admission_error_code"] == (
        "corpus_admission_calibration_invalid"
    )


# ===========================================================================
# 23-25. Composition avec la fusion, et invariants de pool
# ===========================================================================
@override_settings(
    RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED=True,
    RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=True,
    RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT=0.5,
)
def test_weight_applies_only_to_an_admitted_corpus(setup):
    """α pondère un vote ; un corpus rejeté n'en a aucun. Les deux ne se compensent pas."""
    _calibrate(setup["corpus"], NEVER)
    rejected = _run(setup)
    assert "domain_corpus" not in _source_types(rejected)

    DomainCorpusAdmissionCalibration.objects.filter(corpus=setup["corpus"]).delete()
    _calibrate(setup["corpus"], ALWAYS, version=2)
    admitted = _run(setup)
    assert "domain_corpus" in _source_types(admitted)


@GATE_ON
def test_admission_reserves_no_place_and_creates_no_duplicate(setup):
    _calibrate(setup["corpus"], ALWAYS)
    result = _run(setup)
    sources = result.get("sources") or []
    import json as _json

    fingerprints = [_json.dumps(s, sort_keys=True, default=str) for s in sources]
    assert len(fingerprints) == len(set(fingerprints)), "aucun doublon introduit par la porte"
    assert len(sources) <= 8, "aucun quota implicite ne gonfle la sélection"


# ===========================================================================
# 26-30. Observabilité
# ===========================================================================
@GATE_ON
def test_trace_exposes_the_decision_its_inputs_and_its_effect(setup):
    _calibrate(setup["corpus"], ALWAYS, version=7)
    admission = _admission(_run(setup))
    for key in (
        "calibrated_admission_enabled", "admission_signal_name", "admission_signal_value",
        "admission_calibrator_type", "admission_calibration_version",
        "admission_probability", "admission_probability_threshold", "corpus_admitted",
        "corpus_admission_reason", "corpus_admission_error_code",
        "corpus_candidate_count_before_admission", "corpus_candidate_count_after_admission",
        "per_corpus",
    ):
        assert key in admission, key
    assert admission["admission_signal_name"] == SIGNAL_CORPUS_VECTOR_TOP10_MEAN
    assert admission["admission_calibrator_type"] == CALIBRATOR_LOGISTIC
    assert admission["admission_calibration_version"] == 7
    assert admission["admission_probability_threshold"] == 0.65


@GATE_ON
def test_trace_carries_no_question_no_content_and_no_coefficient(setup):
    import json

    _calibrate(setup["corpus"], ALWAYS)
    result = _run(setup)
    payload = json.dumps(_corpus_trace(result))
    assert QUESTION not in payload
    assert CORPUS_MARKER not in payload and PRIVATE_MARKER not in payload
    assert "vc.txt" not in payload
    # Les COEFFICIENTS ne sortent pas : la version identifie la calibration, et c'est
    # tout ce qu'il faut pour rejouer une décision. (Le NOM du signal, lui, est
    # légitimement présent — il ne dit rien de la calibration.)
    assert "intercept" not in payload
    assert str(ALWAYS["intercept"]) not in payload


@GATE_ON
def test_drop_stage_names_the_admission_rather_than_the_merge(setup):
    """Un rejet par décision ne doit pas se lire comme un défaut de classement."""
    _calibrate(setup["corpus"], NEVER)
    assert _corpus_trace(_run(setup))["corpus_drop_stage"] == "admission"


@GATE_ON
def test_summary_states_the_verdict(setup):
    _calibrate(setup["corpus"], NEVER)
    result = _run(setup)
    summary = ((result.get("retrieval_trace_summary") or {}).get("business_corpus")) or {}
    if summary:  # le résumé n'est pas exposé par tous les points d'entrée
        assert summary["corpus_admitted"] is False
        assert summary["corpus_admission_reason"] == REASON_PROBABILITY_BELOW_THRESHOLD


def test_older_traces_without_admission_remain_readable():
    """Compatibilité ascendante : une trace antérieure au lot reste lisible et cohérente."""
    from apps.rag_core.services.query.trace import CorpusTrace

    legacy = CorpusTrace(
        associated_corpus_count=1, active_corpus_count=1, ready_document_count=3,
        candidate_count=5,
    )
    payload = legacy.to_dict()
    assert payload["admission"]["calibrated_admission_enabled"] is False
    assert payload["admission"]["corpus_admitted"] is True
    assert payload["admission"]["corpus_admission_reason"] is None
    assert payload["corpus_drop_stage"] != "admission"
    assert legacy.summary()["corpus_admitted"] is True


@GATE_OFF
def test_disabled_gate_reports_itself_as_disabled_not_as_an_admission(setup):
    result = _run(setup)
    admission = _admission(result)
    assert admission["calibrated_admission_enabled"] is False
    assert admission["admission_probability"] is None
    assert admission["admission_probability_threshold"] is None
    assert admission["per_corpus"] == []
    # La raison `feature_disabled` existe côté domaine ; la trace, elle, n'invente
    # aucune décision qui n'a pas été prise.
    assert REASON_FEATURE_DISABLED not in str(admission["corpus_admission_reason"])

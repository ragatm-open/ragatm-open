"""Tests unitaires de la trace structurée du retrieval (Lot P1-A0).

Purs : aucune base, aucun réseau. Vérifient le contrat de la structure, le calcul
central de ``degraded`` / ``outcome``, la sérialisation et l'absence de données
sensibles.
"""
import pytest

from apps.rag_core.services.query.trace import (
    RETRIEVAL_TRACE_SCHEMA_VERSION,
    CorpusLegRecorder,
    LEG_BUSINESS_CORPUS,
    LEG_FTS,
    LEG_VECTOR,
    RetrievalEntrypoint,
    RetrievalLegStatus,
    RetrievalOutcome,
    RetrievalTraceBuilder,
    coerce_entrypoint,
    safe_error_kind,
)


def test_schema_version_is_one():
    assert RETRIEVAL_TRACE_SCHEMA_VERSION == 1


def test_enum_values_are_stable_snake_case():
    assert RetrievalOutcome.SUCCESS.value == "success"
    assert RetrievalOutcome.EMPTY_LEGITIMATE.value == "empty_legitimate"
    assert RetrievalOutcome.DEGRADED.value == "degraded"
    assert RetrievalOutcome.ERROR.value == "error"
    assert RetrievalLegStatus.NOT_REQUESTED.value == "not_requested"
    assert RetrievalLegStatus.DISABLED.value == "disabled"
    assert RetrievalLegStatus.SUCCEEDED.value == "succeeded"
    assert RetrievalLegStatus.EMPTY.value == "empty"
    assert RetrievalLegStatus.DEGRADED.value == "degraded"
    assert RetrievalLegStatus.FAILED.value == "failed"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("chat_sync", "chat_sync"),
        ("chat_stream", "chat_stream"),
        ("core_api", "core_api"),
        ("benchmark", "benchmark"),
        ("holdout", "holdout"),
        ("internal", "internal"),
        (RetrievalEntrypoint.CORE_API, "core_api"),
        ("unknown_value", "internal"),
        (None, "internal"),
        (123, "internal"),
    ],
)
def test_coerce_entrypoint(value, expected):
    assert coerce_entrypoint(value) == expected


def test_safe_error_kind_is_class_name_only():
    assert safe_error_kind(RuntimeError("SECRET boom sql: SELECT * FROM x")) == "RuntimeError"
    assert safe_error_kind(TypeError("SECRET")) == "TypeError"
    assert safe_error_kind(None) is None


def test_nominal_success_not_degraded():
    b = RetrievalTraceBuilder(entrypoint="core_api", request_id="rid-1")
    b.record_leg(LEG_FTS, RetrievalLegStatus.SUCCEEDED, candidate_count=3)
    b.record_leg(LEG_VECTOR, RetrievalLegStatus.DISABLED)
    trace = b.build(final_candidate_count=3, source_count=2)
    assert trace.degraded is False
    assert trace.outcome is RetrievalOutcome.SUCCESS
    assert trace.source_count == 2


def test_disabled_leg_never_degrades():
    b = RetrievalTraceBuilder(entrypoint="core_api", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.EMPTY)
    b.record_leg(LEG_VECTOR, RetrievalLegStatus.DISABLED)
    trace = b.build(final_candidate_count=0, source_count=0)
    assert trace.degraded is False
    assert trace.outcome is RetrievalOutcome.EMPTY_LEGITIMATE


def test_empty_leg_is_legitimate_not_degraded():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.EMPTY)
    trace = b.build(final_candidate_count=0, source_count=0)
    assert trace.outcome is RetrievalOutcome.EMPTY_LEGITIMATE
    assert trace.degraded is False


def test_failed_leg_marks_degraded():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.DEGRADED, error_kind="RuntimeError")
    trace = b.build(final_candidate_count=0, source_count=0)
    assert trace.degraded is True
    assert trace.outcome is RetrievalOutcome.DEGRADED


def test_degraded_wins_even_with_sources():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.SUCCEEDED, candidate_count=5)
    b.record_leg(LEG_VECTOR, RetrievalLegStatus.DEGRADED, error_kind="TimeoutError")
    trace = b.build(final_candidate_count=5, source_count=4)
    assert trace.degraded is True
    assert trace.outcome is RetrievalOutcome.DEGRADED
    assert trace.source_count == 4


def test_error_outcome_forced():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.SUCCEEDED, candidate_count=1)
    trace = b.build(final_candidate_count=1, source_count=1, error=True)
    assert trace.outcome is RetrievalOutcome.ERROR


def test_fallback_reasons_dedup_and_order():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.add_fallback_reason("rerank_fallback")
    b.add_fallback_reason("rerank_fallback")
    b.add_fallback_reason("vector_error")
    b.add_fallback_reason(None)
    trace = b.build(final_candidate_count=0, source_count=0)
    assert trace.fallback_reasons == ["rerank_fallback", "vector_error"]


def test_record_leg_last_write_wins_keeps_order():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    b.record_leg(LEG_FTS, RetrievalLegStatus.EMPTY)
    b.record_leg(LEG_VECTOR, RetrievalLegStatus.DISABLED)
    b.record_leg(LEG_FTS, RetrievalLegStatus.SUCCEEDED, candidate_count=2)
    trace = b.build(final_candidate_count=2, source_count=2)
    names = [leg.name for leg in trace.legs]
    assert names == [LEG_FTS, LEG_VECTOR]
    fts = next(leg for leg in trace.legs if leg.name == LEG_FTS)
    assert fts.status is RetrievalLegStatus.SUCCEEDED
    assert fts.candidate_count == 2


def test_to_dict_is_json_plain_and_versioned():
    b = RetrievalTraceBuilder(entrypoint="chat_sync", request_id="rid-9")
    b.record_leg(LEG_FTS, RetrievalLegStatus.DEGRADED, candidate_count=0, error_kind="RuntimeError")
    d = b.build(final_candidate_count=0, source_count=0).to_dict()
    assert d["schema_version"] == 1
    assert d["entrypoint"] == "chat_sync"
    assert d["outcome"] == "degraded"
    assert d["degraded"] is True
    assert d["legs"][0]["status"] == "degraded"
    assert d["legs"][0]["error_kind"] == "RuntimeError"
    # types plainement sérialisables
    import json

    json.dumps(d)


def test_summary_is_compact():
    b = RetrievalTraceBuilder(entrypoint="core_api", request_id="rid")
    b.record_leg(LEG_FTS, RetrievalLegStatus.DEGRADED, error_kind="RuntimeError")
    b.record_leg(LEG_VECTOR, RetrievalLegStatus.DISABLED)
    s = b.build(final_candidate_count=0, source_count=0).summary()
    assert s == {
        "schema_version": 1,
        "entrypoint": "core_api",
        "outcome": "degraded",
        "degraded": True,
        "legs": {"fts": "degraded", "vector": "disabled"},
    }


def test_no_sensitive_data_in_trace_dict():
    b = RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
    b.record_leg(
        LEG_FTS,
        RetrievalLegStatus.DEGRADED,
        error_kind=safe_error_kind(RuntimeError("SECRET_QUESTION to_tsquery('boom')")),
    )
    import json

    blob = json.dumps(b.build(final_candidate_count=0, source_count=0).to_dict())
    assert "SECRET_QUESTION" not in blob
    assert "to_tsquery" not in blob


def test_corpus_leg_recorder_states():
    # non demandé
    r = CorpusLegRecorder()
    assert r.leg_status() is RetrievalLegStatus.NOT_REQUESTED
    # demandé, vide
    r = CorpusLegRecorder()
    r.note_requested()
    assert r.leg_status() is RetrievalLegStatus.EMPTY
    # demandé, candidats
    r = CorpusLegRecorder()
    r.note_requested()
    r.note_candidate_count(3)
    assert r.leg_status() is RetrievalLegStatus.SUCCEEDED
    # demandé, erreur FTS -> dégradé, error_kind non sensible
    r = CorpusLegRecorder()
    r.note_requested()
    r.note_fts_error(RuntimeError("SECRET sql"))
    assert r.leg_status() is RetrievalLegStatus.DEGRADED
    assert r.error_kind == "RuntimeError"
    assert r.failed is True

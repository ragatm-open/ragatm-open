"""Tests unitaires des contrats internes de l'orchestrateur (Lot P1-B1). Purs."""
import dataclasses

import pytest

from apps.rag_core.services.query.contracts import (
    CandidateSet,
    PreparedQuery,
    QueryExecutionContext,
    QueryMode,
    QueryRequest,
    QueryResult,
    RankedCandidateSet,
    RetrievalCandidate,
    RetrievalPolicy,
)
from apps.rag_core.services.query.errors import (
    RetrievalConfigurationError,
    RetrievalInvariantError,
)
from apps.rag_core.services.query.trace import RetrievalEntrypoint, RetrievalTraceBuilder


# --- QueryRequest -----------------------------------------------------------
def test_query_request_from_legacy_args_coerces_entrypoint():
    r = QueryRequest.from_legacy_args(rag_space_id="rs", question="q", entrypoint="chat_sync")
    assert r.entrypoint == "chat_sync"
    assert r.entrypoint_enum is RetrievalEntrypoint.CHAT_SYNC
    r2 = QueryRequest.from_legacy_args(rag_space_id="rs", question="q", entrypoint="bogus")
    assert r2.entrypoint == "internal"


def test_query_request_allows_none_rag_space_id():
    # rag_space_id=None est VALIDE (mène à no_answer), pas une erreur.
    r = QueryRequest.from_legacy_args(rag_space_id=None, question="q")
    assert r.rag_space_id is None


def test_query_request_is_frozen():
    r = QueryRequest.from_legacy_args(rag_space_id="rs", question="q")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.question = "x"  # type: ignore[misc]


def test_query_request_rejects_non_str_question():
    with pytest.raises(RetrievalInvariantError):
        QueryRequest.from_legacy_args(rag_space_id="rs", question=123)  # type: ignore[arg-type]


def test_query_request_repr_hides_question():
    r = QueryRequest.from_legacy_args(rag_space_id="rs", question="SECRET_TEXT_XYZ")
    assert "SECRET_TEXT_XYZ" not in repr(r)
    assert "question_chars=15" in repr(r)


# --- QueryMode --------------------------------------------------------------
@pytest.mark.parametrize(
    "flags,expected",
    [
        (dict(inventory=False, explanatory=False, news=False), QueryMode.STANDARD),
        (dict(inventory=True, explanatory=False, news=False), QueryMode.INVENTORY),
        (dict(inventory=False, explanatory=True, news=False), QueryMode.EXPLANATORY),
        (dict(inventory=True, explanatory=True, news=True), QueryMode.NEWS),  # précédence
        (dict(inventory=True, explanatory=True, news=False), QueryMode.EXPLANATORY),
    ],
)
def test_query_mode_precedence(flags, expected):
    assert QueryMode.from_flags(**flags) is expected


# --- PreparedQuery ----------------------------------------------------------
def test_prepared_query_rewritten_is_second_query():
    p = PreparedQuery.from_resolved(
        original_question="orig",
        queries_for_search=["orig", "rewrite-1"],
        primary_retrieval_query="rewrite-1",
        inventory=False, explanatory=False, news=True, has_temporal_intent=True,
    )
    assert p.rewritten_query == "rewrite-1"
    assert p.mode is QueryMode.NEWS
    assert p.has_temporal_intent is True


def test_prepared_query_no_rewrite_single_query():
    p = PreparedQuery.from_resolved(
        original_question="orig", queries_for_search=["orig"], primary_retrieval_query="orig",
        inventory=False, explanatory=False, news=False, has_temporal_intent=False,
    )
    assert p.rewritten_query is None
    assert p.mode is QueryMode.STANDARD


# --- RetrievalPolicy --------------------------------------------------------
def test_policy_valid():
    pol = RetrievalPolicy(
        fts_required=True, hybrid_enabled=True, vector_available=True, temporal_enabled=False,
        rerank_enabled=False, diversity_enabled=False, min_sources=1, min_text_rank=0.02,
        min_score=0.2, top_k=4, vector_top_k=20, candidate_k=12, lexical_weight=1.0,
    )
    assert pol.top_k == 4


def test_policy_rejects_bad_top_k():
    with pytest.raises(RetrievalConfigurationError):
        RetrievalPolicy(
            fts_required=True, hybrid_enabled=True, vector_available=True, temporal_enabled=False,
            rerank_enabled=False, diversity_enabled=False, min_sources=1, min_text_rank=0.02,
            min_score=0.2, top_k=0, vector_top_k=20, candidate_k=12, lexical_weight=1.0,
        )


def test_policy_rejects_non_finite_min_score():
    with pytest.raises(RetrievalConfigurationError):
        RetrievalPolicy(
            fts_required=True, hybrid_enabled=True, vector_available=True, temporal_enabled=False,
            rerank_enabled=False, diversity_enabled=False, min_sources=1, min_text_rank=0.02,
            min_score=float("nan"), top_k=4, vector_top_k=20, candidate_k=12, lexical_weight=1.0,
        )


# --- RetrievalCandidate -----------------------------------------------------
def test_candidate_from_dict_and_validate():
    d = {"chunk_id": "doc.txt#0", "score": 0.9, "text": "hello",
         "source": {"document_id": "d1", "rag_space_id": "rs"}, "retrieval_leg": "fts"}
    c = RetrievalCandidate.from_dict(d)
    assert c.chunk_id == "doc.txt#0"
    assert c.document_id == "d1"
    assert c.source_leg == "fts"
    assert c.score == 0.9


def test_candidate_missing_chunk_id_raises():
    with pytest.raises(RetrievalInvariantError):
        RetrievalCandidate.from_dict({"score": 0.5, "text": "x"})


def test_candidate_non_finite_score_defaults_zero_but_validate_ok():
    c = RetrievalCandidate.from_dict({"chunk_id": "a", "score": float("inf"), "text": "x"})
    assert c.score == 0.0


def test_candidate_repr_hides_text():
    c = RetrievalCandidate.from_dict({"chunk_id": "a", "score": 0.1, "text": "SECRET_BODY"})
    assert "SECRET_BODY" not in repr(c)
    assert "text_chars=11" in repr(c)


# --- CandidateSet -----------------------------------------------------------
def test_candidate_set_counts_and_typed_view_no_copy():
    fts = [{"chunk_id": "a", "score": 0.5, "text": "t"}]
    merged = fts
    cs = CandidateSet(fts=fts, merged=merged)
    assert cs.counts == {"fts": 1, "vector": 0, "corpus": 0, "merged": 1}
    typed = cs.typed("merged")
    assert typed[0].chunk_id == "a"
    # référence, pas de copie de la liste sous-jacente
    assert cs.merged is merged


# --- RankedCandidateSet -----------------------------------------------------
def test_ranked_preserves_order_and_is_frozen():
    sel = [{"chunk_id": "b"}, {"chunk_id": "a"}, {"chunk_id": "c"}]
    r = RankedCandidateSet.from_selected(sel, rerank_applied=True, fallback_used=False)
    assert [c["chunk_id"] for c in r.candidates] == ["b", "a", "c"]  # ordre = contrat
    assert len(r) == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.rerank_applied = False  # type: ignore[misc]


# --- QueryResult (source de vérité unique) ----------------------------------
def test_query_result_round_trip_is_identity():
    d = {"status": "ok", "sources": [{"chunk_id": "a"}], "prompt": "P", "log_id": "L",
         "answer_kind": "ok", "retrieval_degraded": False, "retrieval_trace": {"x": 1}}
    qr = QueryResult.from_legacy_dict(d)
    assert qr.to_legacy_dict() == d
    assert qr.status == "ok"
    assert qr.sources == [{"chunk_id": "a"}]
    assert qr.retrieval_degraded is False


def test_query_result_requires_status():
    with pytest.raises(RetrievalInvariantError):
        QueryResult.from_legacy_dict({"sources": []})


def test_query_result_repr_hides_prompt_and_sources():
    qr = QueryResult.from_legacy_dict({"status": "ok", "prompt": "SECRET_PROMPT",
                                       "sources": [{"text": "SECRET_SRC"}]})
    assert "SECRET_PROMPT" not in repr(qr)
    assert "SECRET_SRC" not in repr(qr)


# --- QueryExecutionContext --------------------------------------------------
def test_execution_context_is_not_god_object():
    req = QueryRequest.from_legacy_args(rag_space_id="rs", question="q")
    tb = RetrievalTraceBuilder(entrypoint="internal", request_id=None)
    ctx = QueryExecutionContext.create(req, started_at=0.0, trace=tb)
    fields = {f.name for f in dataclasses.fields(ctx)}
    assert fields == {"request", "trace", "started_at", "prepared", "policy", "candidates", "ranked"}
    # aucun repository / service / dict fourre-tout
    assert ctx.prepared is None and ctx.candidates is None

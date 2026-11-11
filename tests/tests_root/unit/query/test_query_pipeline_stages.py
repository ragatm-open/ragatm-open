"""Tests unitaires des composants extraits de query_rag (Lot P1-B2).

Testent CandidateRetriever / Reranker / PromptBuilder EN ISOLATION (via un
``QueryExecutionContext`` construit à la main + fonctions injectées), sans passer par
le dict final. Purs sauf le retriever (corpus monkeypatché à vide -> pas de DB).
"""
import pytest

from apps.rag_core.services.query.candidate_reranker import rank_candidates
from apps.rag_core.services.query.candidate_retriever import retrieve_candidates
from apps.rag_core.services.query.contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
    RetrievalPolicy,
)
from apps.rag_core.services.query.errors import (
    RetrievalInvariantError,
    RetrievalLegUnavailable,
)
from apps.rag_core.services.query.prompt_builder import build_prompt_stage
from apps.rag_core.services.query.trace import RetrievalTraceBuilder


def _ctx(*, question="q", hybrid=False, vector_available=True, rerank_enabled=False,
         inventory=False, explanatory=False, news=False, comparative=False, top_k=4):
    req = QueryRequest.from_legacy_args(rag_space_id="rs", question=question, rag_space=None)
    tb = RetrievalTraceBuilder(entrypoint="internal", request_id="rid")
    ctx = QueryExecutionContext.create(req, started_at=0.0, trace=tb)
    ctx.prepared = PreparedQuery.from_resolved(
        original_question=question, queries_for_search=[question],
        primary_retrieval_query=question, inventory=inventory,
        explanatory=explanatory, news=news, has_temporal_intent=False,
        comparative=comparative,
    )
    ctx.policy = RetrievalPolicy(
        fts_required=True, hybrid_enabled=hybrid, vector_available=vector_available,
        temporal_enabled=False, rerank_enabled=rerank_enabled, diversity_enabled=False,
        min_sources=1, min_text_rank=0.0, min_score=0.0, top_k=top_k, vector_top_k=20,
        candidate_k=top_k, lexical_weight=1.0,
        rerank_trigger_min_candidates=1, rerank_top_k=top_k,
    )
    return ctx


@pytest.fixture(autouse=True)
def _no_corpus(monkeypatch):
    # Neutralise le leg corpus (pas de DB) : bi-source = no-op.
    import apps.corpus.services.retrieval as cr
    monkeypatch.setattr(cr, "allowed_corpus_ids_for_workspace", lambda ws: [])


# --- CandidateRetriever -----------------------------------------------------
def test_retriever_fts_nominal_sets_candidateset():
    ctx = _ctx()
    docs = [{"chunk_id": "a", "score": 0.9, "text": "x"}, {"chunk_id": "b", "score": 0.5, "text": "y"}]
    merged = retrieve_candidates(ctx, fts_search=lambda **k: list(docs), vector_search=None)
    assert [c["chunk_id"] for c in merged] == ["a", "b"]
    assert ctx.candidates is not None
    assert ctx.candidates.counts["fts"] == 2
    legs = {leg.name: leg.status.value for leg in ctx.trace.build(final_candidate_count=2, source_count=2).legs}
    assert legs["fts"] == "succeeded"
    assert legs["vector"] == "disabled"


def test_retriever_fts_empty():
    ctx = _ctx()
    merged = retrieve_candidates(ctx, fts_search=lambda **k: [], vector_search=None)
    assert merged == []
    legs = {leg.name: leg.status.value for leg in ctx.trace.build(final_candidate_count=0, source_count=0).legs}
    assert legs["fts"] == "empty"


def test_retriever_fts_typeerror_propagates():
    ctx = _ctx()
    def boom(**k):
        raise TypeError("bad")
    with pytest.raises(RetrievalInvariantError) as ei:
        retrieve_candidates(ctx, fts_search=boom, vector_search=None)
    assert ei.value.leg == "fts"


def test_retriever_fts_dependency_no_fallback_is_error():
    ctx = _ctx(hybrid=False)  # vectoriel désactivé -> aucun fallback
    def boom(**k):
        raise RuntimeError("db down")
    with pytest.raises(RetrievalLegUnavailable):
        retrieve_candidates(ctx, fts_search=boom, vector_search=None)


def test_retriever_missing_fts_function_raises_importerror():
    ctx = _ctx()
    with pytest.raises(ImportError):
        retrieve_candidates(ctx, fts_search=None, vector_search=None)


# --- Reranker ---------------------------------------------------------------
def test_reranker_not_requested_when_disabled():
    ctx = _ctx(rerank_enabled=False)
    chunks = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    selected, info = rank_candidates(ctx, chunks=chunks, selected=chunks[:1],
                                     rerank_fn=lambda **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert info is None
    assert [c["chunk_id"] for c in selected] == ["a"]  # sélection retrieval conservée
    assert ctx.ranked is not None and ctx.ranked.rerank_applied is False


def test_reranker_preserves_authoritative_order():
    ctx = _ctx(rerank_enabled=True, top_k=3)
    chunks = [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}]

    class _Info:
        fallback_used = False

    # Reranker renvoie un ORDRE différent : il doit faire autorité (pas de re-tri).
    def fake_rerank(*, question, chunks):
        reordered = [{"chunk_id": "c"}, {"chunk_id": "a"}, {"chunk_id": "b"}]
        return reordered, _Info()

    selected, info = rank_candidates(ctx, chunks=chunks, selected=chunks, rerank_fn=fake_rerank)
    assert [c["chunk_id"] for c in selected] == ["c", "a", "b"]  # ordre du reranker
    assert ctx.ranked.rerank_applied is True
    assert ctx.ranked.fallback_used is False


def test_reranker_fallback_marks_ranked():
    ctx = _ctx(rerank_enabled=True, top_k=3)
    chunks = [{"chunk_id": "a"}]

    class _Info:
        fallback_used = True

    selected, info = rank_candidates(ctx, chunks=chunks, selected=chunks,
                                     rerank_fn=lambda **k: ([{"chunk_id": "a"}], _Info()))
    assert ctx.ranked.fallback_used is True
    legs = {leg.name: leg.status.value for leg in ctx.trace.build(final_candidate_count=1, source_count=1).legs}
    assert legs["rerank"] == "degraded"


# --- PromptBuilder ----------------------------------------------------------
def _sel():
    return [{"chunk_id": "a", "score": 0.9, "text": "Paris est la capitale."}]


def test_prompt_standard_answer_kind_ok():
    ctx = _ctx(question="Quelle est la capitale ?")
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=False, strict_refusal=False)
    assert res.answer_kind == "ok"
    assert res.prompt and "Paris est la capitale." in res.prompt
    assert len(res.sources) == 1


def test_prompt_explain_kind():
    ctx = _ctx(explanatory=True)
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=False, strict_refusal=False)
    assert res.answer_kind == "explain"


def test_prompt_compare_kind():
    ctx = _ctx(question="Quelles différences entre Tessaly et Valteria ?", comparative=True)
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=False, strict_refusal=False)
    assert res.answer_kind == "compare"
    # Le gabarit comparatif doit exiger la structure ET interdire la comparaison inventée.
    assert "Points communs" in res.prompt
    assert "Principales différences" in res.prompt
    assert "comparaison artificielle" in res.prompt


def test_prompt_compare_precedes_explain():
    """« Compare X et Y en détail » : la comparaison prime sur l'explicatif."""
    ctx = _ctx(question="Compare en détail X et Y", comparative=True, explanatory=True)
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=False, strict_refusal=False)
    assert res.answer_kind == "compare"


def test_prompt_evidence_policies_precede_compare():
    """L'état des PREUVES (low_confidence / partial) reste prioritaire sur la forme."""
    ctx = _ctx(comparative=True)
    low = build_prompt_stage(ctx, selected=_sel(), low_confidence=True, strict_refusal=False)
    assert low.answer_kind == "low_confidence"
    partial = build_prompt_stage(_ctx(comparative=True), selected=_sel(),
                                 low_confidence=False, strict_refusal=True)
    assert partial.answer_kind == "partial"


def test_prompt_low_confidence_kind():
    ctx = _ctx()
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=True, strict_refusal=False)
    assert res.answer_kind == "low_confidence"


def test_prompt_partial_kind():
    ctx = _ctx()
    res = build_prompt_stage(ctx, selected=_sel(), low_confidence=False, strict_refusal=True)
    assert res.answer_kind == "partial"


def test_prompt_result_repr_hides_content():
    ctx = _ctx()
    res = build_prompt_stage(ctx, selected=[{"chunk_id": "a", "text": "SECRET_BODY_XYZ"}],
                             low_confidence=False, strict_refusal=False)
    assert "SECRET_BODY_XYZ" not in repr(res)

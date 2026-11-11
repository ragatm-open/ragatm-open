"""Tests du runner de benchmark avec des callables factices (sans DB ni LLM)."""
from apps.rag_core.benchmark.dataset import BenchmarkCase
from apps.rag_core.benchmark import report as report_mod
from apps.rag_core.benchmark.runner import (
    extract_doc_keys,
    run_benchmark,
)


def _src(filename, chunk_id, score=0.9, document_id=None):
    return {
        "chunk_id": chunk_id,
        "score": score,
        "text": "…",
        "source": {"filename": filename, "document_id": document_id or filename, "page_number": 1},
    }


def test_extract_doc_keys_dedups_by_document():
    sources = [_src("a.pdf", "c1"), _src("a.pdf", "c2"), _src("b.pdf", "c3")]
    assert extract_doc_keys(sources) == ["a.pdf", "b.pdf"]


def _resolver(name):
    return "space-uuid" if name == "demo-fr" else None


def test_run_benchmark_perfect_and_no_answer():
    cases = [
        BenchmarkCase(
            id="q1",
            rag_space="demo-fr",
            question="capitale ?",
            should_answer=True,
            expected_documents=("a.pdf",),
        ),
        BenchmarkCase(
            id="q99",
            rag_space="demo-fr",
            question="hors sujet ?",
            should_answer=False,
        ),
    ]

    def query_fn(space_id, question):
        if "capitale" in question:
            return {"status": "ok", "sources": [_src("a.pdf", "c1"), _src("z.pdf", "c2")]}
        return {"status": "no_answer", "sources": []}

    report = run_benchmark(cases, query_fn=query_fn, space_resolver=_resolver)
    s = report.summary
    assert s["n_cases"] == 2
    assert s["n_errors"] == 0
    assert s["recall_at_k"]["1"] == 1.0
    assert s["mrr"] == 1.0
    assert s["no_answer_accuracy"] == 1.0
    assert s["false_answer_rate"] == 0.0
    assert s["false_refusal_rate"] == 0.0


def test_run_benchmark_records_miss_and_false_answer():
    cases = [
        BenchmarkCase(
            id="q1",
            rag_space="demo-fr",
            question="q ?",
            should_answer=True,
            expected_documents=("good.pdf",),
        ),
        BenchmarkCase(id="q2", rag_space="demo-fr", question="no ?", should_answer=False),
    ]

    def query_fn(space_id, question):
        # mauvais doc pour le positif ; répond à tort pour le négatif
        return {"status": "ok", "sources": [_src("wrong.pdf", "c1")]}

    report = run_benchmark(cases, query_fn=query_fn, space_resolver=_resolver)
    s = report.summary
    assert s["recall_at_k"]["1"] == 0.0
    assert s["mrr"] == 0.0
    assert s["false_answer_rate"] == 1.0  # négatif a répondu
    assert s["no_answer_accuracy"] == 0.0


def test_run_benchmark_unknown_space_is_error():
    cases = [
        BenchmarkCase(id="q1", rag_space="unknown", question="?", should_answer=False),
    ]
    report = run_benchmark(cases, query_fn=lambda s, q: {}, space_resolver=_resolver)
    assert report.summary["n_errors"] == 1
    assert report.cases[0].error and "introuvable" in report.cases[0].error


def test_query_fn_exception_is_captured():
    cases = [
        BenchmarkCase(
            id="q1", rag_space="demo-fr", question="?", should_answer=True,
            expected_documents=("a.pdf",),
        )
    ]

    def boom(space_id, question):
        raise RuntimeError("retrieval down")

    report = run_benchmark(cases, query_fn=boom, space_resolver=_resolver)
    assert report.summary["n_errors"] == 1
    assert "RuntimeError" in report.cases[0].error


def test_thresholds_pass_and_fail():
    cases = [
        BenchmarkCase(
            id="q1", rag_space="demo-fr", question="capitale ?", should_answer=True,
            expected_documents=("a.pdf",),
        ),
    ]
    report = run_benchmark(
        cases,
        query_fn=lambda s, q: {"status": "ok", "sources": [_src("a.pdf", "c1")]},
        space_resolver=_resolver,
    )
    ok, violations = report_mod.check_thresholds(report, {"recall_at_5": 0.8})
    assert ok and not violations

    ok2, violations2 = report_mod.check_thresholds(report, {"recall_at_5": 1.1})
    assert not ok2 and violations2[0]["metric"] == "recall_at_5"


def test_markdown_and_json_render():
    cases = [
        BenchmarkCase(
            id="q1", rag_space="demo-fr", question="capitale ?", should_answer=True,
            expected_documents=("a.pdf",),
        ),
    ]
    report = run_benchmark(
        cases,
        query_fn=lambda s, q: {"status": "ok", "sources": [_src("a.pdf", "c1")]},
        space_resolver=_resolver,
        meta={"dataset": "demo", "embeddings_provider": "stub"},
    )
    md = report_mod.to_markdown(report)
    assert "Rapport Benchmark RAG" in md
    assert "recall@1" in md
    import json as _json

    parsed = _json.loads(report_mod.to_json(report))
    assert parsed["summary"]["n_cases"] == 1
    assert parsed["cases"][0]["case_id"] == "q1"

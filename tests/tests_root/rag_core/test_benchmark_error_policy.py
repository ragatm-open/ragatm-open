"""Tests de la politique d'erreur du benchmark (P1-C).

- retrieval_error : compté comme miss (recall 0, RR 0) ET dans n_errors ;
- obsolete_reference / invalid_dataset_case : exclus du recall, run non promouvable ;
- benchmark vide rejeté ;
- garde CI : un run avec erreurs ne peut pas être PASS silencieux (--fail-under).
"""
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.rag_core.benchmark import validation as v
from apps.rag_core.benchmark.dataset import BenchmarkCase, DatasetError, parse_jsonl
from apps.rag_core.benchmark.runner import run_benchmark


def _resolver(name):
    return "space-uuid" if name else None


def _pos(cid):
    return BenchmarkCase(id=cid, rag_space="s", question="q ?", should_answer=True,
                         expected_documents=("good.pdf",))


def _src(fn, cid):
    return {"chunk_id": cid, "score": 0.9, "source": {"filename": fn, "document_id": fn}}


def test_retrieval_error_counts_as_miss_not_excluded():
    cases = [_pos("ok"), _pos("boom")]

    def query_fn(space_id, question):
        # 'boom' déclenché par le case_id via la question ? on distingue par contenu :
        raise RuntimeError("retrieval down")

    # 1 seul cas qui échoue : on route via deux query_fn différents impossible ; on teste
    # ici que TOUS échouent -> recall 0 sur le dénominateur (inclus, pas exclu).
    report = run_benchmark(cases, query_fn=query_fn, space_resolver=_resolver)
    s = report.summary
    assert s["n_errors"] == 2
    assert s["n_retrieval_errors"] == 2
    assert s["n_positive_scored"] == 2  # inclus dans le dénominateur
    assert s["recall_at_k"]["1"] == 0.0  # recall 0, PAS None (non exclu)
    assert s["mrr"] == 0.0
    assert s["run_status"] == "completed_with_errors"


def test_retrieval_error_mixed_with_success():
    cases = [_pos("good"), _pos("bad")]

    def query_fn(space_id, question):
        return {"status": "ok", "sources": [_src("good.pdf", "c1")]}

    # override: faire échouer un seul cas en levant selon la question
    good, bad = cases
    bad_case = BenchmarkCase(id="bad", rag_space="s", question="__boom__",
                             should_answer=True, expected_documents=("good.pdf",))

    def query_fn2(space_id, question):
        if question == "__boom__":
            raise RuntimeError("boom")
        return {"status": "ok", "sources": [_src("good.pdf", "c1")]}

    report = run_benchmark([good, bad_case], query_fn=query_fn2, space_resolver=_resolver)
    s = report.summary
    assert s["n_retrieval_errors"] == 1
    assert s["n_positive_scored"] == 2
    # 1 succès (recall 1) + 1 erreur (recall 0) => moyenne 0.5
    assert s["recall_at_k"]["1"] == pytest.approx(0.5)
    assert s["run_status"] == "completed_with_errors"


def test_obsolete_reference_excluded_and_run_invalid():
    case = _pos("obs")

    def query_fn(space_id, question):
        return {"status": "ok", "sources": [_src("good.pdf", "c1")]}

    def validate_fn(c, space_id):
        res = v.CaseValidation(case_id=c.id)
        res.add(v.ValidationIssue(v.CODE_EXPECTED_CHUNK_NOT_FOUND, "chunk parti", "error"))
        return res.finalize()

    report = run_benchmark([case], query_fn=query_fn, space_resolver=_resolver,
                           validate_fn=validate_fn)
    s = report.summary
    assert s["n_obsolete_references"] == 1
    assert s["n_errors"] == 1
    # exclu du recall : dénominateur scoré vide -> None (pas 0, pas un miss moteur)
    assert s["n_positive_scored"] == 0
    assert s["recall_at_k"]["1"] is None
    assert s["run_status"] == "invalid_dataset"
    # le cas obsolète n'a PAS appelé query_fn (pas exécuté)
    assert report.cases[0].error_kind == "obsolete_reference"


def test_invalid_dataset_case_marks_run_invalid():
    case = _pos("inv")

    def validate_fn(c, space_id):
        res = v.CaseValidation(case_id=c.id)
        res.add(v.ValidationIssue(v.CODE_REFERENCE_ANSWER_NOT_FOUND, "réponse absente", "error"))
        return res.finalize()

    report = run_benchmark([case], query_fn=lambda s, q: {"status": "ok", "sources": []},
                           space_resolver=_resolver, validate_fn=validate_fn)
    s = report.summary
    assert s["n_invalid_cases"] == 1
    assert s["run_status"] == "invalid_dataset"


def test_empty_benchmark_rejected():
    with pytest.raises(DatasetError):
        parse_jsonl(["# only comment"])


# ---------------------------------------------------------------------------
# Garde CI end-to-end (DB)
# ---------------------------------------------------------------------------





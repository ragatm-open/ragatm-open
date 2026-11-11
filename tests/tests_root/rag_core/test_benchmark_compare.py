"""Tests de la comparaison baseline/candidat (P1-A) — fonctions pures, sans DB."""
import pytest

from apps.rag_core.benchmark import compare as cmp


def _case(cid, *, rank=None, recall5=None, error=None, question="q ?"):
    rec = {}
    for k in (1, 3, 5, 10):
        if recall5 is not None:
            rec[str(k)] = recall5 if k >= 5 else (1.0 if (rank is not None and rank <= k) else 0.0)
        else:
            rec[str(k)] = 1.0 if (rank is not None and rank <= k) else 0.0
    return {
        "case_id": cid,
        "question": question,
        "first_relevant_rank": rank,
        "recall": rec,
        "error": error,
        "status": "error" if error else "ok",
    }


def _report(cases, *, benchmark="demo-fr", n_errors=0, extra_summary=None):
    summary = {
        "run_status": "completed_with_errors" if n_errors else "completed",
        "n_cases": len(cases),
        "n_errors": n_errors,
        "recall_at_k": {"1": 0.5, "3": 0.7, "5": 0.9, "10": 0.95},
        "mrr": 0.8,
        "latency_ms": {"p50": 10.0, "p95": 420.0, "mean": 50.0},
    }
    if extra_summary:
        summary.update(extra_summary)
    return {"meta": {"benchmark": benchmark}, "summary": summary, "cases": cases}


def test_improvement_recall5():
    base = _report([_case("q1", rank=None)])  # échec baseline
    cand = _report([_case("q1", rank=2)])     # succès candidat
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert r.compatible
    assert r.summary["improvements"] == 1
    assert r.summary["regressions"] == 0
    assert cmp.exit_code(r) == 0


def test_regression_recall5():
    base = _report([_case("q1", rank=1)])   # succès baseline
    cand = _report([_case("q1", rank=None)])  # échec candidat
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert r.summary["regressions"] == 1
    assert r.regressions[0].case_id == "q1"
    assert cmp.exit_code(r) == 1  # régression bloquante


def test_rank_improved_and_degraded_deltas():
    base = _report([_case("q1", rank=3), _case("q2", rank=1)])
    cand = _report([_case("q1", rank=1), _case("q2", rank=4)])
    r = cmp.compare_reports(base, cand, decision_k=5)
    by = {c.case_id: c for c in (r.still_passing + r.regressions + r.improvements)}
    # q1 : rang 3 -> 1, rank_delta = 3-1 = +2 (meilleur candidat)
    assert by["q1"].rank_delta == 2
    # q2 : rang 1 -> 4, rank_delta = 1-4 = -3 (dégradé) mais reste dans top5 -> still_passing
    assert by["q2"].rank_delta == -3
    assert by["q2"].category == "still_passing"


def test_still_passing_and_still_failing():
    base = _report([_case("p", rank=1), _case("f", rank=None)])
    cand = _report([_case("p", rank=2), _case("f", rank=None)])
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert r.summary["still_passing"] == 1
    assert r.summary["still_failing"] == 1


def test_error_introduced_is_regression_and_blocks():
    base = _report([_case("q1", rank=1)])
    cand = _report([_case("q1", rank=None, error="RuntimeError: boom")], n_errors=1)
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert r.summary["errors_introduced"] == 1
    assert r.summary["regressions"] == 1  # succès->échec
    assert "erreur(s) introduite(s)" in " ".join(cmp.blocking_reasons(r))
    assert cmp.exit_code(r) == 1


def test_error_resolved():
    base = _report([_case("q1", rank=None, error="boom")], n_errors=1)
    cand = _report([_case("q1", rank=1)])
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert r.summary["errors_resolved"] == 1
    assert r.summary["improvements"] == 1


def test_missing_case_in_candidate_blocks():
    base = _report([_case("q1", rank=1), _case("q2", rank=1)])
    cand = _report([_case("q1", rank=1)])
    r = cmp.compare_reports(base, cand, decision_k=5)
    assert not r.compatible  # jeux de cas différents -> incompatible par défaut
    assert cmp.exit_code(r) == 2


def test_new_case_in_candidate_allowed_with_flag():
    base = _report([_case("q1", rank=1)])
    cand = _report([_case("q1", rank=1), _case("q2", rank=1)])
    r = cmp.compare_reports(base, cand, decision_k=5, allow_case_set_difference=True)
    assert r.compatible
    assert r.new_in_candidate == ["q2"]
    assert r.summary["new_in_candidate"] == 1


def test_duplicate_case_id_is_blocking():
    base = _report([_case("dup", rank=1), _case("dup", rank=2)])
    cand = _report([_case("dup", rank=1)])
    with pytest.raises(cmp.ComparisonError):
        cmp.compare_reports(base, cand)


def test_invalid_json_file():
    with pytest.raises(cmp.ComparisonError):
        cmp.load_report("{not json")


def test_incompatible_benchmarks_refused():
    base = _report([_case("q1", rank=1)], benchmark="demo-fr")
    cand = _report([_case("q1", rank=1)], benchmark="autre-corpus")
    r = cmp.compare_reports(base, cand)
    assert not r.compatible
    assert "distinct" in r.reason
    assert cmp.exit_code(r) == 2


def test_obsolete_reference_refuses_comparison():
    base = _report([_case("q1", rank=1)])
    cand = _report(
        [_case("q1", rank=1)], extra_summary={"n_obsolete_references": 1, "run_status": "invalid_dataset"}
    )
    r = cmp.compare_reports(base, cand)
    assert not r.compatible
    assert cmp.exit_code(r) == 2


def test_latency_delta():
    base = _report([_case("q1", rank=1)])
    cand = _report([_case("q1", rank=1)])
    cand["summary"]["latency_ms"]["p95"] = 465.0
    r = cmp.compare_reports(base, cand)
    lat = r.metrics["latency_p95_ms"]
    assert lat["baseline"] == 420.0
    assert lat["candidate"] == 465.0
    assert lat["delta"] == 45.0
    assert lat["delta_percent"] == pytest.approx(10.71, abs=0.01)


def test_metric_deltas_convention():
    base = _report([_case("q1", rank=1)])
    base["summary"]["recall_at_k"]["1"] = 0.80
    cand = _report([_case("q1", rank=1)])
    cand["summary"]["recall_at_k"]["1"] = 0.83
    r = cmp.compare_reports(base, cand)
    m = r.metrics["recall_at_1"]
    assert m["baseline"] == 0.80 and m["candidate"] == 0.83
    assert m["delta"] == pytest.approx(0.03)  # delta = candidate - baseline


def test_markdown_render_lists_regressions():
    base = _report([_case("q1", rank=1, question="Quel port ?")])
    cand = _report([_case("q1", rank=None, question="Quel port ?")])
    r = cmp.compare_reports(base, cand, decision_k=5)
    md = cmp.to_markdown(r)
    assert "Régressions" in md
    assert "q1" in md
    assert "Quel port ?" in md


def test_return_code_clean_when_no_regression():
    base = _report([_case("q1", rank=1)])
    cand = _report([_case("q1", rank=1)])
    r = cmp.compare_reports(base, cand)
    assert cmp.exit_code(r) == 0

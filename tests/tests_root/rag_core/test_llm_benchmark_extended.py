"""Tests des extensions du benchmark : juge LLM + cold-load. Mocks uniquement.

Vérifie le parsing du score du juge, l'intégration du juge dans le scoring d'un
cas, l'agrégation de la dimension ``judge``, et la mesure ``cold_load_ms``.
"""
from __future__ import annotations

from apps.rag_core.benchmark import llm_models as bench


def _case(**kw) -> bench.Case:
    base = dict(id="c", workspace="w", question="q", category="report",
                sources=[{"filename": "f.txt", "text": "contenu source"}])
    base.update(kw)
    return bench.Case(**base)


# --------------------------------------------------------------- parse juge

def test_parse_judge_score_on_ten_scale():
    assert bench.parse_judge_score('{"score": 8, "rationale": "ok"}') == 0.8
    assert bench.parse_judge_score('```json\n{"score": 10}\n```') == 1.0
    assert bench.parse_judge_score('{"score": 0}') == 0.0


def test_parse_judge_score_normalized_scale():
    assert bench.parse_judge_score('{"score": 0.75}') == 0.75


def test_parse_judge_score_invalid():
    assert bench.parse_judge_score("pas de json") is None
    assert bench.parse_judge_score('{"rationale": "x"}') is None
    assert bench.parse_judge_score('{"score": "abc"}') is None


def test_build_judge_prompt_contains_sources_and_answer():
    c = _case(sources=[{"filename": "budget.txt", "text": "480 000 euros"}])
    p = bench.build_judge_prompt(c, "La réponse cite 480 000 euros.")
    assert "budget.txt" in p and "480 000 euros" in p
    assert "RÉPONSE À ÉVALUER" in p and "480 000 euros" in p


# --------------------------------------------------------------- juge dans run_case

def test_run_case_adds_judge_when_marked():
    c = _case(checks={"structure_sections": ["Section A"], "judge": True})

    def gen(model, prompt, options, think):
        return {"text": "## Section A\nréponse fidèle.", "tokens": 5, "latency_ms": 3.0}

    def judge(prompt):
        return '{"score": 9, "rationale": "fidèle et structuré"}'

    run = bench.run_case(c, model="m", generate_fn=gen, judge_fn=judge)
    assert run.scores.get("judge") == 0.9
    assert run.overall is not None


def test_run_case_skips_judge_when_not_marked():
    c = _case(checks={"must_contain": ["réponse"]})  # pas de judge

    called = {"n": 0}

    def judge(prompt):
        called["n"] += 1
        return '{"score": 5}'

    bench.run_case(c, model="m", generate_fn=lambda *a: {"text": "réponse"}, judge_fn=judge)
    assert called["n"] == 0  # juge non appelé sur un cas non marqué


def test_run_case_judge_failure_is_tolerated():
    c = _case(checks={"structure_sections": ["A"], "judge": True})

    def judge(prompt):
        raise RuntimeError("judge down")

    run = bench.run_case(c, model="m", generate_fn=lambda *a: {"text": "## A"}, judge_fn=judge)
    assert "judge" not in run.scores  # échec juge => pas de score, pas de crash
    assert run.overall is not None


# --------------------------------------------------------------- agrégation + cold-load

def test_aggregate_includes_judge_dimension():
    c = _case(checks={"structure_sections": ["A"], "judge": True})
    report = bench.run_benchmark(
        models=["m"], cases=[c],
        generate_fn=lambda *a: {"text": "## A", "tokens": 1, "latency_ms": 2.0},
        judge_fn=lambda p: '{"score": 6}',
    )
    dims = report["models"]["m"]["summary"]["dimensions"]
    assert dims["judge"] == 0.6


def test_cold_load_measured_and_reported():
    seen = {}

    def cold(model):
        seen[model] = True
        return 1234.5

    report = bench.run_benchmark(
        models=["m"], cases=[_case(checks={"must_contain": ["x"]})],
        generate_fn=lambda *a: {"text": "x", "tokens": 1, "latency_ms": 2.0},
        cold_load_fn=cold,
    )
    assert seen == {"m": True}
    assert report["models"]["m"]["summary"]["cold_load_ms"] == 1234.5


def test_cold_load_none_when_not_requested():
    report = bench.run_benchmark(
        models=["m"], cases=[_case(checks={"must_contain": ["x"]})],
        generate_fn=lambda *a: {"text": "x", "tokens": 1, "latency_ms": 2.0},
    )
    assert report["models"]["m"]["summary"]["cold_load_ms"] is None


def test_render_markdown_has_judge_and_coldload_columns():
    report = bench.run_benchmark(
        models=["m"], cases=[_case(checks={"structure_sections": ["A"], "judge": True})],
        generate_fn=lambda *a: {"text": "## A", "tokens": 1, "latency_ms": 2.0},
        judge_fn=lambda p: '{"score": 7}', cold_load_fn=lambda m: 500.0,
    )
    md = bench.render_markdown(report)
    assert "Juge" in md and "Cold-load" in md

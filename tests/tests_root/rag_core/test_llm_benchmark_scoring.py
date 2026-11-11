"""Tests unitaires du scoring/runner du benchmark multi-modèles (sans DB ni réseau).

Vérifie les heuristiques déterministes : fidélité, hallucination, refus, citations,
JSON, structure, ainsi que le runner (generate_fn injecté), la gestion des réponses
vides/erreurs et le plancher num_predict pour qwen3:14b.
"""
from __future__ import annotations

from apps.rag_core.benchmark import llm_models as bench


# ------------------------------------------------------------------ helpers texte

def test_normalize_strips_accents_and_case():
    assert bench.normalize("Éléphant   À  la Une") == "elephant a la une"


def test_contains_all_reports_missing():
    ok, missing = bench.contains_all("24 mois, Mme Durand", ["24 mois", "Durand"])
    assert ok and missing == []
    ok, missing = bench.contains_all("24 mois", ["24 mois", "Durand"])
    assert not ok and missing == ["Durand"]


def test_looks_like_refusal():
    assert bench.looks_like_refusal("Je ne sais pas.")
    assert bench.looks_like_refusal("L'information n'est pas présente dans les documents.")
    assert bench.looks_like_refusal("Aucun montant de subventions externes n'est mentionné.")
    assert bench.looks_like_refusal("Cette information n'est pas précisée dans les sources.")
    assert not bench.looks_like_refusal("Le budget est de 480 000 euros.")


def test_cited_indices():
    assert bench.cited_indices("Réponse. Sources: [1], [3]") == [1, 3]
    assert bench.cited_indices("aucune citation") == []


def test_extract_json_plain_and_fenced():
    assert bench.extract_json('{"a": 1}') == {"a": 1}
    assert bench.extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert bench.extract_json('Voici: {"a": 3} fin') == {"a": 3}


# ------------------------------------------------------------------ scoring

def _case(**kw) -> bench.Case:
    base = dict(id="c", workspace="w", question="q", category="cat", sources=[{"filename": "f", "text": "t"}])
    base.update(kw)
    return bench.Case(**base)


def test_score_fidelity_and_citation_ok():
    c = _case(sources=[{"filename": "contrat.txt", "text": "..."}],
              checks={"must_contain": ["24 mois", "Durand"], "expect_citation": True})
    s = bench.score_response(c, "La durée est de 24 mois, responsable Mme Durand. Sources: [1]")
    assert s["fidelity"] == 1.0
    assert s["citations"] == 1.0


def test_score_citation_out_of_range_flagged():
    c = _case(sources=[{"filename": "a", "text": "x"}], checks={"expect_citation": True})
    s = bench.score_response(c, "Réponse. Sources: [2]")  # une seule source -> [2] invalide
    assert s["citations"] == 0.0
    assert "citation_out_of_range" in s["flags"]


def test_score_hallucination_detected_by_regex():
    c = _case(checks={"expect_refusal": True,
                      "must_not_contain_regex": [r"\b\d[\d\s.,]*\s*(?:€|euros?)\b"]})
    bad = bench.score_response(c, "Le coût est de 12 000 euros.")
    assert bad["hallucination"] == 0.0
    assert bad["refusal"] == 0.0  # refus invalide car fuite de donnée
    good = bench.score_response(c, "L'information n'est pas présente dans les documents.")
    assert good["hallucination"] == 1.0
    assert good["refusal"] == 1.0


def test_score_isolation_refusal_with_forbidden_substring():
    c = _case(checks={"expect_refusal": True, "must_not_contain": ["480 000", "Horizon"]})
    leak = bench.score_response(c, "Le budget du projet Horizon est de 480 000 euros.")
    assert leak["hallucination"] == 0.0 and leak["refusal"] == 0.0
    ok = bench.score_response(c, "Cette information n'est pas disponible dans les sources fournies.")
    assert ok["refusal"] == 1.0


def test_score_json_valid_and_missing_keys():
    c = _case(checks={"json_required_keys": ["a", "b"]})
    ok = bench.score_response(c, '{"a": 1, "b": 2}')
    assert ok["json"] == 1.0
    miss = bench.score_response(c, '{"a": 1}')
    assert miss["json"] == 0.0 and "json_missing_keys" in miss["flags"]
    bad = bench.score_response(c, "pas de json ici")
    assert bad["json"] == 0.0 and "json_invalid" in bad["flags"]


def test_score_fidelity_must_contain_any_group():
    c = _case(checks={"must_contain": ["24 septembre 2026"],
                      "must_contain_any": [["remplace", "nouvelle date", "au lieu"]]})
    ok = bench.score_response(c, "La date retenue est le 24 septembre 2026, au lieu du 10.")
    assert ok["fidelity"] == 1.0
    partial = bench.score_response(c, "La date retenue est le 24 septembre 2026.")
    assert partial["fidelity"] == 0.5  # groupe 'any' non satisfait


def test_score_structure_sections():
    c = _case(checks={"structure_sections": ["Faits établis", "Risques"]})
    full = bench.score_response(c, "## Faits établis\n...\n## Risques\n...")
    assert full["structure"] == 1.0
    half = bench.score_response(c, "## Faits établis seulement")
    assert half["structure"] == 0.5


def test_weighted_case_score_prioritizes_refusal():
    # refus raté (0) pèse plus lourd que fidélité parfaite
    s = {"refusal": 0.0, "fidelity": 1.0}
    assert bench.weighted_case_score(s) < 0.5


# ------------------------------------------------------------------ runner

def test_run_case_scores_with_fake_generate_fn():
    c = _case(checks={"must_contain": ["ok"], "expect_citation": True},
              sources=[{"filename": "a", "text": "x"}])

    def fake(model, prompt, options, think):
        assert "num_predict" in options  # options bien transmises
        return {"text": "réponse ok. Sources: [1]", "tokens": 10, "cost": 0.0, "latency_ms": 12.0}

    run = bench.run_case(c, model="m", generate_fn=fake)
    assert run.error is None and not run.empty
    assert run.scores["fidelity"] == 1.0 and run.scores["citations"] == 1.0
    assert run.overall is not None and run.tokens == 10


def test_run_case_handles_empty_response():
    c = _case(checks={"must_contain": ["x"]})
    run = bench.run_case(c, model="m", generate_fn=lambda *a: {"text": "  "})
    assert run.empty and run.overall is None and run.scores == {}


def test_run_case_handles_generate_error():
    c = _case()
    def boom(*a):
        raise RuntimeError("gateway down")
    run = bench.run_case(c, model="m", generate_fn=boom)
    assert run.empty and "gateway down" in (run.error or "")


def test_qwen3_14b_num_predict_floor_applied():
    seen = {}
    c = _case(gen={"num_predict": 256})

    def capture(model, prompt, options, think):
        seen["np"] = options["num_predict"]
        return {"text": "x"}

    bench.run_benchmark(models=["qwen3:14b"], cases=[c], generate_fn=capture)
    assert seen["np"] >= 1024  # plancher qwen3:14b imposé


def test_render_markdown_contains_models():
    report = bench.run_benchmark(
        models=["m1"], cases=[_case(checks={"must_contain": ["ok"]})],
        generate_fn=lambda *a: {"text": "ok", "tokens": 3, "latency_ms": 5.0},
    )
    md = bench.render_markdown(report)
    assert "m1" in md and "Fidélité" in md

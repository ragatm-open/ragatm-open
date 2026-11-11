"""Filtres à seuil du générateur : gt, lt, between (nombres) et before/after (dates).

Un dataset `eq`-only mesurerait surtout les fonctions d'agrégation et très peu la
capacité de **sélection** tabulaire. Ces filtres comblent l'écart — à condition que
leurs bornes soient observées, déterministes et découpantes.

Tests PURS : tables en mémoire, aucun classeur, aucune DB, aucun retrieval.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_aggregation_to_query as ad
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_reference_executor as ex

SHA = "a" * 64
UNCAPPED = {k: 500 for k in gen.DEFAULT_CAPS}

HEADERS = ["Dossier", "Statut", "Montant", "Encours", "Echeance"]
DATA = [
    ["D-1", "En cours", 100, 5.5, dt.date(2025, 1, 15)],
    ["D-2", "Clos",     250, 1.25, dt.date(2025, 3, 20)],
    ["D-3", "En cours",  50, 9.0, dt.date(2024, 11, 5)],
    ["D-4", "En cours", 300, 2.5, dt.date(2025, 6, 30)],
    ["D-5", "Clos",     175, 7.75, dt.date(2024, 2, 1)],
    ["D-6", "En cours", 425, 3.0, dt.date(2025, 9, 9)],
]


def _table(headers=None, data=None):
    return ex.build_table(headers or HEADERS, data if data is not None else DATA)


def _source(rows=6, cols="E"):
    return gen.TableSource(
        content_sha256=SHA, filename="d.xlsx", path="/tmp/d.xlsx",
        locator=s.TableLocator(sheet_name="Demandes", table_range=f"A1:{cols}{rows + 1}",
                               header_rows=(1,), data_row_start=2, data_row_end=rows + 1))


def _generate(table=None, source=None):
    stats = gen.GenerationStats()
    cases, rejects = gen.generate_for_table(source or _source(), table or _table(),
                                            rag_space="s", stats=stats, caps=UNCAPPED)
    return cases, rejects, stats


def _filters(case):
    return case["ground_truth"]["selection"]["filters"]


def _by_operator(cases, operator):
    return [c for c in cases if any(f["operator"] == operator for f in _filters(c))]


# --------------------------------------------------------------------------- #
# Sélection de seuil — observée, déterministe, découpante
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operator", [s.F_GT, s.F_LT])
def test_threshold_is_an_observed_value(operator):
    values = [Decimal(x) for x in (10, 20, 30, 40, 50)]
    assert gen.select_threshold(values, operator=operator) in values


@pytest.mark.parametrize("operator", [s.F_GT, s.F_LT])
def test_threshold_splits_the_population_strictly(operator):
    values = [Decimal(x) for x in (10, 20, 30, 40, 50)]
    bound = gen.select_threshold(values, operator=operator)
    kept = ([v for v in values if v > bound] if operator == s.F_GT
            else [v for v in values if v < bound])
    assert 0 < len(kept) < len(values)


def test_threshold_is_independent_of_row_order():
    values = [Decimal(x) for x in (30, 10, 50, 20, 40)]
    assert (gen.select_threshold(values, operator=s.F_GT)
            == gen.select_threshold(sorted(values), operator=s.F_GT)
            == gen.select_threshold(list(reversed(values)), operator=s.F_GT))


def test_threshold_handles_duplicates_and_negatives():
    values = [Decimal(x) for x in (-50, -50, 0, 10, 10, 100)]
    bound = gen.select_threshold(values, operator=s.F_GT)
    assert 0 < len([v for v in values if v > bound]) < len(values)


def test_a_constant_column_yields_no_threshold():
    with pytest.raises(gen._FilterThresholdError) as e:
        gen.select_threshold([Decimal(7)] * 5, operator=s.F_GT)
    assert e.value.code == gen.ERR_INSUFFICIENT_DISTINCT_VALUES


def test_two_distinct_values_still_allow_a_threshold():
    bound = gen.select_threshold([Decimal(1), Decimal(2)], operator=s.F_GT)
    assert bound == Decimal(1)


# --------------------------------------------------------------------------- #
# between
# --------------------------------------------------------------------------- #
def test_between_bounds_are_observed_ordered_and_strict():
    values = [Decimal(x) for x in (10, 20, 30, 40, 50, 60)]
    low, high = gen.select_between_bounds(values)
    assert low in values and high in values and low < high
    assert 0 < len([v for v in values if low <= v <= high]) < len(values)


def test_between_needs_three_distinct_values():
    with pytest.raises(gen._FilterThresholdError) as e:
        gen.select_between_bounds([Decimal(1), Decimal(2)])
    assert e.value.code == gen.ERR_INSUFFICIENT_DISTINCT_VALUES


def test_between_is_independent_of_order():
    values = [Decimal(x) for x in (50, 10, 30, 60, 20, 40)]
    assert gen.select_between_bounds(values) == gen.select_between_bounds(sorted(values))


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
DATES = ["2024-02-01", "2024-11-05", "2025-01-15", "2025-03-20", "2025-06-30"]


@pytest.mark.parametrize("operator", [s.F_BEFORE, s.F_AFTER])
def test_date_threshold_is_an_observed_date(operator):
    assert gen.select_threshold(DATES, operator=operator) in DATES


def test_date_between_uses_observed_bounds():
    low, high = gen.select_between_bounds(DATES)
    assert low in DATES and high in DATES and low < high


def test_fewer_than_three_dates_refuses_an_interval():
    with pytest.raises(gen._FilterThresholdError):
        gen.select_between_bounds(["2025-01-01", "2025-01-02"])


# --------------------------------------------------------------------------- #
# Génération de bout en bout
# --------------------------------------------------------------------------- #
def test_the_generator_now_emits_more_than_equality():
    cases, _, _ = _generate()
    operators = {f["operator"] for c in cases for f in _filters(c)}
    assert operators - {"eq"}, f"toujours eq-only : {operators}"
    assert {"gt", "lt", "between"} & operators


def test_date_operators_are_emitted_when_a_date_column_exists():
    cases, _, _ = _generate()
    operators = {f["operator"] for c in cases for f in _filters(c)}
    assert {"before", "after"} & operators, operators


@pytest.mark.parametrize("operator", ["gt", "lt", "between", "before", "after"])
def test_generated_threshold_questions_name_column_and_bounds(operator):
    cases = _by_operator(_generate()[0], operator)
    if not cases:
        pytest.skip(f"aucun cas {operator} sur cette fixture")
    for case in cases:
        question = case["question"]
        f = next(f for f in _filters(case) if f["operator"] == operator)
        assert f["column"]["source_header"] in question
        bounds = f.get("values") or [f.get("value")]
        for bound in bounds:
            assert str(bound) in question, (operator, question, bound)
        assert question.strip().endswith("?")


def test_threshold_cases_select_a_strict_subset():
    """La garde de non-trivialité : ni sélection vide, ni sélection totale."""
    table = _table()
    for case in _generate(table)[0]:
        filters = _filters(case)
        if not filters or filters[0]["operator"] == "eq":
            continue
        selected = case["ground_truth"]["aggregation_evidence"]["selected_row_count"]
        assert 0 < selected < len(DATA), (case["question"], selected)


def test_generation_with_thresholds_stays_reproducible():
    a, _, _ = _generate()
    b, _, _ = _generate()
    assert [s.dump_case(c) for c in a] == [s.dump_case(c) for c in b]


def test_threshold_cases_have_distinct_case_ids():
    cases = _generate()[0]
    assert len({c["case_id"] for c in cases}) == len(cases)


# --------------------------------------------------------------------------- #
# Adaptabilité sans perte vers tabular_query_* (précision 3 de la mission)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operator,engine", [
    ("gt", "greater_than"), ("lt", "less_than"), ("between", "between"),
    ("before", "less_than"), ("after", "greater_than"),
])
def test_every_new_filter_adapts_to_the_query_harness(operator, engine):
    """Chaîne complète : cas oracle -> adaptateur -> cas exécutable."""
    cases = _by_operator(_generate()[0], operator)
    if not cases:
        pytest.skip(f"aucun cas {operator} sur cette fixture")
    for case in cases:
        adapted = ad.adapt_aggregation_case(case)
        got = next(f for f in adapted.case["expected_filters"]
                   if f["operator"] == engine)
        assert got["column"]
        assert adapted.content_sha256 == SHA
        assert len(adapted.semantic_fingerprint) == 64


def test_adapted_threshold_cases_keep_distinct_fingerprints():
    """Deux seuils différents ne doivent pas se confondre après adaptation."""
    cases = [c for c in _generate()[0] if _filters(c)
             and _filters(c)[0]["operator"] != "eq"]
    prints = {ad.adapt_aggregation_case(c).semantic_fingerprint for c in cases}
    assert len(prints) == len(cases)

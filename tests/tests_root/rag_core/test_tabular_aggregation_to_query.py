"""Convergence oracle → harnais moteur : adaptation, refus, indépendance, couches.

Deux tests portent l'essentiel de la garantie méthodologique :

- **l'indépendance** : changer la sortie du moteur ne doit pas bouger `expected_result`,
  et un moteur qui répond faux doit être détecté comme faux ;
- **la séparation des couches** : un mauvais plan fidèlement exécuté et un bon plan mal
  exécuté ne doivent pas produire le même diagnostic.

Fixtures SYNTHÉTIQUES. Aucun corpus réel, aucun holdout, aucun appel moteur.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_aggregation_to_query as ad
from apps.rag_core.benchmark import tabular_query_metrics as met
from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef

SHA = "a" * 64


def _col(name):
    return sch.ColumnRef(source_header=name)


def _oracle_case(*, operation=sch.OP_COUNT_ROWS, target=None, identity=(), filters=(),
                 result=None, question="Combien de lignes ?", population=None):
    gt = sch.TabularAggregationGroundTruth(
        document_ref=StableDocumentRef(content_sha256=SHA, filename="ventes.xlsx"),
        table_locator=sch.TableLocator(sheet_name="Demandes", table_range="A1:H200",
                                       header_rows=(1,), data_row_start=2,
                                       data_row_end=200),
        aggregation=sch.Aggregation(
            function=operation,
            target_column=_col(target) if target else None,
            identity_columns=tuple(_col(c) for c in identity)),
        expected_result=result or sch.ExpectedResult(type=sch.RESULT_INTEGER, value=17),
        filters=filters,
        row_population_policy=population or sch.RowPopulationPolicy())
    return sch.to_case_dict(case_id="xlsx-agg-test", question=question,
                            rag_space="dev_bench_excel", ground_truth=gt,
                            operation=operation)


# --------------------------------------------------------------------------- #
# Mapping des opérations (§8)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operation,expected_engine_op", [
    (sch.OP_COUNT_ROWS, "count_rows"),
    (sch.OP_FILTERED_COUNT, "count_rows"),
    (sch.OP_SUM, "sum"),
    (sch.OP_FILTERED_SUM, "sum"),
    (sch.OP_AVERAGE, "average"),
    (sch.OP_MINIMUM, "minimum"),
    (sch.OP_MAXIMUM, "maximum"),
    (sch.OP_ARGMIN, "argmin"),
    (sch.OP_ARGMAX, "argmax"),
])
def test_every_mvp_operation_maps_to_the_engine(operation, expected_engine_op):
    """Les neuf opérations du MVP ont un équivalent — aucune n'est perdue."""
    needs_target = operation not in (sch.OP_COUNT_ROWS, sch.OP_FILTERED_COUNT)
    needs_identity = operation in (sch.OP_ARGMIN, sch.OP_ARGMAX)
    filters = ((sch.Filter(column=_col("Statut"), operator=sch.F_EQ, value="ouvert"),)
               if operation in (sch.OP_FILTERED_COUNT, sch.OP_FILTERED_SUM) else ())
    result = (sch.ExpectedResult(type=sch.RESULT_ROW_IDENTIFIER, value={"Ref": "D-4"})
              if needs_identity else
              sch.ExpectedResult(type=sch.RESULT_DECIMAL, value=Decimal("120.50"))
              if needs_target else
              sch.ExpectedResult(type=sch.RESULT_INTEGER, value=17))
    case = _oracle_case(operation=operation, target="Montant" if needs_target else None,
                        identity=("Ref",) if needs_identity else (),
                        filters=filters, result=result)
    out = ad.adapt_aggregation_case(case)
    assert out.case["expected_operation"] == expected_engine_op
    assert out.case["oracle_operation"] == operation


def test_filtered_variants_keep_their_filters_when_the_name_changes():
    """`filtered_count` devient `count_rows` : le sens ne survit que par les filtres."""
    f = (sch.Filter(column=_col("Statut"), operator=sch.F_EQ, value="ouvert"),)
    out = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT, filters=f))
    assert out.case["expected_operation"] == "count_rows"
    assert out.case["expected_filters"] == [{"column": "Statut", "operator": "equals",
                                             "value": "ouvert"}]


# --------------------------------------------------------------------------- #
# Mapping des filtres (§9)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("oracle_op,engine_op", [
    (sch.F_EQ, "equals"), (sch.F_NEQ, "not_equals"),
    (sch.F_GT, "greater_than"), (sch.F_LT, "less_than"),
    (sch.F_GTE, "greater_or_equal"), (sch.F_LTE, "less_or_equal"),
    (sch.F_CONTAINS, "contains"), (sch.F_IS_EMPTY, "is_null"),
    (sch.F_IS_NOT_EMPTY, "is_not_null"),
])
def test_filter_operators_map_to_the_engine(oracle_op, engine_op):
    kw = {} if oracle_op in (sch.F_IS_EMPTY, sch.F_IS_NOT_EMPTY) else {"value": 10}
    f = (sch.Filter(column=_col("Montant"), operator=oracle_op, **kw),)
    out = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT, filters=f))
    assert out.case["expected_filters"][0]["operator"] == engine_op


def test_dates_before_and_after_map_to_ordered_comparisons():
    """Le moteur n'a pas `before`/`after` : une date s'y compare comme toute valeur
    ordonnée. L'oracle fait de même sur des dates ISO — la sémantique est conservée."""
    for oracle_op, engine_op in ((sch.F_BEFORE, "less_than"), (sch.F_AFTER, "greater_than")):
        f = (sch.Filter(column=_col("Échéance"), operator=oracle_op, value="2025-01-01"),)
        out = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT,
                                                     filters=f))
        assert out.case["expected_filters"][0]["operator"] == engine_op


def test_between_carries_both_bounds():
    f = (sch.Filter(column=_col("Montant"), operator=sch.F_BETWEEN, values=(100, 500)),)
    out = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT, filters=f))
    adapted = out.case["expected_filters"][0]
    assert adapted["operator"] == "between" and adapted["values"] == ["100", "500"]


@pytest.mark.parametrize("unmappable", sorted(ad.UNMAPPABLE_FILTERS))
def test_filters_without_engine_equivalent_are_refused(unmappable):
    """Aucun repli silencieux : un opérateur intraduisible fait échouer l'adaptation."""
    kw = {"values": ("a",)} if unmappable == sch.F_NOT_IN else {"value": "a"}
    f = (sch.Filter(column=_col("Statut"), operator=unmappable, **kw),)
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT, filters=f))
    assert e.value.code == ad.ERR_UNSUPPORTED_QUERY_FILTER


# --------------------------------------------------------------------------- #
# Refus (§17)
# --------------------------------------------------------------------------- #
def test_unknown_schema_version_is_refused():
    case = _oracle_case()
    case["schema_version"] = 99
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_INVALID_SCHEMA_VERSION


def test_deferred_operation_is_refused():
    case = _oracle_case()
    case["operation"] = sch.OP_GROUPED_AGGREGATION
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_UNSUPPORTED_QUERY_OPERATION


def test_missing_content_sha256_is_refused():
    """Sans identité documentaire canonique, le cas perdrait sa traçabilité (§18)."""
    case = _oracle_case()
    case["ground_truth"]["document_ref"]["content_sha256"] = ""
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_SEMANTIC_LOSS


def test_missing_table_locator_field_is_refused():
    case = _oracle_case()
    del case["ground_truth"]["table_locator"]["data_row_end"]
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_SEMANTIC_LOSS


def test_non_representable_population_policy_is_refused():
    case = _oracle_case()
    case["ground_truth"]["row_population_policy"]["include_rows"] = "all_rows"
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_UNSUPPORTED_POPULATION_POLICY


def test_unsupported_result_type_is_refused():
    case = _oracle_case()
    case["ground_truth"]["expected_result"]["type"] = "matrix"
    with pytest.raises(ad.AdaptationError) as e:
        ad.adapt_aggregation_case(case)
    assert e.value.code == ad.ERR_UNSUPPORTED_RESULT_TYPE


def test_adapt_many_separates_refusals_without_losing_them():
    good = _oracle_case()
    bad = _oracle_case()
    bad["operation"] = sch.OP_COMPARISON
    adapted, refused = ad.adapt_many([good, bad])
    assert len(adapted) == 1 and len(refused) == 1
    assert refused[0]["code"] == ad.ERR_UNSUPPORTED_QUERY_OPERATION


# --------------------------------------------------------------------------- #
# Identité canonique et empreinte sémantique (§16, §18)
# --------------------------------------------------------------------------- #
def test_canonical_identity_survives_the_adaptation():
    out = ad.adapt_aggregation_case(_oracle_case())
    assert out.content_sha256 == SHA
    assert out.case["content_sha256"] == SHA          # jamais remplacé par un id runtime
    assert out.case["oracle_case_id"] == "xlsx-agg-test"


def test_fingerprint_is_stable_across_json_key_order():
    case = _oracle_case()
    shuffled = dict(reversed(list(case.items())))
    shuffled["ground_truth"] = dict(reversed(list(case["ground_truth"].items())))
    assert (ad.adapt_aggregation_case(case).semantic_fingerprint
            == ad.adapt_aggregation_case(shuffled).semantic_fingerprint)


def test_fingerprint_is_independent_of_filter_order():
    f1 = sch.Filter(column=_col("Statut"), operator=sch.F_EQ, value="ouvert")
    f2 = sch.Filter(column=_col("Zone"), operator=sch.F_EQ, value="Ouest")
    a = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT,
                                               filters=(f1, f2)))
    b = ad.adapt_aggregation_case(_oracle_case(operation=sch.OP_FILTERED_COUNT,
                                               filters=(f2, f1)))
    assert a.semantic_fingerprint == b.semantic_fingerprint


@pytest.mark.parametrize("mutate", [
    lambda c: c["ground_truth"]["table_locator"].__setitem__("sheet_name", "Autre"),
    lambda c: c["ground_truth"]["expected_result"].__setitem__("value", 18),
    lambda c: c.__setitem__("operation", sch.OP_SUM),
])
def test_fingerprint_changes_when_the_meaning_changes(mutate):
    base = ad.adapt_aggregation_case(_oracle_case()).semantic_fingerprint
    case = _oracle_case()
    if mutate.__code__.co_consts and sch.OP_SUM in (mutate.__code__.co_consts or ()):
        case["ground_truth"]["aggregation"]["target_column"] = {"source_header": "Montant",
                                                                "normalized_header": "montant"}
        case["ground_truth"]["expected_result"] = {"type": sch.RESULT_DECIMAL,
                                                   "value": "12.5",
                                                   "comparison": sch.CMP_EXACT_DECIMAL}
    mutate(case)
    assert ad.adapt_aggregation_case(case).semantic_fingerprint != base


# --------------------------------------------------------------------------- #
# §24 — la vérité terrain ne dépend pas du moteur
# --------------------------------------------------------------------------- #
def _observed(value, *, status="ok", operation="count_rows", filters=(), columns=()):
    return {"status": status, "code": "", "value": value, "operation": operation,
            "filters": list(filters), "columns": list(columns),
            "document": "ventes.xlsx", "sheet": "Demandes", "table": "A1:H200"}


def _evaluate(case, observed):
    """`evaluate_case` exige la trace d'exécution : le déterminisme et la latence font
    partie du verdict du harnais, pas de la comparaison de valeur."""
    return met.evaluate_case(case, observed, deterministic=True, latency_ms=1.0)


def test_engine_output_never_moves_the_expected_result():
    """Le cœur de l'indépendance : quoi que réponde le moteur, l'attendu ne bouge pas."""
    adapted = ad.adapt_aggregation_case(_oracle_case()).case
    before = adapted["expected_value"]
    for engine_answer in ("18", "0", "", "17", None):
        _evaluate(adapted, _observed(engine_answer))
        assert adapted["expected_value"] == before == "17"


def test_a_wrong_engine_answer_is_reported_as_wrong():
    """oracle = 17, moteur simulé = 18 -> le cas doit ÉCHOUER, pas passer."""
    adapted = ad.adapt_aggregation_case(_oracle_case()).case
    ev = _evaluate(adapted, _observed("18"))
    assert ev.value_ok is False
    assert not ev.correct


def test_a_right_engine_answer_is_reported_as_right():
    adapted = ad.adapt_aggregation_case(_oracle_case()).case
    ev = _evaluate(adapted, _observed("17", operation="count_rows"))
    assert ev.value_ok is True


def test_an_empty_or_absent_answer_is_not_a_success():
    """Ni « pas d'exception » ni « résultat non vide » ne valent réussite."""
    adapted = ad.adapt_aggregation_case(_oracle_case()).case
    for answer in (None, "", "n/a"):
        assert _evaluate(adapted, _observed(answer)).value_ok is not True


# --------------------------------------------------------------------------- #
# §25 — séparation planification / exécution
# --------------------------------------------------------------------------- #
def test_case1_correct_plan_correct_execution_passes_every_dimension():
    adapted = ad.adapt_aggregation_case(
        _oracle_case(operation=sch.OP_FILTERED_COUNT,
                     filters=(sch.Filter(column=_col("Statut"), operator=sch.F_EQ,
                                         value="ouvert"),))).case
    ev = _evaluate(adapted, _observed(
        "17", operation="count_rows",
        filters=[{"column": "Statut", "operator": "equals", "value": "ouvert"}]))
    assert ev.value_ok is True and ev.operation_ok is True and ev.filters_ok is True


def test_case2_wrong_plan_faithfully_executed_fails_on_the_plan_dimensions():
    """Le moteur exécute fidèlement… un mauvais plan. La valeur ET le plan divergent :
    le diagnostic doit pointer la planification, pas seulement le résultat."""
    adapted = ad.adapt_aggregation_case(
        _oracle_case(operation=sch.OP_FILTERED_COUNT,
                     filters=(sch.Filter(column=_col("Statut"), operator=sch.F_EQ,
                                         value="ouvert"),))).case
    ev = _evaluate(adapted, _observed(
        "42", operation="count_rows",
        filters=[{"column": "Zone", "operator": "equals", "value": "Ouest"}]))
    assert ev.filters_ok is False          # mauvais prédicat -> erreur de planification
    assert ev.value_ok is False


def test_case3_correct_plan_wrong_execution_isolates_the_executor():
    """Plan identique à l'oracle, résultat faux : seule l'exécution est en cause."""
    adapted = ad.adapt_aggregation_case(
        _oracle_case(operation=sch.OP_FILTERED_COUNT,
                     filters=(sch.Filter(column=_col("Statut"), operator=sch.F_EQ,
                                         value="ouvert"),))).case
    ev = _evaluate(adapted, _observed(
        "99", operation="count_rows",
        filters=[{"column": "Statut", "operator": "equals", "value": "ouvert"}]))
    assert ev.operation_ok is True and ev.filters_ok is True   # plan correct
    assert ev.value_ok is False                                # exécution fautive


def test_plan_and_execution_dimensions_are_reported_separately():
    """La confusion planification/exécution est un NO-GO : les axes restent distincts."""
    fields = met.CaseEvaluation.__dataclass_fields__
    for dimension in ("operation_ok", "filters_ok", "columns_ok", "table_ok", "value_ok"):
        assert dimension in fields

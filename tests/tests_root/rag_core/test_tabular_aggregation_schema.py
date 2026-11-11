"""Schéma versionné des cas `tabular_aggregation` — validation et identité stable.

Tests PURS : aucune DB, aucun retrieval, aucun holdout, aucun classeur réel.
"""
from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_types as tt
from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef

SHA = "a" * 64


def _col(name="Montant", idx=3):
    return s.ColumnRef(source_header=name, column_index=idx)


def _locator(**kw):
    base = dict(sheet_name="Demandes", table_range="A1:H200", header_rows=(1,),
                data_row_start=2, data_row_end=200)
    base.update(kw)
    return s.TableLocator(**base)


def _gt(**kw):
    base = dict(
        document_ref=StableDocumentRef(content_sha256=SHA, filename="d.xlsx"),
        table_locator=_locator(),
        aggregation=s.Aggregation(function=s.OP_COUNT_ROWS),
        expected_result=s.ExpectedResult(type=s.RESULT_INTEGER, value=17),
    )
    base.update(kw)
    return s.TabularAggregationGroundTruth(**base)


# --------------------------------------------------------------------------- #
# Identité stable du cas (§23)
# --------------------------------------------------------------------------- #
def _case_id(**kw):
    base = dict(document_sha256=SHA, sheet_name="Demandes", table_range="A1:H200",
                operation=s.OP_FILTERED_COUNT)
    base.update(kw)
    return s.build_aggregation_case_id(**base)


def test_case_id_is_reproducible_and_prefixed():
    assert _case_id() == _case_id()
    assert _case_id().startswith("xlsx-agg-")


def test_case_id_is_independent_of_filter_order():
    """Deux mêmes filtres saisis dans l'ordre inverse décrivent le même cas."""
    f1 = s.Filter(column=_col("Statut"), operator=s.F_EQ, value="En cours")
    f2 = s.Filter(column=_col("Année"), operator=s.F_EQ, value="2025")
    assert _case_id(filters=(f1, f2)) == _case_id(filters=(f2, f1))


def test_case_id_changes_with_every_logical_key():
    ref = _case_id()
    assert _case_id(document_sha256="b" * 64) != ref
    assert _case_id(sheet_name="Autre") != ref
    assert _case_id(table_range="A1:H201") != ref
    assert _case_id(operation=s.OP_COUNT_ROWS) != ref
    assert _case_id(target_column="Montant") != ref
    assert _case_id(filters=(s.Filter(column=_col("S"), operator=s.F_EQ, value="x"),)) != ref


def test_case_id_ignores_header_case_and_spacing():
    """L'identité est logique : la casse d'un en-tête ne change pas le cas."""
    assert _case_id(target_column="Montant") == _case_id(target_column="  MONTANT  ")


def test_case_id_stable_across_processes_and_hash_seeds():
    snippet = (
        "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.dev');"
        "django.setup();"
        "from apps.rag_core.benchmark import tabular_aggregation_schema as s;"
        "print(s.build_aggregation_case_id(document_sha256='a'*64, sheet_name='Réf',"
        "table_range='A1:C3', operation='sum', target_column='Coût'))"
    )
    outs = []
    for hs in ("0", "42", "random"):
        env = {**__import__("os").environ, "PYTHONHASHSEED": hs,
               "DJANGO_SETTINGS_MODULE": "config.settings.dev"}
        r = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stderr[-400:]
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, outs


# --------------------------------------------------------------------------- #
# Version de schéma et task_type (§45)
# --------------------------------------------------------------------------- #
def _case(**kw):
    base = dict(case_id="xlsx-agg-x", question="Combien de lignes ?",
                rag_space="dev_bench_excel", ground_truth=_gt())
    base.update(kw)
    return s.to_case_dict(**base)


def test_valid_case_passes_validation():
    case = _case()
    assert case["schema_version"] == 1
    assert case["task_type"] == s.TASK_TABULAR_AGGREGATION
    s.validate_case(case)


def test_unknown_schema_version_is_refused():
    case = _case()
    case["schema_version"] = 2
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_INVALID_SCHEMA_VERSION


def test_wrong_task_type_is_refused():
    case = _case()
    case["task_type"] = "retrieval_lookup"
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_UNSUPPORTED_OPERATION


def test_empty_question_is_refused():
    case = _case(question="   ")
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_QUESTION_NOT_SELF_CONTAINED


@pytest.mark.parametrize("op", [s.OP_GROUPED_AGGREGATION, s.OP_COMPARISON])
def test_deferred_operations_are_refused_not_silently_accepted(op):
    """Hors périmètre MVP : reconnu, mais explicitement refusé (§7)."""
    case = _case()
    case["operation"] = op
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_UNSUPPORTED_OPERATION


def test_unknown_operation_is_refused():
    case = _case()
    case["operation"] = "median"
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_UNSUPPORTED_OPERATION


# --------------------------------------------------------------------------- #
# Interdiction des UUID (§9)
# --------------------------------------------------------------------------- #
def test_uuid_anywhere_in_the_case_is_refused():
    case = _case()
    case["ground_truth"]["source_chunk_id"] = "ecedae5a-5db6-48d8-9d49-ec2612345678"
    with pytest.raises(s.TabularAggregationError) as e:
        s.validate_case(case)
    assert e.value.code == s.ERR_UUID_FORBIDDEN


def test_case_without_uuid_passes_the_guard():
    s.assert_no_uuid(_case())


# --------------------------------------------------------------------------- #
# Identité documentaire
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "z" * 64])
def test_invalid_document_sha256_is_refused(bad):
    with pytest.raises(s.TabularAggregationError) as e:
        _gt(document_ref=StableDocumentRef(content_sha256=bad)).validate()
    assert e.value.code == s.ERR_INVALID_DOCUMENT_SHA256


# --------------------------------------------------------------------------- #
# Locator de table (§10)
# --------------------------------------------------------------------------- #
def test_valid_locator():
    _locator().validate()


def test_data_start_after_data_end_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        _locator(data_row_start=200, data_row_end=2).validate()
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


def test_rows_outside_the_table_range_are_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        _locator(data_row_end=500).validate()
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


def test_header_row_inside_the_data_population_is_refused():
    """Un en-tête compté comme donnée fausserait tout comptage."""
    with pytest.raises(s.TabularAggregationError) as e:
        _locator(header_rows=(5,), data_row_start=2).validate()
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


def test_empty_header_rows_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        _locator(header_rows=()).validate()
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


def test_exclusion_outside_the_population_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        _locator(excluded_rows=(999,)).validate()
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


def test_locator_normalizes_sheet_and_range():
    loc = _locator(sheet_name="  Demandes  ", table_range="$a$1:$h$200")
    assert loc.sheet_name == "Demandes"
    assert loc.table_range == "A1:H200"


# --------------------------------------------------------------------------- #
# En-têtes (§11)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["col1", "Col 2", "Unnamed: 0", "column3", "", "  ", "7"])
def test_generic_headers_are_refused(bad):
    assert s.is_generic_header(bad)
    with pytest.raises(s.TabularAggregationError) as e:
        s.ColumnRef(source_header=bad).validate()
    assert e.value.code in (s.ERR_GENERIC_OR_INFERRED_HEADER, s.ERR_AMBIGUOUS_TARGET_COLUMN)


@pytest.mark.parametrize("good", ["Montant 2025", "Statut", "Counterparty Sector"])
def test_explicit_headers_are_accepted(good):
    assert not s.is_generic_header(good)
    s.ColumnRef(source_header=good).validate()


def test_column_ref_normalizes_for_matching():
    assert s.ColumnRef(source_header="  Montant  2025 ").normalized_header == "montant 2025"


# --------------------------------------------------------------------------- #
# Filtres (§13, §43)
# --------------------------------------------------------------------------- #
def test_unary_operator_must_not_carry_a_value():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Filter(column=_col("Statut"), operator=s.F_IS_EMPTY, value="x").validate()
    assert e.value.code == s.ERR_INVALID_FILTER


def test_list_operator_requires_a_non_empty_list():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Filter(column=_col("Statut"), operator=s.F_IN, values=()).validate()
    assert e.value.code == s.ERR_INVALID_FILTER


def test_between_requires_exactly_two_bounds():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Filter(column=_col("Montant"), operator=s.F_BETWEEN, values=(1,)).validate()
    assert e.value.code == s.ERR_INVALID_FILTER


def test_binary_operator_requires_a_value():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Filter(column=_col("Statut"), operator=s.F_EQ).validate()
    assert e.value.code == s.ERR_INVALID_FILTER


def test_unknown_operator_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Filter(column=_col("Statut"), operator="matches", value="x").validate()
    assert e.value.code == s.ERR_UNSUPPORTED_FILTER_OPERATOR


@pytest.mark.parametrize("operator,column_type", [
    (s.F_GT, tt.TYPE_STRING),        # comparaison numérique sur du texte
    (s.F_LTE, tt.TYPE_STRING),
    (s.F_BEFORE, tt.TYPE_DECIMAL),   # comparaison de date sur un nombre
    (s.F_CONTAINS, tt.TYPE_DECIMAL),  # sous-chaîne sur un nombre
])
def test_incompatible_operator_and_column_type_are_refused(operator, column_type):
    f = s.Filter(column=_col("X"), operator=operator, value=1)
    with pytest.raises(s.TabularAggregationError) as e:
        f.validate(column_type=column_type)
    assert e.value.code == s.ERR_INCOMPATIBLE_FILTER_TYPE


@pytest.mark.parametrize("operator,column_type", [
    (s.F_EQ, tt.TYPE_STRING), (s.F_GT, tt.TYPE_DECIMAL),
    (s.F_BEFORE, tt.TYPE_DATE), (s.F_CONTAINS, tt.TYPE_STRING),
    (s.F_IS_EMPTY, tt.TYPE_STRING),
])
def test_compatible_operator_and_column_type_pass(operator, column_type):
    kw = {} if operator in (s.F_IS_EMPTY,) else {"value": 1}
    s.Filter(column=_col("X"), operator=operator, **kw).validate(column_type=column_type)


# --------------------------------------------------------------------------- #
# Agrégation (§6, §45)
# --------------------------------------------------------------------------- #
def test_sum_requires_a_target_column():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Aggregation(function=s.OP_SUM).validate()
    assert e.value.code == s.ERR_MISSING_TARGET_COLUMN


def test_count_rows_must_not_carry_a_target_column():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Aggregation(function=s.OP_COUNT_ROWS, target_column=_col()).validate()
    assert e.value.code == s.ERR_INVALID_FILTER


def test_argmax_requires_identity_columns():
    with pytest.raises(s.TabularAggregationError) as e:
        s.Aggregation(function=s.OP_ARGMAX, target_column=_col()).validate()
    assert e.value.code == s.ERR_MISSING_IDENTITY_COLUMN


def test_argmax_with_identity_is_valid():
    s.Aggregation(function=s.OP_ARGMAX, target_column=_col(),
                  identity_columns=(_col("Produit", 1),)).validate()


def test_filtered_count_without_filter_is_refused():
    """Sans filtre, filtered_count EST count_rows : le cas serait mal étiqueté."""
    gt = _gt(aggregation=s.Aggregation(function=s.OP_FILTERED_COUNT))
    with pytest.raises(s.TabularAggregationError) as e:
        gt.validate()
    assert e.value.code == s.ERR_INVALID_FILTER


# --------------------------------------------------------------------------- #
# Résultat attendu (§21)
# --------------------------------------------------------------------------- #
def test_count_must_produce_an_integer():
    with pytest.raises(s.TabularAggregationError) as e:
        s.ExpectedResult(type=s.RESULT_DECIMAL, value="1").validate(operation=s.OP_COUNT_ROWS)
    assert e.value.code == s.ERR_INVALID_EXPECTED_RESULT


def test_argmax_must_produce_a_row_identifier():
    with pytest.raises(s.TabularAggregationError) as e:
        s.ExpectedResult(type=s.RESULT_DECIMAL, value="1").validate(operation=s.OP_ARGMAX)
    assert e.value.code == s.ERR_INVALID_EXPECTED_RESULT


def test_row_identifier_must_be_a_mapping():
    with pytest.raises(s.TabularAggregationError) as e:
        s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value="ABC").validate()
    assert e.value.code == s.ERR_INVALID_EXPECTED_RESULT


def test_tolerance_comparison_requires_a_tolerance():
    with pytest.raises(s.TabularAggregationError) as e:
        s.ExpectedResult(type=s.RESULT_DECIMAL, value="1",
                         comparison=s.CMP_ABSOLUTE_TOLERANCE).validate()
    assert e.value.code == s.ERR_INVALID_EXPECTED_RESULT


def test_unknown_result_type_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        s.ExpectedResult(type="matrix", value=1).validate()
    assert e.value.code == s.ERR_INVALID_EXPECTED_RESULT


# --------------------------------------------------------------------------- #
# Politiques (§12, §29)
# --------------------------------------------------------------------------- #
def test_unknown_include_mode_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        s.RowPopulationPolicy(include_rows="all_rows").validate()
    assert e.value.code == s.ERR_AMBIGUOUS_ROW_POPULATION


def test_total_labels_are_excluded_case_and_accent_insensitively():
    pol = s.RowPopulationPolicy()
    assert pol.is_excluded_label("TOTAL")
    assert pol.is_excluded_label("  total  ")
    assert pol.is_excluded_label("Sous-Total")
    assert not pol.is_excluded_label("Totalisateur")


def test_unknown_missing_value_policy_is_refused():
    with pytest.raises(s.TabularAggregationError) as e:
        s.MissingValuePolicy(on_missing="guess").validate()
    assert e.value.code == s.ERR_INVALID_MISSING_VALUE_POLICY


# --------------------------------------------------------------------------- #
# Sérialisation
# --------------------------------------------------------------------------- #
def test_dump_is_independent_of_key_order():
    case = _case()
    shuffled = dict(reversed(list(case.items())))
    assert s.dump_case(case) == s.dump_case(shuffled)


def test_decimals_are_serialised_as_strings_never_floats():
    """Un float dans le dataset réintroduirait le bruit binaire qu'on a banni."""
    gt = _gt(aggregation=s.Aggregation(function=s.OP_SUM, target_column=_col()),
             expected_result=s.ExpectedResult(type=s.RESULT_DECIMAL,
                                              value=Decimal("12345.67"), scale=2,
                                              comparison=s.CMP_EXACT_DECIMAL))
    dumped = s.dump_case(_case(ground_truth=gt))
    assert '"value":"12345.67"' in dumped
    assert "12345.670000" not in dumped


def test_full_ground_truth_validates():
    gt = _gt(
        aggregation=s.Aggregation(function=s.OP_FILTERED_SUM, target_column=_col("Montant")),
        filters=(s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="En cours"),),
        expected_result=s.ExpectedResult(type=s.RESULT_DECIMAL, value=Decimal("120.50"),
                                         scale=2, comparison=s.CMP_EXACT_DECIMAL),
    )
    gt.validate(column_types={"statut": tt.TYPE_STRING, "montant": tt.TYPE_DECIMAL})

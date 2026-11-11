"""Exécuteur tabulaire de référence — opérations, filtres, refus d'ambiguïté.

Tests PURS : tables construites en mémoire, aucun classeur, aucune DB, aucun retrieval.
Le chargement openpyxl est testé séparément (`test_tabular_excel_loading.py`).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark import tabular_types as tt
from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef

SHA = "a" * 64

#: Types NATIFS, comme les rend openpyxl sur un vrai classeur : une date est un
#: `datetime.date`, pas une chaîne. Une colonne de dates *textuelles* est un cas
#: distinct, volontairement refusé (cf. tests dédiés en fin de fichier).
HEADERS = ["Dossier", "Statut", "Montant", "Échéance"]
DATA = [
    ["D-1", "En cours", 100, dt.date(2025, 1, 15)],
    ["D-2", "Clos",     250, dt.date(2025, 3, 20)],
    ["D-3", "En cours",  50, dt.date(2024, 11, 5)],
    ["D-4", "En cours", 300, dt.date(2025, 6, 30)],
    ["D-5", "Clos",     100, dt.date(2024, 2, 1)],
]


def _table(headers=None, data=None, **kw):
    return ex.build_table(headers or HEADERS, data if data is not None else DATA, **kw)


def _col(name, idx=1):
    return s.ColumnRef(source_header=name, column_index=idx)


def _gt(*, operation, target=None, filters=(), identity=(), result=None,
        locator=None, population=None, missing=None):
    agg = s.Aggregation(
        function=operation,
        target_column=_col(target) if target else None,
        identity_columns=tuple(_col(c) for c in identity),
    )
    expected = result or s.ExpectedResult(type=s.RESULT_INTEGER, value=0)
    return s.TabularAggregationGroundTruth(
        document_ref=StableDocumentRef(content_sha256=SHA, filename="d.xlsx"),
        table_locator=locator or s.TableLocator(
            sheet_name="Feuille1", table_range="A1:D6", header_rows=(1,),
            data_row_start=2, data_row_end=6),
        aggregation=agg, expected_result=expected, filters=filters,
        row_population_policy=population or s.RowPopulationPolicy(),
        missing_value_policy=missing or s.MissingValuePolicy(),
    )


# --------------------------------------------------------------------------- #
# Comptages
# --------------------------------------------------------------------------- #
def test_count_rows_counts_the_whole_population():
    r = ex.execute(_table(), _gt(operation=s.OP_COUNT_ROWS))
    assert r.value == 5
    assert r.result_type == s.RESULT_INTEGER


def test_filtered_count_applies_the_predicate():
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="En cours"),)
    r = ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f))
    assert r.value == 3
    assert r.selected_row_count == 3


def test_filter_matching_ignores_case_and_spacing():
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="  EN COURS "),)
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == 3


# --------------------------------------------------------------------------- #
# Sommes et moyennes
# --------------------------------------------------------------------------- #
def test_sum_uses_decimal_arithmetic():
    r = ex.execute(_table(), _gt(operation=s.OP_SUM, target="Montant"))
    assert r.value == Decimal(800)
    assert isinstance(r.value, Decimal)


def test_filtered_sum():
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="En cours"),)
    r = ex.execute(_table(), _gt(operation=s.OP_FILTERED_SUM, target="Montant", filters=f))
    assert r.value == Decimal(450)


def test_sum_of_decimals_has_no_binary_drift():
    """0.1 + 0.2 doit valoir exactement 0.3 — tout l'intérêt du Decimal."""
    t = _table(["K", "V"], [["a", 0.1], ["b", 0.2]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    assert ex.execute(t, gt).value == Decimal("0.3")


def test_average_denominator_counts_only_valid_numeric_values():
    t = _table(["K", "V"], [["a", 10], ["b", None], ["c", 20]])
    gt = _gt(operation=s.OP_AVERAGE, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B4", header_rows=(1,),
                                    data_row_start=2, data_row_end=4))
    r = ex.execute(t, gt)
    assert r.value == Decimal(15)          # (10+20)/2, pas /3
    assert any("2/3" in w for w in r.warnings)


def test_average_can_reject_instead_of_ignoring_missing_values():
    t = _table(["K", "V"], [["a", 10], ["b", None]])
    gt = _gt(operation=s.OP_AVERAGE, target="V",
             missing=s.MissingValuePolicy(on_missing=s.MISSING_REJECT),
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_UNSUPPORTED_CELL_TYPE


# --------------------------------------------------------------------------- #
# Extremums et arg
# --------------------------------------------------------------------------- #
def test_minimum_and_maximum():
    assert ex.execute(_table(), _gt(operation=s.OP_MINIMUM, target="Montant")).value == Decimal(50)
    assert ex.execute(_table(), _gt(operation=s.OP_MAXIMUM, target="Montant")).value == Decimal(300)


def test_argmax_returns_the_row_identifier():
    r = ex.execute(_table(), _gt(operation=s.OP_ARGMAX, target="Montant",
                                 identity=("Dossier",),
                                 result=s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value={})))
    assert r.value == {"Dossier": "D-4"}
    assert r.result_type == s.RESULT_ROW_IDENTIFIER


def test_argmin_returns_the_row_identifier():
    r = ex.execute(_table(), _gt(operation=s.OP_ARGMIN, target="Montant",
                                 identity=("Dossier",),
                                 result=s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value={})))
    assert r.value == {"Dossier": "D-3"}


def test_argmax_refuses_ties_instead_of_picking_the_first():
    """Deux lignes ex aequo : « quelle ligne » n'a pas de réponse unique (§52)."""
    t = _table(["K", "V"], [["a", 100], ["b", 100]])
    gt = _gt(operation=s.OP_ARGMAX, target="V", identity=("K",),
             result=s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value={}),
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_AMBIGUOUS_ROW_POPULATION


def test_maximum_with_ties_is_allowed_but_warns():
    """La valeur maximale reste unique même si plusieurs lignes la portent."""
    t = _table(["K", "V"], [["a", 100], ["b", 100]])
    gt = _gt(operation=s.OP_MAXIMUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    r = ex.execute(t, gt)
    assert r.value == Decimal(100)
    assert any("ex" in w or "portent" in w for w in r.warnings)


# --------------------------------------------------------------------------- #
# Population de lignes (§12)
# --------------------------------------------------------------------------- #
def test_total_rows_are_excluded_from_the_population():
    """Inclure une ligne TOTAL compterait deux fois les mêmes montants."""
    t = _table(["K", "V"], [["a", 10], ["b", 20], ["TOTAL", 30]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B4", header_rows=(1,),
                                    data_row_start=2, data_row_end=4))
    assert ex.execute(t, gt).value == Decimal(30)   # 10+20, la ligne TOTAL est écartée


def test_empty_rows_are_ignored():
    t = _table(["K", "V"], [["a", 10], [None, None], ["b", 20]])
    gt = _gt(operation=s.OP_COUNT_ROWS,
             locator=s.TableLocator(sheet_name="F", table_range="A1:B4", header_rows=(1,),
                                    data_row_start=2, data_row_end=4))
    assert ex.execute(t, gt).value == 2


def test_explicitly_excluded_rows_are_removed():
    gt = _gt(operation=s.OP_COUNT_ROWS,
             locator=s.TableLocator(sheet_name="Feuille1", table_range="A1:D6",
                                    header_rows=(1,), data_row_start=2, data_row_end=6,
                                    excluded_rows=(3, 4)))
    assert ex.execute(_table(), gt).value == 3


# --------------------------------------------------------------------------- #
# Refus d'ambiguïté (§52)
# --------------------------------------------------------------------------- #
def test_duplicated_header_is_never_arbitrated():
    t = _table(["K", "Montant", "Montant"], [["a", 1, 2]])
    gt = _gt(operation=s.OP_SUM, target="Montant",
             locator=s.TableLocator(sheet_name="F", table_range="A1:C2", header_rows=(1,),
                                    data_row_start=2, data_row_end=2))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_AMBIGUOUS_TARGET_COLUMN


def test_missing_column_is_refused():
    gt = _gt(operation=s.OP_SUM, target="Inexistante")
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(_table(), gt)
    assert e.value.code == s.ERR_AMBIGUOUS_TARGET_COLUMN


def test_mixed_type_column_is_refused():
    t = _table(["K", "V"], [["a", 10], ["b", "texte libre"]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_UNSUPPORTED_CELL_TYPE


def test_sum_on_a_text_column_is_refused():
    gt = _gt(operation=s.OP_SUM, target="Statut")
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(_table(), gt)
    assert e.value.code == s.ERR_UNSUPPORTED_CELL_TYPE


def test_empty_selection_is_refused():
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="Inconnu"),)
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(_table(), _gt(operation=s.OP_FILTERED_SUM, target="Montant", filters=f))
    assert e.value.code == s.ERR_EMPTY_SELECTION


def test_formula_without_cached_value_is_refused():
    t = _table(["K", "V"], [["a", 10], ["b", ex.FORMULA_NO_CACHE]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_FORMULA_RESULT_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Filtres — couverture des opérateurs (§43)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operator,kw,expected", [
    (s.F_EQ, {"value": "En cours"}, 3),
    (s.F_NEQ, {"value": "En cours"}, 2),
    (s.F_IN, {"values": ("Clos", "En cours")}, 5),
    (s.F_NOT_IN, {"values": ("Clos",)}, 3),
    (s.F_CONTAINS, {"value": "cours"}, 3),
    (s.F_NOT_CONTAINS, {"value": "cours"}, 2),
])
def test_text_filters(operator, kw, expected):
    f = (s.Filter(column=_col("Statut", 2), operator=operator, **kw),)
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == expected


@pytest.mark.parametrize("operator,value,expected", [
    (s.F_GT, 100, 2), (s.F_GTE, 100, 4), (s.F_LT, 100, 1), (s.F_LTE, 100, 3),
])
def test_numeric_filters(operator, value, expected):
    f = (s.Filter(column=_col("Montant", 3), operator=operator, value=value),)
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == expected


def test_between_filter():
    f = (s.Filter(column=_col("Montant", 3), operator=s.F_BETWEEN, values=(100, 250)),)
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == 3


def test_between_with_inverted_bounds_is_refused():
    f = (s.Filter(column=_col("Montant", 3), operator=s.F_BETWEEN, values=(250, 100)),)
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f))
    assert e.value.code == s.ERR_INVALID_FILTER


@pytest.mark.parametrize("operator,value,expected", [
    (s.F_BEFORE, "2025-01-01", 2), (s.F_AFTER, "2025-01-01", 3),
])
def test_date_filters(operator, value, expected):
    f = (s.Filter(column=_col("Échéance", 4), operator=operator, value=value),)
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == expected


def test_empty_and_not_empty_filters():
    t = _table(["K", "V"], [["a", 1], ["b", None], ["c", 3]])
    loc = s.TableLocator(sheet_name="F", table_range="A1:B4", header_rows=(1,),
                         data_row_start=2, data_row_end=4)
    empty = (s.Filter(column=_col("V", 2), operator=s.F_IS_EMPTY),)
    filled = (s.Filter(column=_col("V", 2), operator=s.F_IS_NOT_EMPTY),)
    assert ex.execute(t, _gt(operation=s.OP_FILTERED_COUNT, filters=empty, locator=loc)).value == 1
    assert ex.execute(t, _gt(operation=s.OP_FILTERED_COUNT, filters=filled, locator=loc)).value == 2


def test_filters_are_conjunctive():
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="En cours"),
         s.Filter(column=_col("Montant", 3), operator=s.F_GT, value=75))
    assert ex.execute(_table(), _gt(operation=s.OP_FILTERED_COUNT, filters=f)).value == 2


def test_empty_cell_never_satisfies_a_value_predicate():
    t = _table(["K", "V"], [["a", None], ["b", "x"]])
    loc = s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                         data_row_start=2, data_row_end=3)
    f = (s.Filter(column=_col("V", 2), operator=s.F_NEQ, value="x"),)
    assert ex.execute(t, _gt(operation=s.OP_FILTERED_COUNT, filters=f, locator=loc)).value == 0


# --------------------------------------------------------------------------- #
# Valeurs typées particulières
# --------------------------------------------------------------------------- #
def test_negative_values_are_summed_correctly():
    """Excel stocke un négatif comptable comme un nombre négatif ; les parenthèses
    ne sont qu'un format d'affichage."""
    t = _table(["K", "V"], [["a", -100.5], ["b", 200]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    assert ex.execute(t, gt).value == Decimal("99.5")


# --------------------------------------------------------------------------- #
# Limite assumée : les colonnes numériques/dates STOCKÉES EN TEXTE sont refusées
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cells", [
    ["1 234,56", "2 000,00"],        # nombres en texte
    ["15/01/2025", "20/03/2025"],    # dates en texte
])
def test_numeric_or_date_columns_stored_as_text_are_refused(cells):
    """Réserve documentée du MVP.

    Un vrai classeur rend des types natifs ; une colonne textuelle qui *ressemble* à
    des nombres reste du texte. La convertir serait une devinette — et sur ce corpus,
    « 1,234 » vaut 1.234 ou 1234 selon la locale. On préfère perdre le cas.
    """
    t = _table(["K", "V"], [["a", cells[0]], ["b", cells[1]]])
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_UNSUPPORTED_CELL_TYPE


def test_percentages_are_aggregated_as_fractions():
    t = _table(["K", "V"], [["a", 0.10], ["b", 0.20]],
               number_formats={"v": "0.0%"})
    gt = _gt(operation=s.OP_SUM, target="V",
             locator=s.TableLocator(sheet_name="F", table_range="A1:B3", header_rows=(1,),
                                    data_row_start=2, data_row_end=3))
    assert ex.execute(t, gt).value == Decimal("0.3")


# --------------------------------------------------------------------------- #
# Preuve d'exécution (§22)
# --------------------------------------------------------------------------- #
def test_evidence_is_deterministic_and_reflects_the_selection():
    gt = _gt(operation=s.OP_SUM, target="Montant")
    a, b = ex.execute(_table(), gt), ex.execute(_table(), gt)
    assert a.evidence.to_dict() == b.evidence.to_dict()
    assert a.evidence.selected_row_count == 5
    assert len(a.evidence.result_sha256) == 64


def test_evidence_changes_when_the_selection_changes():
    plain = ex.execute(_table(), _gt(operation=s.OP_SUM, target="Montant"))
    f = (s.Filter(column=_col("Statut", 2), operator=s.F_EQ, value="En cours"),)
    filtered = ex.execute(_table(), _gt(operation=s.OP_FILTERED_SUM, target="Montant", filters=f))
    assert plain.evidence.selected_row_keys_sha256 != filtered.evidence.selected_row_keys_sha256
    assert plain.evidence.result_sha256 != filtered.evidence.result_sha256


# --------------------------------------------------------------------------- #
# Typage de colonne
# --------------------------------------------------------------------------- #
def test_column_types_are_inferred_per_column():
    types = ex.column_types(_table())
    assert types["statut"] == tt.TYPE_STRING
    assert types["montant"] == tt.TYPE_INTEGER


def test_integer_and_decimal_mix_unifies_to_decimal():
    t = _table(["K", "V"], [["a", 1], ["b", 2.5]])
    assert ex.column_types(t)["v"] == tt.TYPE_DECIMAL


# --------------------------------------------------------------------------- #
# Formule sans valeur en cache sur le chemin des extrêmes (défense en profondeur)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("operation,extra", [
    (s.OP_MINIMUM, {}),
    (s.OP_MAXIMUM, {}),
    (s.OP_ARGMIN, {"identity": ("K",)}),
    (s.OP_ARGMAX, {"identity": ("K",)}),
])
def test_extremes_refuse_a_formula_without_cached_value(operation, extra):
    """Chemin critique de vérité terrain : le marqueur ne doit jamais être converti.

    Sans garde explicite, `normalize_decimal` le signalerait en `unsupported_cell_type`
    — un code trompeur pour ce qui est en réalité une formule non recalculée.
    """
    t = _table(["K", "V"], [["a", 10], ["b", ex.FORMULA_NO_CACHE], ["c", 30]])
    loc = s.TableLocator(sheet_name="F", table_range="A1:B4", header_rows=(1,),
                         data_row_start=2, data_row_end=4)
    result = (s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value={})
              if extra else s.ExpectedResult(type=s.RESULT_DECIMAL, value=0))
    gt = _gt(operation=operation, target="V", locator=loc, result=result, **extra)
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(t, gt)
    assert e.value.code == s.ERR_FORMULA_RESULT_UNAVAILABLE


def test_extreme_guard_is_reached_even_when_typing_is_bypassed():
    """La garde vit dans `_extreme` lui-même, pas seulement en amont dans le typage."""
    rows = [ex.TableRow(row_number=2, values={"v": 10}),
            ex.TableRow(row_number=3, values={"v": ex.FORMULA_NO_CACHE})]
    with pytest.raises(ex.TabularExecutionError) as e:
        ex._extreme(rows, "v", s.MissingValuePolicy(), largest=True)
    assert e.value.code == s.ERR_FORMULA_RESULT_UNAVAILABLE

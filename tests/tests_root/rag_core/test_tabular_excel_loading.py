"""Chargement d'une table depuis les **octets** d'un classeur (§18-19, §44).

Classeurs SYNTHÉTIQUES construits à la volée : aucun document réel, aucune donnée
sensible, aucune DB, aucun retrieval. C'est le seul fichier de tests qui fait de l'I/O.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from openpyxl import Workbook

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark import tabular_types as tt


def _write(tmp_path, rows, *, sheet="Demandes", name="c.xlsx", merges=(), formats=None):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for line in rows:
        ws.append(line)
    for rng in merges:
        ws.merge_cells(rng)
    for coord, fmt in (formats or {}).items():
        ws[coord].number_format = fmt
    path = tmp_path / name
    wb.save(path)
    return path


def _loc(**kw):
    base = dict(sheet_name="Demandes", table_range="A1:C4", header_rows=(1,),
                data_row_start=2, data_row_end=4)
    base.update(kw)
    return s.TableLocator(**base)


# --------------------------------------------------------------------------- #
# En-têtes
# --------------------------------------------------------------------------- #
def test_simple_headers_and_values(tmp_path):
    p = _write(tmp_path, [["Dossier", "Statut", "Montant"],
                          ["D-1", "En cours", 100],
                          ["D-2", "Clos", 250],
                          ["D-3", "En cours", 50]])
    table = ex.load_table(p, _loc())
    assert table.headers == ("Dossier", "Statut", "Montant")
    assert len(table.rows) == 3
    assert table.rows[0].get("montant") == 100
    assert ex.column_types(table)["montant"] == tt.TYPE_INTEGER


def test_multi_row_headers_are_concatenated(tmp_path):
    """En-tête sur deux lignes : « Montant » + « 2025 » -> « Montant 2025 »."""
    p = _write(tmp_path, [["Dossier", "Montant", "Montant"],
                          ["", "2024", "2025"],
                          ["D-1", 10, 20],
                          ["D-2", 30, 40]])
    table = ex.load_table(p, _loc(table_range="A1:C4", header_rows=(1, 2),
                                  data_row_start=3, data_row_end=4))
    assert table.headers == ("Dossier", "Montant 2024", "Montant 2025")
    assert ex.execute(table, _gt_sum("Montant 2025", "A1:C4", (1, 2), 3, 4)).value == Decimal(60)


def _gt_sum(target, rng, header_rows, start, end, **kw):
    from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef
    return s.TabularAggregationGroundTruth(
        document_ref=StableDocumentRef(content_sha256="a" * 64),
        table_locator=s.TableLocator(sheet_name="Demandes", table_range=rng,
                                     header_rows=header_rows, data_row_start=start,
                                     data_row_end=end),
        aggregation=s.Aggregation(function=s.OP_SUM,
                                  target_column=s.ColumnRef(source_header=target)),
        expected_result=s.ExpectedResult(type=s.RESULT_DECIMAL, value=0),
        **kw)


def test_columns_beyond_z_are_handled(tmp_path):
    """Piège classique : AA vient après Z, pas après A."""
    headers = [f"Col{i}" for i in range(1, 29)]
    headers[27] = "Montant"           # colonne AB
    p = _write(tmp_path, [headers, [*range(1, 28), 500], [*range(1, 28), 300]])
    table = ex.load_table(p, _loc(table_range="A1:AB3", data_row_start=2, data_row_end=3))
    assert table.headers[27] == "Montant"
    assert table.column_index["montant"] == 28
    gt = _gt_sum("Montant", "A1:AB3", (1,), 2, 3)
    assert ex.execute(table, gt).value == Decimal(800)


def test_merged_header_cell_is_readable(tmp_path):
    """`read_only` masquerait la fusion : on ouvre volontairement sans ce mode."""
    p = _write(tmp_path, [["Dossier", "Montants", None],
                          ["", "2024", "2025"],
                          ["D-1", 10, 20]],
               merges=("B1:C1",))
    table = ex.load_table(p, _loc(table_range="A1:C3", header_rows=(1, 2),
                                  data_row_start=3, data_row_end=3))
    # La cellule fusionnée ne porte sa valeur que sur B1 ; C1 est vide côté openpyxl.
    assert table.headers[1] == "Montants 2024"
    assert table.headers[2] == "2025"


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
def test_native_dates_are_typed_as_dates(tmp_path):
    p = _write(tmp_path, [["Dossier", "Échéance"],
                          ["D-1", dt.date(2025, 1, 15)],
                          ["D-2", dt.date(2024, 11, 5)]])
    table = ex.load_table(p, _loc(table_range="A1:B3", data_row_start=2, data_row_end=3))
    assert ex.column_types(table)["échéance"] in (tt.TYPE_DATE, tt.TYPE_DATETIME)


def test_percentage_format_is_detected(tmp_path):
    p = _write(tmp_path, [["K", "Taux"], ["a", 0.125], ["b", 0.25]],
               formats={"B2": "0.0%", "B3": "0.0%"})
    table = ex.load_table(p, _loc(table_range="A1:B3", data_row_start=2, data_row_end=3))
    assert ex.column_types(table)["taux"] == tt.TYPE_PERCENTAGE


def test_decimal_values_keep_their_precision(tmp_path):
    p = _write(tmp_path, [["K", "V"], ["a", 0.1], ["b", 0.2]])
    table = ex.load_table(p, _loc(table_range="A1:B3", data_row_start=2, data_row_end=3))
    gt = _gt_sum("V", "A1:B3", (1,), 2, 3)
    assert ex.execute(table, gt).value == Decimal("0.3")


# --------------------------------------------------------------------------- #
# Formules (§20)
# --------------------------------------------------------------------------- #
def test_formula_without_cached_value_is_detected_not_read_as_empty(tmp_path):
    """openpyxl écrit la formule sans valeur en cache : sans la double lecture, la
    cellule passerait pour vide et la somme serait silencieusement fausse."""
    p = _write(tmp_path, [["K", "V"], ["a", 10], ["b", "=A2*2"]])
    table = ex.load_table(p, _loc(table_range="A1:B3", data_row_start=2, data_row_end=3))
    assert table.rows[1].get("v") is ex.FORMULA_NO_CACHE
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.execute(table, _gt_sum("V", "A1:B3", (1,), 2, 3))
    assert e.value.code == s.ERR_FORMULA_RESULT_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Population et bornes
# --------------------------------------------------------------------------- #
def test_total_row_is_excluded_from_a_real_workbook(tmp_path):
    p = _write(tmp_path, [["K", "V"], ["a", 10], ["b", 20], ["TOTAL", 30]])
    table = ex.load_table(p, _loc(table_range="A1:B4", data_row_start=2, data_row_end=4))
    assert ex.execute(table, _gt_sum("V", "A1:B4", (1,), 2, 4)).value == Decimal(30)


def test_locator_bounds_restrict_the_population(tmp_path):
    p = _write(tmp_path, [["K", "V"], ["a", 10], ["b", 20], ["c", 40]])
    table = ex.load_table(p, _loc(table_range="A1:B4", data_row_start=2, data_row_end=3))
    assert len(table.rows) == 2
    assert ex.execute(table, _gt_sum("V", "A1:B4", (1,), 2, 3)).value == Decimal(30)


def test_missing_sheet_is_refused(tmp_path):
    p = _write(tmp_path, [["K", "V"], ["a", 1]])
    with pytest.raises(ex.TabularExecutionError) as e:
        ex.load_table(p, _loc(sheet_name="Absente", table_range="A1:B2",
                              data_row_start=2, data_row_end=2))
    assert e.value.code == s.ERR_INVALID_TABLE_LOCATOR


# --------------------------------------------------------------------------- #
# Indépendance vis-à-vis du chunking (§47)
# --------------------------------------------------------------------------- #
def test_ground_truth_never_depends_on_chunks():
    """Garde structurelle : l'exécuteur n'importe ni Chunk ni le moindre modèle.

    L'indépendance au chunking n'est pas une propriété à mesurer mais une propriété
    à *garantir* — l'exécuteur ne peut pas dépendre d'un découpage qu'il ne voit pas.
    """
    source = (ex.__file__ or "")
    assert source.endswith("tabular_reference_executor.py")
    with open(source, encoding="utf-8") as fh:
        code = fh.read()
    for forbidden in ("models.chunk", "import Chunk", "Chunk.objects", "rag_core.models"):
        assert forbidden not in code, f"l'exécuteur référence {forbidden!r}"


def test_same_table_same_result_whatever_the_row_window(tmp_path):
    """Deux fenêtres de lecture différentes sur la même population -> même résultat."""
    rows = [["K", "V"]] + [[f"k{i}", i] for i in range(1, 11)]
    p = _write(tmp_path, rows)
    full = ex.load_table(p, _loc(table_range="A1:B11", data_row_start=2, data_row_end=11))
    wide = ex.load_table(p, _loc(table_range="A1:B20", data_row_start=2, data_row_end=11))
    a = ex.execute(full, _gt_sum("V", "A1:B11", (1,), 2, 11))
    b = ex.execute(wide, _gt_sum("V", "A1:B20", (1,), 2, 11))
    assert a.value == b.value == Decimal(55)
    assert a.evidence.result_sha256 == b.evidence.result_sha256

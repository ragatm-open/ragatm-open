"""Double calcul de la vérité terrain (§41) — l'exécuteur contre un calcul naïf.

`pandas` étant délibérément absent des dépendances du projet, le second chemin est une
réimplémentation **volontairement naïve et indépendante** : boucles explicites, aucune
réutilisation du code de l'exécuteur. Deux implémentations écrites différemment qui
convergent sur des centaines de cas donnent une garantie réelle ; deux appels au même
code n'en donneraient aucune.

Tests PURS : aucun classeur, aucune DB, aucun retrieval.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_reference_executor as ex
from apps.rag_core.benchmark.stable_ground_truth import StableDocumentRef

SHA = "a" * 64


# --------------------------------------------------------------------------- #
# Chemin indépendant : arithmétique naïve sur des listes Python
# --------------------------------------------------------------------------- #
def naive_count(rows: list[dict], predicate=None) -> int:
    total = 0
    for row in rows:
        if predicate is None or predicate(row):
            total += 1
    return total


def naive_sum(rows: list[dict], column: str, predicate=None) -> Decimal:
    total = Decimal(0)
    for row in rows:
        if predicate is not None and not predicate(row):
            continue
        value = row.get(column)
        if value is None:
            continue
        total += Decimal(str(value))
    return total


def naive_average(rows: list[dict], column: str) -> Decimal:
    values = [Decimal(str(r[column])) for r in rows if r.get(column) is not None]
    total = Decimal(0)
    for v in values:
        total += v
    return total / Decimal(len(values))


def naive_extreme(rows: list[dict], column: str, *, largest: bool) -> Decimal:
    best = None
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        dec = Decimal(str(value))
        if best is None or (dec > best if largest else dec < best):
            best = dec
    return best


# --------------------------------------------------------------------------- #
# Jeux de données synthétiques variés
# --------------------------------------------------------------------------- #
DATASETS = [
    [{"K": f"k{i}", "V": i, "C": "A" if i % 2 else "B"} for i in range(1, 12)],
    [{"K": f"k{i}", "V": i * 0.1, "C": "A" if i % 3 else "B"} for i in range(1, 21)],
    [{"K": f"k{i}", "V": -i * 7, "C": "A" if i % 5 else "B"} for i in range(1, 16)],
    [{"K": f"k{i}", "V": i * 1000, "C": "A" if i % 2 else "B"} for i in range(1, 9)],
    # Valeurs manquantes intercalées : le dénominateur de la moyenne doit s'ajuster.
    [{"K": f"k{i}", "V": (None if i % 4 == 0 else i * 3), "C": "A" if i % 2 else "B"}
     for i in range(1, 18)],
]


def _table(rows: list[dict]) -> ex.LoadedTable:
    return ex.build_table(["K", "V", "C"], [[r["K"], r["V"], r["C"]] for r in rows])


def _gt(rows: list[dict], *, operation, target=None, filters=()):
    end = 1 + len(rows)
    return s.TabularAggregationGroundTruth(
        document_ref=StableDocumentRef(content_sha256=SHA),
        table_locator=s.TableLocator(sheet_name="F", table_range=f"A1:C{end}",
                                     header_rows=(1,), data_row_start=2, data_row_end=end),
        aggregation=s.Aggregation(
            function=operation,
            target_column=s.ColumnRef(source_header=target) if target else None),
        expected_result=s.ExpectedResult(type=s.RESULT_INTEGER, value=0),
        filters=filters)


@pytest.mark.parametrize("rows", DATASETS, ids=range(len(DATASETS)))
def test_count_matches_the_independent_path(rows):
    got = ex.execute(_table(rows), _gt(rows, operation=s.OP_COUNT_ROWS))
    assert got.value == naive_count(rows)


@pytest.mark.parametrize("rows", DATASETS, ids=range(len(DATASETS)))
def test_sum_matches_the_independent_path(rows):
    got = ex.execute(_table(rows), _gt(rows, operation=s.OP_SUM, target="V"))
    assert got.value == naive_sum(rows, "V")


@pytest.mark.parametrize("rows", DATASETS, ids=range(len(DATASETS)))
def test_average_matches_the_independent_path(rows):
    got = ex.execute(_table(rows), _gt(rows, operation=s.OP_AVERAGE, target="V"))
    assert got.value == naive_average(rows, "V")


@pytest.mark.parametrize("rows", DATASETS, ids=range(len(DATASETS)))
def test_extremes_match_the_independent_path(rows):
    lo = ex.execute(_table(rows), _gt(rows, operation=s.OP_MINIMUM, target="V"))
    hi = ex.execute(_table(rows), _gt(rows, operation=s.OP_MAXIMUM, target="V"))
    assert lo.value == naive_extreme(rows, "V", largest=False)
    assert hi.value == naive_extreme(rows, "V", largest=True)


@pytest.mark.parametrize("rows", DATASETS, ids=range(len(DATASETS)))
def test_filtered_operations_match_the_independent_path(rows):
    f = (s.Filter(column=s.ColumnRef(source_header="C"), operator=s.F_EQ, value="A"),)
    predicate = lambda r: r["C"] == "A"          # noqa: E731 — lisibilité du test

    count = ex.execute(_table(rows), _gt(rows, operation=s.OP_FILTERED_COUNT, filters=f))
    assert count.value == naive_count(rows, predicate)

    if naive_count(rows, predicate) >= 2:
        total = ex.execute(_table(rows),
                           _gt(rows, operation=s.OP_FILTERED_SUM, target="V", filters=f))
        assert total.value == naive_sum(rows, "V", predicate)


def test_a_divergence_would_actually_be_caught():
    """Contrôle du contrôle : si les deux chemins divergeaient, le test le verrait.

    Un test de cohérence qui ne peut pas échouer ne prouve rien. On vérifie donc que
    le calcul naïf est bien sensible à une altération des données.
    """
    rows = DATASETS[0]
    altered = [{**r, "V": r["V"] + 1} for r in rows]
    assert naive_sum(rows, "V") != naive_sum(altered, "V")
    got = ex.execute(_table(rows), _gt(rows, operation=s.OP_SUM, target="V"))
    assert got.value != naive_sum(altered, "V")

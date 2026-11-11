"""Passe A — inventaire exhaustif : garanties de non-plafonnement et de non-production.

Les deux propriétés qui distinguent l'inventaire du générateur sont testées ici, parce
qu'elles sont **silencieuses** : un inventaire plafonné produit des chiffres d'apparence
normale mais mesure les plafonds ; un inventaire qui matérialise ne se voit qu'à la
mémoire consommée.

Tests PURS : tables en mémoire, aucun classeur, aucune DB, aucun retrieval.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_inventory as inv_mod
from apps.rag_core.benchmark import tabular_reference_executor as ex

SHA = "a" * 64

HEADERS = ["Dossier", "Statut", "Montant", "Échéance"]
DATA = [
    ["D-1", "En cours", 100, dt.date(2025, 1, 15)],
    ["D-2", "Clos",     250, dt.date(2025, 3, 20)],
    ["D-3", "En cours",  50, dt.date(2024, 11, 5)],
    ["D-4", "En cours", 300, dt.date(2025, 6, 30)],
    ["D-5", "Clos",     175, dt.date(2024, 2, 1)],
    ["D-6", "En cours", 425, dt.date(2025, 9, 9)],
]


def _table(headers=None, data=None):
    return ex.build_table(headers or HEADERS, data if data is not None else DATA)


def _source(sha=SHA, sheet="Demandes", rows=6, rng=None):
    return gen.TableSource(
        content_sha256=sha, filename="d.xlsx", path="/tmp/d.xlsx",
        locator=s.TableLocator(sheet_name=sheet, table_range=rng or f"A1:D{rows + 1}",
                               header_rows=(1,), data_row_start=2, data_row_end=rows + 1))


def _inventory(table=None, source=None):
    inv = inv_mod.Inventory()
    inv_mod.inventory_table(source or _source(), table or _table(), inv)
    return inv


# --------------------------------------------------------------------------- #
# Garantie 1 : aucun plafond de diversité
# --------------------------------------------------------------------------- #
def test_inventory_counts_more_cases_than_the_capped_generator():
    """Le générateur s'arrête à 3 cas par table ; l'inventaire compte tout le potentiel."""
    inv = _inventory()
    stats = gen.GenerationStats()
    produced, _ = gen.generate_for_table(_source(), _table(), rag_space="x",
                                         stats=stats, caps=gen.DEFAULT_CAPS)
    assert len(produced) <= gen.DEFAULT_CAPS["per_table"]
    assert inv.viable > len(produced), (inv.viable, len(produced))


def test_inventory_never_reports_a_cap_rejection():
    """`duplicate_aggregation_case` signale un plafond : il ne doit jamais apparaître."""
    inv = _inventory()
    assert s.ERR_DUPLICATE_CASE not in inv.rejection_codes


def test_inventory_sees_every_supported_operation_the_table_allows():
    """Sans plafond, une table riche expose toutes ses opérations, pas seulement 3."""
    inv = _inventory()
    assert len(inv.by_operation) >= 6, dict(inv.by_operation)


# --------------------------------------------------------------------------- #
# Garantie 2 : aucune matérialisation
# --------------------------------------------------------------------------- #
def test_inventory_keeps_counters_only_never_cases():
    """Aucun attribut de l'inventaire ne doit contenir un cas complet."""
    inv = _inventory()
    for name, value in vars(inv).items():
        if isinstance(value, (int, set)):
            continue
        for key in value:                      # Counter -> clés scalaires uniquement
            assert isinstance(key, (str, tuple)), (name, type(key))
            assert "ground_truth" not in str(key)


def test_inventory_memory_does_not_grow_with_the_number_of_tables():
    """Cent tables identiques n'ajoutent pas cent entrées : seuls les compteurs montent."""
    inv = inv_mod.Inventory()
    for i in range(100):
        inv_mod.inventory_table(_source(sheet=f"F{i}", rng="A1:D7"), _table(), inv)
    assert inv.tables_inspected == 100
    assert inv.viable >= 100
    # Les valeurs attendues se répètent : la redondance est visible, pas dupliquée.
    assert len(inv.expected_values) < inv.viable


# --------------------------------------------------------------------------- #
# Contenu du rapport (§ passe A)
# --------------------------------------------------------------------------- #
def test_report_contains_every_required_section():
    report = _inventory().to_dict()
    for key in ("tables_inspected", "tables_usable", "candidates", "viable", "rejected",
                "documents_seen", "sheets_seen", "by_operation", "rejection_codes",
                "table_sizes", "column_types", "column_roles", "difficulties",
                "filter_operators", "density", "expected_value_redundancy",
                "template_redundancy", "operations_absent", "operations_rare_under_2pct"):
        assert key in report, key


def test_density_reports_units_median_and_max():
    inv = inv_mod.Inventory()
    for i in range(3):
        inv_mod.inventory_table(_source(sha=f"{i:064d}", rng="A1:D7"), _table(), inv)
    density = inv.to_dict()["density"]["per_document"]
    assert density["units"] == 3
    assert density["max"] >= density["median"] >= 1


def test_expected_value_redundancy_is_measured():
    inv = inv_mod.Inventory()
    for i in range(5):
        inv_mod.inventory_table(_source(sheet=f"F{i}", rng="A1:D7"), _table(), inv)
    ev = inv.to_dict()["expected_value_redundancy"]
    assert ev["distinct_values"] >= 1
    assert 0 < ev["share_of_top_10_pct"] <= 100


def test_report_values_are_not_sensitive():
    """Le rapport est versionnable : ni question, ni valeur de cellule brute."""
    blob = str(_inventory().to_dict())
    assert "Dossier" not in blob or "D-1" not in blob
    assert "Dans la feuille" not in blob        # aucune question générée


# --------------------------------------------------------------------------- #
# Rejets et tailles
# --------------------------------------------------------------------------- #
def test_short_table_is_counted_and_rejected():
    inv = _inventory(_table(["K", "V"], [["a", 1], ["b", 2]]),
                     _source(rows=2, rng="A1:B3"))
    assert inv.tables_inspected == 1 and inv.tables_usable == 0
    assert inv.rejection_codes[s.ERR_TRIVIAL_SINGLE_ROW] == 1


def test_table_size_buckets():
    assert inv_mod.size_bucket(6) == "5-9"
    assert inv_mod.size_bucket(50) == "25-99"
    assert inv_mod.size_bucket(5000) == "2000+"
    assert inv_mod.size_bucket(2) == "<5"


def test_unusable_table_is_rejected_without_candidates():
    inv = _inventory(_table(["col1", "col2"], [[i, i] for i in range(1, 7)]),
                     _source(rows=6, rng="A1:B7"))
    assert inv.tables_usable == 0 and inv.candidates == 0
    assert inv.rejection_codes[s.ERR_CONSTANT_TARGET_COLUMN] == 1


# --------------------------------------------------------------------------- #
# Détection des opérations rares / absentes
# --------------------------------------------------------------------------- #
def test_absent_operations_are_listed():
    """Une table sans colonne d'identité ne peut pas produire d'argmin/argmax."""
    table = _table(["Statut", "Montant"],
                   [["En cours", 10], ["Clos", 20], ["En cours", 30],
                    ["Clos", 40], ["En cours", 50], ["Clos", 60]])
    inv = _inventory(table, _source(rows=6, rng="A1:B7"))
    report = inv.to_dict()
    assert s.OP_ARGMIN in report["operations_absent"]
    assert s.OP_ARGMAX in report["operations_absent"]


@pytest.mark.parametrize("op", sorted(s.SUPPORTED_OPERATIONS))
def test_every_supported_operation_is_reportable(op):
    """Le rapport doit pouvoir nommer chaque opération du MVP, présente ou absente."""
    report = _inventory().to_dict()
    assert op in report["by_operation"] or op in report["operations_absent"]

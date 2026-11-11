"""Métriques `tabular_aggregation` — le résultat calculé, jamais un chunk (§30-32).

Tests PURS : aucune DB, aucun retrieval, aucun classeur.
"""
from __future__ import annotations

from decimal import Decimal

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_metrics as m


# --------------------------------------------------------------------------- #
# Égalité décimale
# --------------------------------------------------------------------------- #
def test_decimal_equality_ignores_trailing_zeros():
    """12.50 et 12.5 sont le même montant — un test de chaîne échouerait ici."""
    assert m.numeric_exact_match("12.50", Decimal("12.5")).correct
    assert m.numeric_exact_match(Decimal("100"), 100).correct


def test_decimal_inequality_reports_the_absolute_error():
    v = m.numeric_exact_match(Decimal("99"), Decimal("100"))
    assert not v.correct
    assert v.absolute_error == Decimal(1)


def test_non_numeric_prediction_is_incorrect_not_crashing():
    v = m.numeric_exact_match("je ne sais pas", Decimal("100"))
    assert not v.correct and "non numérique" in v.detail


# --------------------------------------------------------------------------- #
# Tolérances
# --------------------------------------------------------------------------- #
def test_absolute_tolerance():
    assert m.numeric_tolerance("100.004", "100", tolerance="0.01").correct
    assert not m.numeric_tolerance("100.02", "100", tolerance="0.01").correct


def test_relative_error_scales_with_magnitude():
    """1 € d'écart est négligeable sur 1 M€, rédhibitoire sur 10 €."""
    assert m.relative_error_match("1000001", "1000000", tolerance="0.001").correct
    assert not m.relative_error_match("11", "10", tolerance="0.001").correct


def test_relative_error_survives_a_zero_expected_value():
    v = m.relative_error_match("1", "0", tolerance="0.001")
    assert not v.correct          # pas de division par zéro, pas de crash


# --------------------------------------------------------------------------- #
# Ensembles et séquences
# --------------------------------------------------------------------------- #
def test_set_match_ignores_order_and_case():
    assert m.set_match(["B", "a"], ["A", "b"]).correct


def test_set_match_reports_what_is_missing():
    v = m.set_match(["a"], ["a", "b"])
    assert not v.correct and "manquants" in v.detail


def test_ordered_match_is_sensitive_to_order():
    assert m.ordered_match(["a", "b"], ["a", "b"]).correct
    assert not m.ordered_match(["b", "a"], ["a", "b"]).correct


# --------------------------------------------------------------------------- #
# Identifiant de ligne (argmin/argmax)
# --------------------------------------------------------------------------- #
def test_row_identifier_requires_every_expected_column():
    assert m.row_identifier_match({"Produit": "ABC", "Année": "2025"},
                                  {"Produit": "ABC", "Année": "2025"}).correct
    v = m.row_identifier_match({"Produit": "ABC"}, {"Produit": "ABC", "Année": "2025"})
    assert not v.correct and "absentes" in v.detail


def test_row_identifier_rejects_a_wrong_value():
    v = m.row_identifier_match({"Produit": "XYZ"}, {"Produit": "ABC"})
    assert not v.correct and "divergentes" in v.detail


def test_row_identifier_requires_a_mapping():
    assert not m.row_identifier_match("ABC", {"Produit": "ABC"}).correct


# --------------------------------------------------------------------------- #
# Aiguillage par le cas
# --------------------------------------------------------------------------- #
def test_evaluate_uses_the_comparison_declared_by_the_case():
    exact = s.ExpectedResult(type=s.RESULT_DECIMAL, value=Decimal("100"),
                             comparison=s.CMP_EXACT_DECIMAL)
    assert m.evaluate("100.00", exact).correct
    assert not m.evaluate("100.01", exact).correct

    tolerant = s.ExpectedResult(type=s.RESULT_DECIMAL, value=Decimal("100"),
                                comparison=s.CMP_ABSOLUTE_TOLERANCE, tolerance="0.05")
    assert m.evaluate("100.01", tolerant).correct


def test_integer_results_are_compared_numerically():
    r = s.ExpectedResult(type=s.RESULT_INTEGER, value=17)
    assert m.evaluate(17, r).correct
    assert m.evaluate("17", r).correct
    assert not m.evaluate(18, r).correct


def test_row_identifier_is_routed_regardless_of_declared_comparison():
    r = s.ExpectedResult(type=s.RESULT_ROW_IDENTIFIER, value={"Produit": "ABC"})
    assert m.evaluate({"Produit": "abc"}, r).correct


# --------------------------------------------------------------------------- #
# Agrégation des scores
# --------------------------------------------------------------------------- #
def test_aggregate_scores_counts_by_comparison():
    verdicts = [
        m.numeric_exact_match("1", "1"), m.numeric_exact_match("1", "2"),
        m.exact_match("a", "a"),
    ]
    agg = m.aggregate_scores(verdicts)
    assert agg["cases"] == 3 and agg["correct"] == 2
    assert agg["by_comparison"][s.CMP_EXACT_DECIMAL] == {"total": 2, "correct": 1}


def test_aggregate_scores_on_empty_input_does_not_divide_by_zero():
    assert m.aggregate_scores([])["accuracy"] == Decimal(0)


# --------------------------------------------------------------------------- #
# Garde méthodologique (§32)
# --------------------------------------------------------------------------- #
def test_module_exposes_no_recall_or_mrr_as_a_primary_metric():
    """Recall@k / MRR ne doivent pas devenir le critère de réussite d'un cas calculé."""
    exported = {n for n in dir(m) if not n.startswith("_")}
    assert not {n for n in exported if "recall" in n.lower() or "mrr" in n.lower()}

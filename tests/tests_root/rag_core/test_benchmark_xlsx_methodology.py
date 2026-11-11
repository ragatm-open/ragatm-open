"""Filtres méthodologiques du benchmark Excel — règles déterministes, sans LLM."""
from __future__ import annotations

import pytest

from apps.rag_core.benchmark import xlsx_methodology as m


# --------------------------------------------------------------------------- #
# §40 en-têtes génériques
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("header,generic", [
    ("Code SI", False), ("Montant 2025", False), ("Nom du produit", False),
    ("col1", True), ("COL_2", True), ("column 3", True), ("colonne_4", True),
    ("champ1", True), ("field2", True), ("Unnamed: 0", True), ("Unnamed:1", True),
    ("", True), ("None", True), ("Sans titre", True), ("-", True),
])
def test_is_generic_header(header, generic):
    assert m.is_generic_header(header) is generic


# --------------------------------------------------------------------------- #
# §41 agrégations / comptages / formules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q,agg,expected", [
    ("Dans la feuille S, quel est le Code SI du produit ABC ?", None, None),
    ("Combien de lignes comporte le tableau ?", None, m.REQUIRES_COUNTING),
    ("Quel est le nombre de dossiers actifs ?", None, m.REQUIRES_COUNTING),
    ("Quelle est la somme des montants ?", None, m.REQUIRES_AGGREGATION),
    ("Quelle est la moyenne des valeurs ?", None, m.REQUIRES_AGGREGATION),
    ("Quelle contrepartie a le montant maximal ?", None, m.REQUIRES_AGGREGATION),
    ("valeur", "count", m.REQUIRES_COUNTING),
    ("valeur", "max", m.REQUIRES_AGGREGATION),
])
def test_detect_aggregation(q, agg, expected):
    assert m.detect_aggregation_requirement(q, aggregate=agg) == expected


def test_materialized_total_is_lookup():
    # « à la ligne TOTAL » = valeur matérialisée, PAS un calcul.
    q = "Dans la feuille « Bilan », quel est le « Montant » à la ligne « TOTAL » ?"
    assert m.detect_aggregation_requirement(q) is None
    assert m.classify_task_type(question=q) == m.TASK_RETRIEVAL_LOOKUP


def test_formula_and_multiregion():
    assert m.requires_formula_evaluation("Recalcule la formule de la cellule")
    assert m.detect_multi_region_requirement("Compare les valeurs 2024 et 2025") == m.REQUIRES_MULTI_REGION_REASONING


# --------------------------------------------------------------------------- #
# §44 autonomie de la question / déictique / réponse incluse
# --------------------------------------------------------------------------- #
def test_deictic_and_self_contained():
    assert m.has_deictic_reference("Quelle est la valeur correspondante ?")
    assert m.has_deictic_reference("Quelle est la valeur de cette ligne ?")
    assert not m.is_question_self_contained("Quelle est la valeur ?", sheet_name="S",
                                            row_identifier="ABC", measure="Code SI")
    assert m.is_question_self_contained(
        "Dans la feuille S, quel est le Code SI du produit ABC ?",
        sheet_name="S", row_identifier="ABC", measure="Code SI")


def test_question_contains_answer():
    assert m.question_contains_answer("Le code SI vaut X1 pour ABC ?", ["X1"])
    assert not m.question_contains_answer("Quel est le code SI de ABC ?", ["X1"])
    # Valeur triviale dans la question -> pas un révélateur.
    assert not m.question_contains_answer("Le statut est-il actif ?", ["Actif"])


# --------------------------------------------------------------------------- #
# §45 valeurs triviales / vides
# --------------------------------------------------------------------------- #
def test_trivial_and_empty_values():
    for v in ("Oui", "Non", "0", "1", "N/A", "OK", "Actif"):
        assert m.is_trivial_value(v)
    assert not m.is_trivial_value("X1")
    for v in ("", "  ", "-", "—", "N/A", "null"):
        assert m.is_empty_value(v)
    assert not m.is_empty_value("42")


# --------------------------------------------------------------------------- #
# §42 ambiguïté de ligne
# --------------------------------------------------------------------------- #
def test_row_ambiguity():
    rows = [["ABC", "1"], ["DEF", "2"], ["ABC", "3"]]
    assert m.detect_row_ambiguity(rows, identity_idx=0, row_identifier="ABC")   # dupliqué
    assert not m.detect_row_ambiguity(rows, identity_idx=0, row_identifier="DEF")  # unique
    # Composite unique (Produit+Année).
    rows2 = [["ABC", "2024", "x"], ["ABC", "2025", "y"]]
    assert not m.detect_row_ambiguity(
        rows2, identity_idx=0, row_identifier="ABC",
        composite={"Produit": "ABC", "Année": "2025"},
        composite_indices={"Produit": 0, "Année": 1})


# --------------------------------------------------------------------------- #
# §43 ambiguïté de colonne
# --------------------------------------------------------------------------- #
def test_measure_ambiguity():
    headers = ["Libellé", "Montant 2024", "Montant 2025"]
    # Question qui NE nomme PAS l'en-tête complet -> ambigu.
    assert m.detect_measure_ambiguity("Montant 2025", headers,
                                      "Quel est le montant du poste X ?")
    # Question nommant l'en-tête complet -> désambiguïsé (§21).
    assert not m.detect_measure_ambiguity("Montant 2025", headers,
                                          "Quelle est la valeur de « Montant 2025 » du poste X ?")
    # En-tête non ambigu.
    assert not m.detect_measure_ambiguity("Code SI", ["Nom", "Code SI"], "…")


# --------------------------------------------------------------------------- #
# §46 task types
# --------------------------------------------------------------------------- #
def test_supported_task_types():
    assert m.TASK_RETRIEVAL_LOOKUP in m.SUPPORTED_TASK_TYPES
    assert m.TASK_UNSUPPORTED not in m.SUPPORTED_TASK_TYPES


# --------------------------------------------------------------------------- #
# Classification complète — acceptation / redirection / rejet / revue
# --------------------------------------------------------------------------- #
def _classify(**over):
    base = dict(
        question="Dans la feuille « Produits », quel est le « Code SI » du produit « ABC » ?",
        headers=["Produit", "Code SI"], rows=[["ABC", "X1"], ["DEF", "X2"]],
        identity_idx=0, row_identifier="ABC", measure="Code SI", expected_values=["X1"],
    )
    base.update(over)
    return m.classify_xlsx_case(**base)


def test_classify_accepted():
    r = _classify()
    assert r.accepted and r.decision == m.DEC_ACCEPTED and r.task_type == m.TASK_RETRIEVAL_LOOKUP
    assert r.rejection_codes == ()


def test_classify_redirect_aggregation():
    r = _classify(question="Combien de lignes ont le statut ACTIF ?", aggregate="count")
    assert not r.accepted and r.decision == m.DEC_REDIRECT_AGGREGATION
    assert r.rejection_codes == (m.REQUIRES_COUNTING,)


def test_classify_reject_generic_header():
    r = _classify(headers=["col1", "col2"], measure="col2",
                  question="Dans la feuille « S », quel est le « col2 » du « col1 » « ABC » ?",
                  headers_inferred=True)
    assert not r.accepted and r.decision == m.DEC_REJECTED
    assert m.GENERIC_OR_INFERRED_HEADER in r.rejection_codes


def test_classify_reject_ambiguous_row():
    r = _classify(rows=[["ABC", "X1"], ["ABC", "X9"]])   # ABC dupliqué
    assert not r.accepted and m.AMBIGUOUS_ROW_IDENTIFIER in r.rejection_codes


def test_classify_reject_not_self_contained():
    r = _classify(question="Quelle est la valeur correspondante ?")
    assert not r.accepted
    assert m.DEICTIC_OR_CONTEXT_DEPENDENT in r.rejection_codes
    assert m.QUESTION_NOT_SELF_CONTAINED in r.rejection_codes


def test_classify_trivial_value_unique_locator_accepted():
    r = _classify(measure="Statut", headers=["Produit", "Statut"],
                  question="Dans la feuille « Produits », quel est le « Statut » du produit « ABC » ?",
                  expected_values=["Actif"])
    assert r.accepted and "trivial_value_unique_locator" in r.warnings


def test_classify_trivial_value_ambiguous_locator_review():
    r = _classify(measure="Statut", headers=["Produit", "Statut"],
                  rows=[["ABC", "Actif"], ["ABC", "Inactif"]],
                  question="Dans la feuille « Produits », quel est le « Statut » du produit « ABC » ?",
                  expected_values=["Actif"])
    assert not r.accepted   # ligne ambiguë -> rejet (hard)
    assert m.AMBIGUOUS_ROW_IDENTIFIER in r.rejection_codes


def test_determinism_same_codes_same_order():
    r1 = _classify(headers=["col1", "col2"], measure="col2", headers_inferred=True,
                   question="Quelle est la valeur correspondante ?")
    r2 = _classify(headers=["col1", "col2"], measure="col2", headers_inferred=True,
                   question="Quelle est la valeur correspondante ?")
    assert r1.rejection_codes == r2.rejection_codes and r1.decision == r2.decision

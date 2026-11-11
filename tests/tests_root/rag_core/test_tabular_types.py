"""Types et normalisation du benchmark `tabular_aggregation`.

Tests PURS : aucune DB, aucun retrieval, aucun holdout, aucun fichier Excel réel.
Toutes les valeurs sont synthétiques.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_types as tt


# --------------------------------------------------------------------------- #
# Decimal, jamais float
# --------------------------------------------------------------------------- #
def test_float_does_not_leak_binary_noise():
    """Le piège central : Decimal(0.1) hérite du bruit binaire, Decimal('0.1') non."""
    assert tt.normalize_decimal(12345.67) == Decimal("12345.67")
    assert tt.normalize_decimal(0.1) + tt.normalize_decimal(0.2) == Decimal("0.3")


def test_int_and_decimal_pass_through():
    assert tt.normalize_decimal(42) == Decimal(42)
    assert tt.normalize_decimal(Decimal("3.50")) == Decimal("3.50")


def test_scale_quantizes_half_up_not_half_even():
    """ROUND_HALF_UP est une convention explicite : 2.345 -> 2.35, pas 2.34.

    Entrées `Decimal`/`float` et non `str` : la chaîne "2.345" est légitimement
    refusée comme ambiguë (2345 en format français), cf. le test dédié.
    """
    assert tt.normalize_decimal(Decimal("2.345"), scale=2) == Decimal("2.35")
    assert tt.normalize_decimal(2.345, scale=2) == Decimal("2.35")
    assert tt.normalize_decimal(Decimal("2.355"), scale=2) == Decimal("2.36")
    assert tt.normalize_decimal(1, scale=2) == Decimal("1.00")


def test_boolean_is_not_a_number():
    with pytest.raises(tt.TabularTypeError) as e:
        tt.normalize_decimal(True)
    assert e.value.code == tt.ERR_UNSUPPORTED_CELL_TYPE


# --------------------------------------------------------------------------- #
# Analyse de nombres textuels
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("1234", "1234"),
    ("1 234,56", "1234.56"),            # espace fine insécable
    ("1 234,56", "1234.56"),            # espace insécable
    ("1.234,56", "1234.56"),            # format FR : dernier séparateur = décimal
    ("1,234.56", "1234.56"),            # format US : idem
    ("1.234.567", "1234567"),           # séparateurs répétés = milliers
    ("12 345,67 €", "12345.67"),
    ("-42,5", "-42.5"),
    ("(1 234,56)", "-1234.56"),         # convention comptable
    ("1234,50-", "-1234.50"),           # signe suffixe (exports SAP/AS400)
    ("+7", "7"),
    ("0,5", "0.5"),
    (",5", "0.5"),
])
def test_parse_number_formats(raw, expected):
    assert tt.parse_number(raw) == Decimal(expected)


def test_percentage_is_stored_as_a_fraction():
    assert tt.parse_number("12,5 %") == Decimal("0.125")
    assert tt.parse_number("100%") == Decimal("1")


@pytest.mark.parametrize("raw,expected", [
    ("0.001", "0.001"), ("1234.5", "1234.5"), ("-7", "-7"), (".5", "0.5"), ("1e-3", "0.001"),
])
def test_canonical_decimal_accepts_what_we_author_ourselves(raw, expected):
    """Une tolérance ou un seuil écrit par nous n'est jamais ambigu : point décimal imposé."""
    assert tt.parse_canonical_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1,234", "1 234,56", "12,5 %", "abc"])
def test_canonical_decimal_refuses_locale_dependent_forms(raw):
    """La forme canonique n'accepte ni virgule, ni espace, ni pourcentage."""
    with pytest.raises(tt.TabularTypeError):
        tt.parse_canonical_decimal(raw)


@pytest.mark.parametrize("raw", ["1,234", "1.234", "12,000"])
def test_single_separator_with_three_digits_is_ambiguous(raw):
    """1,234 vaut 1.234 ou 1234 selon la locale : refus plutôt que devinette."""
    with pytest.raises(tt.TabularTypeError) as e:
        tt.parse_number(raw)
    assert e.value.code == tt.ERR_AMBIGUOUS_NUMBER_FORMAT


@pytest.mark.parametrize("raw", ["abc", "", "12abc", "--5"])
def test_non_numeric_strings_are_refused(raw):
    with pytest.raises(tt.TabularTypeError):
        tt.parse_number(raw)


def test_excel_error_values_are_never_silently_text():
    for err in ("#DIV/0!", "#N/A", "#REF!"):
        with pytest.raises(tt.TabularTypeError) as e:
            tt.parse_number(err)
        assert e.value.code == tt.ERR_EXCEL_ERROR_VALUE


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def test_native_date_and_datetime():
    assert tt.normalize_date(dt.date(2025, 4, 3)) == "2025-04-03"
    assert tt.normalize_date(dt.datetime(2025, 4, 3)) == "2025-04-03"
    assert tt.normalize_date(dt.datetime(2025, 4, 3, 14, 30)) == "2025-04-03T14:30:00"


@pytest.mark.parametrize("raw,expected", [
    ("2025-04-03", "2025-04-03"),
    ("2025-04-03T14:30:00", "2025-04-03T14:30:00"),
    ("2025-04-03 14:30", "2025-04-03T14:30:00"),
    ("25/12/2025", "2025-12-25"),   # jour > 12 : non ambigu
    ("31/01/2025", "2025-01-31"),
])
def test_unambiguous_date_strings(raw, expected):
    assert tt.normalize_date(raw) == expected


def test_ambiguous_date_is_refused_without_locale():
    """03/04/2025 : 3 avril ou 4 mars ? Sans locale, c'est une erreur."""
    with pytest.raises(tt.TabularTypeError) as e:
        tt.normalize_date("03/04/2025")
    assert e.value.code == tt.ERR_AMBIGUOUS_DATE


def test_explicit_locale_resolves_the_ambiguity():
    assert tt.normalize_date("03/04/2025", locale=tt.LOCALE_FR) == "2025-04-03"
    assert tt.normalize_date("03/04/2025", locale=tt.LOCALE_US) == "2025-03-04"


def test_unknown_locale_is_refused():
    with pytest.raises(tt.TabularTypeError) as e:
        tt.normalize_date("03/04/2025", locale="de")
    assert e.value.code == tt.ERR_UNSUPPORTED_LOCALE


@pytest.mark.parametrize("raw", ["32/01/2025", "2025-13-01", "pas une date"])
def test_invalid_dates_are_refused(raw):
    with pytest.raises(tt.TabularTypeError) as e:
        tt.normalize_date(raw)
    assert e.value.code in (tt.ERR_INVALID_DATE, tt.ERR_AMBIGUOUS_DATE)


# --------------------------------------------------------------------------- #
# Typage
# --------------------------------------------------------------------------- #
def test_boolean_is_typed_before_integer():
    """Piège Python : isinstance(True, int) est vrai — l'ordre des tests compte."""
    assert tt.infer_cell_type(True) == tt.TYPE_BOOLEAN
    assert tt.infer_cell_type(1) == tt.TYPE_INTEGER


def test_percentage_needs_the_number_format():
    """0.125 seul est un décimal ; c'est le format d'affichage qui en fait un %."""
    assert tt.infer_cell_type(0.125) == tt.TYPE_DECIMAL
    assert tt.infer_cell_type(0.125, number_format="0.0%") == tt.TYPE_PERCENTAGE


@pytest.mark.parametrize("value,expected", [
    (None, tt.TYPE_EMPTY),
    ("   ", tt.TYPE_EMPTY),
    ("texte", tt.TYPE_STRING),
    (42, tt.TYPE_INTEGER),
    (4.2, tt.TYPE_DECIMAL),
    (dt.date(2025, 1, 1), tt.TYPE_DATE),
    (dt.datetime(2025, 1, 1, 9, 0), tt.TYPE_DATETIME),
])
def test_infer_cell_type(value, expected):
    assert tt.infer_cell_type(value) == expected


def test_zero_and_false_are_not_empty():
    """Confusion classique : 0 est une valeur, pas une absence de valeur."""
    assert not tt.is_empty(0)
    assert not tt.is_empty(False)
    assert tt.is_empty(None)
    assert tt.is_empty("  ")


def test_excel_error_in_typing_is_flagged():
    with pytest.raises(tt.TabularTypeError) as e:
        tt.infer_cell_type("#VALUE!")
    assert e.value.code == tt.ERR_EXCEL_ERROR_VALUE


# --------------------------------------------------------------------------- #
# Normalisation de cellule et texte
# --------------------------------------------------------------------------- #
def test_normalize_cell_by_declared_type():
    assert tt.normalize_cell(None, cell_type=tt.TYPE_EMPTY) is None
    assert tt.normalize_cell("1 234,56", cell_type=tt.TYPE_DECIMAL) == Decimal("1234.56")
    assert tt.normalize_cell(dt.date(2025, 4, 3), cell_type=tt.TYPE_DATE) == "2025-04-03"
    assert tt.normalize_cell("oui", cell_type=tt.TYPE_BOOLEAN) is True
    assert tt.normalize_cell("  a   b ", cell_type=tt.TYPE_STRING) == "a b"


def test_unknown_declared_type_is_refused():
    with pytest.raises(tt.TabularTypeError) as e:
        tt.normalize_cell("x", cell_type="quantum")
    assert e.value.code == tt.ERR_UNSUPPORTED_CELL_TYPE


def test_text_normalization_collapses_nbsp_but_keeps_case_and_accents():
    assert tt.normalize_text("  Coût  total  ") == "Coût total"
    assert tt.fold_for_match("Coût TOTAL") == tt.fold_for_match("coût total")


def test_coerce_for_aggregation_returns_none_on_empty_and_raises_on_garbage():
    assert tt.coerce_for_aggregation(None, column_type=tt.TYPE_DECIMAL) is None
    assert tt.coerce_for_aggregation("42", column_type=tt.TYPE_DECIMAL) == Decimal(42)
    # Une cellule non numérique dans une colonne numérique est une anomalie : on lève.
    with pytest.raises(tt.TabularTypeError):
        tt.coerce_for_aggregation("N/D", column_type=tt.TYPE_DECIMAL)

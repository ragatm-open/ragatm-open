"""Parseurs typés de configuration (`config.parsers`).

Règle vérifiée ici : une valeur invalide LÈVE. On n'échoue jamais silencieusement
vers le défaut — c'est l'incident `RAG_HYBRID_ENABLED=True` qui a motivé la règle.
"""
from __future__ import annotations

import pytest

from config import parsers
from config.parsers import ConfigValueError


# --------------------------------------------------------------------------
# Booléens
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " True ", "yes", "on", '"true"'])
def test_bool_true_literals(raw):
    assert parsers.parse_bool("X", raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " off ", "no", "'0'"])
def test_bool_false_literals(raw):
    assert parsers.parse_bool("X", raw) is False


@pytest.mark.parametrize("raw", ["", "maybe", "2", "oui", "vrai", None])
def test_bool_invalid_raises(raw):
    with pytest.raises(ConfigValueError):
        parsers.parse_bool("X", raw)


def test_bool_error_names_the_variable():
    with pytest.raises(ConfigValueError) as excinfo:
        parsers.parse_bool("MA_VARIABLE", "peut-être")
    assert "MA_VARIABLE" in str(excinfo.value)


# --------------------------------------------------------------------------
# Entiers et flottants
# --------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [("42", 42), (" 7 ", 7), ("-3", -3), ('"8"', 8)])
def test_int_valid(raw, expected):
    assert parsers.parse_int("X", raw) == expected


@pytest.mark.parametrize("raw", ["", "3.5", "huit", "1_2_3x", None])
def test_int_invalid_raises(raw):
    with pytest.raises(ConfigValueError):
        parsers.parse_int("X", raw)


@pytest.mark.parametrize(("raw", "expected"), [("0.75", 0.75), (" 1 ", 1.0), ("-0.5", -0.5)])
def test_float_valid(raw, expected):
    assert parsers.parse_float("X", raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "zéro", "1,5"])
def test_float_invalid_raises(raw):
    with pytest.raises(ConfigValueError):
        parsers.parse_float("X", raw)


# --------------------------------------------------------------------------
# URLs — validation syntaxique uniquement, jamais d'appel réseau
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["http://ai-gateway:8100", "https://exemple.test/chemin", "redis://redis:6379/2"],
)
def test_url_valid(raw):
    assert parsers.parse_url("X", raw) == raw


def test_url_empty_allowed_by_default():
    assert parsers.parse_url("X", "") == ""


@pytest.mark.parametrize("raw", ["<hôte-gateway>:8100", "ftp://hote", "http://", "pas-une-url"])
def test_url_invalid_raises(raw):
    with pytest.raises(ConfigValueError):
        parsers.parse_url("X", raw)


def test_url_empty_refused_when_required():
    with pytest.raises(ConfigValueError):
        parsers.parse_url("X", "", allow_empty=False)


# --------------------------------------------------------------------------
# Listes et énumérations
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a,b,c", ["a", "b", "c"]),
        (" a , b ", ["a", "b"]),
        ("a,,b", ["a", "b"]),
        ("", []),
        ("   ", []),
    ],
)
def test_list_parsing(raw, expected):
    assert parsers.parse_list("X", raw) == expected


def test_enum_is_case_insensitive_and_canonicalises():
    assert parsers.parse_enum("X", "OpenAI", ("openai", "none")) == "openai"


def test_enum_rejects_unknown_value():
    with pytest.raises(ConfigValueError) as excinfo:
        parsers.parse_enum("X", "ollama", ("openai", "none"))
    assert "openai" in str(excinfo.value)


# --------------------------------------------------------------------------
# Bornes
# --------------------------------------------------------------------------
def test_bounds_accept_value_inside_range():
    parsers.check_bounds("X", 30, minimum=1, maximum=300)


@pytest.mark.parametrize(("value", "low", "high"), [(0, 1, 300), (301, 1, 300), (-1, 0, None)])
def test_bounds_reject_value_outside_range(value, low, high):
    with pytest.raises(ConfigValueError):
        parsers.check_bounds("X", value, minimum=low, maximum=high)


def test_bounds_ignore_non_numeric_values():
    parsers.check_bounds("X", "texte", minimum=1, maximum=2)


# --------------------------------------------------------------------------
# Sérialisation canonique
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "value_type", "expected"),
    [
        (True, "bool", "true"),
        (False, "bool", "false"),
        (30, "int", "30"),
        (["a", "b"], "list", "a,b"),
        ("a, b", "list", "a,b"),
        (None, "str", ""),
    ],
)
def test_serialize_is_canonical(value, value_type, expected):
    assert parsers.serialize(value, value_type) == expected


def test_parse_then_serialize_normalises_ambiguous_input():
    """« yes » entre, « true » sort : la forme persistée est canonique."""
    parsed = parsers.parse("X", "yes", "bool")
    assert parsers.serialize(parsed, "bool") == "true"

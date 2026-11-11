"""Tests unitaires de la taxonomie d'erreurs du retrieval (Lot P1-A1). Purs."""
import pytest

from apps.rag_core.services.query.errors import (
    RetrievalConfigurationError,
    RetrievalDependencyError,
    RetrievalInvariantError,
    RetrievalLegUnavailable,
    is_programming_error,
    raise_if_programming_error,
)


@pytest.mark.parametrize(
    "exc",
    [TypeError("x"), AttributeError("x"), NameError("x"), KeyError("x"),
     IndexError("x"), ImportError("x"), NotImplementedError("x"), AssertionError("x")],
)
def test_python_bugs_are_programming_errors(exc):
    assert is_programming_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("timeout"), ValueError("bad data"), ConnectionError("net"),
     TimeoutError("slow"), OSError("io")],
)
def test_expected_failures_are_not_programming_errors(exc):
    assert is_programming_error(exc) is False


def test_typed_dependency_and_config_not_programming():
    assert is_programming_error(RetrievalDependencyError("dep")) is False
    assert is_programming_error(RetrievalConfigurationError("cfg")) is False
    assert is_programming_error(RetrievalLegUnavailable("no fallback")) is False


def test_invariant_is_programming():
    assert is_programming_error(RetrievalInvariantError("bug")) is True


def test_django_programming_error_is_programming():
    from django.db import ProgrammingError

    assert is_programming_error(ProgrammingError("column does not exist")) is True


def test_django_operational_error_is_dependency():
    from django.db import OperationalError

    assert is_programming_error(OperationalError("connection timed out")) is False


def test_raise_if_programming_error_propagates_and_keeps_cause():
    original = TypeError("bad signature")
    with pytest.raises(RetrievalInvariantError) as ei:
        raise_if_programming_error(original, leg="fts")
    assert ei.value.leg == "fts"
    assert ei.value.__cause__ is original


def test_raise_if_programming_error_is_noop_for_dependency():
    # Ne lève pas pour une panne attendue.
    assert raise_if_programming_error(RuntimeError("timeout"), leg="vector") is None

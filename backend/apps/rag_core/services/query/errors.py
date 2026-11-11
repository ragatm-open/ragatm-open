"""Taxonomie d'erreurs des chemins critiques de retrieval (Lot P1-A1).

Objectif : distinguer, dans les chemins critiques, un **bug de programmation / un
invariant brisé** (à propager — FAIL CLOSED) d'une **panne de dépendance attendue**
(dégradable si un fallback légitime existe) et d'une **erreur de configuration**.

Ce module est volontairement minimal : pas d'exception par fonction, pas de nouvelle
dépendance. Les valeurs stables consommées par la trace P1-A0 vivent dans
``services/query/trace.py`` ; ici on ne définit que la hiérarchie et le classifieur.

Règle de conservation de cause : toute conversion se fait via ``raise ... from exc``.
"""
from __future__ import annotations


class RetrievalError(Exception):
    """Base des erreurs contrôlées du pipeline de retrieval."""

    def __init__(self, message: str = "", *, leg: str | None = None) -> None:
        super().__init__(message)
        self.leg = leg


class RetrievalDependencyError(RetrievalError):
    """Panne d'une dépendance EXTERNE attendue (SQL/DB indispo, timeout, provider).

    Dégradable UNIQUEMENT si un fallback légitime existe ; sinon, le pipeline doit
    échouer explicitement (FAIL CLOSED) plutôt que produire un résultat trompeur.
    """


class RetrievalConfigurationError(RetrievalError):
    """Configuration invalide (ex. dimension d'embedding incompatible).

    N'est JAMAIS convertie en fallback silencieux : elle remonte (FAIL CLOSED).
    """


class RetrievalInvariantError(RetrievalError):
    """Invariant brisé / bug de programmation dans un chemin critique.

    Toujours propagée : un bug ne doit pas être transformé en ``no_answer`` / ``[]`` /
    fallback silencieux.
    """


class RetrievalLegUnavailable(RetrievalDependencyError):
    """Un leg REQUIS est indisponible et aucun fallback valide n'existe (FAIL CLOSED)."""


# --- Classification --------------------------------------------------------
#
# Types Python qui, dans un chemin critique, signalent un BUG (et non une panne
# attendue). Volontairement conservateur : ``ValueError`` n'y figure PAS (souvent une
# donnée invalide légitime, pas un bug), ni les erreurs DB opérationnelles (timeout /
# connexion = dépendance).
_PROGRAMMING_ERROR_TYPES: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    UnboundLocalError,
    KeyError,
    IndexError,
    ImportError,
    NotImplementedError,
    AssertionError,
)


def _is_db_programming_error(exc: BaseException) -> bool:
    """``django.db.ProgrammingError`` = SQL erroné (colonne/syntaxe) => bug de code.

    Distinct de ``OperationalError`` (connexion/timeout = dépendance) que l'on veut
    pouvoir dégrader.
    """
    try:
        from django.db import ProgrammingError
    except Exception:  # pragma: no cover - Django toujours présent en prod
        return False
    return isinstance(exc, ProgrammingError)


def is_programming_error(exc: BaseException) -> bool:
    """True si ``exc`` est un bug de programmation / invariant brisé (FAIL CLOSED).

    - ``RetrievalInvariantError`` -> True (déjà classée) ;
    - ``RetrievalDependencyError`` / ``RetrievalConfigurationError`` -> False (attendues) ;
    - types Python de bug (TypeError, AttributeError, ...) -> True ;
    - ``django.db.ProgrammingError`` -> True.
    """
    if isinstance(exc, RetrievalInvariantError):
        return True
    if isinstance(exc, (RetrievalDependencyError, RetrievalConfigurationError)):
        return False
    if isinstance(exc, _PROGRAMMING_ERROR_TYPES):
        return True
    return _is_db_programming_error(exc)


def raise_if_programming_error(exc: BaseException, *, leg: str | None = None) -> None:
    """Propage (FAIL CLOSED) si ``exc`` est un bug ; sinon retourne (l'appelant dégrade).

    Convertit en ``RetrievalInvariantError`` typée en conservant la cause, pour que la
    frontière HTTP/SSE la traite comme une erreur interne contrôlée et que la trace
    P1-A0 finisse en ``error``.
    """
    if is_programming_error(exc):
        raise RetrievalInvariantError(
            f"programming error in retrieval leg {leg!r}", leg=leg
        ) from exc

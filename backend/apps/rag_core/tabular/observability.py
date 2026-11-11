"""Observabilité du moteur tabulaire — compteurs et journalisation sans contenu.

**Ce projet n'a pas de backend de métriques** (ni Prometheus, ni statsd). Inventer une
dépendance pour ce lot serait un choix d'infrastructure déguisé en observabilité. Les
compteurs vivent donc en mémoire du processus et sont émis en **journal structuré**,
au format `clé=valeur` déjà utilisé par l'orchestrateur de retrieval. Un exporteur
pourra les lire plus tard sans rien changer aux appelants.

La règle qui gouverne ce module : **rien de ce qui est journalisé ne doit permettre de
reconstituer le contenu d'un document.** Concrètement, ne sortent jamais d'ici :

- une valeur de cellule, même tronquée ;
- le SQL avec ses paramètres (les paramètres SONT les valeurs de filtre) ;
- le chemin réel du fichier sur le disque ;
- le nom d'un document auquel l'appelant n'a pas accès ;
- la question de l'utilisateur en clair.

Ce qui sort : des identifiants techniques, des hashes, des durées, des comptages, des
codes d'erreur stables. De quoi diagnostiquer une lenteur ou un taux de refus, jamais
de quoi lire un tableau.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from contextlib import contextmanager
from time import perf_counter

logger = logging.getLogger(__name__)

#: Compteurs cumulés du processus. Un `Counter` sous verrou : le moteur est appelé
#: depuis des threads de requête, et un `+=` sur un dict n'est pas atomique.
_COUNTERS: Counter[str] = Counter()
_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Noms de compteurs — STABLES (exposés, testés, éventuellement scrapés un jour)
# --------------------------------------------------------------------------- #
QUERIES_TOTAL = "tabular_queries_total"
QUERIES_OK = "tabular_queries_ok_total"
QUERIES_CLARIFICATION = "tabular_queries_clarification_total"
QUERIES_UNSUPPORTED = "tabular_queries_unsupported_total"
QUERIES_ERROR = "tabular_queries_error_total"
MEMORY_LIMIT_ERRORS = "tabular_memory_limit_errors_total"
TIMEOUT_ERRORS = "tabular_timeout_errors_total"
PERMISSION_DENIALS = "tabular_permission_denials_total"
CAPACITY_LIMIT = "tabular_capacity_limit"
CAPACITY_REJECTIONS = "tabular_capacity_rejections_total"
CAPACITY_WAIT_SECONDS = "tabular_capacity_wait_seconds"
TIMEOUT_CAPACITY_WAIT = "tabular_timeout_total_capacity_wait"

#: Histogrammes réduits à leur somme et leur compte : sans backend, des buckets
#: seraient du folklore. La moyenne et le total suffisent à repérer une dérive.
DURATION_QUERY = "tabular_query_duration_seconds"
DURATION_CATALOG = "tabular_catalog_duration_seconds"
DURATION_EXECUTION = "tabular_execution_duration_seconds"
ROWS_SCANNED = "tabular_rows_scanned"
RESULT_ROWS = "tabular_result_rows"

#: Correspondance statut → compteur. Table explicite plutôt qu'un `f"..._{status}"`
#: construit dynamiquement : un nom de métrique dérivé d'une variable est un nom qu'on
#: ne peut plus chercher dans le code.
_STATUS_COUNTERS = {
    "ok": QUERIES_OK,
    "clarification_required": QUERIES_CLARIFICATION,
    "unsupported": QUERIES_UNSUPPORTED,
    "limit_exceeded": QUERIES_ERROR,
    "forbidden": QUERIES_ERROR,
    "error": QUERIES_ERROR,
}

#: Codes d'erreur méritant leur propre compteur — ce sont les signaux d'exploitation
#: (saturation, requête trop coûteuse, tentative d'accès hors périmètre).
_CODE_COUNTERS = {
    "memory_exceeded": MEMORY_LIMIT_ERRORS,
    "query_timeout": TIMEOUT_ERRORS,
    "permission_denied": PERMISSION_DENIALS,
    "tabular_capacity_exceeded": CAPACITY_REJECTIONS,
}


def incr(name: str, value: int | float = 1) -> None:
    with _LOCK:
        _COUNTERS[name] += value


def set_gauge(name: str, value: int | float) -> None:
    """Fixe une valeur ABSOLUE (et non cumulée).

    Nécessaire pour les horodatages « dernier succès » et les âges : incrémenter
    n'a aucun sens pour ces mesures, et un compteur monotone ne permettrait pas
    d'alerter sur « la purge n'a jamais tourné », qui est le mode de panne le plus
    discret d'une tâche planifiée.
    """
    with _LOCK:
        _COUNTERS[name] = value


def snapshot() -> dict[str, float]:
    """Copie des compteurs. Sert aux tests et à un futur exporteur."""
    with _LOCK:
        return dict(_COUNTERS)


def reset() -> None:
    """Remet les compteurs à zéro. **Tests uniquement.**"""
    with _LOCK:
        _COUNTERS.clear()


@contextmanager
def timed(name: str):
    """Chronomètre une étape et l'ajoute au cumul correspondant."""
    started = perf_counter()
    try:
        yield
    finally:
        incr(name, perf_counter() - started)
        incr(f"{name}_count")


def record_query(*, status: str, code: str = "", operation: str = "",
                 duration_s: float = 0.0, rows_scanned: int = 0,
                 result_rows: int = 0, plan_hash: str = "",
                 document_id: str = "", rag_space_id: str = "") -> None:
    """Enregistre l'issue d'une requête tabulaire.

    Les seuls champs identifiants journalisés sont des UUID et un hash de plan : ils
    permettent de relier un incident à une exécution tracée en base sans révéler quoi
    que ce soit du contenu.
    """
    incr(QUERIES_TOTAL)
    incr(_STATUS_COUNTERS.get(status, QUERIES_ERROR))
    if code in _CODE_COUNTERS:
        incr(_CODE_COUNTERS[code])
    if duration_s:
        incr(DURATION_QUERY, duration_s)
        incr(f"{DURATION_QUERY}_count")
    if rows_scanned:
        incr(ROWS_SCANNED, rows_scanned)
    if result_rows:
        incr(RESULT_ROWS, result_rows)

    logger.info(
        "tabular.query status=%s code=%s operation=%s duration_ms=%d "
        "rows_scanned=%d result_rows=%d plan_hash=%s document_id=%s rag_space_id=%s",
        status, code or "-", operation or "-", int(duration_s * 1000),
        rows_scanned, result_rows, (plan_hash or "-")[:16],
        document_id or "-", rag_space_id or "-",
    )

"""Capacité concurrente — une limite réellement APPLIQUÉE, pas seulement documentée.

Jusqu'ici la capacité était mesurée et recommandée, mais rien n'empêchait une rafale de
créer un nombre arbitraire d'exécutions simultanées. Une capacité documentée n'est pas
une capacité garantie.

---- Portée : ce que ce module borne, et ce qu'il ne borne pas ----

Le mécanisme est un **sémaphore en mémoire de processus**. Il borne donc les exécutions
d'UN processus, pas celles du déploiement.

`backend/entrypoint.sh` lance gunicorn avec `GUNICORN_WORKERS` (défaut **2**). La
capacité réellement appliquée sur un déploiement est donc :

    capacité effective = GUNICORN_WORKERS × TABULAR_MAX_CONCURRENT_QUERIES

Avec les défauts : 2 × 2 = **4** exécutions simultanées, pas 2.

Ce n'est pas un défaut d'implémentation, c'est une propriété qu'il faut connaître. Une
limite distribuée (Redis) donnerait une borne globale, mais `DJANGO_CACHE_URL` est
**optionnel** dans ce projet : un limiteur qui dépendrait de Redis se dégraderait en
silence là où il n'est pas configuré — c'est-à-dire qu'il ne limiterait plus rien, sans
que personne ne s'en aperçoive. Entre une borne locale honnête et une borne globale
parfois absente, la première est plus sûre.

`effective_capacity()` calcule la capacité totale pour que le déploiement puisse être
dimensionné en connaissance de cause.

---- Attente ----

L'attente consomme **la même échéance absolue** que le reste de la requête. Elle n'ouvre
jamais un second budget : une requête qui a déjà brûlé neuf secondes sur dix ne dispose
que d'une seconde pour obtenir un slot, pas de dix de plus.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from django.conf import settings

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import observability as OBS

logger = logging.getLogger(__name__)

#: Borne haute de configuration. Au-delà, la valeur est manifestement une erreur de
#: saisie : la mesure montre que le débit plafonne bien avant.
MAX_ALLOWED = 32
#: Valeur retenue par défaut, fondée sur la mesure : 2 workers donnent 1,73× le débit
#: séquentiel pour 211 Mo, contre 289 Mo à 10 workers pour seulement 1,66× de plus.
DEFAULT_LIMIT = 2


class _Limiter:
    """Sémaphore borné, avec compteurs. Un seul par processus."""

    def __init__(self, limit: int):
        self.limit = limit
        self._sem = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0

    # -- état observable ------------------------------------------------- #
    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def waiting(self) -> int:
        with self._lock:
            return self._waiting

    def _enter_wait(self) -> None:
        with self._lock:
            self._waiting += 1

    def _leave_wait(self, *, acquired: bool) -> None:
        with self._lock:
            self._waiting -= 1
            if acquired:
                self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1
        # `BoundedSemaphore` lève si on relâche plus qu'on n'a acquis : une double
        # libération est un bug qu'on veut voir, pas un compteur qui dérive.
        self._sem.release()

    def acquire(self, timeout: float) -> bool:
        self._enter_wait()
        acquired = False
        try:
            acquired = self._sem.acquire(timeout=max(0.0, timeout))
        finally:
            self._leave_wait(acquired=acquired)
        return acquired


_LIMITER: _Limiter | None = None
_LIMITER_LOCK = threading.Lock()


def configured_limit() -> int:
    """Limite validée. Une valeur aberrante est corrigée ET signalée, jamais subie."""
    brut = getattr(settings, "TABULAR_MAX_CONCURRENT_QUERIES", DEFAULT_LIMIT)
    try:
        valeur = int(brut)
    except (TypeError, ValueError):
        logger.warning("tabular.capacity valeur non numérique (%r) -> défaut %d",
                       brut, DEFAULT_LIMIT)
        return DEFAULT_LIMIT
    if valeur < 1:
        logger.warning("tabular.capacity valeur %d < 1 -> 1 (0 bloquerait tout)", valeur)
        return 1
    if valeur > MAX_ALLOWED:
        logger.warning("tabular.capacity valeur %d > %d -> plafonnée", valeur, MAX_ALLOWED)
        return MAX_ALLOWED
    return valeur


def get_limiter() -> _Limiter:
    """Limiteur du processus, créé une fois. Recréé si la limite change (tests)."""
    global _LIMITER
    limite = configured_limit()
    with _LIMITER_LOCK:
        if _LIMITER is None or _LIMITER.limit != limite:
            _LIMITER = _Limiter(limite)
        return _LIMITER


def reset() -> None:
    """Réinitialise le limiteur. **Tests uniquement.**"""
    global _LIMITER
    with _LIMITER_LOCK:
        _LIMITER = None


def effective_capacity(workers: int | None = None) -> dict:
    """Capacité RÉELLE du déploiement, pas seulement celle du processus.

    Le sémaphore borne un processus ; gunicorn en lance plusieurs. Présenter la limite
    par processus comme une borne globale serait un mensonge par omission.
    """
    import os

    if workers is None:
        try:
            workers = int(os.getenv("GUNICORN_WORKERS", "2"))
        except (TypeError, ValueError):
            workers = 2
    limite = configured_limit()
    return {
        "per_process_limit": limite,
        "workers": workers,
        "effective_total": limite * max(1, workers),
        "scope": "process",
        "globally_enforced": False,
    }


@contextmanager
def slot(deadline, *, wait: bool = True):
    """Acquiert un slot, le libère **toujours**.

    L'attente est bornée par l'échéance globale : `deadline.remaining_seconds()`, et
    jamais par un délai propre. Si l'échéance expire pendant l'attente, la requête sort
    en `query_timeout` avec la phase `capacity_wait` — pas en
    `tabular_capacity_exceeded`, qui désigne un refus de capacité, pas un budget épuisé.
    Confondre les deux rendrait les tableaux de bord illisibles.
    """
    limiteur = get_limiter()

    # Un budget déjà épuisé ne doit pas obtenir de slot, même s'il en reste un de
    # libre : entamer un travail qu'on refusera à la première vérification gaspille
    # de la capacité au moment précis où elle est rare.
    if deadline.expired():
        OBS.incr(OBS.TIMEOUT_CAPACITY_WAIT)
        raise E.TabularLimitExceeded(
            E.ERR_QUERY_TIMEOUT,
            "budget déjà épuisé avant l'acquisition d'un slot",
            phase="capacity_wait")

    restant = deadline.remaining_seconds() if wait else 0.0

    OBS.incr(OBS.CAPACITY_LIMIT, 0)  # garantit la présence de la clé
    acquis = limiteur.acquire(timeout=restant)

    if not acquis:
        # Deux issues distinctes selon la CAUSE : budget épuisé, ou capacité saturée
        # alors qu'il restait du temps.
        if deadline.expired():
            OBS.incr(OBS.TIMEOUT_CAPACITY_WAIT)
            raise E.TabularLimitExceeded(
                E.ERR_QUERY_TIMEOUT,
                "budget épuisé en attente d'un slot d'exécution",
                phase="capacity_wait")
        OBS.incr(OBS.CAPACITY_REJECTIONS)
        raise E.TabularLimitExceeded(
            E.ERR_TABULAR_CAPACITY_EXCEEDED,
            "capacité d'exécution tabulaire momentanément saturée")

    attente = deadline.elapsed_seconds()
    OBS.incr(OBS.CAPACITY_WAIT_SECONDS, attente)
    try:
        yield limiteur
    finally:
        # `finally` et non `else` : le slot doit être rendu quel que soit le sort de la
        # requête — succès, clarification, refus, timeout, exception DuckDB ou Python.
        # Un slot perdu réduit définitivement la capacité du processus.
        limiteur._release()

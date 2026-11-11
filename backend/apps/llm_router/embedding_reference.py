"""Référence des identités de pipeline publiée par l'AI Gateway — ``GET /health/embeddings``.

## Pourquoi ce module existe

Le ``pipeline_id`` rendu par ``POST /v1/embeddings`` est une identité **opaque**
(``emb-v2:<empreinte>``) : rien ne s'en déduit, ni la tâche, ni le modèle. Pour savoir
si l'identifiant reçu est bien celui que la Gateway déclare pour *(modèle logique,
tâche)*, RagATM a besoin d'une **référence** : c'est le registre que la Gateway publie
elle-même sur ``/health/embeddings`` — pour chaque profil, ``pipeline_id_query``,
``pipeline_id_document``, ``expected_digest``, ``dimension``, ``identity_verified``.

La référence n'est **pas** une table codée en dur : les identifiants changent avec les
poids ou la version du préfixage, et une constante locale se périmerait exactement
comme l'ancienne hypothèse de format. Elle n'est pas non plus **inférée** de
l'identifiant opaque.

## Cycle de vie

- Lecture **par processus**, mise en cache ``REFERENCE_TTL_SECONDS`` : un lot
  d'embeddings ne déclenche pas un appel ``/health`` supplémentaire ; un worker en
  régime nominal lit la référence quelques fois par heure.
- Expirée, elle est **relue** ; si la relecture échoue, l'erreur est levée. Une
  référence périmée n'est jamais réutilisée au-delà de son TTL : accepter un
  identifiant sur la foi d'une référence qu'on ne peut plus confirmer serait un
  repli silencieux.
- Sur **désaccord** entre l'identifiant reçu et la référence en cache, l'appelant
  demande une relecture forcée (``force_refresh=True``) : une Gateway redéployée
  entre deux lectures ne produit pas de faux rejet, et un identifiant réellement
  inconnu reste rejeté.

## Fail-closed

Toute impossibilité d'établir la référence lève ``GatewayEmbeddingsError`` avec un code
distinct (``pipeline_reference_unavailable`` / ``_invalid`` / ``_missing_model`` /
``_missing_task``). Aucun défaut permissif.

## Journalisation

Identifiants, compteurs et durées seulement — aucun texte, aucun vecteur (§22).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import requests

from .embedding_errors import GatewayEmbeddingsError
from .embedding_pipeline import ALLOWED_TASKS, TASK_DOCUMENT, TASK_QUERY

logger = logging.getLogger("rag.embeddings.reference")

#: Durée de validité d'une référence lue (secondes). Constante de module, pas un
#: réglage : la valeur n'a pas vocation à différer entre environnements, et un
#: réglage de plus serait un angle mort de parité pour un gain nul.
REFERENCE_TTL_SECONDS: float = 300.0

#: Chemin du registre côté Gateway.
REFERENCE_PATH = "/health/embeddings"

#: Horloge monotone, remplaçable par les tests pour simuler l'expiration.
_now: Callable[[], float] = time.monotonic

_COUNTERS: dict[str, int] = {
    "embedding_pipeline_reference_fetch_total": 0,
    "embedding_pipeline_reference_errors_total": 0,
    "embedding_pipeline_reference_cache_hits_total": 0,
}


def _bump(name: str, value: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + int(value)


def counters_snapshot() -> dict[str, int]:
    return dict(_COUNTERS)


def reset_counters() -> None:
    for k in list(_COUNTERS):
        _COUNTERS[k] = 0


@dataclass(frozen=True)
class PipelineReference:
    """Identités déclarées par la Gateway pour UN modèle logique."""

    model_logical: str
    #: ``task -> pipeline_id`` ; une tâche absente ou malformée n'y figure pas.
    pipeline_ids: Mapping[str, str] = field(default_factory=dict)
    expected_digest: str = ""
    dimension: int | None = None
    identity_verified: bool = False
    state: str = ""

    def pipeline_id_for(self, task: str) -> str | None:
        return self.pipeline_ids.get(task)


# Clé : base_url normalisée. Valeur : (instant de lecture, {model_logical: référence}).
_CACHE: dict[str, tuple[float, dict[str, PipelineReference]]] = {}
_LOCK = threading.Lock()


def reset_reference_cache() -> None:
    """Oublie toute référence lue — réservé aux tests et aux diagnostics."""
    with _LOCK:
        _CACHE.clear()


def reference_cache_snapshot() -> dict[str, tuple[float, dict[str, PipelineReference]]]:
    with _LOCK:
        return dict(_CACHE)


def _fail(message: str, *, code: str) -> GatewayEmbeddingsError:
    _bump("embedding_pipeline_reference_errors_total")
    return GatewayEmbeddingsError(message, code=code)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_pipeline_reference(data: Any) -> dict[str, PipelineReference]:
    """Transforme la réponse ``/health/embeddings`` en références par modèle logique.

    Refuse (``pipeline_reference_invalid``) tout ce qui empêcherait une comparaison
    sûre : objet racine non conforme, liste ``models`` absente, entrée non objet,
    modèle logique vide ou **dupliqué** (deux déclarations pour un même modèle
    rendraient la référence ambiguë — on ne choisit pas, on refuse).

    Un identifiant de tâche absent ou malformé (non chaîne, vide) n'invalide pas la
    référence entière : la tâche est simplement non déclarée pour ce modèle, et
    l'appelant qui la demande obtient ``pipeline_reference_missing_task``.
    """
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise _fail(
            "référence de pipeline Gateway invalide : objet `models` attendu",
            code="pipeline_reference_invalid",
        )
    out: dict[str, PipelineReference] = {}
    for entry in data["models"]:
        if not isinstance(entry, dict):
            raise _fail(
                "référence de pipeline Gateway invalide : entrée de modèle non conforme",
                code="pipeline_reference_invalid",
            )
        model_logical = _clean_str(entry.get("model_logical"))
        if not model_logical:
            raise _fail(
                "référence de pipeline Gateway invalide : `model_logical` vide",
                code="pipeline_reference_invalid",
            )
        if model_logical in out:
            raise _fail(
                f"référence de pipeline Gateway invalide : modèle {model_logical!r} déclaré deux fois",
                code="pipeline_reference_invalid",
            )
        ids: dict[str, str] = {}
        for task, key in ((TASK_QUERY, "pipeline_id_query"), (TASK_DOCUMENT, "pipeline_id_document")):
            pid = _clean_str(entry.get(key))
            if pid:
                ids[task] = pid
        dimension = entry.get("dimension")
        out[model_logical] = PipelineReference(
            model_logical=model_logical,
            pipeline_ids=ids,
            expected_digest=_clean_str(entry.get("expected_digest")).lower(),
            dimension=int(dimension) if isinstance(dimension, int) and not isinstance(dimension, bool) else None,
            identity_verified=entry.get("identity_verified") is True,
            state=_clean_str(entry.get("state")),
        )
    return out


def fetch_pipeline_reference(*, base_url: str, timeout_s: float) -> dict[str, PipelineReference]:
    """Lecture RÉSEAU de la référence. Lève, ne devine pas."""
    url = f"{base_url.rstrip('/')}{REFERENCE_PATH}"
    _bump("embedding_pipeline_reference_fetch_total")
    start = time.perf_counter()
    try:
        resp = requests.get(url, timeout=timeout_s)
    except requests.RequestException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(
            "embedding_pipeline_reference",
            extra={"status": "error", "error_class": type(exc).__name__, "gateway_latency_ms": latency_ms},
        )
        # « temporarily unavailable » : marqueur reconnu par la classification des
        # erreurs transitoires de l'ingestion (retry Celery, jamais FAILED définitif
        # sur un incident réseau de la seule référence).
        raise _fail(
            f"référence de pipeline Gateway ({REFERENCE_PATH}) temporarily unavailable : "
            f"{type(exc).__name__}",
            code="pipeline_reference_unavailable",
        ) from exc
    latency_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code >= 400:
        logger.warning(
            "embedding_pipeline_reference",
            extra={"status": "error", "error_class": f"HTTP{resp.status_code}", "gateway_latency_ms": latency_ms},
        )
        transient = " temporarily unavailable" if resp.status_code >= 500 else ""
        raise _fail(
            f"référence de pipeline Gateway ({REFERENCE_PATH}) HTTP {resp.status_code}{transient}",
            code="pipeline_reference_unavailable",
        )
    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning(
            "embedding_pipeline_reference",
            extra={"status": "error", "error_class": "InvalidJSON", "gateway_latency_ms": latency_ms},
        )
        raise _fail(
            f"référence de pipeline Gateway ({REFERENCE_PATH}) : réponse non JSON",
            code="pipeline_reference_unavailable",
        ) from exc

    models = parse_pipeline_reference(data)
    logger.info(
        "embedding_pipeline_reference",
        extra={"status": "ok", "n_models": len(models), "gateway_latency_ms": latency_ms},
    )
    return models


def pipeline_reference_for(
    model_logical: str,
    *,
    base_url: str,
    timeout_s: float,
    force_refresh: bool = False,
) -> PipelineReference:
    """Référence du modèle logique, depuis le cache si elle est encore valide.

    - cache valide et ``force_refresh=False`` → aucun réseau ;
    - cache absent, expiré, ou relecture forcée → lecture réseau ; un échec lève
      (``pipeline_reference_unavailable``/``_invalid``) sans jamais servir une valeur
      périmée ;
    - modèle absent de la référence → ``pipeline_reference_missing_model``.
    """
    key = base_url.rstrip("/")
    now = _now()
    with _LOCK:
        cached = _CACHE.get(key)
        fresh = cached is not None and (now - cached[0]) < REFERENCE_TTL_SECONDS
        if fresh and not force_refresh:
            _bump("embedding_pipeline_reference_cache_hits_total")
            models = cached[1]
        else:
            models = None
    if models is None:
        models = fetch_pipeline_reference(base_url=key, timeout_s=timeout_s)
        with _LOCK:
            _CACHE[key] = (_now(), models)

    ref = models.get((model_logical or "").strip())
    if ref is None:
        raise _fail(
            f"référence de pipeline Gateway : modèle logique {model_logical!r} non déclaré "
            f"(déclarés : {sorted(models)})",
            code="pipeline_reference_missing_model",
        )
    return ref


__all__ = [
    "ALLOWED_TASKS",
    "PipelineReference",
    "REFERENCE_PATH",
    "REFERENCE_TTL_SECONDS",
    "counters_snapshot",
    "fetch_pipeline_reference",
    "parse_pipeline_reference",
    "pipeline_reference_for",
    "reference_cache_snapshot",
    "reset_counters",
    "reset_reference_cache",
]

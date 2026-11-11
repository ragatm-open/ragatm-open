"""Résolveur RUNTIME de la configuration Core — inerte tant que le flag est fermé.

Distinct du résolveur diagnostic (``config_resolver.resolve_effective_config``,
redacté, pour l'admin). Ici on renvoie la VRAIE valeur typée destinée à
l'exécution.

**Consommateurs réels** (depuis PR-G3 — le docstring précédent affirmait à tort
que ce module n'était « branché nulle part », ce qui n'est plus vrai depuis
plusieurs lots) :

- ``apps.llm_router.router`` — activation Gateway, repli, provider, modèle ;
- ``apps.llm_router.embeddings`` — activation Gateway, URL, timeout : donc
  l'**indexation**, y compris dans les workers Celery ;
- ``apps.llm_router.egress`` — politique de repli.

Autrement dit : quand ``CORE_CONFIG_RUNTIME_ENABLED=1``, une ligne d'overlay
change le comportement de génération ET d'ingestion. C'est ce qui justifie le
durcissement ``config_policy``.

Garanties :

- ``CORE_CONFIG_RUNTIME_ENABLED=False`` (défaut) => renvoie STRICTEMENT la valeur
  settings/default, sans aucun accès DB ni cache ;
- ``RAG_EMBEDDING_DIM`` est exclu DUREMENT : toujours ``settings.RAG_EMBEDDING_DIM``,
  quel que soit le flag ou un éventuel override ;
- les clés d'INFRASTRUCTURE (URL de la Gateway, activation de la politique IA,
  provider et modèle primaires) ne sont plus éditables au registre : une ligne
  préexistante en base est **neutralisée** à la résolution, et ``config_policy``
  la refuse en défense en profondeur ;
- une valeur d'overlay qui violerait un invariant de démarrage n'est JAMAIS
  retournée : on retombe sur les settings en journalisant (``config_policy``) ;
- toute erreur cache/DB => fallback settings/default, jamais d'exception runtime ;
- aucune valeur sensible n'est journalisée.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache

from .config_policy import guard_runtime_value
from .config_resolver import (
    _coerce,
    allowed_max_scope,
    coerce_value_for_storage,
    current_environment,
    key_spec,
    scope_rank,
)

logger = logging.getLogger("ragatm.core.runtime_config")

# Clé d'epoch partagée (cache Django/Redis). Incrémentée à chaque écriture
# d'override => invalidation cohérente HTTP + workers Celery.
EPOCH_CACHE_KEY = "ragatm_core_config_epoch"
_CACHE_TTL_SECONDS = 30

# Clés JAMAIS pilotées par l'overlay runtime (exclusion dure).
EXCLUDED_KEYS = frozenset({"RAG_EMBEDDING_DIM"})


def runtime_enabled() -> bool:
    return bool(getattr(settings, "CORE_CONFIG_RUNTIME_ENABLED", False))


def current_epoch() -> int:
    """Epoch courant (0 si absent ou cache indisponible)."""
    try:
        v = cache.get(EPOCH_CACHE_KEY)
        return int(v) if v is not None else 0
    except Exception:
        return 0


def bump_epoch() -> None:
    """Incrémente l'epoch (invalidation). Ne lève jamais : une écriture ne doit
    pas échouer parce que le cache est indisponible."""
    try:
        if not cache.add(EPOCH_CACHE_KEY, 1, None):
            try:
                cache.incr(EPOCH_CACHE_KEY)
            except ValueError:
                cache.set(EPOCH_CACHE_KEY, 1, None)
    except Exception:
        # Fallback sûr : TTL backstop assure la convergence même sans bump.
        logger.warning("core_config_epoch_bump_failed")


def _settings_value(key: str, default: Any) -> Any:
    """Valeur depuis settings, coercée selon le type du registre si connu."""
    spec = key_spec(key)
    if spec is not None:
        return _coerce(getattr(settings, key, spec.default), spec.value_type)
    return getattr(settings, key, default)


def _cache_key(key: str, epoch: int, workspace_id, rag_space_id, env: str) -> str:
    return f"ragatm_core_cfg:{key}:{epoch}:{workspace_id}:{rag_space_id}:{env}"


def _best_override_raw(key: str, env: str, workspace_id, rag_space_id) -> str | None:
    """Meilleure valeur d'override (texte) pour la clé, en RESPECTANT max_scope.

    Un override stocké à un niveau supérieur au max autorisé pour la clé est ignoré
    (défense en profondeur, même si PR-F l'empêche déjà à l'écriture)."""
    from django.db.models import Q

    from apps.rag_core.models import CoreConfigOverride

    max_scope = allowed_max_scope(key)
    if max_scope is None:
        return None
    max_rank = scope_rank(max_scope)

    pairs = [("global", "")]
    pairs.append(("environment", env))
    if workspace_id is not None:
        pairs.append(("workspace", str(workspace_id)))
    if rag_space_id:
        pairs.append(("ragspace", str(rag_space_id)))
    pairs = [(st, ref) for (st, ref) in pairs if scope_rank(st) <= max_rank]
    if not pairs:
        return None

    q = Q()
    for st, ref in pairs:
        q |= Q(scope_type=st, scope_ref=ref)

    best_value: str | None = None
    best_rank = -1
    for row in CoreConfigOverride.objects.filter(q, key=key).only("value", "scope_type"):
        r = scope_rank(row.scope_type)
        if r > best_rank:
            best_rank = r
            best_value = row.value
    return best_value


def _compute(key: str, env: str, workspace_id, rag_space_id, default: Any) -> Any:
    spec = key_spec(key)
    raw = _best_override_raw(key, env, workspace_id, rag_space_id)
    if spec is not None and raw is not None:
        try:
            canonical = coerce_value_for_storage(key, raw)
            value = _coerce(canonical, spec.value_type)
        except ValueError:
            # Valeur DB invalide => fallback settings, sans logger la valeur.
            logger.warning("core_config_runtime_invalid_db_value key=%s", key)
        else:
            # DERNIER REMPART avant l'exécution : une ligne peut préexister en base
            # (posée avant ce durcissement, ou par écriture SQL directe). Une valeur
            # qui violerait un invariant de démarrage n'atteint jamais le runtime.
            return guard_runtime_value(
                key, value, _settings_value(key, default), environment=env
            )
    return _settings_value(key, default)


def get(key: str, *, workspace_id=None, rag_space_id=None, default: Any = None) -> Any:
    """Valeur effective runtime d'une clé Core.

    Flag off ou clé exclue => settings/default (aucun accès DB/cache). Flag on =>
    overlay DB (cache + epoch), fallback settings sur toute erreur.
    """
    # Exclusion dure : indépendante du flag et de tout override.
    if key in EXCLUDED_KEYS:
        return _settings_value(key, default)

    if not runtime_enabled():
        return _settings_value(key, default)

    try:
        env = current_environment()
        epoch = current_epoch()
        ck = _cache_key(key, epoch, workspace_id, rag_space_id, env)
        hit = cache.get(ck)
        if hit is not None:
            return hit["v"]
        value = _compute(key, env, workspace_id, rag_space_id, default)
        try:
            cache.set(ck, {"v": value}, _CACHE_TTL_SECONDS)
        except Exception:
            # Best-effort (le cache n'est pas la source de vérité) mais rendu visible.
            logger.warning("core_config_cache_set_failed key=%s", key)
        return value
    except Exception:
        # Cache/DB indisponible => settings, jamais d'erreur runtime.
        logger.warning("core_config_runtime_fallback key=%s", key)
        return _settings_value(key, default)

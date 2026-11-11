"""Tampon éphémère de traces de retrieval.

POURQUOI UN TAMPON, ET PAS UNE TABLE
------------------------------------
On écrit pour 100 % des réponses alors qu'une infime minorité sera signalée. Une
table paierait ce déséquilibre en écritures, en index et en purges. Un tampon à
TTL court paie zéro : ce qui n'est pas réclamé disparaît tout seul, sans purge,
sans migration, sans dette.

CE QUI Y EST ÉCRIT
------------------
Des identifiants, des rangs, des scores, des raisons d'exclusion. **Jamais de
texte** — ni chunk, ni nom de fichier, ni question, ni réponse, ni prompt. C'est
la condition qui rend acceptable une collecte systématique, et elle est vérifiée
par un test dédié.

FAIL-OPEN, SANS EXCEPTION
-------------------------
Redis indisponible, sérialisation impossible, quota dépassé : le chat continue.
Une capture d'observabilité qui casse une réponse utilisateur serait un très
mauvais marché.

NOTE D'IMPLÉMENTATION — la clé logique est ``ragatm:feedback-trace:<log_id>``.
Le stockage passe par le cache Django (``CACHES["default"]``, Redis partagé en
staging/prod), donc la clé RÉELLE porte en plus le préfixe de version du cache
Django (``:1:``). On réutilise volontairement cette abstraction plutôt qu'un
client Redis brut : elle est déjà configurée, déjà partagée entre le process HTTP
et les workers, et elle se teste sans Redis.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

KEY_PREFIX = "ragatm:feedback-trace:"

#: Plafond de sécurité du tampon. Très au-dessus de la taille attendue
#: (~3 kB pour 20 candidats) : il n'est là que pour empêcher une dérive.
MAX_BUFFER_BYTES = 65536


def buffer_key(log_id) -> str:
    return f"{KEY_PREFIX}{log_id}"


def is_enabled() -> bool:
    """Interrupteur global. OFF par défaut : la capture est un choix explicite."""
    return bool(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED", False))


def ttl_seconds() -> int:
    try:
        return int(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS", 172800))
    except (TypeError, ValueError):
        return 172800


def top_k() -> int:
    try:
        return max(1, int(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TOP_K", 20)))
    except (TypeError, ValueError):
        return 20


def _shrink_to_budget(payload: dict[str, Any]) -> tuple[str, bool]:
    """Sérialise en respectant le plafond, en retirant des candidats par la fin.

    La troncature est SIGNALÉE dans la charge utile : une capture partielle qui
    se présente comme complète est pire qu'une absence de capture.
    """
    data = dict(payload)
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    truncated = bool(data.get("truncated", False))

    while len(blob.encode("utf-8")) > MAX_BUFFER_BYTES and data.get("candidates"):
        data["candidates"] = data["candidates"][:-1]
        truncated = True
        data["truncated"] = True
        blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    return blob, truncated


def write_trace(log_id, payload: dict[str, Any]) -> bool:
    """Écrit la trace. Ne lève JAMAIS ; renvoie ``True`` si l'écriture a eu lieu."""
    if not log_id or not is_enabled():
        return False
    try:
        blob, truncated = _shrink_to_budget(payload)
        cache.set(buffer_key(log_id), blob, timeout=ttl_seconds())
        logger.info(
            "feedback_buffer_written log_id=%s candidate_count=%s size=%s truncated=%s",
            log_id,
            len(payload.get("candidates") or []),
            len(blob.encode("utf-8")),
            truncated,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - fail-open absolu
        # Codes uniquement : le contenu de la trace ne doit jamais atteindre les logs.
        logger.warning(
            "feedback_buffer_write_failed log_id=%s error_code=%s",
            log_id,
            type(exc).__name__,
        )
        return False


def read_trace(log_id) -> dict[str, Any] | None:
    """Relit la trace. ``None`` si absente, expirée ou illisible."""
    if not log_id:
        return None
    try:
        blob = cache.get(buffer_key(log_id))
        if not blob:
            return None
        return json.loads(blob) if isinstance(blob, str) else dict(blob)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "feedback_buffer_read_failed log_id=%s error_code=%s",
            log_id,
            type(exc).__name__,
        )
        return None


def delete_trace(log_id) -> None:
    """Supprime la trace (promotion réussie : elle n'a plus de raison d'exister)."""
    try:
        cache.delete(buffer_key(log_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "feedback_buffer_delete_failed log_id=%s error_code=%s",
            log_id,
            type(exc).__name__,
        )

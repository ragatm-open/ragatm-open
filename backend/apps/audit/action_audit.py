# backend/apps/audit/action_audit.py
"""Émission centralisée + sanitation des événements d'audit d'action (lot 5).

Point d'entrée UNIQUE : ``emit_action_event`` — ne jamais faire
``ActionAuditEvent.objects.create(...)`` ailleurs.

Garanties :
- **fail-open** : si l'écriture d'audit échoue, l'action métier n'est JAMAIS
  transformée en erreur ; on journalise ``action_audit_write_failed`` sans contenu
  sensible.
- **sanitation récursive** des métadonnées : clés sensibles supprimées, profondeur /
  nombre de clés / taille de chaînes / taille JSON bornés.
- **flag** ``ACTION_AUDIT_ENABLED`` (défaut ON) : à 0 → aucune écriture (rollback).
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# Sous-chaînes toujours sensibles (credential-like) : match par inclusion.
_DENY_KEY_SUBSTRINGS = (
    "password", "secret", "token", "authorization", "api_key", "apikey",
    "prompt", "question", "answer", "embedding",
    "signed_url", "signedurl", "cookie", "traceback", "stacktrace",
)
# Clés interdites EXACTES (on veut interdire « chunk »/« content » mais AUTORISER
# « chunk_count »/« content_type » — cf. §5 metadata autorisées).
_DENY_KEY_EXACT = {
    "chunk", "chunks", "content", "chunk_text", "chunk_content",
    "text", "ocr_text", "raw", "body",
}

_MAX_DEPTH = 4
_MAX_KEYS = 40
_MAX_STR = 500
_MAX_JSON_BYTES = 8_000
_REDACTED = "[redacted]"


def _key_is_denied(key: str) -> bool:
    k = str(key).lower()
    return k in _DENY_KEY_EXACT or any(bad in k for bad in _DENY_KEY_SUBSTRINGS)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[truncated:depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_KEYS:
                out["[truncated:keys]"] = True
                break
            if _key_is_denied(k):
                out[str(k)] = _REDACTED
                continue
            out[str(k)] = _sanitize(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth=depth + 1) for v in list(value)[:_MAX_KEYS]]
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else (value[:_MAX_STR] + "…")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # types inconnus : représentation courte et sûre
    return _sanitize(str(value), depth=depth + 1)


def sanitize_metadata(metadata: dict | None) -> dict:
    """Sanitation récursive + repli borné si le JSON dépasse la taille max."""
    if not metadata:
        return {}
    safe = _sanitize(dict(metadata))
    try:
        import json

        if len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > _MAX_JSON_BYTES:
            logger.error("action_audit_metadata_truncated (json too large)")
            return {"[truncated:size]": True}
    except Exception:  # pragma: no cover - json non sérialisable après sanitation
        return {"[truncated:unserializable]": True}
    return safe if isinstance(safe, dict) else {}


def is_audit_enabled() -> bool:
    return bool(getattr(settings, "ACTION_AUDIT_ENABLED", True))


_ALLOWED_STATUS = {"detected", "queued", "started", "completed", "failed", "denied"}


def emit_action_event(
    *,
    event_name: str,
    action_name: str,
    actor=None,
    status: str,
    workspace_id: str = "",
    rag_space_id=None,
    resource_type: str = "",
    resource_id=None,
    request_id: str = "",
    task_id: str = "",
    idempotency_key: str = "",
    error_code: str = "",
    detection_method: str = "",
    duration_ms: int | None = None,
    metadata: dict | None = None,
):
    """Émet un ``ActionAuditEvent`` (append-only). Ne lève jamais (fail-open)."""
    if not is_audit_enabled():
        return None
    try:
        from .models import ActionAuditEvent

        return ActionAuditEvent.objects.create(
            event_name=str(event_name)[:100],
            action_name=str(action_name)[:100],
            actor=actor if getattr(actor, "pk", None) else None,
            status=(status if status in _ALLOWED_STATUS else "started")[:30],
            workspace_id=str(workspace_id or "")[:64],
            rag_space_id=rag_space_id or None,
            resource_type=str(resource_type or "")[:50],
            resource_id=resource_id or None,
            request_id=str(request_id or "")[:100],
            task_id=str(task_id or "")[:255],
            idempotency_key=str(idempotency_key or "")[:255],
            error_code=str(error_code or "")[:100],
            detection_method=str(detection_method or "")[:50],
            duration_ms=duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None,
            metadata=sanitize_metadata(metadata),
        )
    except Exception:
        # fail-open : ne jamais casser l'action métier à cause de l'audit.
        logger.exception("action_audit_write_failed action=%s event=%s", action_name, event_name)
        return None

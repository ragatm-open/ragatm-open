# backend/apps/chat/services/chat_audit.py
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_request_id_from_user(user) -> str:
    """
    Best-effort extraction of X-Request-ID.

    Pattern supported:
    - api layer sets: request.user._request = request
    """
    try:
        req = getattr(user, "_request", None)
        if not req:
            return ""
        return (req.META.get("HTTP_X_REQUEST_ID") or "").strip()
    except Exception:
        return ""


def maybe_update_log(log_id: str | None, **fields):
    if not log_id:
        return

    from apps.audit.models import RagQueryLog

    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        return

    try:
        allowed = {f.name for f in RagQueryLog._meta.get_fields() if getattr(f, "concrete", False)}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            # Avant : les kwargs inconnus (ex. llm_tokens/llm_cost) étaient filtrés
            # silencieusement -> tokens jamais journalisés sans aucun signal.
            logger.warning(
                "maybe_update_log: champs RagQueryLog inconnus ignorés: %s",
                ", ".join(unknown),
            )
        payload = {k: v for k, v in payload.items() if k in allowed}
        if not payload:
            return
        RagQueryLog.objects.filter(id=log_id).update(**payload)
    except Exception:
        # P1-A1 : plus de swallow SILENCIEUX (constat EXC-020). L'audit est SECONDAIRE
        # — le chat continue —, mais l'échec devient OBSERVABLE et corrélable. On logue
        # les NOMS des champs perdus (jamais les valeurs : `answer` peut porter du
        # contenu) et on SIGNALE explicitement la perte éventuelle d'un statut d'erreur,
        # pour qu'un bug de construction de payload remonte en test/staging.
        logger.exception(
            "rag_query_log_update_failed log_id=%s fields=%s lost_error_status=%s",
            log_id,
            ",".join(sorted(payload)),
            payload.get("status") == "error",
        )
        return


def safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)

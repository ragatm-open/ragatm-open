# backend/apps/chat/services/rephrasing_async.py
"""Enfilement HORS worker HTTP des suggestions de reformulation (F2).

Remplace la génération inline (qui bloquait le worker/flux SSE). Ne génère RIEN ici :
crée l'état ``pending`` et enfile la tâche Celery avec un payload d'IDENTIFIANTS
seulement (aucun prompt). Fail-open : n'impacte jamais la réponse principale.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _flag_enabled() -> bool:
    return bool(getattr(settings, "RAG_REPHRASING_SUGGESTIONS_ENABLED", False))


def enqueue_no_answer_suggestions(*, thread, request_id: str) -> None:
    """Crée l'état pending et enfile la tâche. No-op si flag off, thread absent ou
    request_id vide. Ne lève jamais (best-effort)."""
    if not _flag_enabled() or thread is None:
        return
    rid = (request_id or "").strip()
    if not rid:
        return
    try:
        from apps.chat.models import RephrasingSuggestion
        from apps.chat.tasks import generate_rephrasing_suggestions_task

        RephrasingSuggestion.objects.get_or_create(
            thread=thread, request_id=rid, defaults={"status": RephrasingSuggestion.STATUS_PENDING}
        )
        # Payload = identifiants uniquement (la question est relue en base par la tâche).
        generate_rephrasing_suggestions_task.delay(thread_id=str(thread.id), request_id=rid)
        # Événement comptable : permet de rapprocher enqueued / ready / failed (aucun contenu).
        logger.info(
            "event=rephrasing_suggestions_enqueued thread_id=%s request_id=%s", thread.id, rid
        )
    except Exception:
        # L'échec d'enfilement ne doit jamais casser la réponse principale (fail-open).
        logger.exception(
            "event=rephrasing_suggestions_enqueue_failed thread_id=%s request_id=%s",
            getattr(thread, "id", None), rid,
        )

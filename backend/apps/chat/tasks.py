# backend/apps/chat/tasks.py
"""Tâches Celery du chat — génération ASYNCHRONE des suggestions de reformulation (F2).

Découple la génération LLM du worker HTTP. Payload = identifiants uniquement
(thread_id, request_id) ; la question est relue depuis la base. Aucun prompt ni
contenu dans le payload ni dans les logs. Zéro retry, timeout borné, provider local
uniquement (comportement de ``generate_rephrasing_suggestions`` réutilisé).

Observabilité — le dépôt n'a PAS d'infrastructure de métriques : le mécanisme retenu est
le log structuré. Chaque issue émet un ``event=`` stable, comptable par un opérateur
(``rephrasing_suggestions_ready`` / ``_failed``), corrélé par thread_id/request_id/task_id
et catégorisé par un ``error_code`` borné. Aucun contenu utilisateur n'y figure jamais.

Statut Celery — Option A (échec absorbé, tâche SUCCESS), conforme aux tâches existantes
(``rag_core.tasks`` : ingestion et résumé persistent FAILED, loguent, et retournent
``{"ok": False, ...}`` sans relancer). La visibilité de la panne passe par les logs
``event=rephrasing_suggestions_failed``, pas par le statut Celery.
"""
from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

logger = logging.getLogger(__name__)

# error_code bornés (jamais renvoyés à l'API ; labels d'observabilité).
ERR_NO_QUESTION = "no_question"
ERR_TIMEOUT = "timeout"
ERR_GENERATION = "generation_error"


def _flag_enabled() -> bool:
    return bool(getattr(settings, "RAG_REPHRASING_SUGGESTIONS_ENABLED", False))


@shared_task(
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=5,
    time_limit=8,
)
def generate_rephrasing_suggestions_task(self, *, thread_id: str, request_id: str) -> dict:
    """Génère les suggestions (question relue en base) et persiste pending->ready/failed.

    Idempotent : si un résultat ``ready`` existe déjà pour (thread, request_id), sort
    sans régénérer. Ne lève jamais (les erreurs -> statut ``failed`` + log ERROR)."""
    from apps.chat.models import ChatMessage, ChatThread, RephrasingSuggestion
    from apps.chat.services.chat_formatting import normalize_llm_overrides
    from apps.chat.services.rephrasing_suggestions import generate_rephrasing_suggestions

    task_id = getattr(getattr(self, "request", None), "id", None)

    def _fail(row, code: str, *, provider=None, model=None, unexpected: bool = False) -> dict:
        """Persiste ``failed`` puis émet l'événement d'échec comptable (sans contenu)."""
        row.status = RephrasingSuggestion.STATUS_FAILED
        row.error_code = code
        row.save(update_fields=["status", "error_code", "updated_at"])
        # Inattendu -> stack trace ; attendu et déjà catégorisé -> ERROR sans trace.
        log = logger.exception if unexpected else logger.error
        log(
            "event=rephrasing_suggestions_failed thread_id=%s request_id=%s task_id=%s "
            "error_code=%s provider=%s model=%s",
            thread_id, request_id, task_id, code, provider, model,
        )
        return {"ok": False, "reason": code}

    if not _flag_enabled():
        return {"ok": False, "reason": "disabled"}

    thread = ChatThread.objects.select_related("rag_space").filter(id=thread_id).first()
    if thread is None:
        logger.warning(
            "event=rephrasing_suggestions_thread_not_found thread_id=%s request_id=%s task_id=%s",
            thread_id, request_id, task_id,
        )
        return {"ok": False, "reason": "thread_not_found"}

    row, _created = RephrasingSuggestion.objects.get_or_create(
        thread=thread, request_id=request_id, defaults={"status": RephrasingSuggestion.STATUS_PENDING}
    )
    if row.status == RephrasingSuggestion.STATUS_READY:
        return {"ok": True, "reason": "already_ready"}  # idempotence

    # Revalidation À L'EXÉCUTION (Lot 3) : annule (statut failed, sans retry) si
    # l'accès du propriétaire du thread a changé depuis l'enqueue. No-op flags OFF.
    from apps.workspaces.services import task_guard

    _proceed, _reason = task_guard.enforce_or_log(
        user_id=thread.user_id, rag_space_id=thread.rag_space_id,
        required_permission="read", job_label="rephrasing_suggestions", request_id=request_id,
    )
    if not _proceed:
        return _fail(row, _reason)

    # Question = message utilisateur de ce tour (relu en base, jamais dans le payload).
    msg = (
        ChatMessage.objects.filter(thread=thread, role="user", request_id=request_id)
        .order_by("-created_at")
        .only("content")
        .first()
    )
    question = (getattr(msg, "content", None) or "").strip()
    if not question:
        return _fail(row, ERR_NO_QUESTION)

    provider, model = normalize_llm_overrides(thread.rag_space)
    try:
        suggestions = generate_rephrasing_suggestions(question=question, provider=provider, model=model)
    except SoftTimeLimitExceeded:
        # Attendu et catégorisé (provider lent / injoignable) : pas de stack trace.
        return _fail(row, ERR_TIMEOUT, provider=provider, model=model)
    except Exception:
        # Inattendu : stack trace conservée. Aucun contenu/prompt dans le log.
        return _fail(row, ERR_GENERATION, provider=provider, model=model, unexpected=True)

    row.suggestions = list(suggestions or [])
    row.status = RephrasingSuggestion.STATUS_READY
    row.error_code = ""
    row.save(update_fields=["suggestions", "status", "error_code", "updated_at"])
    # Compte seulement (jamais les suggestions elles-mêmes).
    logger.info(
        "event=rephrasing_suggestions_ready thread_id=%s request_id=%s task_id=%s count=%d",
        thread_id, request_id, task_id, len(row.suggestions),
    )
    return {"ok": True, "count": len(row.suggestions)}

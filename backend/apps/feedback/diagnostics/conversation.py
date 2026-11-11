"""Contexte conversationnel injecté — le cas « le terme était juste avant ».

Une partie des incidents observés porte sur des questions dont la réponse
figurait dans le tour de conversation PRÉCÉDENT (un résumé de document, par
exemple). Sans le contexte injecté, ces cas sont irreproductibles : on rejoue une
question isolée, alors que le modèle, lui, avait reçu autre chose.

Ce module capture donc le contexte, mais avec parcimonie :

- des identifiants et des **hash** par défaut, jamais le texte ;
- le texte intégral uniquement si la capture sensible est autorisée ;
- une fenêtre BORNÉE — pas la conversation entière.
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

CONVERSATION_CONTEXT_VERSION = 1

#: Fenêtre maximale de messages capturés. Le contexte injecté par le pipeline
#: est lui-même borné ; capturer davantage serait collecter sans servir.
MAX_MESSAGES = 10


def _sha256(text: str | None) -> str:
    if not text:
        return ""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def capture_conversation_context(
    *, thread_id, before_message_id=None, include_text: bool = False
) -> dict:
    """Contexte conversationnel autour du message signalé.

    ``include_text`` n'est ouvert que si la politique de capture sensible du
    déploiement l'autorise — c'est du contenu utilisateur.
    """
    result: dict = {
        "conversation_context_version": CONVERSATION_CONTEXT_VERSION,
        "captured": False,
        "messages": [],
        "memory_summary_sha256": "",
        "includes_text": bool(include_text),
    }
    if not thread_id:
        return result

    try:
        from apps.chat.models import ChatMessage, ChatThread

        thread = ChatThread.objects.filter(id=thread_id).first()
        if thread is None:
            return result

        result["memory_summary_sha256"] = _sha256(thread.memory_summary)
        result["memory_summary_until_message_id"] = (
            str(thread.memory_summary_until_message_id)
            if thread.memory_summary_until_message_id
            else ""
        )

        qs = ChatMessage.objects.filter(thread_id=thread_id)
        if before_message_id:
            anchor = ChatMessage.objects.filter(id=before_message_id).first()
            if anchor is not None:
                qs = qs.filter(created_at__lte=anchor.created_at)
        rows = list(qs.order_by("-created_at")[:MAX_MESSAGES])

        messages = []
        for m in reversed(rows):
            entry = {
                "message_id": str(m.id),
                "role": m.role,
                "created_at": m.created_at.isoformat() if m.created_at else "",
                "content_sha256": _sha256(m.content),
                "content_chars": len(m.content or ""),
            }
            if include_text:
                entry["content"] = m.content or ""
            messages.append(entry)

        result["messages"] = messages
        result["captured"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - jamais bloquant
        logger.warning(
            "feedback_conversation_capture_failed thread_id=%s error_code=%s",
            thread_id,
            type(exc).__name__,
        )
        return result


def term_appears_in_context(context: dict, term: str) -> bool:
    """Le terme déclaré apparaît-il dans le contexte capturé ?

    Répond ``False`` quand le texte n'a pas été capturé : on ne devine pas. Un
    bonus de priorité fondé sur une supposition serait pire qu'un bonus absent.
    """
    if not term or not context.get("includes_text"):
        return False
    needle = term.strip().lower()
    if not needle:
        return False
    return any(needle in (m.get("content") or "").lower() for m in context.get("messages") or [])

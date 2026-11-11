"""Machine à états du cycle de vie d'un incident.

Un statut librement modifiable ne documente rien : « exporté » doit signifier
qu'une autorisation a précédé, sinon la traçabilité de l'export est une
fiction. Les transitions sont donc explicites, refusées par défaut, et **toutes
auditées** via ``platform_audit`` — le même mécanisme append-only que les
mutations de la console plateforme, dont la sanitation garantit qu'aucun
contenu documentaire n'entre dans l'événement.
"""
from __future__ import annotations

import logging

from apps.feedback.models import AnswerFeedback

logger = logging.getLogger(__name__)

S = AnswerFeedback

#: Transitions AUTORISÉES. Toute paire absente est refusée.
ALLOWED: dict[str, frozenset[str]] = {
    S.STATUS_NEW: frozenset({S.STATUS_QUALIFIED, S.STATUS_REJECTED, S.STATUS_CLOSED}),
    S.STATUS_QUALIFIED: frozenset(
        {S.STATUS_EXPORT_APPROVED, S.STATUS_FIXED, S.STATUS_REJECTED, S.STATUS_CLOSED}
    ),
    S.STATUS_EXPORT_APPROVED: frozenset(
        {S.STATUS_EXPORTED, S.STATUS_REJECTED, S.STATUS_CLOSED}
    ),
    S.STATUS_EXPORTED: frozenset({S.STATUS_FIXED, S.STATUS_CLOSED}),
    S.STATUS_FIXED: frozenset({S.STATUS_CLOSED}),
    S.STATUS_CLOSED: frozenset(),
    S.STATUS_REJECTED: frozenset(),
}

#: Événement d'audit émis pour chaque statut atteint.
_AUDIT_ACTION = "feedback.transition"


class InvalidTransition(Exception):
    """Transition de statut non autorisée."""


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, frozenset())


def transition(
    feedback: AnswerFeedback,
    *,
    target: str,
    actor=None,
    request=None,
    reason: str = "",
) -> AnswerFeedback:
    """Applique une transition de statut. Lève :class:`InvalidTransition`."""
    current = feedback.status
    if target not in AnswerFeedback.STATUS_VALUES:
        raise InvalidTransition(f"statut inconnu: {target!r}")
    if not can_transition(current, target):
        raise InvalidTransition(f"transition refusée: {current} -> {target}")

    feedback.status = target
    feedback.save(update_fields=["status", "updated_at"])

    _audit(
        feedback,
        actor=actor,
        request=request,
        before=current,
        after=target,
        reason=reason,
    )
    logger.info(
        "feedback_status_changed feedback_id=%s status=%s previous=%s",
        feedback.id,
        target,
        current,
    )
    return feedback


def audit_feedback_event(
    feedback,
    *,
    action: str,
    actor=None,
    request=None,
    before=None,
    after=None,
    reason: str = "",
    extra: dict | None = None,
    status: str = "completed",
) -> None:
    """Audit append-only d'une opération de gouvernance sur un feedback.

    Passe par ``platform_audit`` quand une requête HTTP existe (chemin normal :
    admin Django, API) et retombe sur ``emit_action_event`` sinon — commande de
    gestion, tâche Celery. Sans ce repli, toute opération hors HTTP serait
    silencieusement NON auditée, ce qui est précisément le trou qu'on refuse.

    Ne transporte QUE des métadonnées : jamais de question, de réponse, de
    prompt, d'extrait ni de commentaire utilisateur.
    """
    meta = {
        "feedback_id": str(feedback.id),
        "rag_space_id_hist": str(feedback.rag_space_id_hist or ""),
        "priority_score": feedback.priority_score,
        "kind": feedback.kind or "",
    }
    if extra:
        meta.update(extra)

    try:
        if request is not None:
            from api.v1.platform_admin.audit import platform_audit

            platform_audit(
                request,
                action=action,
                status=status,
                workspace_id=feedback.workspace_id_hist or None,
                before=before,
                after=after,
                reason=(reason or "")[:255],
                extra=meta,
            )
            return

        from apps.audit.action_audit import emit_action_event

        emit_action_event(
            event_name=f"action.{status}",
            action_name=f"platform.{action}",
            actor=actor if getattr(actor, "pk", None) else None,
            status=status,
            workspace_id=str(feedback.workspace_id_hist or ""),
            resource_type="answer_feedback",
            resource_id=feedback.id,
            metadata={**meta, "before": before, "after": after, "reason": (reason or "")[:255]},
        )
    except Exception as exc:  # noqa: BLE001 - l'audit ne casse jamais l'action
        logger.warning(
            "feedback.audit failed feedback_id=%s action=%s error_code=%s",
            feedback.id,
            action,
            type(exc).__name__,
        )


def _audit(feedback, *, actor, request, before, after, reason) -> None:
    audit_feedback_event(
        feedback,
        action=_AUDIT_ACTION,
        actor=actor,
        request=request,
        before={"status": before},
        after={"status": after},
        reason=reason,
    )

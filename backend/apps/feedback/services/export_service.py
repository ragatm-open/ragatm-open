"""Orchestration de l'export : approbation, génération, audit.

Deux gestes SÉPARÉS, et cette séparation est le cœur du contrôle :

- :func:`approve_export` — une personne habilitée décide qu'un incident *peut*
  sortir, à un niveau donné, avec un motif. L'incident passe à
  ``export_approved`` et l'événement d'audit est émis. **Rien n'est produit.**
- :func:`generate_package` — la génération elle-même, qui EXIGE que
  l'approbation ait déjà eu lieu.

Confondre les deux (« générer = autoriser ») supprimerait toute possibilité de
refuser après examen, et l'audit ne dirait plus que ce qui a été fait, jamais ce
qui a été décidé.
"""
from __future__ import annotations

import logging

from apps.feedback.export import levels, package
from apps.feedback.models import AnswerFeedback
from apps.feedback.services.transitions import audit_feedback_event, transition

logger = logging.getLogger(__name__)


def approve_export(
    feedback: AnswerFeedback,
    *,
    level_value: int,
    user,
    justification: str = "",
    request=None,
) -> AnswerFeedback:
    """Autorise l'export d'un incident QUALIFIÉ. Ne produit aucun paquet."""
    if feedback.status != AnswerFeedback.STATUS_QUALIFIED:
        raise levels.ExportRefused(
            "Seul un incident QUALIFIÉ peut être autorisé à l'export "
            f"(statut actuel : {feedback.status})."
        )

    level = levels.check_authorisation(
        user=user, level_value=level_value, justification=justification
    )

    transition(
        feedback,
        target=AnswerFeedback.STATUS_EXPORT_APPROVED,
        actor=user,
        request=request,
        reason=justification,
    )
    audit_feedback_event(
        feedback,
        action="feedback.export_approved",
        actor=user,
        request=request,
        after={"export_level": level.value},
        reason=justification,
        extra={"export_level_label": level.label},
    )
    logger.info(
        "feedback_export_approved feedback_id=%s level=%s", feedback.id, level.value
    )
    return feedback


def generate_package(
    feedback: AnswerFeedback,
    *,
    level_value: int,
    user,
    justification: str = "",
    request=None,
    allow_secrets_override: bool = False,
) -> dict:
    """Génère le paquet d'un incident DÉJÀ autorisé.

    Le contrôle d'autorisation est refait ici, et ce n'est pas redondant : entre
    l'approbation et la génération, le rôle de l'opérateur a pu changer, et le
    niveau demandé pourrait ne pas être celui approuvé.
    """
    if feedback.status != AnswerFeedback.STATUS_EXPORT_APPROVED:
        raise levels.ExportRefused(
            "L'export exige une autorisation préalable "
            f"(statut actuel : {feedback.status})."
        )

    snapshot = getattr(feedback, "diagnostic_snapshot", None)
    result = package.build_package(
        feedback=feedback,
        snapshot=snapshot,
        level_value=level_value,
        user=user,
        justification=justification,
        allow_secrets_override=allow_secrets_override,
    )

    audit_feedback_event(
        feedback,
        action="feedback.exported",
        actor=user,
        request=request,
        after={
            "export_level": result["manifest"]["export_level"],
            "encryption_status": result["manifest"]["encryption_status"],
            "redaction_status": result["manifest"]["redaction_status"],
            "content_sha256": result["manifest"]["content_sha256"],
        },
        reason=justification,
        extra={"expires_at": result["manifest"]["expires_at"]},
    )

    transition(
        feedback,
        target=AnswerFeedback.STATUS_EXPORTED,
        actor=user,
        request=request,
        reason="paquet généré",
    )
    return result


def audit_download(feedback, *, user, request=None) -> None:
    """Trace un TÉLÉCHARGEMENT.

    Distinct de la génération : produire un paquet et le sortir de la machine
    sont deux événements différents, et c'est le second qui matérialise la
    diffusion.
    """
    audit_feedback_event(
        feedback,
        action="feedback.package_downloaded",
        actor=user,
        request=request,
        reason="téléchargement du paquet d'incident",
    )

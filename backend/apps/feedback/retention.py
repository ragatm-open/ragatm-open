"""Rétention des snapshots diagnostiques.

DEUX HORIZONS, DÉLIBÉRÉMENT DISSOCIÉS
-------------------------------------
- ``AnswerFeedback`` — signal produit, peu sensible : **durable**. Aucune règle
  de purge automatique ; sa suppression relève d'une décision produit, pas d'une
  horloge.
- ``FeedbackDiagnosticSnapshot`` — extraits, prompt, réponse : **30 jours**.
  C'est du contenu de production, il ne se garde pas « au cas où ».

Purger l'un ne purge JAMAIS l'autre. C'est l'invariant central de ce lot, et il
est testé : après la purge, l'incident reste visible, priorisable et
regroupable ; seul le matériel sensible a disparu.

La purge s'appuie sur ``expires_at``, posé à la création, et non sur un calcul
au moment de la purge : prolonger la rétention d'un incident précis devient
possible sans toucher à la règle globale (et cette prolongation est auditée).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Plafond de prolongation. Au-delà, ce n'est plus une investigation, c'est de
#: la conservation — et elle doit passer par une décision explicite de politique.
MAX_EXTENSION_DAYS = 90

#: Bornes d'exécution, alignées sur `apps.audit.retention` : une suppression non
#: bornée tiendrait une transaction ouverte trop longtemps.
DEFAULT_BATCH_SIZE = 1000


def _batch_size() -> int:
    try:
        return max(1, int(getattr(settings, "AUDIT_PURGE_BATCH_SIZE", DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def purge_expired_snapshots(*, apply: bool = False, now=None) -> dict:
    """Supprime les snapshots dont ``expires_at`` est dépassé.

    Utilise ``QuerySet.delete()`` — qui court-circuite volontairement le
    ``delete()`` unitaire interdit du modèle. C'est exactement la convention
    retenue pour ``ActionAuditEvent`` : append-only pour le code applicatif,
    purgeable par la rétention.
    """
    from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot

    now = now or timezone.now()
    qs = FeedbackDiagnosticSnapshot.objects.filter(expires_at__lt=now)
    eligible = qs.count()

    result = {
        "key": "feedback_diagnostic_snapshot",
        "category": "technical",
        "eligible": eligible,
        "deleted": 0,
        "batches": 0,
        "feedback_preserved": AnswerFeedback.objects.count(),
    }
    if not apply or eligible == 0:
        return result

    deleted_total = 0
    while True:
        ids = list(
            FeedbackDiagnosticSnapshot.objects.filter(expires_at__lt=now)
            .order_by("expires_at")
            .values_list("pk", flat=True)[: _batch_size()]
        )
        if not ids:
            break
        with transaction.atomic():
            n = FeedbackDiagnosticSnapshot.objects.filter(pk__in=ids).delete()[0]
        if not n:
            break
        deleted_total += n
        result["batches"] += 1

    result["deleted"] = deleted_total
    logger.info(
        "feedback_snapshot_purged deleted=%s eligible=%s feedback_preserved=%s",
        deleted_total,
        eligible,
        result["feedback_preserved"],
    )
    return result


def extend_retention(snapshot, *, days: int, actor=None, request=None, reason: str = ""):
    """Prolonge la rétention d'UN snapshot, dans la limite du plafond.

    Prolonger la conservation d'un contenu de production est une décision, pas
    un réglage : elle exige un motif et elle est auditée.
    """
    from apps.feedback.services.transitions import audit_feedback_event

    if not reason.strip():
        raise ValueError("La prolongation de rétention exige un motif explicite.")

    days = int(days)
    if days <= 0:
        raise ValueError("La prolongation doit être strictement positive.")

    created = snapshot.created_at or timezone.now()
    target = created + timedelta(days=min(days, MAX_EXTENSION_DAYS))
    if days > MAX_EXTENSION_DAYS:
        raise ValueError(
            f"Prolongation refusée : plafond de {MAX_EXTENSION_DAYS} jours "
            f"(demandé : {days})."
        )

    previous = snapshot.expires_at
    # `expires_at` est le SEUL champ mutable du snapshot : il ne décrit pas
    # l'incident, il décrit la décision de conservation. La mise à jour passe
    # par le QuerySet, le `save()` du modèle étant volontairement interdit.
    type(snapshot).objects.filter(pk=snapshot.pk).update(expires_at=target)
    snapshot.expires_at = target

    audit_feedback_event(
        snapshot.feedback,
        action="feedback.retention_extended",
        actor=actor,
        request=request,
        before={"expires_at": previous.isoformat() if previous else None},
        after={"expires_at": target.isoformat()},
        reason=reason,
        extra={"snapshot_id": str(snapshot.id), "days": days},
    )
    return snapshot

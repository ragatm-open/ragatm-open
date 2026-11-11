"""Helper d'écriture APPEND-ONLY du journal de config Core (PR-E).

Unique point d'écriture du journal ``CoreConfigAuditEvent``. En PR-E, aucun chemin
de production ne l'appelle (l'édition arrive en PR-F) ; il est prêt et testé pour
être branché plus tard. On n'écrit jamais de secret : les valeurs de clés sensibles
sont redactées à la LECTURE (API), pas au stockage.
"""
from __future__ import annotations

from .config_resolver import current_environment


def record_config_event(
    *,
    action: str,
    key: str = "",
    scope_type: str = "",
    scope_ref: str = "",
    old_value: str = "",
    new_value: str = "",
    environment: str | None = None,
    reason: str = "",
    actor=None,
):
    """Crée un événement d'audit immuable. Retourne l'instance créée."""
    from apps.rag_core.models import CoreConfigAuditEvent

    return CoreConfigAuditEvent.objects.create(
        action=action,
        key=key or "",
        scope_type=scope_type or "",
        scope_ref=scope_ref or "",
        old_value="" if old_value is None else str(old_value),
        new_value="" if new_value is None else str(new_value),
        environment=environment or current_environment(),
        reason=reason or "",
        actor=actor,
    )

"""Audit durable des mutations PLATEFORME.

S'appuie sur `emit_action_event` (append-only, fail-open, métadonnées sanitisées).
Journalise l'acteur, la cible, l'avant/après et le motif — JAMAIS de contenu
documentaire, prompt, message de chat, secret ou token (la sanitation d'action_audit
supprime déjà ces clés).
"""
from __future__ import annotations


def platform_audit(
    request,
    *,
    action: str,
    status: str,
    workspace_id=None,
    target_user_id=None,
    membership_id=None,
    source_owner_scope_id=None,
    target_owner_scope_id=None,
    before=None,
    after=None,
    reason: str = "",
    extra: dict | None = None,
) -> None:
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    meta = {
        "target_user_id": target_user_id,
        "membership_id": membership_id,
        "source_owner_scope_id": source_owner_scope_id,
        "target_owner_scope_id": target_owner_scope_id,
        "before": before,
        "after": after,
        "reason": reason or "",
    }
    if extra:
        meta.update(extra)
    emit_action_event(
        event_name=f"action.{status}",
        action_name=f"platform.{action}",
        actor=getattr(request, "user", None),
        status=status,
        workspace_id=str(workspace_id) if workspace_id is not None else "",
        request_id=request.headers.get("x-request-id") or "",
        metadata=meta,
    )

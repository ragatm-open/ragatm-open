"""Émission d'audit des périmètres owner (Lot 2 — mode observation).

Invariant du mode audit :  réponse effective = décision LEGACY ; journal = décision
CIBLE. Ces fonctions **ne lèvent jamais**, **ne changent jamais** la réponse HTTP, et
sont **no-op** quand `OWNER_SCOPE_ENABLED` et `OWNER_SCOPE_AUDIT_ONLY` sont tous deux
faux (⇒ aucun événement owner-scope avec tous les flags OFF).

Un seul événement par requête (jamais par token/chunk/RagSpace). Ne journalise
JAMAIS de contenu sensible (question, prompt, nom de fichier, extrait, réponse) :
uniquement des identifiants et la décision.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership
from apps.workspaces.services.access import (
    require_rag_space_access,
    target_ragspace_decision,
    visible_ragspaces_for_user,
)

logger = logging.getLogger("owner_scope.audit")


def _audit_active() -> bool:
    return bool(
        getattr(settings, "OWNER_SCOPE_ENABLED", False)
        or getattr(settings, "OWNER_SCOPE_AUDIT_ONLY", False)
    )


def _request_id(request) -> str | None:
    if request is None:
        return None
    rid = getattr(request, "request_id", None)
    if rid:
        return str(rid)
    meta = getattr(request, "META", None) or {}
    return meta.get("HTTP_X_REQUEST_ID")


def _classify(legacy_allow: bool, target_allow: bool) -> str:
    if legacy_allow and target_allow:
        return "matched_allow"
    if not legacy_allow and not target_allow:
        return "matched_deny"
    if legacy_allow and not target_allow:
        return "would_deny"
    return "would_allow"


def _log(payload: dict) -> None:
    # JSON compact, stable, sans contenu sensible.
    logger.info("owner_scope_audit %s", json.dumps(payload, default=str, sort_keys=True))


def emit_ragspace_decision(*, user, rag_space_id, permission: str, endpoint: str, request=None) -> None:
    """Compare legacy vs cible pour un accès RagSpace unique et journalise 1 événement.

    Fail-open : toute erreur interne est avalée (jamais de changement de comportement).
    """
    if not _audit_active():
        return
    try:
        base_perm = "write" if permission in ("write", "manage") else "read"
        try:
            require_rag_space_access(user=user, rag_space_id=rag_space_id, permission=base_perm)
            legacy_allow = True
        except PermissionDenied:
            legacy_allow = False

        rag_space = RagSpace.objects.select_related("workspace").filter(id=rag_space_id).first()
        ws_role = space_role = None
        if rag_space is None:
            target_allow, reason, ws_id, scope_id = False, "ragspace_not_found", None, None
        else:
            membership = Membership.objects.filter(workspace=rag_space.workspace, user=user).first()
            target_allow, reason = target_ragspace_decision(
                user=user, rag_space=rag_space, permission=permission, membership=membership
            )
            ws_id, scope_id = rag_space.workspace_id, rag_space.owner_scope_id
            # Dimensions d'observation (noms de rôles, pas de contenu métier).
            ws_role = membership.role if membership else None
            space_role = (
                RagSpaceAccess.objects.filter(rag_space=rag_space, user=user, revoked_at__isnull=True)
                .values_list("space_role", flat=True)
                .first()
            )

        _log(
            {
                "event": "ragspace_decision",
                "endpoint": endpoint,
                "request_id": _request_id(request),
                "user_id": getattr(user, "id", None),
                "workspace_id": ws_id,
                "owner_scope_id": scope_id,
                "rag_space_id": str(rag_space_id),
                "permission": permission,
                "workspace_role": ws_role,
                "space_role": space_role,
                "legacy_decision": "allow" if legacy_allow else "deny",
                "target_decision": "allow" if target_allow else "deny",
                "audit": _classify(legacy_allow, target_allow),
                "reason": reason,
            }
        )
    except Exception:  # noqa: BLE001 — l'audit ne doit jamais casser l'endpoint
        logger.exception("owner_scope audit probe failed (fail-open)")


def emit_ragspace_list_decision(*, user, workspace_id, legacy_ids, endpoint: str, request=None) -> None:
    """Compare l'ensemble legacy vs l'ensemble CIBLE (agrégé, 1 événement)."""
    if not _audit_active():
        return
    try:
        legacy = {str(i) for i in legacy_ids}
        target = {
            str(i)
            for i in visible_ragspaces_for_user(
                user=user, workspace_id=workspace_id, mode="content", enforce=True
            ).values_list("id", flat=True)
        }
        _log(
            {
                "event": "ragspace_list_decision",
                "endpoint": endpoint,
                "request_id": _request_id(request),
                "user_id": getattr(user, "id", None),
                "workspace_id": workspace_id,
                "legacy_count": len(legacy),
                "target_count": len(target),
                "legacy_only_count": len(legacy - target),
                "target_only_count": len(target - legacy),
                "would_hide_count": len(legacy - target),  # espaces que la cible masquerait
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("owner_scope audit list probe failed (fail-open)")

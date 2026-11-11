"""Octroi / révocation des accès explicites RagSpace (RagSpaceAccess) — Lot ACL.

Doctrine d'octroi (déterministe) :
  - **owner du périmètre** → accès implicite `manage`, AUCUN RagSpaceAccess requis
    (un octroi explicite sur son propre espace est un no-op `owner_implicit`) ;
  - **manager** explicite → gestion de l'espace ;
  - **editor** explicite → lecture + écriture ;
  - **viewer** explicite → lecture ;
  - admin/member/viewer workspace → AUCUN accès automatique sans RagSpaceAccess.

Le rôle workspace reste un **plafond D1** (jamais une source d'accès à lui seul) :
`validate_ragspace_access_invariants` refuse un `space_role` au-dessus du rôle
workspace, un utilisateur hors du périmètre owner de l'espace, ou non-membre.

Révocation = `revoked_at` (jamais de suppression physique : historique conservé).
Idempotent, transactionnel, `select_for_update`. Ne touche AUCUN flag.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership
from apps.workspaces.services.access import (
    effective_owner_scope_pk,
    validate_ragspace_access_invariants,
)

# --- Codes de résultat stables ----------------------------------------------
GRANTED = "granted"
ALREADY_GRANTED = "already_granted"          # idempotent : même rôle actif
ROLE_CHANGED = "role_changed"                # ancien révoqué + nouveau actif
CONFLICT = "conflict"                        # accès actif différent, sans intention de changer
OWNER_IMPLICIT = "owner_implicit"            # owner du périmètre : aucun grant nécessaire
REVOKED = "revoked"
GRANT_NOT_FOUND = "grant_not_found"
RAGSPACE_DELETED = "ragspace_deleted"
INVALID = "invalid"                          # invariant violé (détail joint)

# Codes considérés comme des ERREURS (exit ≠ 0 côté commande).
ERROR_CODES = {CONFLICT, RAGSPACE_DELETED, INVALID, GRANT_NOT_FOUND}


def _active_grant(rag_space: RagSpace, user) -> RagSpaceAccess | None:
    return RagSpaceAccess.objects.filter(
        rag_space=rag_space, user=user, revoked_at__isnull=True
    ).first()


def grant_access(
    *, rag_space: RagSpace, user, space_role: str, assigned_by, apply: bool,
    allow_role_change: bool = False,
) -> dict:
    """Octroie (ou change) un RagSpaceAccess. Idempotent, sûr, transactionnel."""
    result = {"rag_space_id": str(rag_space.id), "user_id": getattr(user, "id", None), "space_role": space_role}

    if rag_space.deleted_at is not None:
        return {**result, "code": RAGSPACE_DELETED}

    membership = Membership.objects.filter(workspace_id=rag_space.workspace_id, user=user).first()
    if membership is None:
        return {**result, "code": INVALID, "detail": "not_member"}
    # Owner du périmètre : accès implicite manage, aucun grant à créer.
    if membership.role == Membership.ROLE_OWNER and effective_owner_scope_pk(membership) == rag_space.owner_scope_id:
        return {**result, "code": OWNER_IMPLICIT}

    # Invariants : même workspace, même owner_scope, plafond D1.
    try:
        validate_ragspace_access_invariants(user=user, rag_space=rag_space, space_role=space_role)
    except ValueError as exc:
        return {**result, "code": INVALID, "detail": str(exc)}

    existing = _active_grant(rag_space, user)
    if existing is not None:
        if existing.space_role == space_role:
            return {**result, "code": ALREADY_GRANTED, "grant_id": str(existing.id)}
        if not allow_role_change:
            return {**result, "code": CONFLICT, "current_role": existing.space_role, "grant_id": str(existing.id)}
        if apply:
            with transaction.atomic():
                locked = RagSpaceAccess.objects.select_for_update().filter(
                    pk=existing.pk, revoked_at__isnull=True
                ).first()
                if locked is None:
                    return {**result, "code": CONFLICT}  # état changé entre-temps
                locked.revoked_at = timezone.now()
                locked.save(update_fields=["revoked_at"])
                new = RagSpaceAccess.objects.create(
                    rag_space=rag_space, user=user, space_role=space_role, assigned_by=assigned_by
                )
            return {**result, "code": ROLE_CHANGED, "grant_id": str(new.id), "previous_role": existing.space_role}
        return {**result, "code": ROLE_CHANGED, "previous_role": existing.space_role}

    if apply:
        with transaction.atomic():
            # anti-course : re-vérifier l'absence de grant actif sous verrou.
            locked_rs = RagSpace.objects.select_for_update().get(pk=rag_space.pk)
            if _active_grant(locked_rs, user) is not None:
                return {**result, "code": CONFLICT}
            g = RagSpaceAccess.objects.create(
                rag_space=locked_rs, user=user, space_role=space_role, assigned_by=assigned_by
            )
        return {**result, "code": GRANTED, "grant_id": str(g.id)}
    return {**result, "code": GRANTED}


def change_role(*, rag_space: RagSpace, user, space_role: str, assigned_by, apply: bool) -> dict:
    return grant_access(
        rag_space=rag_space, user=user, space_role=space_role,
        assigned_by=assigned_by, apply=apply, allow_role_change=True,
    )


def revoke_access(*, rag_space: RagSpace, user, apply: bool) -> dict:
    """Révoque l'accès actif (revoked_at) — jamais de suppression physique."""
    result = {"rag_space_id": str(rag_space.id), "user_id": getattr(user, "id", None)}
    g = _active_grant(rag_space, user)
    if g is None:
        return {**result, "code": GRANT_NOT_FOUND}
    if apply:
        with transaction.atomic():
            locked = RagSpaceAccess.objects.select_for_update().filter(
                pk=g.pk, revoked_at__isnull=True
            ).first()
            if locked is None:
                return {**result, "code": GRANT_NOT_FOUND}
            locked.revoked_at = timezone.now()
            locked.save(update_fields=["revoked_at"])
    return {**result, "code": REVOKED, "grant_id": str(g.id)}


def revoke_access_by_id(*, grant_id, apply: bool) -> dict:
    g = RagSpaceAccess.objects.filter(pk=grant_id, revoked_at__isnull=True).select_related("rag_space").first()
    if g is None:
        return {"grant_id": str(grant_id), "code": GRANT_NOT_FOUND}
    return revoke_access(rag_space=g.rag_space, user=g.user, apply=apply)


def list_grants(*, rag_space: RagSpace, include_revoked: bool = False):
    qs = RagSpaceAccess.objects.filter(rag_space=rag_space).select_related("user").order_by("-created_at")
    if not include_revoked:
        qs = qs.filter(revoked_at__isnull=True)
    return qs

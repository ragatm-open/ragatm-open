"""Backfill des périmètres owner — logique pure, réutilisée par les commandes
`assign_membership_owner_scope`, `assign_ragspace_owner_scope`, `backfill_owner_scopes`.

Propriétés garanties ici (les commandes ne font qu'exposer ces fonctions) :
  - **dry-run** possible partout (`apply=False` = aucune écriture) ;
  - **idempotence** : ré-appliquer ne change rien (`already_scoped`) ;
  - **transactions atomiques** lors de l'écriture ;
  - **refus** des scopes cross-workspace ou pointant vers un non-owner ;
  - **aucune attribution arbitraire** en multi-owner / sans owner (`no_single_owner`) ;
  - **codes de résultat stables** (constantes ci-dessous) ;
  - **aucun** changement de feature flag, **aucun** enforcement.

Ne touche que la donnée `owner_scope`. Ne lit aucun flag.
"""
from __future__ import annotations

from django.db import transaction

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import validate_membership_owner_scope

# --- Codes de résultat stables ----------------------------------------------
ASSIGNED = "assigned"                          # écriture (ou would-write en dry-run)
ALREADY_SCOPED = "already_scoped"              # idempotent : scope déjà = cible
SKIPPED_OWNER = "skipped_owner"                # membership owner : jamais de scope
CONFLICT = "conflict"                          # scope existant ≠ cible (pas d'écrasement)
INVALID_TARGET_NOT_OWNER = "invalid_target_not_owner"
INVALID_CROSS_WORKSPACE = "invalid_cross_workspace"
NO_SINGLE_OWNER = "no_single_owner"            # 0 ou >1 owner : aucune écriture auto

# Création du premier owner d'un workspace orphelin.
OWNER_CREATED = "owner_created"
ALREADY_OWNER = "already_owner"                # idempotent : user déjà owner
WORKSPACE_HAS_OWNER = "workspace_has_owner"    # refuse (sauf --allow-additional-owner)
MEMBER_EXISTS = "member_exists"                # user déjà membre non-owner (refuse sauf --promote)
PROMOTED = "promoted"                          # membership non-owner promu owner (--promote)

# Activation / désactivation douce de workspace.
WORKSPACE_DISABLED = "workspace_disabled"
WORKSPACE_ENABLED = "workspace_enabled"
ALREADY_DISABLED = "already_disabled"
ALREADY_ENABLED = "already_enabled"

# Codes considérés comme des ERREURS (exit ≠ 0 côté commande).
ERROR_CODES = {
    CONFLICT,
    INVALID_TARGET_NOT_OWNER,
    INVALID_CROSS_WORKSPACE,
    WORKSPACE_HAS_OWNER,
    MEMBER_EXISTS,
}


def _validate_owner_target(*, owner: Membership, workspace_id: int) -> str | None:
    if owner.role != Membership.ROLE_OWNER:
        return INVALID_TARGET_NOT_OWNER
    if owner.workspace_id != workspace_id:
        return INVALID_CROSS_WORKSPACE
    return None


def assign_membership_scope(*, membership: Membership, owner: Membership, apply: bool) -> dict:
    """Rattache un membership NON-owner au périmètre `owner`. Idempotent, sûr."""
    result = {"type": "membership", "id": membership.pk, "before": membership.owner_scope_id}
    if membership.role == Membership.ROLE_OWNER:
        return {**result, "code": SKIPPED_OWNER}

    err = _validate_owner_target(owner=owner, workspace_id=membership.workspace_id)
    if err:
        return {**result, "code": err, "owner_membership_id": owner.pk}

    if membership.owner_scope_id == owner.pk:
        return {**result, "code": ALREADY_SCOPED}
    if membership.owner_scope_id is not None:
        return {**result, "code": CONFLICT, "requested": owner.pk}

    # Belt-and-suspenders : invariants complets (dont ≠ self) avant écriture.
    try:
        validate_membership_owner_scope(membership, owner)
    except ValueError:
        return {**result, "code": INVALID_TARGET_NOT_OWNER, "owner_membership_id": owner.pk}

    if apply:
        with transaction.atomic():
            locked = Membership.objects.select_for_update().get(pk=membership.pk)
            if locked.role == Membership.ROLE_OWNER or locked.owner_scope_id is not None:
                # état changé entre-temps : ne pas écraser
                return {**result, "code": CONFLICT, "requested": owner.pk}
            locked.owner_scope = owner
            locked.save(update_fields=["owner_scope"])
    return {**result, "code": ASSIGNED, "after": owner.pk}


def assign_ragspace_scope(*, rag_space: RagSpace, owner: Membership, apply: bool) -> dict:
    """Rattache un RagSpace au périmètre `owner`. Idempotent, sûr."""
    result = {"type": "ragspace", "id": str(rag_space.id), "before": rag_space.owner_scope_id}
    err = _validate_owner_target(owner=owner, workspace_id=rag_space.workspace_id)
    if err:
        return {**result, "code": err, "owner_membership_id": owner.pk}

    if rag_space.owner_scope_id == owner.pk:
        return {**result, "code": ALREADY_SCOPED}
    if rag_space.owner_scope_id is not None:
        return {**result, "code": CONFLICT, "requested": owner.pk}

    if apply:
        with transaction.atomic():
            locked = RagSpace.objects.select_for_update().get(pk=rag_space.id)
            if locked.owner_scope_id is not None:
                return {**result, "code": CONFLICT, "requested": owner.pk}
            locked.owner_scope = owner
            locked.save(update_fields=["owner_scope"])
    return {**result, "code": ASSIGNED, "after": owner.pk}


def single_owner(workspace: Workspace) -> Membership | None:
    """Retourne l'unique owner du workspace, ou None si 0 ou >1 (pas d'auto-backfill)."""
    owners = list(Membership.objects.filter(workspace=workspace, role=Membership.ROLE_OWNER)[:2])
    return owners[0] if len(owners) == 1 else None


def backfill_workspace(*, workspace: Workspace, apply: bool) -> dict:
    """Backfill DÉTERMINISTE d'un workspace mono-owner (non-owners + RagSpaces → l'owner unique).

    Workspace sans owner ou multi-owner → `no_single_owner`, AUCUNE écriture
    (mapping manuel obligatoire via les commandes assign_*). Toute la mutation
    d'un workspace est atomique (all-or-nothing).
    """
    owner = single_owner(workspace)
    if owner is None:
        n = Membership.objects.filter(workspace=workspace, role=Membership.ROLE_OWNER).count()
        return {"workspace_id": workspace.id, "code": NO_SINGLE_OWNER, "owners": n, "changes": []}

    def _run(apply_inner: bool) -> list[dict]:
        changes: list[dict] = []
        for m in Membership.objects.filter(workspace=workspace).exclude(role=Membership.ROLE_OWNER):
            changes.append(assign_membership_scope(membership=m, owner=owner, apply=apply_inner))
        for s in RagSpace.objects.filter(workspace=workspace, deleted_at__isnull=True):
            changes.append(assign_ragspace_scope(rag_space=s, owner=owner, apply=apply_inner))
        return changes

    if apply:
        with transaction.atomic():
            changes = _run(True)
    else:
        changes = _run(False)

    return {
        "workspace_id": workspace.id,
        "code": "ok",
        "owner_membership_id": owner.pk,
        "changes": changes,
    }


def create_workspace_owner(
    *, workspace: Workspace, user, apply: bool, allow_additional: bool = False, promote: bool = False
) -> dict:
    """Crée (ou promeut) le premier owner d'un workspace. Réservé opérateur/superuser.

    - Refuse si le workspace a déjà un owner (sauf `allow_additional`) ;
    - user déjà owner → `already_owner` (idempotent) ;
    - user déjà membre non-owner → refuse (`member_exists`) sauf `promote` ;
    - sinon crée `Membership(role=owner)` (owner_scope reste NULL : un owner EST
      son périmètre). Atomique + select_for_update. N'accorde AUCUN accès à
      d'autres workspaces.
    """
    result = {"workspace_id": workspace.id, "user_id": getattr(user, "id", None)}

    # Idempotence d'abord : si CE user est déjà owner, no-op (même si d'autres
    # owners existent) — le garde "workspace a déjà un owner" ne vise qu'un
    # user DIFFÉRENT.
    existing = Membership.objects.filter(workspace=workspace, user=user).first()
    if existing is not None and existing.role == Membership.ROLE_OWNER:
        return {**result, "code": ALREADY_OWNER, "membership_id": existing.pk}

    has_owner = Membership.objects.filter(workspace=workspace, role=Membership.ROLE_OWNER).exists()
    if has_owner and not allow_additional:
        return {**result, "code": WORKSPACE_HAS_OWNER}

    if existing is not None:
        if not promote:
            return {**result, "code": MEMBER_EXISTS, "membership_id": existing.pk, "current_role": existing.role}
        if apply:
            with transaction.atomic():
                locked = Membership.objects.select_for_update().get(pk=existing.pk)
                locked.role = Membership.ROLE_OWNER
                locked.owner_scope = None  # owner = son propre périmètre
                locked.save(update_fields=["role", "owner_scope"])
        return {**result, "code": PROMOTED, "membership_id": existing.pk}

    membership_id = None
    if apply:
        with transaction.atomic():
            # anti-course : re-vérifier l'absence d'owner sous verrou workspace
            locked_ws = Workspace.objects.select_for_update().get(pk=workspace.pk)
            if (
                Membership.objects.filter(workspace=locked_ws, role=Membership.ROLE_OWNER).exists()
                and not allow_additional
            ):
                return {**result, "code": WORKSPACE_HAS_OWNER}
            m = Membership.objects.create(workspace=locked_ws, user=user, role=Membership.ROLE_OWNER)
            membership_id = m.pk
    return {**result, "code": OWNER_CREATED, "membership_id": membership_id}


def set_workspace_active(*, workspace: Workspace, active: bool, apply: bool) -> dict:
    """Active/désactive un workspace (soft). Idempotent, atomique. Jamais de delete."""
    result = {"workspace_id": workspace.id, "was_active": workspace.is_active}
    if workspace.is_active == active:
        return {**result, "code": ALREADY_ENABLED if active else ALREADY_DISABLED}
    if apply:
        with transaction.atomic():
            locked = Workspace.objects.select_for_update().get(pk=workspace.pk)
            locked.is_active = active
            locked.save(update_fields=["is_active"])
    return {**result, "code": WORKSPACE_ENABLED if active else WORKSPACE_DISABLED}


def summarize(changes: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["code"]] = counts.get(c["code"], 0) + 1
    return counts


def has_errors(changes: list[dict]) -> bool:
    return any(c["code"] in ERROR_CODES for c in changes)

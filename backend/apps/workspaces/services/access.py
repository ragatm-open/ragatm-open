from __future__ import annotations

import logging
from enum import Enum

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace

# Journal dédié au mode audit des périmètres owner (dry-run). Émet des codes
# STABLES (would_allow / would_deny / owner_scope_mismatch / owner_scope_missing)
# sans jamais bloquer, pour observer l'impact avant l'enforcement.
_scope_audit = logging.getLogger("owner_scope.audit")


def require_workspace_access(*, user, workspace_id: int, permission: str = "read") -> Workspace:
    if not user or not user.is_authenticated:
        raise PermissionDenied("Authentication required")

    ws = Workspace.objects.filter(id=workspace_id).first()
    if ws is None:
        # anti-enumeration
        raise PermissionDenied("No access to this workspace")

    membership = Membership.objects.filter(workspace_id=workspace_id, user=user).first()
    if membership is None:
        raise PermissionDenied("No access to this workspace")

    if permission == "write" and membership.role == Membership.ROLE_VIEWER:
        raise PermissionDenied("No access to this workspace")

    return ws


def require_workspace_admin(*, user, workspace_id: int) -> Workspace:
    """
    Admin workspace = OWNER ou ADMIN (peut gérer membres + ragspaces)
    """
    ws = require_workspace_access(user=user, workspace_id=workspace_id, permission="write")
    m = Membership.objects.filter(workspace_id=workspace_id, user=user).first()
    if m is None or m.role not in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN):
        raise PermissionDenied("No access to this workspace")
    return ws


def require_rag_space_access(*, user, rag_space_id, permission: str = "read") -> RagSpace:
    if not user or not user.is_authenticated:
        raise PermissionDenied("Authentication required")

    if permission not in ("read", "write"):
        raise PermissionDenied("No access to this RAG space")

    rag_space = RagSpace.objects.select_related("workspace").filter(id=rag_space_id).first()
    if rag_space is None:
        # anti-enumeration: don't reveal existence
        raise PermissionDenied("No access to this RAG space")
    
    # Soft-delete safety: never allow access to a deleted RagSpace, even if the caller knows the id.
    # Keep message generic for anti-enumeration.
    if getattr(rag_space, "deleted_at", None) is not None:
        raise PermissionDenied("No access to this RAG space")

    membership = Membership.objects.filter(workspace=rag_space.workspace, user=user).first()
    if membership is None:
        raise PermissionDenied("No access to this RAG space")

    if permission == "write" and membership.role == Membership.ROLE_VIEWER:
        raise PermissionDenied("No access to this RAG space")

    return rag_space


# =============================================================================
# Périmètres owner (owner_scope) & ACL par RagSpace — service canonique cible.
#
# ⚠ ÉTAT (Lot 1 « fondations ») : ces fonctions NE SONT PAS ENCORE câblées dans
# les endpoints. Elles portent la logique cible, entièrement pilotée par les
# flags OWNER_SCOPE_ENABLED / RAGSPACE_ACL_ENABLED (+ *_AUDIT_ONLY). Tant que
# les flags sont OFF (défaut), `require_ragspace_access` délègue STRICTEMENT au
# chemin legacy `require_rag_space_access` : aucun changement de comportement.
# Le câblage des endpoints, des tâches et de l'UI arrive aux lots suivants.
# =============================================================================

_SPACE_ROLE_RANK = {
    RagSpaceAccess.ROLE_VIEWER: 1,
    RagSpaceAccess.ROLE_EDITOR: 2,
    RagSpaceAccess.ROLE_MANAGER: 3,
}
# Rôle d'espace minimal requis par niveau de permission demandé.
_MIN_SPACE_ROLE = {
    "read": RagSpaceAccess.ROLE_VIEWER,
    "write": RagSpaceAccess.ROLE_EDITOR,
    "manage": RagSpaceAccess.ROLE_MANAGER,
}


def _owner_scope_enforced() -> bool:
    """L'isolation par périmètre owner bloque-t-elle réellement (hors audit-only) ?"""
    return bool(getattr(settings, "OWNER_SCOPE_ENABLED", False)) and not bool(
        getattr(settings, "OWNER_SCOPE_AUDIT_ONLY", False)
    )


class AclMode(str, Enum):
    """Mode d'application de l'ACL par RagSpace. UN mode, jamais deux booléens.

    Deux booléens indépendants laissaient chaque appelant recomposer la sémantique
    à sa façon — et c'est exactement ce qui est arrivé : le chemin HTTP combinait
    `ENABLED and not AUDIT_ONLY` (correct) tandis que le chemin Celery utilisait
    `ENABLED or AUDIT_ONLY` (la SIMULATION) pour décider d'annuler une tâche.
    """

    DISABLED = "disabled"
    AUDIT_ONLY = "audit_only"
    ENFORCED = "enforced"


def resolve_acl_mode() -> AclMode:
    """Mode ACL effectif, seule lecture autorisée des deux drapeaux.

    **Les deux drapeaux à 1 => AUDIT_ONLY.** Ce n'est pas un choix arbitraire :
    c'est la convention DÉJÀ appliquée au périmètre owner
    (`_owner_scope_enforced` = `ENABLED and not AUDIT_ONLY`). Poser explicitement
    `AUDIT_ONLY=1` est une déclaration d'intention — « j'observe encore » — et la
    respecter est le comportement le moins surprenant. Retenir l'inverse
    (enforcement prioritaire) ferait diverger les deux mécanismes, et un exploitant
    qui pose le drapeau d'observation obtiendrait un blocage.
    """
    if bool(getattr(settings, "RAGSPACE_ACL_AUDIT_ONLY", False)):
        return AclMode.AUDIT_ONLY
    if bool(getattr(settings, "RAGSPACE_ACL_ENABLED", False)):
        return AclMode.ENFORCED
    return AclMode.DISABLED


def _ragspace_acl_enforced() -> bool:
    """L'ACL par RagSpace (RagSpaceAccess) bloque-t-elle réellement ?"""
    return resolve_acl_mode() is AclMode.ENFORCED


# --- Plateforme --------------------------------------------------------------

def require_platform_admin(user) -> None:
    """Gouvernance plateforme = superuser actif. AUCUN accès contenu implicite."""
    if not user or not user.is_authenticated or not user.is_active or not user.is_superuser:
        raise PermissionDenied("Platform administration requires a superuser")


def require_workspace_owner(*, user, workspace_id: int) -> Workspace:
    ws = require_workspace_access(user=user, workspace_id=workspace_id, permission="write")
    m = Membership.objects.filter(workspace_id=workspace_id, user=user).first()
    if m is None or m.role != Membership.ROLE_OWNER:
        raise PermissionDenied("No access to this workspace")
    return ws


# --- Résolution du périmètre owner ------------------------------------------

def get_owner_scope_for_membership(membership: Membership | None) -> Membership | None:
    """Membership `owner` représentant le périmètre de ce membership.

    Convention : un membership `owner` EST son propre périmètre (owner_scope
    reste NULL en base) → on renvoie le membership lui-même. Un non-owner
    renvoie son `owner_scope` (peut être NULL tant que le backfill n'est pas
    fait). Retourne None si `membership` est None.
    """
    if membership is None:
        return None
    if membership.role == Membership.ROLE_OWNER:
        return membership
    return membership.owner_scope


def validate_membership_owner_scope(membership: Membership, owner_scope: Membership | None) -> None:
    """Valide qu'`owner_scope` est un périmètre légal pour `membership`.

    Invariants (levée de ValueError sinon) — utilisé par la création/invitation
    et les commandes de migration :
      - owner  -> owner_scope doit être None (un owner est son propre périmètre) ;
      - non-owner -> owner_scope requis, rôle owner, même workspace, ≠ self.
    """
    if membership.role == Membership.ROLE_OWNER:
        if owner_scope is not None:
            raise ValueError("Un membership owner ne doit pas porter d'owner_scope (il EST son périmètre).")
        return
    if owner_scope is None:
        raise ValueError("Un membership non-owner doit avoir un owner_scope.")
    if owner_scope.pk == membership.pk:
        raise ValueError("owner_scope ne peut pas être le membership lui-même.")
    if owner_scope.role != Membership.ROLE_OWNER:
        raise ValueError("owner_scope doit référencer un membership de rôle owner.")
    if owner_scope.workspace_id != membership.workspace_id:
        raise ValueError("owner_scope doit appartenir au même workspace.")


def effective_owner_scope_pk(membership: Membership | None) -> int | None:
    """PK du Membership `owner` représentant le périmètre effectif.

    Résolution CANONIQUE unique (point de revue #1) : owner → sa propre PK ;
    non-owner → PK de son `owner_scope` (None si non rattaché). Tous les
    comparateurs de périmètre DOIVENT passer par ici — jamais comparer
    `membership.owner_scope` brut, sinon un owner (owner_scope=NULL) ne
    retrouverait pas son périmètre.
    """
    scope = get_owner_scope_for_membership(membership)
    return scope.pk if scope is not None else None


# Doctrine D1 — le rôle workspace PLAFONNE le rôle d'espace assignable.
_ALLOWED_SPACE_ROLES_BY_WS_ROLE: dict[str, set[str]] = {
    Membership.ROLE_OWNER: {RagSpaceAccess.ROLE_MANAGER, RagSpaceAccess.ROLE_EDITOR, RagSpaceAccess.ROLE_VIEWER},
    Membership.ROLE_ADMIN: {RagSpaceAccess.ROLE_MANAGER, RagSpaceAccess.ROLE_EDITOR, RagSpaceAccess.ROLE_VIEWER},
    Membership.ROLE_MEMBER: {RagSpaceAccess.ROLE_EDITOR, RagSpaceAccess.ROLE_VIEWER},
    Membership.ROLE_VIEWER: {RagSpaceAccess.ROLE_VIEWER},
}


def validate_ragspace_access_invariants(*, user, rag_space: RagSpace, space_role: str) -> Membership:
    """Valide une ligne RagSpaceAccess (point de revue #5). Lève ValueError sinon.

    - user membre du workspace du RagSpace ;
    - même périmètre owner (résolution canonique) ;
    - space_role plafonné par le rôle workspace (D1).
    Retourne le Membership de l'utilisateur (pour l'appelant transactionnel).
    À appeler dans la transaction de création/réactivation/modification d'accès.
    """
    if space_role not in _SPACE_ROLE_RANK:
        raise ValueError(f"space_role invalide: {space_role!r}.")
    membership = Membership.objects.filter(workspace_id=rag_space.workspace_id, user=user).first()
    if membership is None:
        raise ValueError("L'utilisateur doit être membre du workspace du RagSpace.")
    if rag_space.owner_scope_id is None:
        raise ValueError("Le RagSpace n'a pas de owner_scope (donnée ambiguë).")
    if effective_owner_scope_pk(membership) != rag_space.owner_scope_id:
        raise ValueError("L'utilisateur n'appartient pas au périmètre owner du RagSpace.")
    if space_role not in _ALLOWED_SPACE_ROLES_BY_WS_ROLE.get(membership.role, set()):
        raise ValueError(
            f"space_role '{space_role}' dépasse le plafond du rôle workspace '{membership.role}' (D1)."
        )
    return membership


# --- Accès RagSpace (cible) --------------------------------------------------

def _active_ragspace_access(*, user, rag_space_id) -> RagSpaceAccess | None:
    return (
        RagSpaceAccess.objects.filter(
            rag_space_id=rag_space_id, user=user, revoked_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )


def _decide_ragspace(
    *, membership: Membership, rag_space: RagSpace, permission: str, apply_acl: bool
) -> tuple[bool, str]:
    """Décision d'accès cible (périmètre owner + ACL). Retourne (autorisé, code).

    Pure : ne lève rien, ne lit aucun flag (`apply_acl` est fourni explicitement) —
    sert aussi bien à l'enforcement qu'au mode audit (simulation). L'appelant a déjà
    validé l'appartenance workspace.
    """
    # Fail-closed sur donnée ambiguë : un RagSpace sans périmètre n'est ouvrable
    # par personne une fois l'enforcement actif (l'activation est interdite tant
    # que le diagnostic signale des ambiguïtés — cf. audit_workspace_owner_scopes).
    if rag_space.owner_scope_id is None:
        return False, "ragspace_without_scope"

    # Résolution CANONIQUE du périmètre (owner → sa propre PK ; non-owner → owner_scope).
    scope_pk = effective_owner_scope_pk(membership)
    if scope_pk is None:
        return False, "owner_scope_missing"
    if scope_pk != rag_space.owner_scope_id:
        return False, "owner_scope_mismatch"

    # Même périmètre. L'owner responsable bypass l'ACL espace ; un non-owner doit
    # posséder un RagSpaceAccess actif suffisant (si l'ACL est appliquée/simulée).
    if membership.role != Membership.ROLE_OWNER and apply_acl:
        access = _active_ragspace_access(user=membership.user, rag_space_id=rag_space.id)
        if access is None:
            return False, "access_missing"
        needed = _MIN_SPACE_ROLE.get(permission, RagSpaceAccess.ROLE_VIEWER)
        if _SPACE_ROLE_RANK[access.space_role] < _SPACE_ROLE_RANK[needed]:
            return False, "space_role_insufficient"
    return True, "would_allow"


def require_ragspace_access(*, user, rag_space_id, permission: str = "read") -> RagSpace:
    """Point d'entrée canonique cible pour l'accès à un RagSpace.

    - Applique d'abord la garde legacy (auth, existence, soft-delete, membership
      workspace, viewer en écriture).
    - Si l'isolation owner_scope n'est pas active (flags OFF) → comportement
      legacy strict (identique à `require_rag_space_access`).
    - Si active → applique en plus le périmètre owner (+ ACL espace si activée).
    - Si audit-only → journalise la décision cible sans jamais bloquer.
    """
    base_perm = "write" if permission in ("write", "manage") else "read"
    rag_space = require_rag_space_access(user=user, rag_space_id=rag_space_id, permission=base_perm)

    membership = Membership.objects.filter(workspace=rag_space.workspace, user=user).first()
    allowed, code = _decide_ragspace(
        membership=membership, rag_space=rag_space, permission=permission,
        apply_acl=_ragspace_acl_enforced(),
    )

    if _owner_scope_enforced() and not allowed:
        # L'appel legacy a DÉJÀ réussi (l'utilisateur est membre du workspace) :
        # un refus owner_scope signifie « membre mais hors périmètre / sans accès ».
        # - `space_role_insufficient` : la ressource est VISIBLE (l'utilisateur a un
        #   accès mais un rôle trop faible) → 403 honnête.
        # - sinon (mismatch / missing / access_missing) : la ressource ne doit pas
        #   être révélée → 404 anti-énumération (indiscernable d'un inexistant).
        if code == "space_role_insufficient":
            raise PermissionDenied("No access to this RAG space")
        raise NotFound("not_found")

    # Aucun log ici : l'observation est émise UNE seule fois par l'appelant via
    # `scope_audit.emit_ragspace_decision` (évite le double événement d'audit).
    return rag_space


def _acl_considered() -> bool:
    """L'ACL est-elle prise en compte pour SIMULER une décision ?

    ⚠️ Ne JAMAIS utiliser pour produire un refus effectif : à `AUDIT_ONLY`, cette
    fonction rend `True` alors que l'ACL ne doit rien bloquer. C'est la confusion
    qui annulait l'ingestion des non-owners. Pour décider, passer par
    `resolve_acl_mode()` ou `effective_ragspace_decision()`.
    """
    return resolve_acl_mode() is not AclMode.DISABLED


def simulated_ragspace_decision(
    *, user, rag_space: RagSpace, permission: str = "read", membership: Membership | None = None
) -> tuple[bool, str]:
    """Décision SIMULÉE — « qu'aurait décidé l'ACL ? ». Ne bloque JAMAIS rien.

    L'ACL est appliquée dès qu'elle est *considérée* (enforcement OU observation).
    Le résultat est destiné à l'AUDIT : il répond « aurait-on refusé ? », pas
    « faut-il refuser ? ». Utiliser `effective_ragspace_decision` pour décider.
    """
    if membership is None:
        membership = Membership.objects.filter(workspace=rag_space.workspace, user=user).first()
    if membership is None:
        return False, "not_member"
    return _decide_ragspace(
        membership=membership, rag_space=rag_space, permission=permission,
        apply_acl=_acl_considered(),
    )


def effective_ragspace_decision(
    *, user, rag_space: RagSpace, permission: str = "read", membership: Membership | None = None
) -> tuple[bool, str]:
    """Décision EFFECTIVE — celle qui autorise ou refuse réellement.

    L'ACL n'y intervient qu'en mode `ENFORCED`. C'est exactement le contrat du
    chemin HTTP (`require_ragspace_access`) : le chemin asynchrone DOIT utiliser
    celle-ci, sans quoi les deux divergent — c'était le défaut P0-1.
    """
    if membership is None:
        membership = Membership.objects.filter(workspace=rag_space.workspace, user=user).first()
    if membership is None:
        return False, "not_member"
    return _decide_ragspace(
        membership=membership, rag_space=rag_space, permission=permission,
        apply_acl=_ragspace_acl_enforced(),
    )


#: Alias historique conservé pour `scope_audit`, dont l'usage est légitimement une
#: SIMULATION. Le nom `target_*` ne disait pas assez qu'il ne décide de rien.
target_ragspace_decision = simulated_ragspace_decision


def effective_ragspace_access(*, user, rag_space: RagSpace, membership: Membership | None = None) -> dict:
    """Rôle EFFECTIF de `user` sur `rag_space` — **lecture seule pour l'UI**.

    Le backend reste l'autorité : ce helper calcule exactement ce que l'enforcement
    accorderait (flag-aware), pour que le frontend AFFICHE le bon niveau sans jamais
    dupliquer la doctrine. Retourne :
      - `permission_level` ∈ {"read","write","manage"} ou `None` (aucun accès) ;
      - `space_role` ∈ {"manager","editor","viewer"} ou `None` (grant actif) ;
      - `is_owner_implicit` : owner du périmètre (accès manage implicite, pas un grant).
    """
    if membership is None:
        membership = Membership.objects.filter(workspace_id=rag_space.workspace_id, user=user).first()
    out = {"permission_level": None, "space_role": None, "is_owner_implicit": False}
    if membership is None:
        return out

    is_owner_implicit = membership.role == Membership.ROLE_OWNER and (
        rag_space.owner_scope_id is None or effective_owner_scope_pk(membership) == rag_space.owner_scope_id
    )
    grant = (
        RagSpaceAccess.objects.filter(rag_space=rag_space, user=user, revoked_at__isnull=True)
        .order_by("-created_at").first()
    )
    out["space_role"] = grant.space_role if grant else None
    out["is_owner_implicit"] = is_owner_implicit

    # Flags OFF → doctrine legacy (rôle workspace) ; c'est le BACKEND qui décide.
    if not _owner_scope_enforced():
        if membership.role == Membership.ROLE_VIEWER:
            out["permission_level"] = "read"
        elif membership.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN):
            out["permission_level"] = "manage"
        else:
            out["permission_level"] = "write"
        return out

    # owner_scope actif : rejoue la même décision que require_ragspace_access.
    apply_acl = _ragspace_acl_enforced()
    read_ok, _ = _decide_ragspace(membership=membership, rag_space=rag_space, permission="read", apply_acl=apply_acl)
    if not read_ok:
        return out  # hors périmètre / sans accès → level None
    write_ok, _ = _decide_ragspace(membership=membership, rag_space=rag_space, permission="write", apply_acl=apply_acl)
    write_ok = write_ok and membership.role != Membership.ROLE_VIEWER  # plafond legacy viewer
    if not write_ok:
        out["permission_level"] = "read"
        return out
    manage_ok, _ = _decide_ragspace(membership=membership, rag_space=rag_space, permission="manage", apply_acl=apply_acl)
    manage_ok = manage_ok and (
        is_owner_implicit
        or membership.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN)
        or out["space_role"] == RagSpaceAccess.ROLE_MANAGER
    )
    out["permission_level"] = "manage" if manage_ok else "write"
    return out


def require_ragspace_write(*, user, rag_space_id) -> RagSpace:
    return require_ragspace_access(user=user, rag_space_id=rag_space_id, permission="write")


def require_ragspace_manage(*, user, rag_space_id) -> RagSpace:
    return require_ragspace_access(user=user, rag_space_id=rag_space_id, permission="manage")


def visible_ragspaces_for_user(
    *, user, workspace_id: int, mode: str = "content", enforce: bool | None = None,
    apply_acl: bool | None = None,
) -> QuerySet:
    """RagSpaces visibles par `user` dans un workspace, filtrés par périmètre.

    `mode="content"` : espaces réellement ouvrables (owner de leur périmètre, ou
    non-owner du même périmètre avec accès actif si l'ACL est en vigueur).
    `mode="catalog"` : owner/admin voient le catalogue de LEUR périmètre.

    `enforce=None` → dérive du flag (`_owner_scope_enforced()`) : flags OFF ⇒ legacy
    (tout le workspace). `enforce=True` force le calcul de l'ensemble CIBLE (utilisé
    par le mode audit pour comparer au legacy sans changer la réponse HTTP).
    `apply_acl=None` → dérive du flag ; sinon force/simule l'ACL.
    """
    base = RagSpace.objects.filter(workspace_id=workspace_id, deleted_at__isnull=True)
    membership = Membership.objects.filter(workspace_id=workspace_id, user=user).first()
    if membership is None:
        return base.none()

    if enforce is None:
        enforce = _owner_scope_enforced()
    if not enforce:
        return base  # legacy : tout le workspace

    # Résolution canonique (owner → sa propre PK) : jamais de comparaison brute.
    scope_pk = effective_owner_scope_pk(membership)
    if scope_pk is None:
        return base.none()
    scoped = base.filter(owner_scope_id=scope_pk)

    # L'owner voit tout SON périmètre (bypass ACL). L'admin voit le catalogue de
    # son périmètre (métadonnées) sans exiger l'accès contenu.
    if membership.role == Membership.ROLE_OWNER:
        return scoped
    if mode == "catalog" and membership.role == Membership.ROLE_ADMIN:
        return scoped

    if apply_acl is None:
        apply_acl = _ragspace_acl_enforced()
    if apply_acl:
        accessible_ids = RagSpaceAccess.objects.filter(
            user=user, revoked_at__isnull=True, rag_space__workspace_id=workspace_id
        ).values_list("rag_space_id", flat=True)
        return scoped.filter(id__in=accessible_ids)
    return scoped

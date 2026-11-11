"""API d'administration PLATEFORME — mutations (Lots P2/P3).

Toutes les opérations :
  - superuser only (`IsRagATMOwner`) ;
  - délèguent aux SERVICES canoniques (owner_scope_migration) — aucune règle
    dupliquée, aucune écriture de FK directe ;
  - transactionnelles, auditées, jamais de hard-delete, jamais de choix d'owner
    automatique en cas d'ambiguïté.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.rag_core.models.reindex_job import JOB_ACTIVE_STATUSES, RagSpaceReindexJob
from apps.workspaces.models import Membership, Workspace, WorkspaceQuota
from apps.workspaces.services import owner_scope_migration as osm
from apps.workspaces.services import quota as quota_service
from apps.workspaces.services.access import (
    _ALLOWED_SPACE_ROLES_BY_WS_ROLE,
    effective_owner_scope_pk,
    validate_membership_owner_scope,
)

from .audit import platform_audit
from .common import PLATFORM_PERMS, user_public_dict

User = get_user_model()

# --- Mapping codes service → statut HTTP -------------------------------------
_CODE_STATUS = {
    "workspace_created": 201,
    osm.OWNER_CREATED: 201,
    osm.ALREADY_OWNER: 200,
    osm.PROMOTED: 200,
    osm.WORKSPACE_HAS_OWNER: 409,
    osm.MEMBER_EXISTS: 409,
    "membership_created": 201,
    "already_member": 200,
    osm.ASSIGNED: 200,
    osm.ALREADY_SCOPED: 200,
    osm.SKIPPED_OWNER: 409,
    osm.CONFLICT: 409,
    osm.INVALID_TARGET_NOT_OWNER: 422,
    osm.INVALID_CROSS_WORKSPACE: 422,
    "owner_scope_required": 422,
    "invalid_role": 400,
    "user_inactive": 400,
    "user_not_found": 404,
    "workspace_not_found": 404,
    "membership_not_found": 404,
    "owner_membership_invalid": 422,
    osm.WORKSPACE_ENABLED: 200,
    osm.WORKSPACE_DISABLED: 200,
    osm.ALREADY_ENABLED: 200,
    osm.ALREADY_DISABLED: 200,
    # P1 — utilisateurs / memberships
    "user_disabled": 200, "user_enabled": 200,
    "already_user_disabled": 200, "already_user_enabled": 200,
    "last_active_superuser": 409, "self_disable_requires_confirm": 409,
    "role_changed": 200, "owner_promotion_requires_workflow": 422,
    "last_owner": 409, "owner_scope_not_empty": 409,
    "grants_exceed_role": 409, "grants_incompatible": 409,
    "active_grants": 409, "active_jobs": 409,
    "membership_removed": 200, "invalid_transition": 422,
}


def _status_for(code: str) -> int:
    return _CODE_STATUS.get(code, 400)


def _load_active_user(user_id):
    """Retourne (user, error_code|None). Refuse absent/inactif."""
    if user_id is None:
        return None, "user_not_found"
    u = User.objects.filter(id=user_id).first()
    if u is None:
        return None, "user_not_found"
    if not u.is_active:
        return None, "user_inactive"
    return u, None


class _OwnerCreationFailed(Exception):
    pass


def perform_create_workspace(request) -> Response:
    """POST /api/v1/platform/workspaces/ — crée un workspace + son premier owner.

    Payload : {name, owner_user_id}. Atomique : si la création de l'owner échoue,
    le workspace est annulé (rollback). Deux workspaces de même nom NE sont PAS
    considérés identiques (pas d'idempotence sur le nom). Appelée depuis la vue
    liste (même URL `workspaces/`, méthode POST).
    """
    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name_required"}, status=400)
    owner, err = _load_active_user(request.data.get("owner_user_id"))
    if err:
        platform_audit(request, action="workspace_created", status="denied",
                       target_user_id=request.data.get("owner_user_id"), reason=err)
        return Response({"detail": err}, status=_status_for(err))

    # Quotas documentaires (OPTIONNELS) : préremplis côté client avec les défauts
    # de plateforme, personnalisables par le superuser. Absents => défauts courants
    # (compat clients existants, jamais d'échec). Validés (>0, ≤ plafonds absolus).
    quota_payload = request.data.get("quota") or {}
    try:
        max_count = (
            quota_service.validate_count(quota_payload.get("max_document_count"))
            if quota_payload.get("max_document_count") is not None
            else quota_service.default_max_document_count()
        )
        max_bytes = (
            quota_service.validate_bytes(quota_payload.get("max_document_bytes"))
            if quota_payload.get("max_document_bytes") is not None
            else quota_service.default_max_document_bytes()
        )
    except quota_service.QuotaValidationError as e:
        platform_audit(request, action="workspace_created", status="denied",
                       target_user_id=owner.id, reason=e.code)
        return Response({"detail": e.code, "field": e.field}, status=400)

    try:
        with transaction.atomic():
            ws = Workspace.objects.create(name=name)
            res = osm.create_workspace_owner(workspace=ws, user=owner, apply=True)
            if res["code"] != osm.OWNER_CREATED:
                # Ne devrait pas arriver sur un workspace neuf ; rollback strict.
                raise _OwnerCreationFailed(res["code"])
            # Création du quota DANS la même transaction : rollback total si l'une
            # des étapes échoue (aucun workspace partiellement créé).
            quota_service.ensure_quota(
                ws, max_document_count=max_count, max_document_bytes=max_bytes
            )
    except _OwnerCreationFailed as e:
        platform_audit(request, action="workspace_created", status="failed",
                       target_user_id=owner.id, reason=str(e))
        return Response({"detail": str(e)}, status=_status_for(str(e)))

    platform_audit(request, action="workspace_created", status="completed",
                   workspace_id=ws.id, target_user_id=owner.id,
                   membership_id=res.get("membership_id"),
                   after={"workspace_id": ws.id, "owner_membership_id": res.get("membership_id"),
                          "quota": {"max_document_count": max_count, "max_document_bytes": max_bytes}})
    quota_service.audit_quota_created(
        actor=request.user, workspace=ws,
        max_document_count=max_count, max_document_bytes=max_bytes,
        request_id=request.headers.get("x-request-id") or "",
    )
    return Response({
        "code": "workspace_created",
        "workspace": {"id": ws.id, "name": ws.name, "is_active": ws.is_active},
        "owner": {"membership_id": res.get("membership_id"), "user": user_public_dict(owner)},
        "quota": {"max_document_count": max_count, "max_document_bytes": max_bytes},
    }, status=201)


class PlatformWorkspaceOwnerView(APIView):
    """POST /api/v1/platform/workspaces/<id>/owner/ — attribue le premier owner.

    Payload : {user_id, allow_additional?, promote?}. Délègue à
    `create_workspace_owner` (réutilisation stricte de assign_workspace_owner).
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        user, err = _load_active_user(request.data.get("user_id"))
        if err:
            return Response({"detail": err}, status=_status_for(err))

        allow_additional = bool(request.data.get("allow_additional"))
        promote = bool(request.data.get("promote"))
        res = osm.create_workspace_owner(
            workspace=ws, user=user, apply=True, allow_additional=allow_additional, promote=promote
        )
        code = res["code"]
        ok = code in (osm.OWNER_CREATED, osm.PROMOTED, osm.ALREADY_OWNER)
        platform_audit(request, action="owner_assigned",
                       status="completed" if ok else "denied",
                       workspace_id=ws.id, target_user_id=user.id,
                       membership_id=res.get("membership_id"), reason="" if ok else code,
                       after={"membership_id": res.get("membership_id"), "code": code})
        return Response(res, status=_status_for(code))


class PlatformMembershipCreateView(APIView):
    """POST /api/v1/platform/workspaces/<id>/memberships/ — ajoute un membre.

    Payload : {user_id, role, owner_membership_id?}. Un non-owner DOIT recevoir un
    owner_scope (owner_membership_id valide, même workspace, role=owner) ; un owner
    conserve owner_scope=NULL. Invariants via validate_membership_owner_scope +
    Membership.full_clean(). Atomique, audité.
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        user, err = _load_active_user(request.data.get("user_id"))
        if err:
            return Response({"detail": err}, status=_status_for(err))
        role = request.data.get("role")
        if role not in dict(Membership.ROLE_CHOICES):
            return Response({"detail": "invalid_role"}, status=400)

        existing = Membership.objects.filter(workspace=ws, user=user).first()
        if existing is not None:
            return Response({"code": "already_member", "membership_id": existing.pk,
                             "current_role": existing.role}, status=200)

        owner_scope = None
        if role != Membership.ROLE_OWNER:
            omid = request.data.get("owner_membership_id")
            if not omid:
                return Response({"detail": "owner_scope_required"}, status=422)
            owner = Membership.objects.filter(id=omid).first()
            if owner is None or owner.role != Membership.ROLE_OWNER:
                return Response({"detail": "owner_membership_invalid"}, status=422)
            if owner.workspace_id != ws.id:
                return Response({"detail": "cross_workspace"}, status=422)
            owner_scope = owner

        with transaction.atomic():
            m = Membership(workspace=ws, user=user, role=role, owner_scope=owner_scope)
            if owner_scope is not None:
                try:
                    validate_membership_owner_scope(m, owner_scope)
                except ValueError:
                    return Response({"detail": "owner_membership_invalid"}, status=422)
            m.save()

        platform_audit(request, action="membership_created", status="completed",
                       workspace_id=ws.id, target_user_id=user.id, membership_id=m.pk,
                       target_owner_scope_id=owner_scope.pk if owner_scope else None,
                       after={"role": role, "owner_scope_membership_id": m.owner_scope_id})
        return Response({"code": "membership_created", "membership_id": m.pk,
                         "role": m.role, "owner_scope_membership_id": m.owner_scope_id}, status=201)


class PlatformMembershipOwnerScopeView(APIView):
    """PATCH /api/v1/platform/workspaces/<id>/memberships/<mid>/owner-scope/.

    Rattache/re-rattache un membership NON-owner à un owner du même workspace.
    Refuse self-reference et cible owner. Les RagSpaceAccess actifs devenus
    incompatibles (space d'un autre périmètre) BLOQUENT (409 grants_incompatible)
    sauf `revoke_incompatible=true` → révocation (revoked_at) explicite, auditée.
    Jamais de suppression physique. Atomique, audit avant/après.
    """
    permission_classes = PLATFORM_PERMS

    def patch(self, request, workspace_id, membership_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        m = Membership.objects.filter(id=membership_id, workspace=ws).first()
        if m is None:
            return Response({"detail": "membership_not_found"}, status=404)
        if m.role == Membership.ROLE_OWNER:
            return Response({"detail": "cannot_scope_owner"}, status=422)

        omid = request.data.get("owner_membership_id")
        owner = Membership.objects.filter(id=omid).first() if omid else None
        if owner is None or owner.role != Membership.ROLE_OWNER:
            return Response({"detail": "owner_membership_invalid"}, status=422)
        if owner.workspace_id != ws.id:
            return Response({"detail": "cross_workspace"}, status=422)
        if owner.pk == m.pk:
            return Response({"detail": "self_reference"}, status=422)

        # RagSpaceAccess actifs de CE user devenus incompatibles avec le nouveau
        # périmètre (space rattaché à un autre owner_scope).
        incompatible = list(
            RagSpaceAccess.objects.filter(
                user=m.user, rag_space__workspace=ws, revoked_at__isnull=True
            ).exclude(rag_space__owner_scope_id=owner.pk).select_related("rag_space")
        )
        revoke_incompatible = bool(request.data.get("revoke_incompatible"))
        if incompatible and not revoke_incompatible:
            return Response({
                "code": "grants_incompatible",
                "detail": "grants_incompatible",
                "incompatible_grants": [
                    {"grant_id": str(g.id), "rag_space_id": str(g.rag_space_id),
                     "rag_space_name": g.rag_space.name, "space_role": g.space_role}
                    for g in incompatible
                ],
            }, status=409)

        from django.utils import timezone
        before = m.owner_scope_id
        revoked_ids = []
        with transaction.atomic():
            locked = Membership.objects.select_for_update().get(pk=m.pk)
            if revoke_incompatible:
                for g in incompatible:
                    RagSpaceAccess.objects.filter(pk=g.pk, revoked_at__isnull=True).update(
                        revoked_at=timezone.now()
                    )
                    revoked_ids.append(str(g.id))
            locked.owner_scope = owner
            try:
                validate_membership_owner_scope(locked, owner)
            except ValueError:
                return Response({"detail": "owner_membership_invalid"}, status=422)
            locked.save(update_fields=["owner_scope"])

        platform_audit(request, action="membership_owner_scope_changed", status="completed",
                       workspace_id=ws.id, target_user_id=m.user_id, membership_id=m.pk,
                       source_owner_scope_id=before, target_owner_scope_id=owner.pk,
                       before={"owner_scope_membership_id": before},
                       after={"owner_scope_membership_id": owner.pk, "revoked_grants": revoked_ids})
        return Response({"code": "membership_owner_scope_changed", "membership_id": m.pk,
                         "owner_scope_membership_id": owner.pk, "revoked_grants": revoked_ids}, status=200)


class PlatformWorkspaceQuotaView(APIView):
    """PATCH /api/v1/platform/workspaces/<id>/quota/ — modifie les quotas (superuser).

    Payload : {max_document_count?, max_document_bytes?, reason?}. PATCH PARTIEL :
    modifier une seule limite n'écrase pas l'autre. Refuse zéro, négatif, non
    entier, dépassement du plafond absolu, champ inconnu (contrat fermé), workspace
    inexistant. Autorisé aux SEULS superusers (PLATFORM_PERMS) — un owner/admin/
    membre workspace n'y accède jamais (403 avant le corps de la vue, aucun event).

    RÉDUCTION SOUS L'USAGE autorisée : aucune suppression de document ; la réponse
    signale ``is_exceeded`` + un ``warning`` si la nouvelle limite est sous l'usage
    courant. Tout changement est audité (workspace.quota.updated).
    """
    permission_classes = PLATFORM_PERMS

    _ALLOWED_FIELDS = {"max_document_count", "max_document_bytes", "reason"}

    def patch(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)

        data = request.data or {}
        unknown = set(data.keys()) - self._ALLOWED_FIELDS
        if unknown:
            return Response({"detail": "unknown_fields", "fields": sorted(unknown)}, status=400)
        if "max_document_count" not in data and "max_document_bytes" not in data:
            return Response({"detail": "no_quota_change"}, status=400)

        quota = quota_service.get_or_create_quota(ws)
        old_count = int(quota.max_document_count)
        old_bytes = int(quota.max_document_bytes)
        try:
            new_count = (
                quota_service.validate_count(data["max_document_count"])
                if "max_document_count" in data else old_count
            )
            new_bytes = (
                quota_service.validate_bytes(data["max_document_bytes"])
                if "max_document_bytes" in data else old_bytes
            )
        except quota_service.QuotaValidationError as e:
            return Response({"detail": e.code, "field": e.field}, status=400)

        reason = (data.get("reason") or "").strip()
        with transaction.atomic():
            locked = WorkspaceQuota.objects.select_for_update().get(pk=quota.pk)
            locked.max_document_count = new_count
            locked.max_document_bytes = new_bytes
            locked.save(update_fields=["max_document_count", "max_document_bytes", "updated_at"])

        usage = quota_service.get_usage(ws, quota=locked)
        quota_service.audit_quota_updated(
            actor=request.user, workspace=ws,
            old_count=old_count, new_count=new_count,
            old_bytes=old_bytes, new_bytes=new_bytes,
            reason=reason, is_exceeded=usage["is_exceeded"],
            request_id=request.headers.get("x-request-id") or "",
        )
        body = {"code": "workspace_quota_updated", "quota": usage}
        if usage["is_exceeded"]:
            body["warning"] = "The new limit is below the current workspace usage."
        return Response(body, status=200)


class PlatformWorkspaceDisableView(APIView):
    """POST /api/v1/platform/workspaces/<id>/disable/ {reason} — désactivation douce.

    is_active=False (jamais de hard-delete). Motif requis (auditabilité). Les gardes
    métier bloquent alors l'accès ; les tâches asynchrones sont revalidées à
    l'exécution (task_guard existant). Idempotent (already_disabled).
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "reason_required"}, status=400)
        res = osm.set_workspace_active(workspace=ws, active=False, apply=True)
        platform_audit(request, action="workspace_disabled", status="completed",
                       workspace_id=ws.id, reason=reason,
                       before={"is_active": res.get("was_active")}, after={"is_active": False})
        return Response({**res, "reason": reason}, status=_status_for(res["code"]))


class PlatformWorkspaceEnableView(APIView):
    """POST /api/v1/platform/workspaces/<id>/enable/ {reason?} — réactivation.

    DOCTRINE : un workspace **sans owner** ne peut PAS être réactivé pour un usage
    métier (il resterait ingérable). Refus explicite `workspace_without_owner` tant
    qu'un premier owner n'est pas attribué.
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        reason = (request.data.get("reason") or "").strip()
        if not ws.is_active and not Membership.objects.filter(
            workspace=ws, role=Membership.ROLE_OWNER
        ).exists():
            platform_audit(request, action="workspace_enabled", status="denied",
                           workspace_id=ws.id, reason="workspace_without_owner")
            return Response({"detail": "workspace_without_owner"}, status=409)
        res = osm.set_workspace_active(workspace=ws, active=True, apply=True)
        platform_audit(request, action="workspace_enabled", status="completed",
                       workspace_id=ws.id, reason=reason,
                       before={"is_active": res.get("was_active")}, after={"is_active": True})
        return Response({**res, "reason": reason}, status=_status_for(res["code"]))


# ============================================================================
# P1 — Utilisateurs (désactivation/réactivation) — jamais de hard-delete.
# ============================================================================

def _revoke_user_tokens(user) -> None:
    """Best-effort : révoque les jetons d'accès et challenges MFA (force la reconnexion).

    Défense en profondeur : `CookieTokenAuthentication` refuse déjà un compte inactif.
    """
    try:
        from apps.accounts.mfa import revoke_auth_tokens, revoke_challenges
        revoke_auth_tokens(user)
        revoke_challenges(user)
    except Exception:  # noqa: BLE001
        pass


class PlatformUserDisableView(APIView):
    """POST /api/v1/platform/users/<id>/disable/ {reason, confirm_self?} — is_active=False.

    Refuse : le **dernier superuser actif** ; l'**auto-désactivation** sans
    `confirm_self=true`. Idempotent. Jamais de hard-delete. Révoque les tokens.
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request, user_id):
        u = User.objects.filter(id=user_id).first()
        if u is None:
            return Response({"detail": "user_not_found"}, status=404)
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "reason_required"}, status=400)
        if not u.is_active:
            return Response({"code": "already_user_disabled"}, status=200)
        if u.is_superuser and not User.objects.filter(
            is_superuser=True, is_active=True
        ).exclude(pk=u.pk).exists():
            return Response({"detail": "last_active_superuser"}, status=409)
        if u.pk == request.user.pk and not bool(request.data.get("confirm_self")):
            return Response({"detail": "self_disable_requires_confirm"}, status=409)
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=u.pk)
            locked.is_active = False
            locked.save(update_fields=["is_active"])
            _revoke_user_tokens(locked)
        platform_audit(request, action="user_disabled", status="completed",
                       target_user_id=u.id, reason=reason,
                       before={"is_active": True}, after={"is_active": False})
        return Response({"code": "user_disabled"}, status=200)


class PlatformUserEnableView(APIView):
    """POST /api/v1/platform/users/<id>/enable/ {reason} — is_active=True."""
    permission_classes = PLATFORM_PERMS

    def post(self, request, user_id):
        u = User.objects.filter(id=user_id).first()
        if u is None:
            return Response({"detail": "user_not_found"}, status=404)
        reason = (request.data.get("reason") or "").strip()
        if u.is_active:
            return Response({"code": "already_user_enabled"}, status=200)
        u.is_active = True
        u.save(update_fields=["is_active"])
        platform_audit(request, action="user_enabled", status="completed",
                       target_user_id=u.id, reason=reason,
                       before={"is_active": False}, after={"is_active": True})
        return Response({"code": "user_enabled"}, status=200)


# ============================================================================
# P1 — Membership : changement de rôle + retrait contrôlé.
# ============================================================================

def _owner_count(ws) -> int:
    return Membership.objects.filter(workspace=ws, role=Membership.ROLE_OWNER).count()


def _perimeter_not_empty(m: Membership) -> bool:
    """True si l'owner `m` a encore des memberships ou RagSpaces rattachés."""
    return (
        Membership.objects.filter(owner_scope=m).exists()
        or RagSpace.objects.filter(owner_scope=m, deleted_at__isnull=True).exists()
    )


def _active_grants(user, ws):
    return RagSpaceAccess.objects.filter(
        user=user, rag_space__workspace=ws, revoked_at__isnull=True
    ).select_related("rag_space")


class PlatformMembershipDetailView(APIView):
    """PATCH/DELETE /api/v1/platform/workspaces/<id>/memberships/<mid>/.

    PATCH {role, owner_membership_id?, revoke_incompatible?} : change le rôle
    workspace. Promotion vers owner INTERDITE ici (workflow explicite via .../owner/).
    Rétrograder le dernier owner est refusé ; un owner à périmètre non vide n'est pas
    rétrogradable. Le plafond D1 des RagSpaceAccess est revalidé.

    DELETE {revoke_grants?} : retrait contrôlé. Refuse le dernier owner et un owner à
    périmètre non vide ; bloque sur grants actifs (sauf revoke_grants) et jobs actifs.
    """
    permission_classes = PLATFORM_PERMS

    def _load(self, workspace_id, membership_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return None, None, Response({"detail": "workspace_not_found"}, status=404)
        m = Membership.objects.select_related("user").filter(id=membership_id, workspace=ws).first()
        if m is None:
            return ws, None, Response({"detail": "membership_not_found"}, status=404)
        return ws, m, None

    def patch(self, request, workspace_id, membership_id):
        ws, m, err = self._load(workspace_id, membership_id)
        if err:
            return err
        new_role = request.data.get("role")
        if new_role not in dict(Membership.ROLE_CHOICES):
            return Response({"detail": "invalid_role"}, status=400)
        if new_role == m.role:
            return Response({"code": "role_changed", "role": m.role}, status=200)
        # Promotion owner : workflow explicite obligatoire (pas de PATCH silencieux).
        if new_role == Membership.ROLE_OWNER:
            return Response({"detail": "owner_promotion_requires_workflow"}, status=422)

        owner_scope = m.owner_scope
        if m.role == Membership.ROLE_OWNER:
            # Rétrogradation owner -> non-owner
            if _owner_count(ws) <= 1:
                return Response({"detail": "last_owner"}, status=409)
            if _perimeter_not_empty(m):
                return Response({"detail": "owner_scope_not_empty"}, status=409)
            omid = request.data.get("owner_membership_id")
            owner_target = Membership.objects.filter(id=omid).first() if omid else None
            if owner_target is None or owner_target.role != Membership.ROLE_OWNER or owner_target.workspace_id != ws.id:
                return Response({"detail": "owner_scope_required"}, status=422)
            owner_scope = owner_target

        # Revalidation D1 des grants actifs sous le NOUVEAU rôle workspace.
        allowed = _ALLOWED_SPACE_ROLES_BY_WS_ROLE.get(new_role, set())
        incompatible = [g for g in _active_grants(m.user, ws) if g.space_role not in allowed]
        revoke_incompatible = bool(request.data.get("revoke_incompatible"))
        if incompatible and not revoke_incompatible:
            return Response({
                "code": "grants_exceed_role", "detail": "grants_exceed_role",
                "incompatible_grants": [
                    {"grant_id": str(g.id), "rag_space_name": g.rag_space.name, "space_role": g.space_role}
                    for g in incompatible
                ],
            }, status=409)

        from django.utils import timezone
        before = {"role": m.role, "owner_scope_membership_id": m.owner_scope_id}
        revoked = []
        with transaction.atomic():
            locked = Membership.objects.select_for_update().get(pk=m.pk)
            if revoke_incompatible:
                for g in incompatible:
                    RagSpaceAccess.objects.filter(pk=g.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
                    revoked.append(str(g.id))
            locked.role = new_role
            locked.owner_scope = owner_scope if new_role != Membership.ROLE_OWNER else None
            if new_role != Membership.ROLE_OWNER and owner_scope is not None:
                try:
                    validate_membership_owner_scope(locked, owner_scope)
                except ValueError:
                    return Response({"detail": "owner_membership_invalid"}, status=422)
            locked.save(update_fields=["role", "owner_scope"])
        platform_audit(request, action="membership_role_changed", status="completed",
                       workspace_id=ws.id, target_user_id=m.user_id, membership_id=m.pk,
                       before=before, after={"role": new_role, "revoked_grants": revoked})
        return Response({"code": "role_changed", "role": new_role, "revoked_grants": revoked}, status=200)

    def delete(self, request, workspace_id, membership_id):
        ws, m, err = self._load(workspace_id, membership_id)
        if err:
            return err
        if m.role == Membership.ROLE_OWNER:
            if _owner_count(ws) <= 1:
                return Response({"detail": "last_owner"}, status=409)
            if _perimeter_not_empty(m):
                return Response({"detail": "owner_scope_not_empty"}, status=409)
        # Jobs de réindex actifs sur le périmètre du membre.
        scope_pk = effective_owner_scope_pk(m)
        active_jobs = RagSpaceReindexJob.objects.filter(
            rag_space__workspace=ws, rag_space__owner_scope_id=scope_pk,
            status__in=list(JOB_ACTIVE_STATUSES),
        ).exists() if scope_pk else False
        if active_jobs:
            return Response({"detail": "active_jobs"}, status=409)
        grants = list(_active_grants(m.user, ws))
        revoke_grants = bool(request.data.get("revoke_grants"))
        if grants and not revoke_grants:
            return Response({
                "code": "active_grants", "detail": "active_grants",
                "active_grants": [
                    {"grant_id": str(g.id), "rag_space_name": g.rag_space.name, "space_role": g.space_role}
                    for g in grants
                ],
            }, status=409)
        from django.utils import timezone
        revoked = []
        with transaction.atomic():
            for g in grants:
                RagSpaceAccess.objects.filter(pk=g.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
                revoked.append(str(g.id))
            uid = m.user_id
            m.delete()  # retrait physique (pattern existant) — aucune FK orpheline
        platform_audit(request, action="membership_removed", status="completed",
                       workspace_id=ws.id, target_user_id=uid, membership_id=membership_id,
                       before={"role": m.role}, after={"revoked_grants": revoked})
        return Response({"code": "membership_removed", "revoked_grants": revoked}, status=200)

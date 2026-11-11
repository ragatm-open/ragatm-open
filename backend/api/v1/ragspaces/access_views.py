"""Endpoints platform d'affectation RagSpaceAccess (list / grant / change / revoke).

Niveau requis : **manage** — l'acteur doit gouverner le PÉRIMÈTRE de l'espace
(owner du périmètre, admin du même périmètre, ou manager de l'espace). Tout accès
hors périmètre renvoie 404 (anti-énumération). La révocation renseigne `revoked_at`
(jamais de suppression physique : historique conservé). Ne touche aucun flag.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership
from apps.workspaces.services import ragspace_access as svc
from apps.workspaces.services.access import _ALLOWED_SPACE_ROLES_BY_WS_ROLE, effective_owner_scope_pk

_CODE_STATUS = {
    svc.GRANTED: 201, svc.ALREADY_GRANTED: 200, svc.ROLE_CHANGED: 200,
    svc.OWNER_IMPLICIT: 200, svc.REVOKED: 200,
    svc.CONFLICT: 409, svc.INVALID: 400, svc.RAGSPACE_DELETED: 404, svc.GRANT_NOT_FOUND: 404,
}


def _load_rs(rag_space_id):
    return RagSpace.objects.select_related("workspace").filter(
        id=rag_space_id, deleted_at__isnull=True
    ).first()


def _can_manage_grants(user, rs: RagSpace) -> bool:
    """Manage = gouverne le périmètre de l'espace. Refuse cross-owner-scope."""
    m = Membership.objects.filter(workspace=rs.workspace, user=user).first()
    if m is None:
        return False
    if rs.owner_scope_id is None:
        # espace non encore scopé : repli legacy owner/admin du workspace.
        return m.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN)
    if effective_owner_scope_pk(m) != rs.owner_scope_id:
        return False  # hors périmètre
    if m.role in (Membership.ROLE_OWNER, Membership.ROLE_ADMIN):
        return True
    # manager d'espace (grant actif) peut gérer les affectations de son espace.
    return RagSpaceAccess.objects.filter(
        rag_space=rs, user=user, space_role=RagSpaceAccess.ROLE_MANAGER, revoked_at__isnull=True
    ).exists()


def _audit(request, rs, res, action):
    try:
        from apps.audit.action_audit import emit_action_event
    except Exception:  # noqa: BLE001
        return
    code = res.get("code")
    ok = code in (svc.GRANTED, svc.ROLE_CHANGED, svc.REVOKED)
    emit_action_event(
        event_name=f"ragspace_access_{action}", action_name=f"ragspace.access.{action}",
        status="completed" if ok else "denied", actor=request.user,
        workspace_id=str(rs.workspace_id), rag_space_id=str(rs.id),
        resource_type="ragspace_access", resource_id=res.get("grant_id"),
        request_id=request.headers.get("x-request-id") or "",
        metadata={"user_id": res.get("user_id"), "space_role": res.get("space_role"), "code": code},
    )


def _user_dict(u) -> dict | None:
    if u is None:
        return None
    return {"id": u.id, "username": getattr(u, "username", "") or "", "email": getattr(u, "email", "") or ""}


def _grant_dict(g: RagSpaceAccess) -> dict:
    return {
        "id": str(g.id), "user_id": g.user_id, "user": _user_dict(getattr(g, "user", None)),
        "space_role": g.space_role, "assigned_by": g.assigned_by_id,
        "created_at": g.created_at, "revoked_at": g.revoked_at,
    }


class RagSpaceAccessListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, rag_space_id):
        rs = _load_rs(rag_space_id)
        if rs is None or not _can_manage_grants(request.user, rs):
            return Response({"detail": "not_found"}, status=404)
        include = str(request.query_params.get("include_revoked") or "").lower() in ("1", "true", "yes")
        grants = svc.list_grants(rag_space=rs, include_revoked=include)
        return Response([_grant_dict(g) for g in grants], status=200)

    def post(self, request, rag_space_id):
        rs = _load_rs(rag_space_id)
        if rs is None or not _can_manage_grants(request.user, rs):
            return Response({"detail": "not_found"}, status=404)
        target = get_user_model().objects.filter(pk=request.data.get("user_id")).first()
        if target is None:
            return Response({"detail": "user_not_found"}, status=400)
        res = svc.grant_access(
            rag_space=rs, user=target, space_role=request.data.get("space_role"),
            assigned_by=request.user, apply=True,
        )
        _audit(request, rs, res, "grant")
        return Response(res, status=_CODE_STATUS.get(res["code"], 400))


class RagSpaceAccessEligibleView(APIView):
    """GET /api/v1/rag-spaces/<uuid>/access/eligible/ — utilisateurs octroyables.

    Périmètre-scopé (même owner_scope), SANS l'owner implicite ni ceux qui ont déjà
    un grant actif, avec les rôles d'espace autorisés (plafond D1). Manage-gated.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, rag_space_id):
        rs = _load_rs(rag_space_id)
        if rs is None or not _can_manage_grants(request.user, rs):
            return Response({"detail": "not_found"}, status=404)
        granted_ids = set(
            RagSpaceAccess.objects.filter(rag_space=rs, revoked_at__isnull=True).values_list("user_id", flat=True)
        )
        out = []
        for m in Membership.objects.filter(workspace_id=rs.workspace_id).select_related("user"):
            if m.role == Membership.ROLE_OWNER:
                continue  # owner = manage implicite, jamais un grant
            if rs.owner_scope_id is not None and effective_owner_scope_pk(m) != rs.owner_scope_id:
                continue  # hors périmètre
            if m.user_id in granted_ids:
                continue  # déjà un grant actif
            allowed = sorted(_ALLOWED_SPACE_ROLES_BY_WS_ROLE.get(m.role, set()))
            out.append({
                "user": _user_dict(m.user), "workspace_role": m.role, "allowed_space_roles": allowed,
            })
        return Response(out, status=200)


class RagSpaceAccessDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _load_grant(self, rag_space_id, grant_id, user):
        rs = _load_rs(rag_space_id)
        if rs is None or not _can_manage_grants(user, rs):
            return None, None
        g = RagSpaceAccess.objects.filter(id=grant_id, rag_space=rs).select_related("user").first()
        return rs, g

    def patch(self, request, rag_space_id, grant_id):
        rs, g = self._load_grant(rag_space_id, grant_id, request.user)
        if rs is None or g is None:
            return Response({"detail": "not_found"}, status=404)
        res = svc.change_role(
            rag_space=rs, user=g.user, space_role=request.data.get("space_role"),
            assigned_by=request.user, apply=True,
        )
        _audit(request, rs, res, "role_change")
        return Response(res, status=_CODE_STATUS.get(res["code"], 400))

    def delete(self, request, rag_space_id, grant_id):
        rs, g = self._load_grant(rag_space_id, grant_id, request.user)
        if rs is None or g is None:
            return Response({"detail": "not_found"}, status=404)
        res = svc.revoke_access(rag_space=rs, user=g.user, apply=True)
        _audit(request, rs, res, "revoke")
        return Response(res, status=_CODE_STATUS.get(res["code"], 400))

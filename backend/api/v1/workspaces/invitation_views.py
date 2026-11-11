"""Endpoints d'invitation workspace (OWNER / ADMIN) — INV-A2/A3/A4.

Routes (montées sous /api/v1/workspaces/<workspace_id>/invitations/) :
- POST   ""                    → créer une invitation, renvoie le lien one-shot
- GET    ""                    → lister les invitations (jamais de token/URL)
- POST   "<id>/regenerate/"    → régénérer le token, renvoie le nouveau lien
- POST   "<id>/revoke/"        → révoquer une invitation

Toute réponse contenant un lien porte ``Cache-Control: no-store`` + ``Pragma:
no-cache``. Les permissions passent par le service d'accès canonique
(``require_workspace_admin``), pas seulement par le frontend.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.workspaces.models import WorkspaceInvitation
from apps.workspaces.services.access import require_workspace_admin
from apps.workspaces.services import invitations as inv_svc

from .serializers import (
    InvitationCreateSerializer,
    InvitationListItemSerializer,
)


def _no_store(response: Response) -> Response:
    """En-têtes anti-cache pour toute réponse contenant un secret (lien/token)."""
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _request_id(request) -> str:
    return getattr(request, "request_id", "") or request.headers.get("X-Request-ID", "") or ""


def _err(e: inv_svc.InvitationError) -> Response:
    return Response({"detail": e.message, "code": e.code}, status=e.http_status)


def _can_regenerate(invitation: WorkspaceInvitation) -> bool:
    return invitation.status() in (
        WorkspaceInvitation.STATUS_PENDING,
        WorkspaceInvitation.STATUS_EXPIRED,
    )


def _can_revoke(invitation: WorkspaceInvitation) -> bool:
    return invitation.status() in (
        WorkspaceInvitation.STATUS_PENDING,
        WorkspaceInvitation.STATUS_EXPIRED,
    )


def _list_item(invitation: WorkspaceInvitation) -> dict:
    return {
        "id": invitation.id,
        "email": invitation.email_normalized,
        "role": invitation.role,
        "status": invitation.status(),
        "generation": invitation.generation,
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "can_regenerate": _can_regenerate(invitation),
        "can_revoke": _can_revoke(invitation),
    }


class WorkspaceInvitationsView(APIView):
    """POST : créer (lien one-shot) · GET : lister (sans secret)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation_create"

    def get(self, request, workspace_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)
        qs = (
            WorkspaceInvitation.objects.filter(workspace_id=workspace_id)
            .order_by("-created_at")
        )
        items = [_list_item(i) for i in qs]
        return Response(InvitationListItemSerializer(items, many=True).data)

    def post(self, request, workspace_id: int):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)

        ser = InvitationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        role = ser.validated_data.get("role")

        try:
            invitation, raw_token = inv_svc.create_invitation(
                workspace_id=workspace_id,
                email=email,
                role=role,
                actor=request.user,
                request_id=_request_id(request),
            )
        except inv_svc.InvitationError as e:
            return _err(e)

        # Email best-effort (le lien manuel reste la source fiable).
        invited_by = (getattr(request.user, "email", "") or "").strip()
        email_sent, _ = inv_svc.send_invitation_email(
            invitation=invitation, raw_token=raw_token, invited_by_email=invited_by
        )

        payload = {
            "id": str(invitation.id),
            "email": invitation.email_normalized,
            "role": invitation.role,
            "status": invitation.status(),
            "generation": invitation.generation,
            "expires_at": invitation.expires_at,
            "email_sent": email_sent,
            # Lien retourné UNE seule fois (dev ET prod). Non stocké, non récupérable.
            "invitation_url": inv_svc.build_invitation_url(raw_token),
        }
        return _no_store(Response(payload, status=201))


class WorkspaceInvitationRegenerateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation_regenerate"

    def post(self, request, workspace_id: int, invitation_id):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)
        try:
            invitation, raw_token = inv_svc.regenerate_invitation(
                workspace_id=workspace_id,
                invitation_id=invitation_id,
                actor=request.user,
                request_id=_request_id(request),
            )
        except inv_svc.InvitationError as e:
            return _err(e)

        payload = {
            "id": str(invitation.id),
            "generation": invitation.generation,
            "status": invitation.status(),
            "expires_at": invitation.expires_at,
            "invitation_url": inv_svc.build_invitation_url(raw_token),
        }
        return _no_store(Response(payload, status=200))


class WorkspaceInvitationRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id: int, invitation_id):
        require_workspace_admin(user=request.user, workspace_id=workspace_id)
        try:
            invitation, changed = inv_svc.revoke_invitation(
                workspace_id=workspace_id,
                invitation_id=invitation_id,
                actor=request.user,
                request_id=_request_id(request),
            )
        except inv_svc.InvitationError as e:
            return _err(e)

        return Response(
            {"id": str(invitation.id), "status": invitation.status(), "changed": changed},
            status=200,
        )

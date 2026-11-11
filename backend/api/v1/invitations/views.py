"""Endpoints publics d'acceptation d'invitation (INV-A2).

- POST /api/v1/invitations/validate/              (public) → état du token + flux
- POST /api/v1/invitations/accept/                (public) → nouveau/inactif : mdp
- POST /api/v1/invitations/accept-authenticated/  (auth)   → compte actif : session

Le token transite dans le CORPS de la requête (jamais dans l'URL de ces appels)
pour éviter les fuites via logs/Referer. Les vues publiques désactivent l'auth
(``authentication_classes = []``) : le token EST la crédence ; aucune session
n'intervient, donc pas de contrôle CSRF applicable. La variante authentifiée
conserve l'auth par cookie et le contrôle CSRF standard.
"""
from __future__ import annotations

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.workspaces.services import invitations as inv_svc

from .serializers import (
    InvitationAcceptAuthenticatedSerializer,
    InvitationAcceptSerializer,
    InvitationTokenSerializer,
)


def _no_store(response: Response) -> Response:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _request_id(request) -> str:
    return getattr(request, "request_id", "") or request.headers.get("X-Request-ID", "") or ""


def _err(e: inv_svc.InvitationError) -> Response:
    return _no_store(Response({"detail": e.message, "code": e.code}, status=e.http_status))


class InvitationValidateView(APIView):
    """Valide un token et renvoie l'état + le flux à suivre (public)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        ser = InvitationTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            info = inv_svc.lookup_invitation(raw_token=ser.validated_data["token"])
        except inv_svc.InvitationError as e:
            return _err(e)
        return _no_store(Response(info, status=200))


class InvitationAcceptView(APIView):
    """Acceptation anonyme (compte nouveau ou inactif). Pose le mot de passe."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation_accept"

    def post(self, request):
        ser = InvitationAcceptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            res = inv_svc.accept_invitation(
                raw_token=ser.validated_data["token"],
                password=ser.validated_data["password"],
                password_confirmation=ser.validated_data["password_confirmation"],
                request_id=_request_id(request),
            )
        except inv_svc.InvitationError as e:
            return _err(e)
        return _no_store(
            Response(
                {
                    "ok": True,
                    "workspace_id": res["invitation"].workspace_id,
                    "role": res["membership"].role,
                },
                status=200,
            )
        )


class InvitationAcceptAuthenticatedView(APIView):
    """Acceptation par un compte déjà actif et connecté. Ne touche jamais le mdp."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invitation_accept"

    def post(self, request):
        ser = InvitationAcceptAuthenticatedSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            res = inv_svc.accept_invitation(
                raw_token=ser.validated_data["token"],
                authenticated_user=request.user,
                request_id=_request_id(request),
            )
        except inv_svc.InvitationError as e:
            return _err(e)
        return _no_store(
            Response(
                {
                    "ok": True,
                    "workspace_id": res["invitation"].workspace_id,
                    "role": res["membership"].role,
                },
                status=200,
            )
        )

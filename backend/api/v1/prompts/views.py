from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from apps.workspaces.models import Workspace, Membership
from apps.chat.models import PromptTemplate
from apps.chat.services.prompt_templates import create_prompt_template_version, activate_prompt_template
from .serializers import (
    PromptTemplateSerializer,
    PromptTemplateCreateSerializer,
)


def _require_workspace_member(*, user, workspace: Workspace) -> Membership:
    m = Membership.objects.filter(user=user, workspace=workspace).first()
    if not m:
        # 403 plutôt que 404, pour rester cohérent avec tes tests access control
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("forbidden")
    return m


class PromptTemplateListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # GET /api/v1/prompts?workspace_id=...&name=...&active_only=1
        workspace_id = request.query_params.get("workspace_id")
        name = request.query_params.get("name")
        active_only = request.query_params.get("active_only") in ("1", "true", "True")

        if not workspace_id:
            return Response({"detail": "workspace_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        workspace = get_object_or_404(Workspace, id=workspace_id)
        _require_workspace_member(user=request.user, workspace=workspace)

        qs = PromptTemplate.objects.filter(workspace=workspace).order_by("name", "-version")
        if name:
            qs = qs.filter(name=name)
        if active_only:
            qs = qs.filter(is_active=True)

        data = PromptTemplateSerializer(qs, many=True).data
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        # POST /api/v1/prompts {workspace_id, name, content, activate?}
        ser = PromptTemplateCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        workspace = get_object_or_404(Workspace, id=ser.validated_data["workspace_id"])
        membership = _require_workspace_member(user=request.user, workspace=workspace)

        # 권한 : viewer ne crée pas de prompt
        if membership.role == Membership.ROLE_VIEWER:
            return Response({"detail": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

        tpl = create_prompt_template_version(
            workspace=workspace,
            name=ser.validated_data["name"],
            content=ser.validated_data["content"],
            activate=ser.validated_data["activate"],
        )

        return Response(PromptTemplateSerializer(tpl).data, status=status.HTTP_201_CREATED)


class PromptTemplateDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, prompt_id):
        tpl = get_object_or_404(PromptTemplate.objects.select_related("workspace"), id=prompt_id)
        _require_workspace_member(user=request.user, workspace=tpl.workspace)
        return Response(PromptTemplateSerializer(tpl).data, status=status.HTTP_200_OK)


class PromptTemplateActivateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, prompt_id):
        tpl = get_object_or_404(PromptTemplate.objects.select_related("workspace"), id=prompt_id)
        membership = _require_workspace_member(user=request.user, workspace=tpl.workspace)

        if membership.role == Membership.ROLE_VIEWER:
            return Response({"detail": "forbidden"}, status=status.HTTP_403_FORBIDDEN)

        tpl = activate_prompt_template(prompt_template=tpl)
        return Response(PromptTemplateSerializer(tpl).data, status=status.HTTP_200_OK)

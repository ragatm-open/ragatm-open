from __future__ import annotations

from rest_framework import serializers
from apps.chat.models import PromptTemplate


class PromptTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromptTemplate
        fields = ["id", "workspace", "name", "version", "content", "is_active", "created_at"]
        read_only_fields = ["id", "version", "is_active", "created_at"]


class PromptTemplateCreateSerializer(serializers.Serializer):
    # ✅ Workspace.id chez toi n'est pas un UUID → IntegerField
    workspace_id = serializers.IntegerField(required=False)
    workspace = serializers.IntegerField(required=False)

    name = serializers.CharField(max_length=128)
    content = serializers.CharField()
    activate = serializers.BooleanField(default=False)

    def validate(self, attrs):
        workspace_id = attrs.get("workspace_id") or attrs.get("workspace")
        if workspace_id in (None, ""):
            raise serializers.ValidationError({"workspace_id": "workspace_id (or workspace) is required"})
        attrs["workspace_id"] = workspace_id
        return attrs


class PromptTemplateActivateSerializer(serializers.Serializer):
    # vide : activation via URL /{id}/activate
    pass

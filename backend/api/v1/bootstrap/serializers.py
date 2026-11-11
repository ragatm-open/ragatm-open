from rest_framework import serializers


class BootstrapWorkspaceSerializer(serializers.Serializer):
    """Workspace info returned by /api/v1/bootstrap/."""
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.CharField()


class BootstrapRagSpaceSerializer(serializers.Serializer):
    """RagSpace info returned by /api/v1/bootstrap/."""
    id = serializers.UUIDField()
    name = serializers.CharField()


class BootstrapResponseSerializer(serializers.Serializer):
    """Response wrapper for /api/v1/bootstrap/."""
    created = serializers.BooleanField()
    workspace = BootstrapWorkspaceSerializer()
    ragspace = BootstrapRagSpaceSerializer()
    # Langue d'interface persistée (fr | en) : permet au frontend de synchroniser
    # le cookie `ragatm_locale` juste après login, SANS second appel réseau.
    preferred_locale = serializers.CharField()
    # Langue des réponses IA persistée (auto | fr | en), DISTINCTE de l'interface.
    preferred_answer_language = serializers.CharField()

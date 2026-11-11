from rest_framework import serializers


class InvitationTokenSerializer(serializers.Serializer):
    """Validation d'un token (lookup public)."""

    token = serializers.CharField(max_length=512, trim_whitespace=False)


class InvitationAcceptSerializer(serializers.Serializer):
    """Acceptation anonyme : token + mot de passe.

    Le rôle N'EST PAS accepté ici — il vient exclusivement de l'invitation en base.
    """

    token = serializers.CharField(max_length=512, trim_whitespace=False)
    password = serializers.CharField(trim_whitespace=False, min_length=1, max_length=128)
    password_confirmation = serializers.CharField(trim_whitespace=False, min_length=1, max_length=128)


class InvitationAcceptAuthenticatedSerializer(serializers.Serializer):
    """Acceptation par un utilisateur déjà connecté : token seul, jamais de mot de passe."""

    token = serializers.CharField(max_length=512, trim_whitespace=False)

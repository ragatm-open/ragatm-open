from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


class EmailLoginSerializer(serializers.Serializer):
    """Forme seulement : la vérification des identifiants vit dans
    `apps.accounts.mfa.authenticate_password` (sous surveillance d'Axes)."""

    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class MfaTokenSerializer(serializers.Serializer):
    """Code TOTP (6 chiffres) ou jeton de secours statique."""

    token = serializers.CharField(max_length=16, trim_whitespace=True)

    def validate_token(self, value):
        return value.replace(" ", "")


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """
    Compat: accepte soit
      - new_password (format backend)
      - new_password1/new_password2 (format front legacy)

    Normalise en validated_data["new_password"].
    """
    uid = serializers.CharField()
    token = serializers.CharField()

    # format préféré
    new_password = serializers.CharField(required=False, trim_whitespace=False, min_length=8, max_length=128)

    # format legacy (souvent côté front)
    new_password1 = serializers.CharField(required=False, trim_whitespace=False, min_length=8, max_length=128)
    new_password2 = serializers.CharField(required=False, trim_whitespace=False, min_length=8, max_length=128)

    def validate(self, attrs):
        pwd = (attrs.get("new_password") or "").strip()
        p1 = (attrs.get("new_password1") or "").strip()
        p2 = (attrs.get("new_password2") or "").strip()

        if pwd:
            final_pwd = pwd
        else:
            if not p1 or not p2:
                # message identique à celui que tu vois côté UI
                raise serializers.ValidationError({"new_password": ["Ce champ est obligatoire."]})
            if p1 != p2:
                raise serializers.ValidationError({"new_password2": ["Les mots de passe ne correspondent pas."]})
            final_pwd = p1

        # applique les validateurs Django (force, etc.)
        validate_password(final_pwd)

        # normalisation pour la view
        attrs["new_password"] = final_pwd
        return attrs

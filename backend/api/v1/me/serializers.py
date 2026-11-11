from rest_framework import serializers

from apps.accounts.models import ANSWER_LANGUAGE_CHOICES, LOCALE_CHOICES


class PreferencesSerializer(serializers.Serializer):
    """Préférences utilisateur modifiables (PATCH partiel).

    - ``preferred_locale`` : langue de l'INTERFACE (fr | en).
    - ``preferred_answer_language`` : langue des RÉPONSES IA (auto | fr | en), DISTINCTE.

    Les deux champs sont facultatifs mais AU MOINS UN doit être présent. Toute valeur
    hors des choix → 400 structuré. Aucun autre champ n'est accepté.
    """

    preferred_locale = serializers.ChoiceField(choices=LOCALE_CHOICES, required=False)
    preferred_answer_language = serializers.ChoiceField(choices=ANSWER_LANGUAGE_CHOICES, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one preference field is required.")
        return attrs

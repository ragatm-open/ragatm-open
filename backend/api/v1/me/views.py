from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import get_or_create_profile

from .serializers import PreferencesSerializer


def _payload(profile) -> dict:
    return {
        "preferred_locale": profile.preferred_locale,
        "preferred_answer_language": profile.preferred_answer_language,
    }


class PreferencesView(APIView):
    """Préférences de l'utilisateur COURANT (jamais d'un autre compte).

    - GET   : lit `preferred_locale` (interface) et `preferred_answer_language` (réponses IA).
    - PATCH : met à jour l'un et/ou l'autre (partiel). 400 structuré sur valeur invalide.

    L'isolation est garantie par `request.user` : aucun identifiant d'utilisateur n'est
    accepté en entrée. Non audité : préférences UI non sensibles.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = get_or_create_profile(request.user)
        return Response(_payload(profile), status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = get_or_create_profile(request.user)

        updated = ["updated_at"]
        if "preferred_locale" in serializer.validated_data:
            profile.preferred_locale = serializer.validated_data["preferred_locale"]
            updated.append("preferred_locale")
        if "preferred_answer_language" in serializer.validated_data:
            profile.preferred_answer_language = serializer.validated_data["preferred_answer_language"]
            updated.append("preferred_answer_language")
        profile.save(update_fields=updated)
        return Response(_payload(profile), status=status.HTTP_200_OK)

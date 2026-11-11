# backend/api/v1/capabilities/views.py
from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.llm_router.capabilities import embeddings_info, provider_capabilities


class CapabilitiesView(APIView):
    """
    GET /api/v1/capabilities/

    Capacités globales NON sensibles, lisibles par tout utilisateur authentifié.

    Expose :
      - `providers` : capacités chat/embeddings par provider (défauts, clé OpenAI
        présente ou non) — permet à l'UI de griser OpenAI à la CRÉATION d'un espace
        (avant qu'un id de RagSpace n'existe) ;
      - `embeddings` : réglage d'embeddings GLOBAL (repli d'affichage pour les
        messages d'erreur d'import quand le RagSpace n'est pas connu) ;
      - `ingest.formats` : formats d'upload réellement exploitables sur CET
        environnement, `ingest.image_formats` : les sous-formats de la famille
        `image`, et `ingest.msg` : le détail du format sous flag — le frontend en
        dérive TOUTE sa liste de dépôt au lieu d'en tenir une copie figée ;

    Ne renvoie aucun secret (ni clé API, ni URL interne).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.rag_core.services.ingest.celery_health import reindex_capability
        from apps.rag_core.services.ingest.extraction import (
            audio_capability,
            eml_capability,
            msg_capability,
            supported_formats,
            supported_image_formats,
        )
        from apps.llm_router.model_policy import canonical_model_config, can_edit_model_configuration

        return Response(
            {
                "providers": provider_capabilities(),
                "embeddings": embeddings_info(),
                # Réindexation durable : le frontend sait si un worker async répond.
                "reindex": reindex_capability(),
                "ingest": {
                    "formats": supported_formats(),
                    # Sous-formats de la famille `image` : le frontend en dérive les
                    # extensions au lieu d'en tenir une copie figée.
                    "image_formats": supported_image_formats(),
                    "msg": msg_capability(),
                    "eml": eml_capability(),
                    # extensions, plafond (octets décimaux), durée, chemin
                    # d'upload dédié. L'admission PAR ESPACE est portée par chaque RagSpace
                    # (`audio_upload_enabled`) : aucune liste d'UUID n'est exposée ici.
                    "audio": audio_capability(),
                },
                # Politique modèles RagSpace : valeurs imposées aux non-superusers +
                # capacité d'édition de l'appelant (source unique pour l'UI read-only).
                "ragspace_model_policy": {
                    **canonical_model_config(),
                    "can_edit": can_edit_model_configuration(request.user),
                },
            },
            status=200,
        )

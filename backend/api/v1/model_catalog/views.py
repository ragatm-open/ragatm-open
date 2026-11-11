"""Catalogue des modèles et pipelines — projection publique pour l'interface.

Le frontend ne doit **jamais** appeler la passerelle IA directement : ni son URL ni sa
topologie n'ont à sortir du réseau privé. Cet endpoint est la seule voie, et il fait
plus que relayer — il enrichit le registre amont de la gouvernance locale
(`allowed` par environnement, `qualified` par mesure, `recommended` par décision).
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class ModelCatalogView(APIView):
    """GET /api/v1/ai/model-catalog/

    Réponse :

        {
          "chat":       [ {id, display_name, available, allowed, qualified, recommended, …} ],
          "embeddings": [ {…, dimension} ],
          "available":  true,
          "stale":      false,
          "qualification_revision": "…",
          "expected_embedding_dimension": 768,
          "generation_registry_available": true,
          "default_chat_model": "qwen3:30b-instruct",
          "default_chat_status": "available",
          "recommended_chat_reason": "…",
          "chat_providers": {"ollama": {"selectable": true, "reason": ""}}
        }

    Une entrée `chat` porte `generation_status` (supported | unsupported |
    unsafe_output | not_verified) ; seule `supported` est `allowed`.

    `stale=true` signale un catalogue servi depuis le dernier état connu, la passerelle
    étant momentanément injoignable. C'est une information d'exploitation : l'appelant
    doit pouvoir savoir qu'il lit une photo et non le direct.

    Lisible par tout utilisateur authentifié — il ne contient ni secret, ni URL interne,
    ni digest. `?refresh=1` force une relecture amont ; réservé aux administrateurs
    plateforme, un rafraîchissement étant un appel réseau que n'importe qui ne doit pas
    pouvoir déclencher en boucle.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.llm_router.model_catalog import build_catalog, invalidate_catalog_cache
        from apps.llm_router.model_policy import can_manage_ragspace_models

        wants_refresh = str(request.query_params.get("refresh", "")).strip() in ("1", "true", "yes")
        if wants_refresh and can_manage_ragspace_models(request.user):
            invalidate_catalog_cache()

        from apps.llm_router.capabilities import ALLOWED_CHAT_PROVIDERS, chat_provider_error

        payload = build_catalog()
        payload["can_edit_model_configuration"] = can_manage_ragspace_models(request.user)
        # Moteurs de chat sélectionnables pour un NOUVEL état — même règle que celle
        # appliquée à l'enregistrement (`catalog_config_error`). Le formulaire n'en
        # déduit rien : il affiche.
        payload["chat_providers"] = {
            p: {"selectable": chat_provider_error(p) is None, "reason": chat_provider_error(p) or ""}
            for p in ALLOWED_CHAT_PROVIDERS
        }
        status = 200 if payload.get("available") else 503
        return Response(payload, status=status)

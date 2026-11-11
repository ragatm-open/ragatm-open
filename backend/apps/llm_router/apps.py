from django.apps import AppConfig
from django.core.checks import register


class LlmRouterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.llm_router"

    def ready(self) -> None:
        # System checks — politique IA (AI Gateway obligatoire,
        # jamais d'Ollama direct, fallback contrôlé).
        from .checks import (
            check_ai_gateway_base_url,
            check_ai_gateway_fallback_provider,
            check_ai_gateway_mandatory,
            check_gateway_endpoints,
            check_gateway_routes_consistent,
            check_no_direct_ollama,
            check_no_external_provider_configuration,
            check_test_escape_hatch_scope,
        )

        register(check_ai_gateway_mandatory)
        register(check_ai_gateway_base_url)
        register(check_no_direct_ollama)
        register(check_ai_gateway_fallback_provider)
        register(check_no_external_provider_configuration)
        # Les TROIS URL Gateway (génération, embeddings, rerank) : cibles gouvernées seulement,
        # Gateway directe ou AI Gateway Pool.
        register(check_gateway_endpoints)
        register(check_gateway_routes_consistent)
        # L'échappatoire de tests ne doit jamais éteindre les gardes ci-dessus
        # dans un environnement réel, où seul un Warning la signalerait.
        register(check_test_escape_hatch_scope)

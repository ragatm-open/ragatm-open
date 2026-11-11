"""Client interne AI Gateway (PR 5a — inerte).

Surface : ``from apps.llm_router.gateway import AIGatewayClient, gateway_enabled``.
Voir ``client`` pour les garanties (aucun appel réseau si désactivé, erreurs typées).
"""
from .client import AIGatewayClient, gateway_enabled
from .dto import GatewayGenerationRequest, GatewayGenerationResult
from .exceptions import (
    GatewayAuthenticationError,
    GatewayConfigError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayDisabledError,
    GatewayError,
    GatewayRateLimited,
    GatewayResponseError,
    GatewayStreamError,
    GatewayTemporaryUnavailable,
    GatewayTimeoutError,
    GatewayUnavailableError,
    VisionContractDrift,
    VisionGatewayError,
    classify_http_status,
    is_fallback_eligible,
)

__all__ = [
    "AIGatewayClient",
    "gateway_enabled",
    "GatewayGenerationRequest",
    "GatewayGenerationResult",
    "GatewayError",
    "GatewayDisabledError",
    "GatewayConfigError",
    "GatewayConfigurationError",
    "GatewayTimeoutError",
    "GatewayResponseError",
    "GatewayTemporaryUnavailable",
    "GatewayRateLimited",
    "GatewayAuthenticationError",
    "GatewayContractError",
    "GatewayStreamError",
    "GatewayUnavailableError",
    "VisionGatewayError",
    "VisionContractDrift",
    "classify_http_status",
    "is_fallback_eligible",
]

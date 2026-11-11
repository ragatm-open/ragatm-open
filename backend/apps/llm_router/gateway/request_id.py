"""Corrélation ``X-Request-ID`` vers l'AI Gateway / le Pool (qualification).

L'identifiant transmis est, dans l'ordre : celui porté explicitement par l'appel
(``GatewayGenerationRequest.request_id`` = ``LlmExecutionContext.correlation_id`` = identifiant
de la requête HTTP côté chat), sinon celui de la requête HTTP en cours (``ContextVar`` posée par
``config.middleware.request_id``), sinon rien (la Gateway en génère un). Seule la grammaire du
contrat est relayée ; un identifiant non conforme n'est jamais mis sur le fil. L'identifiant ne
descend QUE dans l'en-tête : le corps JSON des requêtes (schéma public) est inchangé.
"""
from __future__ import annotations

from config.middleware.request_id import (
    REQUEST_ID_HEADER,
    get_current_request_id,
    is_valid_request_id,
    new_request_id,
)

__all__ = ["REQUEST_ID_HEADER", "correlation_headers", "resolve_request_id"]


def resolve_request_id(explicit: str | None = None, *, generate: bool = False) -> str:
    """Identifiant à transmettre, ou ``""``. ``generate=True`` : en fabrique un hors requête
    HTTP (tâches Celery) afin que le journal local et celui de la Gateway partagent une clé."""
    if is_valid_request_id(explicit):
        return explicit  # type: ignore[return-value]
    current = get_current_request_id()
    if is_valid_request_id(current):
        return current
    return new_request_id() if generate else ""


def correlation_headers(explicit: str | None = None, *, generate: bool = False) -> dict[str, str]:
    rid = resolve_request_id(explicit, generate=generate)
    return {REQUEST_ID_HEADER: rid} if rid else {}

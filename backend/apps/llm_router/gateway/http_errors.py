"""Projection HTTP des erreurs du client AI Gateway (qualification interne, durcissement ciblé).

Auparavant, toute défaillance de l'AI Gateway remontait aux vues comme une
exception quelconque et devenait un ``500 internal_error`` opaque. Ce module donne
à la frontière HTTP/SSE une classification STABLE et fermée :

* indisponibilité transitoire (connexion refusée, 5xx amont, délai dépassé,
  politique « aucun repli ») => ``503 ai_gateway_unavailable`` + ``Retry-After`` ;
* défaut de configuration ou de contrat (Gateway désactivée, URL absente,
  authentification refusée, réponse hors contrat) => ``502 ai_gateway_error``,
  non réessayable : c'est un défaut de déploiement à corriger, pas une panne.

Aucun détail interne (message d'exception, URL, trace) n'est exposé : seul le code
stable et le ``request_id`` sortent. Le journal serveur conserve l'exception.
Ce module ne décide JAMAIS d'un repli : il ne fait que nommer l'échec.
"""
from __future__ import annotations

from dataclasses import dataclass

from .exceptions import (
    GatewayAuthenticationError,
    GatewayConfigError,
    GatewayContractError,
    GatewayDisabledError,
    GatewayError,
    GatewayTemporaryUnavailable,
    GatewayTimeoutError,
    GatewayUnavailableError,
)

#: Délai (secondes) conseillé au client avant une nouvelle tentative.
RETRY_AFTER_SECONDS = 10

#: Codes stables (contrat d'erreur de l'édition Open).
CODE_UNAVAILABLE = "ai_gateway_unavailable"
CODE_ERROR = "ai_gateway_error"


@dataclass(frozen=True)
class GatewayHttpError:
    status: int
    detail: str
    retryable: bool

    def payload(self, *, request_id: str | None) -> dict:
        return {"detail": self.detail, "request_id": request_id, "retryable": self.retryable}

    def headers(self) -> dict:
        return {"Retry-After": str(RETRY_AFTER_SECONDS)} if self.retryable else {}


_TRANSIENT = (GatewayUnavailableError, GatewayTemporaryUnavailable, GatewayTimeoutError)
_DEPLOYMENT = (GatewayDisabledError, GatewayConfigError, GatewayAuthenticationError, GatewayContractError)


def classify_for_http(exc: BaseException) -> GatewayHttpError | None:
    """Rend la projection HTTP d'une ``GatewayError`` (ou ``None`` si ce n'en est pas une).

    Une sous-classe inconnue de ``GatewayError`` est traitée comme transitoire :
    une panne nommée vaut mieux qu'un 500 anonyme, et ``retryable`` reste vrai, ce
    qui est le comportement le moins surprenant pour un client.
    """
    if isinstance(exc, _TRANSIENT):
        return GatewayHttpError(503, CODE_UNAVAILABLE, True)
    if isinstance(exc, _DEPLOYMENT):
        return GatewayHttpError(502, CODE_ERROR, False)
    if isinstance(exc, GatewayError):
        return GatewayHttpError(503, CODE_UNAVAILABLE, True)
    return None


def find_gateway_error(exc: BaseException, *, max_depth: int = 6) -> GatewayError | None:
    """Remonte la chaîne ``__cause__``/``__context__`` jusqu'à une ``GatewayError``.

    Les services intermédiaires (retrieval, embeddings) enveloppent parfois la
    défaillance d'origine (``raise ... from exc``) : la cause reste classifiable.
    """
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth <= max_depth:
        if isinstance(cur, GatewayError):
            return cur
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return None

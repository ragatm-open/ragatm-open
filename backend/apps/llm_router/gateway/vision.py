"""Client AI Gateway Vision V1 — ``POST /v1/vision/analyze``.

Chemin unique : RAGatm -> AI Gateway -> backend modèle. Aucun appel direct à un
serveur de modèles, aucun repli externe : une image ne quitte jamais le Gateway.

Garanties :

* passerelle désactivée ou sans URL : AUCUN appel réseau ;
* UN appel HTTP par ``analyze`` — aucune relance cachée (Celery possède les relances,
  le Gateway possède sa propre relance de sortie malformée) ;
* erreurs classées par ``detail.code`` et ``detail.retryable`` uniquement ; tout 4xx
  est forcé non relançable ; le message n'est jamais lu ni conservé ;
* succès validé contre le contrat V1 FERMÉ (``vision_contract``) — un champ en plus
  est une ``VisionContractDrift`` non relançable ;
* journaux : métadonnées seulement — jamais l'image, le contexte ou la charge utile.

Aucun appelant de production dans ce lot : le client existe sans être branché.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests
from django.conf import settings

from . import vision_contract as contract
from .retry_after import parse_retry_after
from .exceptions import VisionContractDrift, VisionGatewayError
from .pool_errors import attach_pool_error, parse_pool_error

logger = logging.getLogger(__name__)

VISION_PATH = "/v1/vision/analyze"
#: Plafond du délai de CONNEXION ; le délai de lecture est le timeout configuré.
_CONNECT_TIMEOUT_CAP_S = 10.0
#: Borne défensive du corps de succès lu (une réponse V1 est un JSON compact).
VISION_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: Codes 5xx sans enveloppe Gateway considérés comme transitoires.
_TRANSIENT_STATUS = frozenset({502, 503, 504})
#: Un ``code`` Gateway est un identifiant ; tout autre contenu est ignoré.
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

CODE_DISABLED = "visual_gateway_disabled"
CODE_MISCONFIGURED = "visual_gateway_misconfigured"
CODE_UNAVAILABLE = "visual_gateway_unavailable"
CODE_CLIENT_TIMEOUT = "visual_client_timeout"
CODE_TRANSPORT = "visual_gateway_transport_error"
CODE_HTTP = "visual_gateway_http_error"
CODE_REQUEST_INVALID = "vision_request_invalid"
CODE_LOCAL_REQUEST_INVALID = "visual_request_invalid"


@dataclass(frozen=True)
class VisionAnalyzeRequest:
    """Corps V1 déjà construit (cf. ``rag_core.services.ingest.visual``)."""

    payload: dict = field(repr=False)

    @property
    def request_id(self) -> str | None:
        return self.payload.get("request_id")


@dataclass(frozen=True)
class VisionAnalyzeResult:
    """Réponse V1 validée. ``data`` est TRANSITOIRE : jamais persistée telle quelle."""

    data: dict = field(repr=False)
    http_status: int = 200


def classify_vision_error(status: int, body, headers=None) -> VisionGatewayError:
    """Erreur typée d'une réponse HTTP non-succès. Le message n'est JAMAIS lu.

    ``retry_after_s`` (CV-3D1) : délai demandé par le Gateway (en-tête ``Retry-After``
    ou ``detail.retry_after_seconds``), lu défensivement ; ``None`` sinon."""
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    request_id = None
    if isinstance(code, str) and _CODE_RE.match(code):
        retryable = detail.get("retryable") is True
        rid = detail.get("request_id")
        request_id = rid if isinstance(rid, str) else None
    elif status in (400, 422):
        # Enveloppe de validation générique sans `code` ni `retryable`.
        code, retryable = CODE_REQUEST_INVALID, False
    else:
        code, retryable = CODE_HTTP, status in _TRANSIENT_STATUS
    if 400 <= status < 500:
        retryable = False
    err = VisionGatewayError(code, retryable=retryable, http_status=status,
                             request_id=request_id)
    err.retry_after_s = parse_retry_after(headers, detail)
    # Refus PROPRE à AI Gateway Pool : code d'origine et identifiant conservés pour le
    # diagnostic, `code` client inchangé (cf. pool_errors.attach_pool_error).
    return attach_pool_error(err, parse_pool_error(status, headers, body))


class VisionGatewayClient:
    def __init__(self, base_url: str, timeout_s: float, enabled: bool) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_s = float(timeout_s)
        self.enabled = bool(enabled)

    @classmethod
    def from_settings(cls) -> "VisionGatewayClient":
        return cls(
            base_url=getattr(settings, "AI_GATEWAY_BASE_URL", "") or "",
            timeout_s=getattr(settings, "AI_GATEWAY_VISION_TIMEOUT_SECONDS", 120),
            enabled=bool(getattr(settings, "AI_GATEWAY_ENABLED", False)),
        )

    def analyze(self, request: VisionAnalyzeRequest) -> VisionAnalyzeResult:
        if not self.enabled:
            raise VisionGatewayError(CODE_DISABLED, retryable=False)
        if not self.base_url:
            raise VisionGatewayError(CODE_MISCONFIGURED, retryable=False)
        try:
            contract.validate_request_shape(request.payload)
        except contract.ContractViolation as exc:
            logger.warning("vision request rejected locally reason=%s", exc.reason)
            raise VisionGatewayError(CODE_LOCAL_REQUEST_INVALID, retryable=False) from None

        request_id = request.request_id
        try:
            resp = requests.post(
                f"{self.base_url}{VISION_PATH}",
                headers={"Content-Type": "application/json"},
                json=request.payload,
                timeout=(min(_CONNECT_TIMEOUT_CAP_S, self.timeout_s), self.timeout_s),
            )
        # ConnectTimeout hérite de ConnectionError ET de Timeout : Timeout d'abord.
        except requests.Timeout:
            logger.warning("vision gateway timeout request_id=%s", request_id)
            raise VisionGatewayError(CODE_CLIENT_TIMEOUT, retryable=True,
                                     request_id=request_id) from None
        except requests.ConnectionError:
            logger.warning("vision gateway unavailable request_id=%s", request_id)
            raise VisionGatewayError(CODE_UNAVAILABLE, retryable=True,
                                     request_id=request_id) from None
        except requests.RequestException:
            logger.warning("vision gateway transport error request_id=%s", request_id)
            raise VisionGatewayError(CODE_TRANSPORT, retryable=False,
                                     request_id=request_id) from None

        status = resp.status_code
        if status != 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            err = classify_vision_error(status, body, getattr(resp, "headers", None))
            logger.warning("vision gateway error http_status=%s code=%s retryable=%s "
                           "request_id=%s pool_code=%s", status, err.code, err.retryable,
                           err.request_id or request_id, getattr(err.pool_error, "code", "-"))
            raise err

        if len(resp.content or b"") > VISION_MAX_RESPONSE_BYTES:
            raise self._drift("response: body too large", request_id)
        try:
            data = resp.json()
        except ValueError:
            raise self._drift("response: invalid JSON", request_id) from None
        try:
            contract.validate_response(data, expected_request_id=request_id,
                                       expected_task=request.payload.get("task"))
        except contract.ContractViolation as exc:
            raise self._drift(exc.reason, request_id) from None
        logger.info("vision gateway ok visual_kind=%s warnings=%d requires_visual_check=%s "
                    "request_id=%s", data["visual_kind"], len(data["warnings"]),
                    data["requires_visual_check"], request_id)
        return VisionAnalyzeResult(data=data, http_status=status)

    @staticmethod
    def _drift(reason: str, request_id: str | None) -> VisionContractDrift:
        logger.warning("vision contract drift reason=%s request_id=%s", reason, request_id)
        return VisionContractDrift(reason, http_status=200, request_id=request_id)

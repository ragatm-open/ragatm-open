"""Client AI Gateway Vision **V2** — rail des crops.

Distinct du client V1 (``vision``), qui reste inchangé et FERMÉ : ce module ne demande
que ``schema_version = "2"`` et n'accepte que des réponses V2, validées par un contrat
tout aussi fermé (``vision_contract_v2``). Aucun repli vers V1.

UN appel HTTP par ``analyze`` — le client ne relance jamais (le propriétaire des
relances est ``rag_core.services.raster.vision.analyze_crop``). Trois portes, toutes
BLOQUANTES et non relançables :

* K2 — contrat : ``schema_version == "2"`` et tous les champs de provenance valides ;
* K1 — intégrité : ``image.source_sha256`` == SHA-256 des octets envoyés ;
* K3 — pipeline : ``pipeline.fingerprint`` ∈ allowlist configurée. ``/health/vision``
  n'approuve jamais rien.

Erreurs : codes stables, jamais d'exception HTTP brute ; le message du Gateway n'est
jamais lu ni conservé. Journaux : métadonnées seulement — ni image, ni Base64, ni hash
d'image, ni charge utile.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import requests

from . import vision_contract_v2 as contract
from .retry_after import parse_retry_after
from .exceptions import VisionGatewayError
from .pool_errors import attach_pool_error, parse_pool_error

logger = logging.getLogger(__name__)

VISION_PATH = "/v1/vision/analyze"
SCHEMA_VERSION = "2"
_CONNECT_TIMEOUT_CAP_S = 10.0
VISION_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: Borne du Gateway sur ``image.data`` (OpenAPI de la Gateway : ``maxLength`` 5 592 408).
IMAGE_DATA_MAX_CHARS = 5_592_408
#: Budget modèle de la Gateway (``vision_timeout_seconds``, staging) : à son
#: expiration elle rend un 504 STRUCTURÉ ``vision_timeout``. Le timeout client doit le
#: dépasser d'une marge, sinon le client coupe juste avant et ne voit qu'un timeout
#: transport (relancé à l'aveugle).
GATEWAY_VISION_BUDGET_S = 60
CLIENT_TIMEOUT_MARGIN_S = 10
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_DETAIL_KEYS = frozenset({"status", "provider", "message", "code", "retryable", "request_id"})
_ERROR_DETAIL_OPTIONAL = frozenset({"retry_after_seconds"})

# -- taxonomie (codes stables) ----------------------------------------------------------
LOCAL_CROP_INTEGRITY_FAILURE = "local_crop_integrity_failure"
VISION_V2_CONTRACT_ERROR = "vision_v2_contract_error"
PROVENANCE_INTEGRITY_FAILURE = "provenance_integrity_failure"
PIPELINE_NOT_ALLOWED = "pipeline_not_allowed"
VISION_GATEWAY_OVERLOADED = "vision_gateway_overloaded"
VISION_GATEWAY_TIMEOUT = "vision_gateway_timeout"
VISION_UPSTREAM_ERROR = "vision_upstream_error"
VISION_REQUEST_INVALID = "vision_request_invalid"
VISION_V2_NOT_CONFIGURED = "vision_v2_not_configured"
VISION_GATEWAY_DISABLED = "visual_gateway_disabled"
VISION_GATEWAY_UNAVAILABLE = "visual_gateway_unavailable"
VISION_CLIENT_TIMEOUT = "visual_client_timeout"
VISION_TRANSPORT_ERROR = "visual_gateway_transport_error"

#: Codes Gateway → code interne. Absents : VISION_UPSTREAM_ERROR (5xx) / REQUEST_INVALID (4xx).
_GATEWAY_CODES = {"vision_overloaded": VISION_GATEWAY_OVERLOADED, "vision_timeout": VISION_GATEWAY_TIMEOUT}


class VisionV2Error(VisionGatewayError):
    """Échec du rail V2. ``retry_after_s`` : délai demandé par le Gateway (503), ou None."""

    def __init__(self, code: str, *, retryable: bool, http_status: Optional[int] = None,
                 request_id: Optional[str] = None, reason: str = "",
                 retry_after_s: Optional[float] = None, gateway_code: Optional[str] = None) -> None:
        super().__init__(code, retryable=retryable, http_status=http_status, request_id=request_id)
        self.reason = reason
        self.retry_after_s = retry_after_s
        self.gateway_code = gateway_code


@dataclass(frozen=True)
class VisionV2Result:
    """Réponse V2 VALIDÉE (portes K1/K2/K3 passées). ``raw`` = JSON validé complet,
    sérialisable, transitoire (la persistance appartient à CV-3F)."""

    schema_version: str
    task: str
    visual_kind: str
    summary: Optional[str]
    payload: Any = field(repr=False)
    warnings: tuple
    requires_visual_check: bool
    model: dict
    pipeline: dict
    image: dict
    usage: dict
    latency_ms: float
    request_id: Optional[str]
    raw: dict = field(repr=False)
    http_status: int = 200

    @property
    def source_sha256(self) -> str:
        return self.image["source_sha256"]

    @property
    def normalized_sha256(self) -> str:
        return self.image["normalized_sha256"]

    @property
    def fingerprint(self) -> str:
        return self.pipeline["fingerprint"]


def classify_error(status: int, body, headers=None, *, request_id: Optional[str]) -> VisionV2Error:
    """Voir ``_classify_error`` ; rattache en plus un éventuel refus PROPRE au pool
    (``X-AIGP-Error``) sans changer le code client (cf. ``pool_errors``)."""
    return attach_pool_error(_classify_error(status, body, headers, request_id=request_id),
                             parse_pool_error(status, headers, body))


def _classify_error(status: int, body, headers=None, *, request_id: Optional[str]) -> VisionV2Error:
    """Erreur typée d'une réponse non-200. L'enveloppe Gateway est validée STRICTEMENT :
    ``{"detail": {status, provider, message, code, retryable, request_id[, retry_after_seconds]}}``.
    Une enveloppe présente mais malformée est une dérive de contrat (non relançable) ; une
    réponse sans enveloppe (proxy, coupure) est classée par statut HTTP."""
    detail = body.get("detail") if isinstance(body, dict) and set(body) == {"detail"} else None
    if isinstance(detail, dict):
        keys = set(detail)
        valid = (_ERROR_DETAIL_KEYS <= keys <= _ERROR_DETAIL_KEYS | _ERROR_DETAIL_OPTIONAL
                 and detail["status"] == "error" and detail["provider"] == "ai_gateway"
                 and isinstance(detail["message"], str) and isinstance(detail["code"], str)
                 and _CODE_RE.match(detail["code"]) and isinstance(detail["retryable"], bool)
                 and (detail["request_id"] is None or isinstance(detail["request_id"], str)))
        if not valid:
            return VisionV2Error(VISION_V2_CONTRACT_ERROR, retryable=False, http_status=status,
                                 request_id=request_id, reason="error envelope")
        gw = detail["code"]
        if 400 <= status < 500:
            return VisionV2Error(VISION_REQUEST_INVALID, retryable=False, http_status=status,
                                 request_id=request_id, gateway_code=gw)
        code = _GATEWAY_CODES.get(gw, VISION_UPSTREAM_ERROR)
        return VisionV2Error(code, retryable=detail["retryable"], http_status=status,
                             request_id=request_id, gateway_code=gw,
                             retry_after_s=parse_retry_after(headers, detail))
    if 400 <= status < 500:
        return VisionV2Error(VISION_REQUEST_INVALID, retryable=False, http_status=status,
                             request_id=request_id)
    return VisionV2Error(VISION_UPSTREAM_ERROR, retryable=status in (502, 503, 504),
                         http_status=status, request_id=request_id,
                         retry_after_s=parse_retry_after(headers, None))


class VisionV2Client:
    def __init__(self, base_url: str, timeout_s: float, enabled: bool,
                 allowed_fingerprints: Sequence[str]) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_s = float(timeout_s)
        self.enabled = bool(enabled)
        self.allowed_fingerprints = frozenset(f.strip() for f in (allowed_fingerprints or ()) if f.strip())

    @classmethod
    def from_settings(cls) -> "VisionV2Client":
        from django.conf import settings

        return cls(
            base_url=getattr(settings, "AI_GATEWAY_BASE_URL", "") or "",
            timeout_s=getattr(settings, "AI_GATEWAY_VISION_TIMEOUT_SECONDS", 120),
            enabled=bool(getattr(settings, "AI_GATEWAY_ENABLED", False)),
            allowed_fingerprints=getattr(settings, "AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS", []) or [],
        )

    def ensure_ready(self) -> None:
        """Refus AVANT tout réseau : passerelle éteinte, sans URL, ou rail V2 non configuré."""
        if not self.enabled:
            raise VisionV2Error(VISION_GATEWAY_DISABLED, retryable=False)
        if not self.base_url or not self.allowed_fingerprints:
            raise VisionV2Error(VISION_V2_NOT_CONFIGURED, retryable=False)
        if self.timeout_s < GATEWAY_VISION_BUDGET_S + CLIENT_TIMEOUT_MARGIN_S:
            raise VisionV2Error(VISION_V2_NOT_CONFIGURED, retryable=False,
                                reason="client timeout does not exceed the gateway vision budget")

    def analyze(self, payload: dict, *, expected_source_sha256: str) -> VisionV2Result:
        """UN appel ; ``payload`` déjà construit (``build_request``)."""
        self.ensure_ready()
        request_id = payload.get("request_id")
        try:
            contract.validate_request(payload)
        except contract.ContractViolation as exc:
            raise VisionV2Error(VISION_REQUEST_INVALID, retryable=False, request_id=request_id,
                                reason=exc.reason) from None
        try:
            resp = requests.post(
                f"{self.base_url}{VISION_PATH}", headers={"Content-Type": "application/json"},
                json=payload, timeout=(min(_CONNECT_TIMEOUT_CAP_S, self.timeout_s), self.timeout_s),
            )
        except requests.Timeout:
            logger.warning("vision v2 client timeout request_id=%s", request_id)
            raise VisionV2Error(VISION_CLIENT_TIMEOUT, retryable=True, request_id=request_id) from None
        except requests.ConnectionError:
            logger.warning("vision v2 gateway unavailable request_id=%s", request_id)
            raise VisionV2Error(VISION_GATEWAY_UNAVAILABLE, retryable=True, request_id=request_id) from None
        except requests.RequestException:
            logger.warning("vision v2 transport error request_id=%s", request_id)
            raise VisionV2Error(VISION_TRANSPORT_ERROR, retryable=False, request_id=request_id) from None

        status = resp.status_code
        if status != 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            err = classify_error(status, body, resp.headers, request_id=request_id)
            logger.warning("vision v2 error http_status=%s code=%s retryable=%s request_id=%s pool_code=%s",
                           status, err.code, err.retryable, err.request_id or request_id,
                           getattr(err.pool_error, "code", "-"))
            raise err

        if len(resp.content or b"") > VISION_MAX_RESPONSE_BYTES:
            raise self._fail(VISION_V2_CONTRACT_ERROR, "response: body too large", request_id)
        try:
            data = resp.json()
        except ValueError:
            raise self._fail(VISION_V2_CONTRACT_ERROR, "response: invalid JSON", request_id) from None
        # K2 — contrat fermé.
        try:
            contract.validate_response(data, expected_request_id=request_id,
                                       expected_task=payload.get("task"))
        except contract.ContractViolation as exc:
            raise self._fail(VISION_V2_CONTRACT_ERROR, exc.reason, request_id) from None
        # K1 — les octets reçus par le Gateway sont ceux qu'on a envoyés.
        if data["image"]["source_sha256"] != expected_source_sha256:
            raise self._fail(PROVENANCE_INTEGRITY_FAILURE, "image.source_sha256", request_id)
        # K3 — pipeline autorisé par CONFIGURATION, jamais par /health/vision.
        if data["pipeline"]["fingerprint"] not in self.allowed_fingerprints:
            raise self._fail(PIPELINE_NOT_ALLOWED, "pipeline.fingerprint", request_id)

        logger.info("vision v2 ok visual_kind=%s warnings=%d requires_visual_check=%s request_id=%s",
                    data["visual_kind"], len(data["warnings"]), data["requires_visual_check"], request_id)
        return VisionV2Result(
            schema_version=data["schema_version"], task=data["task"], visual_kind=data["visual_kind"],
            summary=data["summary"], payload=data["payload"], warnings=tuple(data["warnings"]),
            requires_visual_check=data["requires_visual_check"], model=dict(data["model"]),
            pipeline=dict(data["pipeline"]), image=dict(data["image"]), usage=dict(data["usage"]),
            latency_ms=data["latency_ms"], request_id=data["request_id"], raw=data, http_status=status,
        )

    @staticmethod
    def _fail(code: str, reason: str, request_id: Optional[str]) -> VisionV2Error:
        logger.warning("vision v2 rejected code=%s reason=%s request_id=%s", code, reason, request_id)
        return VisionV2Error(code, retryable=False, http_status=200, request_id=request_id, reason=reason)

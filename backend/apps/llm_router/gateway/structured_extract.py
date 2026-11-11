"""Client AI Gateway ``POST /v1/vision/structured-extract``.

Extraction structurée EXPÉRIMENTALE (PP-StructureV3 derrière le Gateway) : ``chart``
-> catégories/séries/valeurs, ``table`` -> lignes/cellules/HTML natif. Contrat
``structured-extract/1``, distinct de Vision V1 (``/v1/vision/analyze``) : aucun champ
commun hormis l'enveloppe d'image.

Mêmes garanties que le client Vision V1 (``vision``) :

* passerelle désactivée ou sans URL : AUCUN appel réseau ;
* UN appel HTTP par ``extract`` — la relance (bornée, une seule) appartient à l'appelant ;
* erreurs classées par ``detail.code``/``detail.retryable`` (``classify_vision_error``),
  ``Retry-After`` lu ; le message du Gateway n'est jamais lu ni conservé ;
* journaux : métadonnées seulement — jamais l'image ni la charge utile.

Le délai de lecture par défaut couvre le démarrage à froid du moteur (chargement
~13 s mesuré) et le timeout propre du Gateway (150 s) : un délai de 10-15 s rendrait le
premier appel mécaniquement impossible.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

from .exceptions import VisionContractDrift, VisionGatewayError
from .vision import (
    _CONNECT_TIMEOUT_CAP_S,
    CODE_CLIENT_TIMEOUT,
    CODE_DISABLED,
    CODE_LOCAL_REQUEST_INVALID,
    CODE_MISCONFIGURED,
    CODE_TRANSPORT,
    CODE_UNAVAILABLE,
    classify_vision_error,
)

logger = logging.getLogger(__name__)

STRUCTURED_EXTRACT_PATH = "/v1/vision/structured-extract"
CONTRACT = "structured-extract/1"
SCHEMA_VERSION = "1"
#: Versions de schéma servies par le contrat ``ai-gateway-visual-contract/22A.1``.
#: La version est CHOISIE par configuration (``STRUCTURED_VISUAL_SCHEMA_VERSION``), jamais découverte par essais :
#: la production Gateway V1 refuse ``"2"`` en 400.
SCHEMA_VERSIONS = frozenset({"1", "2"})
TASKS = frozenset({"chart", "table"})
MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
#: Borne du Gateway sur ``image.data`` (base64), reprise de son OpenAPI déployée.
MAX_IMAGE_B64_CHARS = 5_592_408
#: Une réponse porte ``raw_output`` (OCR, boîtes de cellules) : plus large que V1.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
#: Clés de succès obligatoires ; les autres sont tolérées (contrat expérimental OUVERT).
_REQUIRED_KEYS = ("status", "contract", "task", "experimental", "extraction_status", "items",
                  "warnings", "engine")


@dataclass(frozen=True)
class StructuredExtractResult:
    """Réponse validée. ``data`` est la réponse COMPLÈTE (``raw_output`` compris)."""

    data: dict = field(repr=False)
    http_status: int = 200


def build_request(*, task: str, content: bytes, mime_type: str, request_id: str | None = None,
                  schema_version: str = SCHEMA_VERSION) -> dict:
    if schema_version not in SCHEMA_VERSIONS:
        raise VisionGatewayError(CODE_MISCONFIGURED, retryable=False)
    if task not in TASKS:
        raise VisionGatewayError(CODE_LOCAL_REQUEST_INVALID, retryable=False)
    if mime_type not in MIME_TYPES or not content:
        raise VisionGatewayError(CODE_LOCAL_REQUEST_INVALID, retryable=False)
    data = base64.b64encode(bytes(content)).decode("ascii")
    if len(data) > MAX_IMAGE_B64_CHARS:
        raise VisionGatewayError("structured_image_too_large", retryable=False)
    payload = {"schema_version": schema_version, "task": task,
               "image": {"data": data, "mime_type": mime_type}}
    if request_id:
        payload["request_id"] = request_id[:128]
    return payload


def validate_response(data, *, task: str, request_id: str | None, schema_version: str = SCHEMA_VERSION) -> None:
    """Contrôle MINIMAL : contrat, tâche, écho de requête, forme des éléments.

    Ne réécrit rien ; lève ``VisionContractDrift`` (non relançable) avec un CHEMIN de
    champ, jamais une valeur."""
    def drift(reason: str):
        return VisionContractDrift(reason, http_status=200, request_id=request_id)

    if not isinstance(data, dict):
        raise drift("response: object expected")
    for key in _REQUIRED_KEYS:
        if key not in data:
            raise drift(f"response.{key}: missing")
    if data["contract"] != CONTRACT:
        raise drift("response.contract: unexpected")
    if data["task"] != task:
        raise drift("response.task: mismatch")
    # Écho de version : une réponse d'une autre version que celle demandée n'est jamais interprétée.
    if str(data.get("schema_version", SCHEMA_VERSION)) != schema_version:
        raise drift("response.schema_version: mismatch")
    if schema_version == "2":
        # Schéma "2" : blocs de preuve et de qualification OBLIGATOIRES (gateway-provenance-contract.md).
        if not isinstance(data.get("evidence"), dict) or not isinstance(data["evidence"].get("certified"), bool):
            raise drift("response.evidence: missing or invalid")
        if not isinstance(data.get("qualification"), dict):
            raise drift("response.qualification: missing or invalid")
    if request_id and data.get("request_id") not in (None, request_id):
        raise drift("response.request_id: mismatch")
    if not isinstance(data["items"], list) or not isinstance(data["warnings"], list):
        raise drift("response.items: list expected")
    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            raise drift(f"response.items[{i}]: object expected")
        if task == "chart":
            if not isinstance(item.get("categories"), list) or not isinstance(item.get("series"), list):
                raise drift(f"response.items[{i}]: categories/series expected")
            for s in item["series"]:
                if not isinstance(s, dict) or not isinstance(s.get("values"), list):
                    raise drift(f"response.items[{i}].series: values expected")
        elif not isinstance(item.get("cells"), list):
            raise drift(f"response.items[{i}].cells: list expected")


class StructuredExtractClient:
    def __init__(self, base_url: str, timeout_s: float, enabled: bool,
                 schema_version: str = SCHEMA_VERSION) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_s = float(timeout_s)
        self.enabled = bool(enabled)
        self.schema_version = str(schema_version or SCHEMA_VERSION).strip()

    @classmethod
    def from_settings(cls) -> "StructuredExtractClient":
        return cls(
            base_url=getattr(settings, "AI_GATEWAY_BASE_URL", "") or "",
            timeout_s=getattr(settings, "STRUCTURED_VISUAL_TIMEOUT_SECONDS", 180),
            enabled=bool(getattr(settings, "AI_GATEWAY_ENABLED", False)),
            schema_version=getattr(settings, "STRUCTURED_VISUAL_SCHEMA_VERSION", SCHEMA_VERSION),
        )

    def extract(self, *, task: str, content: bytes, mime_type: str,
                request_id: str | None = None) -> StructuredExtractResult:
        if not self.enabled:
            raise VisionGatewayError(CODE_DISABLED, retryable=False)
        if not self.base_url:
            raise VisionGatewayError(CODE_MISCONFIGURED, retryable=False)
        payload = build_request(task=task, content=content, mime_type=mime_type, request_id=request_id,
                                schema_version=self.schema_version)
        try:
            resp = requests.post(
                f"{self.base_url}{STRUCTURED_EXTRACT_PATH}",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=(min(_CONNECT_TIMEOUT_CAP_S, self.timeout_s), self.timeout_s),
            )
        except requests.Timeout:
            logger.warning("structured extract timeout task=%s request_id=%s", task, request_id)
            raise VisionGatewayError(CODE_CLIENT_TIMEOUT, retryable=True, request_id=request_id) from None
        except requests.ConnectionError:
            logger.warning("structured extract unavailable task=%s request_id=%s", task, request_id)
            raise VisionGatewayError(CODE_UNAVAILABLE, retryable=True, request_id=request_id) from None
        except requests.RequestException:
            logger.warning("structured extract transport error task=%s request_id=%s", task, request_id)
            raise VisionGatewayError(CODE_TRANSPORT, retryable=False, request_id=request_id) from None

        status = resp.status_code
        if status != 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            err = classify_vision_error(status, body, getattr(resp, "headers", None))
            logger.warning("structured extract error http_status=%s code=%s retryable=%s task=%s "
                           "request_id=%s pool_code=%s", status, err.code, err.retryable, task,
                           err.request_id or request_id, getattr(err.pool_error, "code", "-"))
            raise err

        if len(resp.content or b"") > MAX_RESPONSE_BYTES:
            raise VisionContractDrift("response: body too large", http_status=200, request_id=request_id)
        try:
            data = resp.json()
        except ValueError:
            raise VisionContractDrift("response: invalid JSON", http_status=200,
                                      request_id=request_id) from None
        validate_response(data, task=task, request_id=request_id, schema_version=self.schema_version)
        logger.info("structured extract ok task=%s schema=%s extraction_status=%s items=%d warnings=%d "
                    "certified=%s request_id=%s", task, self.schema_version, data.get("extraction_status"),
                    len(data["items"]), len(data["warnings"]),
                    (data.get("evidence") or {}).get("certified") if isinstance(data.get("evidence"), dict) else None,
                    request_id)
        return StructuredExtractResult(data=data, http_status=status)

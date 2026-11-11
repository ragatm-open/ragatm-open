"""Analyse Vision **V2** d'un ``Crop`` — PROPRIÉTAIRE UNIQUE des relances.

    Crop (fichier immuable) ─K1 local─► octets EXACTS ─Base64─► Gateway V2
        ─K2 contrat─► K1 source_sha256 == crop_sha256 ─K3 fingerprint autorisé─► résultat

* K1 local : les octets relus du stockage doivent avoir ``Crop.crop_sha256`` — sinon
  ``local_crop_integrity_failure`` et AUCUN appel. Les octets partent tels quels : pas
  de décodage, de ré-encodage ni de recompression entre le stockage et la requête.
* Relances (``RETRY_OWNER = analyze_crop``) : le client HTTP n'en fait aucune ; cette
  fonction rejoue SEULEMENT les échecs relançables, avec la politique du rail
  historique (``ASSET_MAX_RETRIES`` = 3, attente 2/4/8 s) et :
  - ``Retry-After`` (503) honoré : attente = max(backoff, Retry-After) ;
  - budget MURAL strict : une relance n'est tentée que si attente + timeout client
    tiennent avant l'échéance (le rail V1 ne comptait que l'attente) ;
  - à l'épuisement, l'erreur finale est marquée NON relançable (``exhausted``) : aucune
    couche appelante (Celery, orchestration) ne peut multiplier les tentatives.
* Portes K1/K2/K3 : échecs d'intégrité/contrat, jamais relancés, jamais dégradés en
  avertissement.

Aucune persistance (``VisualObservation`` = CV-3F). L'orchestration (CV-3E1) observe
chaque tentative par deux crochets ADDITIFS et typés — jamais par les journaux :

* ``before_attempt(n)`` est appelé AVANT chaque envoi ; il peut lever pour l'empêcher
  (budget modèle épuisé, bail perdu) : l'exception remonte telle quelle, aucun POST ;
* ``after_attempt(AttemptOutcome)`` est appelé après chaque tentative ÉMISE, avec sa
  classe (``classify_attempt``) et ``model_consumed`` — faux UNIQUEMENT pour un 503
  ``vision_overloaded`` OBSERVÉ (refus d'admission prouvé par la Gateway) ;
* ``before_wait(n, wait_s)`` (CV-3E4A) est appelé avant chaque attente de relance ; il
  peut lever pour abandonner sans attendre (porte fermée, bail perdu).
Ces crochets CONSULTENT ; ils ne relancent jamais : ``analyze_crop`` reste l'unique
propriétaire des relances HTTP. Sans crochet, le comportement est celui de CV-3D.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.core.files.storage import default_storage

from apps.llm_router.gateway import vision_v2 as v2
from apps.rag_core.models import Crop
from apps.rag_core.models.page_raster import MAX_CROP_PIXELS
from apps.rag_core.services.ingest.visual.contracts import VisualContext, logical_model
from apps.rag_core.services.ingest.visual.rail import ASSET_MAX_RETRIES, DEFAULT_LOCALE, _retry_budget_s

logger = logging.getLogger(__name__)

RETRY_OWNER = "apps.rag_core.services.raster.vision.analyze_crop"
#: Garde finale (ragatm-crop/2) : octets du crop encore au-delà du plafond Base64 de la
#: Gateway. Jamais redimensionné ni recompressé : le crop est INÉLIGIBLE (terminal).
CROP_ENCODED_OVERSIZE = "crop_encoded_oversize"
#: Appels HTTP maximum pour UN crop, toutes couches confondues.
MAX_HTTP_ATTEMPTS = 1 + ASSET_MAX_RETRIES
DEFAULT_TASK = "describe"
_sleep = time.sleep
_clock = time.monotonic


# -- classes de tentative (CV-3E1) -------------------------------------------------
ATTEMPT_SUCCESS = "success"
ATTEMPT_OVERLOADED = "overloaded_503"            # admission refusée : aucune inférence
ATTEMPT_MODEL_TIMEOUT = "model_timeout_504"
ATTEMPT_REJECTED = "rejected_4xx"
ATTEMPT_AMBIGUOUS = "transport_ambiguous"        # timeout client / connexion / transport
ATTEMPT_PROVENANCE = "provenance_failure"        # 200 refusé par K1/K2/K3
ATTEMPT_UPSTREAM = "upstream_error"              # autre 5xx
_PROVENANCE_CODES = frozenset({v2.VISION_V2_CONTRACT_ERROR, v2.PROVENANCE_INTEGRITY_FAILURE,
                               v2.PIPELINE_NOT_ALLOWED})
_AMBIGUOUS_CODES = frozenset({v2.VISION_CLIENT_TIMEOUT, v2.VISION_GATEWAY_UNAVAILABLE,
                              v2.VISION_TRANSPORT_ERROR})


@dataclass(frozen=True)
class AttemptOutcome:
    attempt: int
    kind: str
    code: Optional[str]
    http_status: Optional[int]
    #: Faux UNIQUEMENT pour un 503 ``vision_overloaded`` observé : la réservation de budget
    #: modèle peut alors être rendue. Tout autre cas (200, 504, timeout, coupure) consomme.
    model_consumed: bool
    retry_after_s: Optional[float] = None


def classify_attempt(attempt: int, exc: Optional[v2.VisionV2Error]) -> AttemptOutcome:
    if exc is None:
        return AttemptOutcome(attempt, ATTEMPT_SUCCESS, None, 200, True)
    status, code = exc.http_status, exc.code
    if status == 503 and exc.gateway_code == "vision_overloaded":
        kind = ATTEMPT_OVERLOADED
    elif code in _PROVENANCE_CODES:
        kind = ATTEMPT_PROVENANCE
    elif code == v2.VISION_GATEWAY_TIMEOUT or status == 504:
        kind = ATTEMPT_MODEL_TIMEOUT
    elif status is not None and 400 <= status < 500:
        kind = ATTEMPT_REJECTED
    elif code in _AMBIGUOUS_CODES or status is None:
        kind = ATTEMPT_AMBIGUOUS
    else:
        kind = ATTEMPT_UPSTREAM
    return AttemptOutcome(attempt, kind, code, status, kind != ATTEMPT_OVERLOADED,
                          getattr(exc, "retry_after_s", None))


@dataclass(frozen=True)
class CropAnalysis:
    crop_id: str
    result: v2.VisionV2Result
    attempts: int


def _backoff_s(retry_idx: int) -> float:
    from apps.rag_core.tasks import _backoff_s as backoff

    return float(backoff(retry_idx))


def crop_request_id(crop: Crop, *, task: str, context: VisualContext) -> str:
    """Corrélation de traces (PAS une provenance) : stable pour un même crop, une même
    tâche et un même contexte — donc identique d'une relance à l'autre."""
    seed = f"{crop.id}|{crop.crop_sha256}|{task}|{context.fingerprint()}"
    return "rgv2-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def read_crop_bytes(crop: Crop) -> bytes:
    """Octets EXACTS du fichier de crop, vérifiés contre ``Crop.crop_sha256`` (K1 local)."""
    if not crop.storage_key or not crop.crop_sha256:
        raise v2.VisionV2Error(v2.VISION_REQUEST_INVALID, retryable=False, reason="crop_not_sendable")
    try:
        with default_storage.open(crop.storage_key, "rb") as f:
            data = f.read()
    except OSError:
        raise v2.VisionV2Error(v2.LOCAL_CROP_INTEGRITY_FAILURE, retryable=False,
                               reason="crop file unreadable") from None
    if hashlib.sha256(data).hexdigest() != crop.crop_sha256:
        raise v2.VisionV2Error(v2.LOCAL_CROP_INTEGRITY_FAILURE, retryable=False,
                               reason="stored bytes differ from Crop.crop_sha256")
    return data


def build_request(crop: Crop, data: bytes, *, task: str = DEFAULT_TASK,
                  context: Optional[VisualContext] = None) -> dict:
    """Corps V2. ``image.data`` = Base64 des octets EXACTS reçus (aucune transformation)."""
    if crop.width * crop.height > MAX_CROP_PIXELS:
        raise v2.VisionV2Error(v2.VISION_REQUEST_INVALID, retryable=False, reason="crop over 2 MP")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > v2.IMAGE_DATA_MAX_CHARS:
        raise v2.VisionV2Error(CROP_ENCODED_OVERSIZE, retryable=False,
                               reason="crop bytes exceed gateway image.data limit")
    ctx = context or VisualContext()
    payload = {
        "schema_version": v2.SCHEMA_VERSION,
        "task": task,
        "locale": DEFAULT_LOCALE,
        "request_id": crop_request_id(crop, task=task, context=ctx),
        "image": {"data": encoded, "mime_type": "image/png"},
        "context": ctx.as_request(),
    }
    from django.conf import settings

    model = logical_model(getattr(settings, "AI_GATEWAY_VISION_MODEL", "") or None)
    if model:
        payload["model"] = model
    return payload


def analyze_crop(crop: Crop, *, task: str = DEFAULT_TASK, context: Optional[VisualContext] = None,
                 client: Optional[v2.VisionV2Client] = None, budget_s: Optional[float] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 before_attempt: Optional[Callable[[int], None]] = None,
                 after_attempt: Optional[Callable[[AttemptOutcome], None]] = None,
                 before_wait: Optional[Callable[[int, float], None]] = None) -> CropAnalysis:
    """Analyse V2 d'un crop encodé ; au plus ``MAX_HTTP_ATTEMPTS`` appels, budget mural strict.

    ``before_attempt`` / ``after_attempt`` : crochets d'orchestration (cf. en-tête)."""
    client = client or v2.VisionV2Client.from_settings()
    client.ensure_ready()                      # rail non configuré : aucun octet lu, aucun appel
    data = read_crop_bytes(crop)               # K1 local, AVANT tout appel
    payload = build_request(crop, data, task=task, context=context)
    now = clock or _clock
    deadline = now() + (budget_s if budget_s is not None else _retry_budget_s())
    attempts = 0
    while True:
        attempts += 1
        if before_attempt is not None:
            before_attempt(attempts)           # peut lever : aucun envoi
        try:
            result = client.analyze(payload, expected_source_sha256=crop.crop_sha256)
        except v2.VisionV2Error as exc:
            exc.attempts = attempts
            if after_attempt is not None:
                after_attempt(classify_attempt(attempts, exc))
            if not exc.retryable:
                raise
            wait = _backoff_s(attempts)
            if exc.retry_after_s is not None:
                wait = max(wait, exc.retry_after_s)
            if attempts >= MAX_HTTP_ATTEMPTS or now() + wait + client.timeout_s > deadline:
                exc.retryable = False          # épuisé : aucune couche ne relance au-dessus
                exc.exhausted = True
                logger.warning("vision v2 crop exhausted code=%s attempts=%d crop=%s",
                               exc.code, attempts, crop.id)
                raise
            logger.warning("vision v2 crop retry code=%s attempt=%d/%d wait=%ss crop=%s",
                           exc.code, attempts, MAX_HTTP_ATTEMPTS, wait, crop.id)
            if before_wait is not None:
                before_wait(attempts, wait)    # peut lever : abandon sans attendre
            (sleep or _sleep)(wait)
            continue
        if after_attempt is not None:
            after_attempt(classify_attempt(attempts, None))
        return CropAnalysis(crop_id=str(crop.id), result=result, attempts=attempts)

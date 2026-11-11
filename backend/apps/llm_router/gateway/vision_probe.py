"""Sondes LECTURE SEULE du rail Vision V2 : capacité et préflight.

Deux GET, jamais d'inférence, jamais de relance (un appelant = au plus un appel) :

* ``GET /v1/vision/capacity`` — CONSULTATIF (``advisory = true``) : il peut éviter un POST
  voué au 503, il n'autorise jamais rien. Le POST ``/v1/vision/analyze`` reste
  l'AUTORITÉ. Toute sonde inexploitable (timeout, connexion, 5xx, JSON invalide, contrat
  inattendu) rend ``UNKNOWN`` : l'appelant tente le POST, il ne reporte pas.
  ``model.resident = false`` n'est PAS une saturation (modèle installé, à charger).
* ``GET /health/vision`` — préflight d'ENFILEMENT : la Gateway sert-elle le schéma ``"2"``
  pour la tâche, et quelle empreinte de pipeline ? L'empreinte servie n'est retenue que si
  elle figure dans l'allowlist configurée (porte K3, fail-closed : allowlist vide = rien).
  ``/health/vision`` diagnostique, il n'approuve jamais.

Journaux : métadonnées seulement (statut HTTP, décision, raison courte).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

import requests

logger = logging.getLogger(__name__)

CAPACITY_PATH = "/v1/vision/capacity"
HEALTH_VISION_PATH = "/health/vision"
CAPACITY_CONTRACT_VERSION = "1"
#: Les sondes sont courtes : une sonde lente ne doit pas manger le budget du POST.
PROBE_CONNECT_TIMEOUT_S = 2.0
PROBE_READ_TIMEOUT_S = 5.0
_MAX_PROBE_BYTES = 256 * 1024

CAPACITY_AVAILABLE = "available"
CAPACITY_SATURATED = "saturated"
CAPACITY_UNKNOWN = "unknown"
_KNOWN_STATUSES = frozenset({CAPACITY_AVAILABLE, CAPACITY_SATURATED})


@dataclass(frozen=True)
class CapacityProbe:
    decision: str                       # available | saturated | unknown
    retry_after_s: Optional[float] = None
    reason: str = ""
    http_status: Optional[int] = None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return (_is_int(v) or isinstance(v, float)) and math.isfinite(v)


def _get_json(url: str):
    """(status, body | None, reason). Ne lève jamais."""
    try:
        resp = requests.get(url, timeout=(PROBE_CONNECT_TIMEOUT_S, PROBE_READ_TIMEOUT_S),
                            headers={"Cache-Control": "no-store"})
    except requests.Timeout:
        return None, None, "timeout"
    except requests.RequestException:
        return None, None, "connection"
    if resp.status_code != 200:
        return resp.status_code, None, f"http_{resp.status_code}"
    if len(resp.content or b"") > _MAX_PROBE_BYTES:
        return 200, None, "body_too_large"
    try:
        return 200, resp.json(), ""
    except ValueError:
        return 200, None, "invalid_json"


def parse_capacity(body) -> CapacityProbe:
    """Validation STRICTE des champs utilisés ; tout écart → ``UNKNOWN`` (repli POST)."""
    def unknown(reason):
        return CapacityProbe(CAPACITY_UNKNOWN, reason=reason, http_status=200)

    if not isinstance(body, dict):
        return unknown("not_an_object")
    if (body.get("provider") != "ai_gateway" or body.get("contract_version") != CAPACITY_CONTRACT_VERSION
            or body.get("scope") != "instance" or body.get("advisory") is not True):
        return unknown("contract_header")
    vision = body.get("vision")
    if not isinstance(vision, dict):
        return unknown("vision_missing")
    status = vision.get("status")
    slots, inflight, maxc = vision.get("available_slots"), vision.get("in_flight"), vision.get("max_concurrency")
    retry_after = vision.get("retry_after_seconds")
    if (not isinstance(vision.get("enabled"), bool) or status not in _KNOWN_STATUSES
            or not (_is_int(slots) and slots >= 0) or not (_is_int(inflight) and inflight >= 0)
            or not (_is_int(maxc) and maxc >= 1)
            or not (retry_after is None or (_is_num(retry_after) and retry_after >= 0))):
        return unknown("vision_fields")
    model = body.get("model")
    if model is not None and (not isinstance(model, dict)
                              or not isinstance(model.get("resident", False), bool)):
        return unknown("model_fields")
    if not vision["enabled"]:
        # Rail éteint côté Gateway : pas une saturation. Le POST dira la vérité.
        return unknown("vision_disabled")
    ra = float(retry_after) if retry_after is not None else None
    if status == CAPACITY_SATURATED or slots == 0:
        return CapacityProbe(CAPACITY_SATURATED, retry_after_s=ra, http_status=200)
    return CapacityProbe(CAPACITY_AVAILABLE, retry_after_s=ra, http_status=200)


def probe_capacity(base_url: str) -> CapacityProbe:
    """UNE sonde. Jamais de boucle, jamais de cache entre appels."""
    status, body, reason = _get_json(f"{(base_url or '').rstrip('/')}{CAPACITY_PATH}")
    if body is None:
        logger.info("vision capacity probe unusable reason=%s", reason)
        return CapacityProbe(CAPACITY_UNKNOWN, reason=reason, http_status=status)
    probe = parse_capacity(body)
    if probe.decision == CAPACITY_UNKNOWN:
        logger.info("vision capacity probe invalid reason=%s", probe.reason)
    return probe


@dataclass(frozen=True)
class V2Preflight:
    ok: bool
    fingerprint: Optional[str] = None
    reason: str = ""


def parse_health_vision(body, *, task: str, logical_model: Optional[str],
                        allowed_fingerprints: Sequence[str]) -> V2Preflight:
    """Empreinte V2 servie pour ``task`` ET allowlistée, ou un refus motivé."""
    allowed = frozenset(f.strip() for f in (allowed_fingerprints or ()) if f and f.strip())
    if not allowed:
        return V2Preflight(False, reason="fingerprint_allowlist_empty")
    if not isinstance(body, dict) or body.get("enabled") is not True:
        return V2Preflight(False, reason="vision_disabled")
    models = body.get("models")
    if not isinstance(models, list):
        return V2Preflight(False, reason="models_missing")
    wanted = logical_model or body.get("default_model")
    entry = next((m for m in models if isinstance(m, dict) and m.get("model_logical") == wanted), None)
    if entry is None:
        return V2Preflight(False, reason="model_not_served")
    versions = entry.get("schema_versions")
    if not isinstance(versions, list) or "2" not in versions:
        return V2Preflight(False, reason="schema_v2_absent")
    tasks = entry.get("tasks")
    if not isinstance(tasks, list) or task not in tasks:
        return V2Preflight(False, reason="task_not_served")
    pipeline = (entry.get("pipelines") or {}).get(task) if isinstance(entry.get("pipelines"), dict) else None
    fp = pipeline.get("fingerprint") if isinstance(pipeline, dict) else None
    if not isinstance(fp, str) or not fp:
        return V2Preflight(False, reason="fingerprint_missing")
    if fp not in allowed:
        return V2Preflight(False, reason="fingerprint_not_allowed")
    return V2Preflight(True, fingerprint=fp)


def preflight_v2(base_url: str, *, task: str, logical_model: Optional[str],
                 allowed_fingerprints: Sequence[str]) -> V2Preflight:
    """UN GET ``/health/vision`` (consultatif). Aucun retry : un échec = pas d'enfilement."""
    if not frozenset(f for f in (allowed_fingerprints or ()) if f and f.strip()):
        return V2Preflight(False, reason="fingerprint_allowlist_empty")      # aucun réseau
    if not base_url:
        return V2Preflight(False, reason="gateway_url_missing")
    status, body, reason = _get_json(f"{base_url.rstrip('/')}{HEALTH_VISION_PATH}")
    if body is None:
        return V2Preflight(False, reason=f"health_{reason}")
    return parse_health_vision(body, task=task, logical_model=logical_model,
                               allowed_fingerprints=allowed_fingerprints)

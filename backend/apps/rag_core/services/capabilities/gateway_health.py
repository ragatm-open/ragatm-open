"""Sondes AI Gateway NON INFÉRENTIELLES pour le tableau des capacités.

Contrat du pool (``gateway/contracts.py``, champ ``gpu_compute``) : ``/health/ready``,
``/health/embeddings``, ``/health/rerank`` CALCULENT réellement (embedding, rerank) et
``/health/structured-extract`` est traitée comme calculante (coût non documenté). Ce module
ne les appelle donc JAMAIS. Il lit uniquement :

- ``GET /health/runtime`` — registre de la Gateway (environnement, build, modèles de
  génération, section ``rerank``), « appelable en boucle sans coût » ;
- ``GET /v1/models`` — inventaire (dont les modèles d'embeddings et leur statut au registre) ;
- ``GET /health/vision`` et ``GET /health/asr`` — non inférentielles selon le même contrat.

Statuts : AVAILABLE (sondé, prêt) · DECLARED (publié par un inventaire, inférence NON
vérifiée) · DEGRADED · UNAVAILABLE · UNKNOWN (non sondable sans inférence, non configuré ou
illisible). ``DECLARED`` n'est JAMAIS présenté comme « testé ». Appels parallèles bornés
(``TIMEOUT_SECONDS``), cache ``CACHE_TTL_SECONDS``. Aucune URL, aucun jeton renvoyé.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("rag.capabilities.gateway")

TIMEOUT_SECONDS = 3
CACHE_TTL_SECONDS = 30
CACHE_KEY = "ragatm:capabilities:gateway_health:v2"

AVAILABLE, DECLARED, DEGRADED, UNAVAILABLE, UNKNOWN = "AVAILABLE", "DECLARED", "DEGRADED", "UNAVAILABLE", "UNKNOWN"
#: Du plus défavorable au plus favorable (agrégations).
STATUS_ORDER = (UNAVAILABLE, DEGRADED, UNKNOWN, DECLARED, AVAILABLE)

#: Routes Gateway qui déclenchent (ou peuvent déclencher) un calcul de modèle : interdites ici.
FORBIDDEN_PATHS = frozenset({"/health/ready", "/health/embeddings", "/health/rerank", "/health/structured-extract"})

#: Lectures effectuées : (réglage de l'URL de base, chemin). Toutes NON inférentielles.
READS = {
    "runtime": ("AI_GATEWAY_BASE_URL", "/health/runtime"),
    "models": ("AI_GATEWAY_EMBEDDINGS_BASE_URL", "/v1/models"),
    "vision": ("AI_GATEWAY_BASE_URL", "/health/vision"),
    "asr": ("AI_GATEWAY_BASE_URL", "/health/asr"),
    "rerank_runtime": ("RAG_RERANKER_BASE_URL", "/health/runtime"),
}
#: Services présentés (le registre des capacités s'y réfère).
PROBES = ("vision", "structured_extract", "asr", "embeddings", "rerank")
assert not FORBIDDEN_PATHS & {p for _, p in READS.values()}


def _base(setting: str) -> str:
    url = str(getattr(settings, setting, "") or "").strip().rstrip("/")
    if not url and setting != "AI_GATEWAY_BASE_URL":
        url = str(getattr(settings, "AI_GATEWAY_BASE_URL", "") or "").strip().rstrip("/")
    return url


def _get(read: str) -> tuple:
    import requests

    setting, path = READS[read]
    if path in FORBIDDEN_PATHS:  # défense en profondeur
        return None, "forbidden_probe"
    base = _base(setting)
    if not base:
        return None, "not_configured"
    try:
        resp = requests.get(base + path, timeout=(TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException:
        return None, "unreachable"
    if resp.status_code >= 400:
        return None, f"http_{resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return None, "unparseable"
    return (data if isinstance(data, dict) else None), ("" if isinstance(data, dict) else "unparseable")


def _checks_ok(data: dict) -> bool:
    checks = data.get("checks")
    return isinstance(checks, dict) and bool(checks) and all(v is True for v in checks.values())


def _failed(service: str, error: str) -> dict:
    status = UNKNOWN if error in ("not_configured", "unparseable", "forbidden_probe") else UNAVAILABLE
    return {"service": service, "status": status, "reason": error, "verification": "none"}


def _vision(data, error) -> dict:
    if data is None:
        return _failed("vision", error)
    models = [m for m in (data.get("models") or []) if isinstance(m, dict)]
    default = str(data.get("default_model") or "")
    main = next((m for m in models if m.get("model_logical") == default), models[0] if models else {})
    tasks = sorted(main.get("tasks") or [])
    ready = bool(main.get("installed")) and bool(main.get("allowed_here", True)) and main.get("digest_match", True) is not False
    ok = str(data.get("status") or "").lower() == "ok"
    describe = "describe" in tasks
    status = AVAILABLE if ok and data.get("enabled") is not False and ready and describe else (DEGRADED if ok else UNAVAILABLE)
    return {"service": "vision", "status": status, "verification": "health_probe", "environment": data.get("environment"),
            "build": data.get("gateway_build"), "model_logical": main.get("model_logical"), "model_concrete": main.get("concrete"),
            "tasks": tasks, "describe_available": describe and ready, "semantic_enabled": bool(data.get("semantic_enabled"))}


def _asr(data, error) -> dict:
    if data is None:
        return _failed("asr", error)
    ok = str(data.get("status") or "").lower() == "ok"
    status = AVAILABLE if ok and data.get("enabled") is not False and _checks_ok(data) else (DEGRADED if ok else UNAVAILABLE)
    return {"service": "asr", "status": status, "verification": "health_probe", "environment": data.get("environment"),
            "model_logical": data.get("model"), "model_concrete": data.get("artifact"),
            "model_revision": data.get("observed_revision") or data.get("expected_revision"),
            "capabilities": sorted(data.get("capabilities") or [])}


def _embeddings(data, error, runtime_ok: bool) -> dict:
    if data is None:
        return _failed("embeddings", error)
    models = [m for m in (data.get("models") or []) if isinstance(m, dict)
              and (m.get("kind") == "embedding" or "embedding" in (m.get("capabilities") or []))]
    declared = [m for m in models if m.get("model_logical") and m.get("registry_status", "ready") == "ready"]
    status = DECLARED if declared else (DEGRADED if models else UNKNOWN)
    return {"service": "embeddings", "status": status, "verification": "inventory_only",
            "runtime_reachable": runtime_ok,
            "models": [{"model_logical": m.get("model_logical"), "model_concrete": m.get("name"),
                        "dimension": m.get("dimension"), "declared": m in declared} for m in models if m.get("model_logical")]}


def _rerank(data, error) -> dict:
    if data is None:
        return _failed("rerank", error)
    section = data.get("rerank") if isinstance(data.get("rerank"), dict) else {}
    models = [m for m in (section.get("models") or []) if isinstance(m, dict)]
    default = str(section.get("default_model") or "")
    main = next((m for m in models if m.get("model_logical") == default), models[0] if models else {})
    status = DECLARED if section.get("enabled") and main else (UNAVAILABLE if section.get("enabled") is False else UNKNOWN)
    return {"service": "rerank", "status": status, "verification": "registry_only", "environment": data.get("environment"),
            "model_logical": main.get("model_logical"), "model_concrete": main.get("artifact"), "model_revision": main.get("revision")}


def _structured_extract(runtime_ok: bool) -> dict:
    # Aucune route non inférentielle ne publie l'état de StructX : jamais sondé ici.
    return {"service": "structured_extract", "status": UNKNOWN, "reason": "no_non_inferential_probe",
            "verification": "none", "runtime_reachable": runtime_ok}


def probe_all() -> dict:
    """Lectures parallèles ; une lecture lente ou en échec n'invalide pas les autres."""
    started = time.time()
    same_rerank_base = _base("RAG_RERANKER_BASE_URL") == _base("AI_GATEWAY_BASE_URL")
    reads = [r for r in READS if not (r == "rerank_runtime" and same_rerank_base)]
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futures = {r: pool.submit(_get, r) for r in reads}
        raw = {}
        for r, fut in futures.items():
            try:
                raw[r] = fut.result(timeout=TIMEOUT_SECONDS + 2)
            except Exception:  # noqa: BLE001
                raw[r] = (None, "timeout")
    if same_rerank_base:
        raw["rerank_runtime"] = raw["runtime"]
    runtime, runtime_err = raw["runtime"]
    runtime_ok = runtime is not None
    services = {
        "vision": _vision(*raw["vision"]),
        "structured_extract": _structured_extract(runtime_ok),
        "asr": _asr(*raw["asr"]),
        "embeddings": _embeddings(*raw["models"], runtime_ok),
        "rerank": _rerank(*raw["rerank_runtime"]),
    }
    gateway = {"status": AVAILABLE if runtime_ok else _failed("runtime", runtime_err)["status"],
               "reason": "" if runtime_ok else runtime_err,
               "environment": (runtime or {}).get("environment"),
               "build": ((runtime or {}).get("build") or {}).get("git_sha")}
    return {"services": services, "gateway_runtime": gateway, "probed_at": started, "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "paths_read": sorted({READS[r][1] for r in reads})}


def probes_enabled() -> bool:
    """Porte des sondes. Fermée par défaut : tant que le
    caractère non inférentiel des quatre routes n'est pas attesté sur le code Gateway déployé,
    ouvrir la console d'administration ne doit déclencher AUCUN appel Gateway."""
    return bool(getattr(settings, "CAPABILITIES_GATEWAY_PROBES_ENABLED", False))


def disabled_snapshot() -> dict:
    """Résultat sans aucun appel réseau : tout service UNKNOWN, motif explicite."""
    services = {s: {"service": s, "status": UNKNOWN, "reason": "probes_disabled", "verification": "none"} for s in PROBES}
    gateway = {"status": UNKNOWN, "reason": "probes_disabled", "environment": None, "build": None}
    return {"services": services, "gateway_runtime": gateway, "probed_at": None, "cache_ttl_seconds": 0,
            "paths_read": [], "probes_enabled": False, "cached": False}


def snapshot(*, refresh: bool = False) -> dict:
    """Résultat en cache (30 s) ; ``refresh`` force une nouvelle lecture (bouton Actualiser).
    Porte fermée (``CAPABILITIES_GATEWAY_PROBES_ENABLED=0``, défaut) : aucune lecture, aucun cache."""
    if not probes_enabled():
        return disabled_snapshot()
    if not refresh:
        try:
            cached = cache.get(CACHE_KEY)
        except Exception:  # noqa: BLE001
            cached = None
        if cached:
            return {**cached, "cached": True}
    result = probe_all()
    try:
        cache.set(CACHE_KEY, result, CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("capabilities_gateway_cache_unavailable")
    return {**result, "cached": False}

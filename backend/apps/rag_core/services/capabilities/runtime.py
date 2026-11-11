"""Empreinte de configuration EFFECTIVE de chaque processus.

Chaque worker Celery publie, au démarrage (``worker_ready``) puis toutes les
``PUBLISH_INTERVAL_SECONDS``, une empreinte des réglages de capacité QU'IL a chargés :
pour chaque nom, un condensé court de la valeur résolue (jamais la valeur, jamais un
secret). Le backend compare ces empreintes **par consommateur** : un rôle qui ne lit pas un
flag ne peut pas être « en divergence » sur ce flag.

Stockage : Redis du broker (comme ``beat_heartbeat``), clé par instance, TTL
``KEY_TTL_SECONDS``. Une empreinte plus vieille que ``STALE_AFTER_SECONDS`` est périmée :
elle n'est plus considérée comme l'état actuel. Rien d'autre n'est écrit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Optional

from django.conf import settings

logger = logging.getLogger("rag.capabilities.runtime")

SCHEMA_VERSION = 1
KEY_PREFIX = "ragatm:capfp:"
STALE_AFTER_SECONDS = 180          # au-delà : WORKER_STATE = STALE
KEY_TTL_SECONDS = 3600             # une instance disparue s'efface d'elle-même
PUBLISH_INTERVAL_SECONDS = 60

#: Préfixe du nœud Celery (``--hostname=<préfixe>@%h``) → rôle.
NODE_ROLES = {
    "default": "worker", "celery": "worker", "reindex": "worker_reindex", "visual": "worker_visual",
    "audio": "worker_audio", "ifc": "worker_ifc", "dwg": "worker_dwg",
    "analytics": "worker_analytics", "maintenance": "worker_maintenance",
}

STATE_FRESH = "FRESH"
STATE_STALE = "STALE"
STATE_MISSING = "MISSING"


def _redis():
    url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis

        return redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:  # noqa: BLE001
        return None


def environment() -> str:
    return str(getattr(settings, "DJANGO_ENV", "") or "").strip().lower() or "unknown"


def deployment_sha() -> str:
    return str(getattr(settings, "APP_GIT_SHA", "") or "")


def _secret_names() -> frozenset:
    try:
        from config import registry

        return frozenset(d.name for d in registry.secrets())
    except Exception:  # noqa: BLE001
        return frozenset()


def value_digest(value: Any) -> str:
    """Condensé court et stable d'une valeur résolue (comparable, non réversible en pratique)."""
    raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def local_digests() -> dict:
    """Condensés des réglages de capacité tels que CE processus les a chargés."""
    from .registry import referenced_flags

    secrets = _secret_names()
    return {name: value_digest(getattr(settings, name, None))
            for name in sorted(referenced_flags()) if name not in secrets}


#: Suffixes d'environnement des noms de nœud (compose staging : ``audio-staging@%h``).
_NODE_ENV_SUFFIXES = ("-staging", "-prod", "-production", "-dev")


def role_from_hostname(hostname: str) -> str:
    node = str(hostname or "").split("@", 1)[0].strip().lower()
    for suffix in _NODE_ENV_SUFFIXES:
        if node.endswith(suffix):
            node = node[: -len(suffix)]
            break
    return NODE_ROLES.get(node, f"worker_{node}" if node else "worker_unknown")


def build_fingerprint(role: str, identity: str) -> dict:
    digests = local_digests()
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": environment(),
        "service_role": role,
        "process_identity": identity,
        "deployment_sha": deployment_sha(),
        "config_schema_version": SCHEMA_VERSION,
        "consumed_flags_fingerprint": value_digest(digests),
        "digests": digests,
        "observed_at": time.time(),
    }


def publish(role: str, identity: str) -> bool:
    """Écrit l'empreinte de ce processus. Ne lève jamais : l'observation n'est pas critique."""
    client = _redis()
    if client is None:
        return False
    try:
        fp = build_fingerprint(role, identity)
        client.set(f"{KEY_PREFIX}{fp['environment']}:{role}:{identity}", json.dumps(fp), ex=KEY_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("capabilities_fingerprint_publish_failed role=%s", role)
        return False


def read_all() -> Optional[list]:
    """Toutes les empreintes de l'environnement courant ; ``None`` si Redis est injoignable."""
    client = _redis()
    if client is None:
        return None
    try:
        out = []
        for key in client.scan_iter(match=f"{KEY_PREFIX}{environment()}:*", count=200):
            raw = client.get(key)
            if raw:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
        return out
    except Exception:  # noqa: BLE001
        return None


def _role_states(fingerprints: list, now: float) -> dict:
    """Par rôle : état (FRESH si au moins une instance fraîche) et instances retenues.

    Une instance périmée dont un pair du même rôle est frais est ignorée : c'est l'ancien
    conteneur d'un service recréé, pas un worker en panne."""
    roles: dict = {}
    for fp in fingerprints:
        role = str(fp.get("service_role") or "worker_unknown")
        age = now - float(fp.get("observed_at") or 0)
        roles.setdefault(role, {"fresh": [], "stale": []})
        roles[role]["fresh" if age <= STALE_AFTER_SECONDS else "stale"].append({**fp, "age_seconds": round(age)})
    out = {}
    for role, inst in roles.items():
        if inst["fresh"]:
            out[role] = {"state": STATE_FRESH, "instances": inst["fresh"]}
        else:
            out[role] = {"state": STATE_STALE, "instances": inst["stale"]}
    return out


def consistency(fingerprints: Optional[list], *, now: Optional[float] = None) -> dict:
    """Parité de configuration jugée PAR CONSOMMATEUR.

    Le processus courant (backend) est inclus avec ses valeurs vivantes. Pour chaque flag,
    seuls les rôles qui le consomment sont comparés ; une différence chez un rôle
    non-consommateur est comptée à titre informatif, jamais comme une divergence.
    """
    from .registry import consumers_by_flag

    now = time.time() if now is None else now
    consumers = consumers_by_flag()
    backend_digests = local_digests()
    if fingerprints is None:
        return {"status": "UNKNOWN", "reason": "redis_unreachable", "roles": {}, "flags": {},
                "mismatches": [], "ignored_non_consumer_differences": 0, "deployment_sha_mismatches": []}

    roles = _role_states([fp for fp in fingerprints if fp.get("schema_version") == SCHEMA_VERSION], now)
    sha = deployment_sha()
    role_summary = {"backend": {"state": STATE_FRESH, "instances": 1, "deployment_sha": sha, "age_seconds": 0}}
    sha_mismatch = []
    for role, info in roles.items():
        shas = sorted({i.get("deployment_sha") or "" for i in info["instances"]})
        newest = min(i["age_seconds"] for i in info["instances"])
        role_summary[role] = {"state": info["state"], "instances": len(info["instances"]),
                              "deployment_sha": shas[0] if len(shas) == 1 else shas, "age_seconds": newest}
        if info["state"] == STATE_FRESH and sha and any(s and s != sha for s in shas):
            sha_mismatch.append(role)

    flags: dict = {}
    mismatches: list = []
    ignored = 0
    for name, backend_digest in backend_digests.items():
        wanted = consumers.get(name, frozenset())
        values: dict = {}
        if "backend" in wanted:
            values["backend"] = {backend_digest}
        missing = []
        for role in sorted(wanted - {"backend"}):
            info = roles.get(role)
            if not info or info["state"] != STATE_FRESH:
                missing.append(role)
                continue
            values[role] = {i.get("digests", {}).get(name) for i in info["instances"]}
        for role, info in roles.items():
            if role in wanted or info["state"] != STATE_FRESH:
                continue
            if any(i.get("digests", {}).get(name) != backend_digest for i in info["instances"]):
                ignored += 1
        distinct = set().union(*values.values()) if values else set()
        if len(distinct) > 1:
            status = "CONFIGURATION_MISMATCH"
            mismatches.append({"flag": name, "roles": sorted(values)})
        elif missing:
            status = "UNKNOWN"
        elif values:
            status = "OK"
        else:
            status = "UNKNOWN"
        flags[name] = {"status": status, "consumers": sorted(wanted), "unobserved_consumers": missing}
    overall = "MISMATCH" if mismatches else ("OK" if all(f["status"] == "OK" for f in flags.values()) else "PARTIAL")
    return {"status": overall, "reason": "", "roles": role_summary, "flags": flags, "mismatches": mismatches,
            "ignored_non_consumer_differences": ignored, "deployment_sha_mismatches": sorted(sha_mismatch),
            "stale_after_seconds": STALE_AFTER_SECONDS}


# --------------------------------------------------------------------------- #
# Publication côté worker (signal worker_ready + fil léger)
# --------------------------------------------------------------------------- #
_started = threading.Event()


def start_worker_publisher(hostname: str) -> None:
    """Publie tout de suite puis périodiquement. Idempotent par processus."""
    if _started.is_set():
        return
    _started.set()
    role = role_from_hostname(hostname)
    identity = f"{socket.gethostname()}:{os.getpid()}"

    def _loop():
        while True:
            publish(role, identity)
            time.sleep(PUBLISH_INTERVAL_SECONDS)

    threading.Thread(target=_loop, name="capabilities-fingerprint", daemon=True).start()


def connect_signals() -> None:
    from celery.signals import worker_ready

    def _on_ready(sender=None, **_kw):
        try:
            hostname = getattr(sender, "hostname", "") or ""
            start_worker_publisher(hostname)
        except Exception:  # noqa: BLE001
            logger.warning("capabilities_fingerprint_start_failed")

    worker_ready.connect(_on_ready, weak=False)

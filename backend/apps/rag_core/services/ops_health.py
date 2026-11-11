"""Santé d'EXPLOITATION — ordonnanceur, tâches périodiques, sauvegarde (Lot 4-G).

Distinct du healthcheck de *liveness* (``/api/health/``), qui répond « le
processus web répond ». Celui-ci répond à une question différente et plus
utile : **les garanties d'exploitation sont-elles effectives en ce moment ?**

Trois sondes, trois modes de panne réels :

* **Beat** — le heartbeat prouve la chaîne scheduler → broker → worker → base.
  Un processus vivant ne prouve rien : il peut être vivant et ne plus planifier.
* **Purge de rétention** — le mode de panne le plus discret est « n'a jamais
  tourné ». Un simple compteur d'erreurs ne le détecterait pas : il n'y a pas
  d'erreur, il n'y a rien.
* **Sauvegarde** — lue depuis le `latest-status.json` produit par le Lot 3, sans
  rien exécuter.

CONFIDENTIALITÉ — la sortie ne contient ni périmètre client, ni contenu, ni
secret : uniquement des états, des âges et des compteurs.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
STATUS_DISABLED = "disabled"

#: Ordre de gravité — sert à agréder le statut global sans le sous-estimer.
_SEVERITY = {
    STATUS_OK: 0,
    STATUS_DISABLED: 1,
    STATUS_UNKNOWN: 2,
    STATUS_WARN: 3,
    STATUS_ERROR: 4,
}


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 2))


def check_broker() -> dict[str, Any]:
    """Le broker Celery répond-il ? Sans lui, rien n'est distribué."""
    url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
    if not url:
        return {"status": STATUS_ERROR, "detail": "CELERY_BROKER_URL vide"}
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return {"status": STATUS_UNKNOWN, "detail": "broker non-Redis (non sondé)"}
    try:
        import redis

        redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2).ping()
        return {"status": STATUS_OK}
    except Exception as exc:  # noqa: BLE001
        return {"status": STATUS_ERROR, "detail": type(exc).__name__}


def check_beat() -> dict[str, Any]:
    """État de l'ordonnanceur, d'après le heartbeat."""
    from apps.rag_core.services.beat_heartbeat import (
        STATUS_MISSING,
        STATUS_OK as HB_OK,
        STATUS_STALE,
        read_heartbeat,
    )

    hb = read_heartbeat()
    mapping = {
        HB_OK: STATUS_OK,
        STATUS_STALE: STATUS_ERROR,     # Beat a cessé d'ordonnancer
        STATUS_MISSING: STATUS_ERROR,   # Beat n'a jamais ordonnancé
    }
    return {
        "status": mapping.get(hb["status"], STATUS_UNKNOWN),
        "heartbeat_status": hb["status"],
        "last_heartbeat": hb["timestamp"],
        "age_seconds": hb["age_seconds"],
        "max_age_seconds": hb["max_age_seconds"],
        "instance_id": hb["instance_id"],
    }


def check_periodic_tasks() -> dict[str, Any]:
    """Compteurs d'exécution et d'échec des tâches périodiques."""
    from apps.rag_core.services import periodic
    from apps.rag_core.tabular import observability as obs

    snap = obs.snapshot()
    runs = int(snap.get(periodic.PERIODIC_RUNS, 0))
    failures = int(snap.get(periodic.PERIODIC_FAILURES, 0))
    skips = int(snap.get(periodic.PERIODIC_LOCK_SKIPS, 0))

    # Ces compteurs sont PAR PROCESSUS (pas de backend de métriques partagé) :
    # ils renseignent sur le processus web interrogé, pas sur le worker. On le
    # dit explicitement plutôt que de laisser croire à une vue globale.
    return {
        "status": STATUS_WARN if failures else STATUS_OK,
        "runs_total": runs,
        "failures_total": failures,
        "lock_skips_total": skips,
        "scope": "process-local",
    }


def check_audit_purge() -> dict[str, Any]:
    """La purge de rétention tourne-t-elle réellement ?

    Le mode de panne visé est « jamais exécutée » — celui qui ne produit aucune
    erreur, donc aucune alerte, et qu'on ne découvre qu'en constatant une table
    qui a grossi pendant un an.
    """
    if not bool(getattr(settings, "AUDIT_PURGE_ENABLED", True)):
        return {"status": STATUS_DISABLED, "detail": "AUDIT_PURGE_ENABLED=0"}

    from apps.audit.tasks import PURGE_LAST_SUCCESS
    from apps.rag_core.tabular import observability as obs

    last = obs.snapshot().get(PURGE_LAST_SUCCESS)
    if not last:
        return {
            "status": STATUS_UNKNOWN,
            "detail": "aucun succès enregistré dans ce processus",
            "scope": "process-local",
        }
    age = time.time() - float(last)
    period = int(getattr(settings, "CELERY_BEAT_AUDIT_PURGE_SECONDS", 86400))
    return {
        "status": STATUS_OK if age <= period * 2 else STATUS_WARN,
        "age_seconds": round(age, 1),
        "expected_period_seconds": period,
        "scope": "process-local",
    }


def check_backup() -> dict[str, Any]:
    """État de la dernière sauvegarde, lu depuis `latest-status.json` (Lot 3).

    Ne lance rien : lecture seule d'un fichier d'état déjà produit.
    """
    root = str(
        getattr(settings, "RAGATM_BACKUP_ROOT", "")
        or os.getenv("RAGATM_BACKUP_ROOT", "")
    )
    if not root:
        return {"status": STATUS_UNKNOWN, "detail": "RAGATM_BACKUP_ROOT non configuré"}

    path = Path(root) / "latest-status.json"
    if not path.is_file():
        return {
            "status": STATUS_ERROR,
            "detail": "aucun latest-status.json : aucune sauvegarde n'a jamais abouti",
        }
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"status": STATUS_ERROR, "detail": f"illisible ({type(exc).__name__})"}

    from django.utils.dateparse import parse_datetime

    created = parse_datetime(str(data.get("created_at", "")) or "")
    age = None
    if created:
        from django.utils import timezone

        ref = timezone.now() if timezone.is_aware(created) else None
        age = (ref - created).total_seconds() if ref else None

    max_age = int(getattr(settings, "BACKUP_MAX_AGE_SECONDS", 172800))  # 48 h
    last_status = str(data.get("status", "") or "unknown")

    if last_status == "complete":
        status = STATUS_OK
    elif last_status == "degraded":
        # Sauvegarde locale valide mais copie secondaire absente : le scénario
        # « perte du serveur » n'est plus couvert. Ce n'est pas « ok ».
        status = STATUS_WARN
    else:
        status = STATUS_ERROR

    if age is not None and age > max_age:
        status = _worst(status, STATUS_ERROR)

    return {
        "status": status,
        "last_status": last_status,
        "created_at": data.get("created_at"),
        "age_seconds": round(age, 1) if age is not None else None,
        "max_age_seconds": max_age,
        "environment": data.get("environment"),
    }


def check_media_storage_audit() -> dict[str, Any]:
    """Le contrôle de cohérence base ↔ fichiers tourne-t-il, et que dit-il ?

    Deux modes de panne distincts, et il faut les deux :
    « le contrôle n'a jamais tourné » (silence) et « le contrôle a trouvé quelque
    chose » (anomalie). Le premier ne produit aucune erreur — c'est ce qui l'a
    rendu invisible jusqu'ici.
    """
    if not bool(getattr(settings, "RAG_MEDIA_STORAGE_AUDIT_ENABLED", False)):
        return {"status": STATUS_DISABLED, "detail": "RAG_MEDIA_STORAGE_AUDIT_ENABLED=0"}

    from apps.rag_core.tasks import (
        MEDIA_AUDIT_LAST_SUCCESS,
        MEDIA_AUDIT_MISSING_FILES,
        MEDIA_AUDIT_ORPHAN_FILES,
    )
    from apps.rag_core.tabular import observability as obs

    snap = obs.snapshot()
    last = snap.get(MEDIA_AUDIT_LAST_SUCCESS)
    if not last:
        return {
            "status": STATUS_UNKNOWN,
            "detail": "aucun audit stockage enregistré dans ce processus",
            "scope": "process-local",
        }

    age = time.time() - float(last)
    period = int(getattr(settings, "CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS", 86400))
    missing = int(snap.get(MEDIA_AUDIT_MISSING_FILES, 0))
    orphans = int(snap.get(MEDIA_AUDIT_ORPHAN_FILES, 0))

    if missing:
        # Référence sans fichier = document devenu inconsultable. C'est la trace
        # que laisse une purge interrompue après la suppression des fichiers.
        status = STATUS_ERROR
    elif orphans or age > period * 2:
        status = STATUS_WARN
    else:
        status = STATUS_OK

    return {
        "status": status,
        "age_seconds": round(age, 1),
        "expected_period_seconds": period,
        "missing_file": missing,
        "orphan_file": orphans,
        "scope": "process-local",
    }


def check_ragspace_deletions() -> dict[str, Any]:
    """Des suppressions définitives de RagSpace ont-elles échoué récemment ?

    Interroge l'audit directement (et non un compteur en mémoire) : contrairement
    aux autres sondes, l'information est PERSISTÉE et vaut donc pour toute
    l'installation, pas seulement pour le processus interrogé.
    """
    if not bool(getattr(settings, "RAG_RAGSPACE_DELETE_MONITORING_ENABLED", False)):
        return {
            "status": STATUS_DISABLED,
            "detail": "RAG_RAGSPACE_DELETE_MONITORING_ENABLED=0",
        }

    from apps.rag_core.services import rag_space_deletion_monitoring as monitoring

    window = int(getattr(settings, "RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS", 86400))
    try:
        report = monitoring.scan(window_seconds=window)
    except Exception as exc:  # noqa: BLE001 — une sonde ne casse jamais l'agrégat.
        return {"status": STATUS_UNKNOWN, "detail": type(exc).__name__}

    return {
        "status": STATUS_ERROR if report["failures_total"] else STATUS_OK,
        "failures_total": report["failures_total"],
        "window_seconds": window,
        "scope": "installation",
    }


def collect() -> dict[str, Any]:
    """Agrège toutes les sondes. Le statut global prend le PIRE des sous-états."""
    broker = check_broker()
    beat = check_beat()
    periodic_tasks = check_periodic_tasks()
    purge = check_audit_purge()
    backup = check_backup()
    media_storage = check_media_storage_audit()
    ragspace_deletions = check_ragspace_deletions()

    overall = _worst(
        broker["status"], beat["status"], periodic_tasks["status"],
        purge["status"], backup["status"], media_storage["status"],
        ragspace_deletions["status"],
    )
    return {
        "status": overall,
        "celery_broker": broker,
        "celery_beat": beat,
        "periodic_tasks": periodic_tasks,
        "audit_purge": purge,
        "backup": backup,
        "media_storage_audit": media_storage,
        "ragspace_deletions": ragspace_deletions,
    }

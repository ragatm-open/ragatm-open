"""Tâches périodiques d'audit — purge de rétention (Lot 4-D).

Cette tâche applique la politique définie dans :mod:`apps.audit.retention`. Elle
existe parce que la commande de purge n'était **ordonnancée nulle part** : la
rétention était documentée, pas appliquée.

Trois propriétés, toutes exigées d'une purge planifiée :

* **idempotente** — elle ne supprime que ce qui est déjà expiré ; la rejouer ne
  détruit rien de plus ;
* **bornée** — par lots, avec un plafond par exécution ; une interruption laisse
  une base cohérente et le reste part au passage suivant ;
* **exclusive** — verrou distribué : deux purges concurrentes sur les mêmes
  lignes ne produisent que du travail inutile et des conflits de verrous.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from apps.audit import retention
from apps.rag_core.services.periodic import LockNotAcquired, periodic_run
from apps.rag_core.tabular import observability as obs

logger = logging.getLogger(__name__)

PURGE_LAST_SUCCESS = "ragatm_audit_purge_last_success_timestamp"
PURGE_DELETED_ROWS = "ragatm_audit_purge_deleted_rows_total"
PURGE_FAILURES = "ragatm_audit_purge_failures_total"


@shared_task(
    name="apps.audit.tasks.purge_expired_audit_logs_task",
    soft_time_limit=540,
    time_limit=600,
    # AUCUN retry automatique : une purge qui échoue à mi-course a déjà supprimé
    # des lots. La rejouer immédiatement n'apporte rien (le prochain cycle s'en
    # charge) et masquerait un problème de fond derrière des tentatives répétées.
    max_retries=0,
)
def purge_expired_audit_logs_task() -> dict:
    """Applique la rétention à tous les journaux. Planifiée quotidiennement."""
    import time

    if not bool(getattr(settings, "AUDIT_PURGE_ENABLED", True)):
        # Interrupteur d'exploitation : suspendre la purge en incident SANS
        # désactiver l'audit lui-même (les événements continuent d'être écrits).
        logger.warning("audit.purge disabled AUDIT_PURGE_ENABLED=0 — aucune suppression")
        return {"skipped": True, "reason": "AUDIT_PURGE_ENABLED=0"}

    try:
        with periodic_run("purge_expired_audit_logs") as run:
            report = retention.purge_all(apply=True)
            run["deleted"] = report["deleted_total"]
            run["eligible"] = report["eligible_total"]
            for r in report["results"]:
                run[f"{r['key']}_deleted"] = r["deleted"]
                if r.get("truncated"):
                    run[f"{r['key']}_truncated"] = 1
    except LockNotAcquired:
        return {"skipped": True, "reason": "lock_held"}
    except Exception:
        obs.incr(PURGE_FAILURES)
        raise

    obs.incr(PURGE_DELETED_ROWS, report["deleted_total"])
    # Horodatage de dernier succès : c'est lui qui permet d'alerter sur « la purge
    # n'a jamais tourné », le mode de panne le plus discret d'une tâche planifiée.
    obs.set_gauge(PURGE_LAST_SUCCESS, time.time())
    return report

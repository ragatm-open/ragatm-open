"""Reap des résumés bloqués en PENDING (Priorité C — résilience).

Passe à FAILED les ``DocumentSummary`` restés en PENDING au-delà du délai
(``SUMMARY_PENDING_TIMEOUT_S``, défaut 900s). Utile en exécution manuelle ou via
cron si Celery beat n'est pas déployé.

    python manage.py reap_stale_summaries
    python manage.py reap_stale_summaries --timeout 300
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.rag_core.services.summary_service import reap_stale_summaries


class Command(BaseCommand):
    help = "Passe à FAILED les résumés bloqués en PENDING au-delà du délai (tâche perdue)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Délai en secondes (défaut: SUMMARY_PENDING_TIMEOUT_S).",
        )

    def handle(self, *args, **opts):
        reaped = reap_stale_summaries(timeout_s=opts.get("timeout"))
        self.stdout.write(self.style.SUCCESS(f"✓ {reaped} résumé(s) PENDING reapé(s)."))

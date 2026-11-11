"""Supervision des suppressions DÉFINITIVES de RagSpace en échec (P0-3).

Lit les ``ActionAuditEvent`` ``ragspace.delete`` / ``status="failed"`` et les
présente à un opérateur ou à une supervision. **Strictement en lecture** : cette
commande ne supprime, ne modifie et n'écrit aucun événement.

Elle partage sa logique avec la tâche planifiée
(``monitor_ragspace_deletion_failures_task``) via
``apps.rag_core.services.rag_space_deletion_monitoring`` — la tâche n'interprète
jamais la sortie de cette commande.

Exemples :
    python manage.py monitor_ragspace_deletions
    python manage.py monitor_ragspace_deletions --json --window-seconds 3600
    python manage.py monitor_ragspace_deletions --json --fail-on-alert   # supervision
    python manage.py monitor_ragspace_deletions --use-cursor             # comme la tâche
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.rag_core.services import rag_space_deletion_monitoring as monitoring


class Command(BaseCommand):
    help = (
        "Liste les suppressions définitives de RagSpace en ÉCHEC sur une fenêtre. "
        "Lecture seule — n'écrit ni ne modifie aucun audit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-seconds", type=int, default=None,
            help="Fenêtre d'observation (défaut : "
                 "RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS).",
        )
        parser.add_argument(
            "--since", default=None,
            help="Borne basse explicite (ISO 8601). Prioritaire sur --use-cursor.",
        )
        parser.add_argument(
            "--use-cursor", action="store_true",
            help="Repartir du curseur de déduplication (comportement de la tâche). "
                 "Le curseur n'est JAMAIS écrit par cette commande.",
        )
        parser.add_argument(
            "--limit", type=int, default=monitoring.DEFAULT_LIMIT,
            help=f"Nombre maximum d'échecs détaillés (défaut {monitoring.DEFAULT_LIMIT}).",
        )
        parser.add_argument("--json", action="store_true", help="Sortie JSON (supervision).")
        parser.add_argument(
            "--fail-on-alert", action="store_true",
            help="Code de sortie non nul si au moins un échec est détecté.",
        )

    def handle(self, *args, **opts):
        window = opts["window_seconds"]
        if window is None:
            window = int(
                getattr(settings, "RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS", 86400)
            )
        if window <= 0:
            raise CommandError("--window-seconds doit être strictement positif.")

        since = None
        if opts["since"]:
            from datetime import datetime

            try:
                since = datetime.fromisoformat(opts["since"])
            except ValueError as e:
                raise CommandError(
                    f"--since : date ISO 8601 invalide « {opts['since']} »."
                ) from e
            if timezone.is_naive(since):
                since = timezone.make_aware(since)
        elif opts["use_cursor"]:
            since = monitoring.read_cursor()

        report = monitoring.scan(window_seconds=window, since=since, limit=opts["limit"])

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            self._render_text(report)

        if opts["fail_on_alert"] and report["status"] != monitoring.STATUS_OK:
            raise SystemExit(1)
        return None

    # ------------------------------------------------------------------ rendu
    def _render_text(self, report) -> None:
        w = self.stdout.write
        if report["status"] == monitoring.STATUS_OK:
            w(self.style.SUCCESS(
                f"✅ aucune purge de RagSpace en échec depuis {report['since']}"
            ))
            return

        w(self.style.ERROR(
            f"❌ {report['failures_total']} purge(s) de RagSpace en ÉCHEC "
            f"depuis {report['since']}"
        ))
        for f in report["failures"]:
            w(
                f"\n  event_id     : {f['event_id']}\n"
                f"  créé le      : {f['created_at']}\n"
                f"  workspace    : {f['workspace_id'] or '—'}\n"
                f"  rag_space    : {f['rag_space_id'] or '—'}\n"
                f"  acteur (id)  : {f['actor_id'] if f['actor_id'] is not None else '— (commande)'}\n"
                f"  error_code   : {f['error_code'] or '—'}\n"
                f"  durée (ms)   : {f['duration_ms'] if f['duration_ms'] is not None else '—'}\n"
                f"  documents    : {_n(f['documents_total'])}   chunks : {_n(f['chunks_total'])}\n"
                f"  fichiers     : total {_n(f['files_total'])} · supprimés "
                f"{_n(f['files_deleted'])} · absents {_n(f['files_missing'])} · "
                f"échoués {_n(f['files_failed'])}"
            )
        if report["truncated"]:
            w(self.style.WARNING(
                f"\n… {report['failures_total'] - report['reported']} échec(s) non détaillé(s) "
                "(utiliser --limit)"
            ))
        w(
            "\nConduite à tenir : `files_deleted` indique combien de fichiers ont déjà "
            "été détruits alors que la base a été restaurée par le rollback. Croiser avec "
            "`manage.py audit_media_storage --list-missing`, puis restaurer depuis la "
            "sauvegarde (cf. docs/07-operations/sauvegarde-restauration.md)."
        )


def _n(value):
    """Compteur absent (audit antérieur au P0-4) affiché explicitement."""
    return "?" if value is None else value

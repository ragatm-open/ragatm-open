"""``python manage.py asr_drain_preflight [--json] [--verbose]`` — LECTURE SEULE.

Préflight de vidange ASR exigé avant de changer l'acheminement Gateway (bascule vers
AI Gateway Pool ou retour en direct), cf. ``services/ingest/audio/drain.py`` et
``docs/06-deployment/ai-gateway-pool.md``.

Sortie : lignes ``CLÉ=valeur`` (``ASR_ACTIVE_JOBS``, ``ASR_PENDING_JOBS``,
``ASR_UNKNOWN_JOBS``, ``ASR_DRAIN_STATUS``…), ou un JSON avec ``--json``.

Code de sortie : 0 = PASS, 2 = BLOCKED (attendre), 3 = UNKNOWN (enquêter). La commande
n'annule, ne supprime et ne relance AUCUN job : vider, c'est attendre.
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from apps.rag_core.services.ingest.audio import drain

EXIT_CODES = {drain.PASS: 0, drain.BLOCKED: 2, drain.UNKNOWN: 3}


class Command(BaseCommand):
    help = "Préflight de vidange ASR (lecture seule) avant un changement d'acheminement Gateway."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Sortie JSON.")
        parser.add_argument("--verbose", action="store_true",
                            help="Liste les identifiants des tentatives actives (aucun nom de fichier).")

    def handle(self, *args, **opts):
        report = drain.build_report()
        if opts.get("json"):
            data = report.as_dict()
            if not opts.get("verbose"):
                data.pop("active_transcripts", None)
            self.stdout.write(json.dumps(data, sort_keys=True))
        else:
            for line in report.as_lines():
                self.stdout.write(line)
            if report.error:
                self.stdout.write(f"ASR_DRAIN_ERROR={report.error}")
            for reason in report.unknown_reasons:
                self.stdout.write(f"ASR_UNKNOWN_REASON={reason}")
            if opts.get("verbose"):
                for t in report.active_transcripts:
                    self.stdout.write("ASR_ACTIVE_TRANSCRIPT=" + json.dumps(t, sort_keys=True))
        code = EXIT_CODES[report.status]
        if code:
            sys.exit(code)

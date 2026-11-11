"""Santé d'exploitation — ordonnanceur, tâches périodiques, sauvegarde (Lot 4-G).

Lecture seule. N'exécute aucune tâche, ne purge rien, ne déclenche aucune
sauvegarde.

    python manage.py ops_health
    python manage.py ops_health --json
    python manage.py ops_health --fail-on-error     # supervision / CI

Cette commande est le point d'entrée d'exploitation : c'est elle qui répond à
« Beat ordonnance-t-il vraiment ? », « la purge a-t-elle tourné ? », « à quand
remonte la dernière sauvegarde ? » — trois questions auxquelles ni `docker ps`
ni `manage.py check` ne peuvent répondre.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.rag_core.services import ops_health

_SYMBOLS = {
    "ok": "✓",
    "warn": "!",
    "error": "✗",
    "unknown": "?",
    "disabled": "-",
}


class Command(BaseCommand):
    help = "État d'exploitation (Beat, tâches périodiques, purge, sauvegarde). Lecture seule."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Sortie JSON.")
        parser.add_argument(
            "--fail-on-error",
            action="store_true",
            help="Code de sortie 1 si le statut global est 'error'.",
        )

    def handle(self, *args, **opts):
        report = ops_health.collect()

        if opts["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            self.stdout.write("Santé d'exploitation RagATM\n")
            for key in ("celery_broker", "celery_beat", "periodic_tasks", "audit_purge", "backup"):
                section = report[key]
                sym = _SYMBOLS.get(section["status"], "?")
                details = " ".join(
                    f"{k}={v}"
                    for k, v in section.items()
                    if k != "status" and v is not None
                )
                self.stdout.write(f"  {sym} {key:<18} {section['status']:<9} {details}")
            self.stdout.write("")
            self.stdout.write("  ✓ ok   ! warn   ✗ error   ? inconnu   - désactivé")
            self.stdout.write("")

            overall = report["status"]
            if overall == "ok":
                self.stdout.write(self.style.SUCCESS("Statut global : ok"))
            elif overall == "error":
                self.stdout.write(self.style.ERROR(f"Statut global : {overall}"))
            else:
                self.stdout.write(self.style.WARNING(f"Statut global : {overall}"))

            # Les compteurs en mémoire ne valent que pour CE processus : le dire
            # évite de conclure « la purge n'a jamais tourné » alors qu'elle
            # tourne dans le worker de maintenance.
            self.stdout.write(
                "\n  Note : les compteurs marqués scope=process-local ne reflètent que "
                "le processus interrogé.\n  Pour Beat et la sauvegarde, l'état est "
                "PARTAGÉ (Redis / fichier) et donc fiable."
            )

        if opts["fail_on_error"] and report["status"] == "error":
            raise SystemExit(1)

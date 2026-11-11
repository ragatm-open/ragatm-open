"""Audit de la configuration : registre canonique ↔ code ↔ fichiers `.env.example`.

    python manage.py config_audit
    python manage.py config_audit --format json
    python manage.py config_audit --strict     # les warnings deviennent bloquants

Code de sortie non nul dès qu'une ERREUR est détectée (contrat CI).

Ne lit JAMAIS un `.env` réel : uniquement les fichiers d'exemple versionnés.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from config.registry import audit as registry_audit


class Command(BaseCommand):
    help = (
        "Vérifie la cohérence entre le registre de configuration, le code et les "
        "fichiers .env.example versionnés."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Format de sortie (défaut : text).",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Traite les avertissements comme des erreurs (code de sortie non nul).",
        )
        parser.add_argument(
            "--no-code-scan",
            action="store_true",
            help="N'analyse pas le code (audit des seuls fichiers .env.example).",
        )
        parser.add_argument(
            "--root",
            default=None,
            help="Racine du dépôt à auditer (défaut : racine déduite du paquet).",
        )

    def handle(self, *args, **options) -> None:
        root = Path(options["root"]) if options["root"] else _repo_root()
        report = registry_audit.run_audit(root, scan_code=not options["no_code_scan"])

        if options["format"] == "json":
            self.stdout.write(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        else:
            self._render_text(report)

        blocking = len(report.errors) + (len(report.warnings) if options["strict"] else 0)
        if blocking:
            raise CommandError(
                f"Audit de configuration en échec : {len(report.errors)} erreur(s), "
                f"{len(report.warnings)} avertissement(s)."
            )

    def _render_text(self, report: registry_audit.AuditReport) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Registre de configuration"))
        for key, value in sorted(report.stats.items()):
            self.stdout.write(f"  {key:20} {value}")

        if not report.findings:
            self.stdout.write(self.style.SUCCESS("\nAucun constat."))
            return

        self.stdout.write("")
        for severity, style in (
            ("error", self.style.ERROR),
            ("warning", self.style.WARNING),
            ("info", self.style.NOTICE),
        ):
            items = [f for f in report.findings if f.severity == severity]
            if not items:
                continue
            self.stdout.write(style(f"{severity.upper()} ({len(items)})"))
            for finding in items:
                self.stdout.write(f"  {finding.render()}")
            self.stdout.write("")


def _repo_root() -> Path:
    # backend/apps/rag_core/management/commands/config_audit.py -> racine du dépôt
    return Path(__file__).resolve().parents[5]

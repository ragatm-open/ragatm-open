"""Génération d'un paquet d'incident depuis la ligne de commande.

Même chemin de contrôle que l'action d'administration : autorisation, niveau,
motif, rédaction, chiffrement, audit. Une commande qui court-circuiterait ces
contrôles offrirait une porte dérobée à l'export — ce serait la seule chose à
retenir de tout le lot.
"""
from __future__ import annotations

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.feedback.export.levels import ExportRefused
from apps.feedback.export.package import ExportBlocked
from apps.feedback.models import AnswerFeedback
from apps.feedback.services.export_service import approve_export, generate_package


class Command(BaseCommand):
    help = (
        "Génère le paquet d'un incident (feedback) autorisé à l'export. "
        "Aucun export n'est possible sans autorisation préalable et sans opérateur habilité."
    )

    def add_arguments(self, parser):
        parser.add_argument("--incident", required=True, help="UUID du feedback.")
        parser.add_argument(
            "--level", type=int, default=1,
            help="1=métadonnées, 2=fragments ciblés, 3=fichier source (superuser).",
        )
        parser.add_argument(
            "--as-user", required=True,
            help="Nom d'utilisateur de l'opérateur : c'est SON habilitation qui est vérifiée.",
        )
        parser.add_argument("--justification", default="", help="Motif (obligatoire dès le niveau 2).")
        parser.add_argument("--out", required=True, help="Répertoire de destination.")
        parser.add_argument(
            "--approve", action="store_true",
            help="Autorise l'export (incident QUALIFIÉ) avant de générer, en un seul geste tracé.",
        )
        parser.add_argument(
            "--allow-secrets-override", action="store_true",
            help="Passe outre une détection de secret, EN CONNAISSANCE DE CAUSE (audité).",
        )

    def handle(self, *args, **opts):
        feedback = AnswerFeedback.objects.filter(id=opts["incident"]).first()
        if feedback is None:
            raise CommandError(f"Incident introuvable : {opts['incident']}")

        user = get_user_model().objects.filter(username=opts["as_user"]).first()
        if user is None:
            raise CommandError(f"Utilisateur introuvable : {opts['as_user']}")

        try:
            if opts["approve"]:
                approve_export(
                    feedback,
                    level_value=opts["level"],
                    user=user,
                    justification=opts["justification"],
                )
            result = generate_package(
                feedback,
                level_value=opts["level"],
                user=user,
                justification=opts["justification"],
                allow_secrets_override=opts["allow_secrets_override"],
            )
        except (ExportRefused, ExportBlocked) as exc:
            raise CommandError(str(exc)) from None

        out_dir = Path(opts["out"])
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / result["filename"]
        target.write_bytes(result["payload"])

        manifest = result["manifest"]
        self.stdout.write(self.style.SUCCESS(f"Paquet écrit : {target}"))
        self.stdout.write(f"  niveau              : {manifest['export_level']} ({manifest['export_level_label']})")
        self.stdout.write(f"  contenu production  : {manifest['contains_production_content']}")
        self.stdout.write(f"  chiffrement         : {manifest['encryption_status']}")
        self.stdout.write(f"  rédaction           : {manifest['redaction_status']}")
        self.stdout.write(f"  capture             : {manifest['capture_mode']} (exacte={manifest['capture_is_exact']})")
        self.stdout.write(f"  replay possible     : {', '.join(manifest['replay_modes_supported']) or 'aucun'}")
        self.stdout.write(f"  expire le           : {manifest['expires_at']}")
        if not manifest["capture_is_exact"]:
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ La trace n'est PAS la capture d'origine : conclusions à pondérer."
                )
            )

"""Audit NON DESTRUCTIF du stockage documentaire local (ADR-001).

Compare les références en base et les fichiers présents sous ``MEDIA_ROOT`` :

- ``referenced_ok``      : référence en base ET fichier présent (nominal) ;
- ``missing_file``       : référence en base SANS fichier  -> document non consultable ;
- ``orphan_file``        : fichier sur disque SANS référence -> espace occupé pour rien.

Cette commande **ne supprime jamais rien** : elle ne fait qu'un rapport. Toute purge
éventuelle devra faire l'objet d'une commande dédiée, avec garde-fou explicite.

La logique vit dans ``apps.rag_core.services.media_storage_audit`` : la tâche
périodique (``audit_media_storage_task``) appelle le MÊME service, elle ne relit
jamais la sortie de cette commande.

Exemples :
    python manage.py audit_media_storage
    python manage.py audit_media_storage --json
    python manage.py audit_media_storage --list-missing --limit 50
    python manage.py audit_media_storage --json --fail-on-anomaly   # supervision / CI
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.rag_core.services import media_storage_audit

# Conservé pour compatibilité : des scripts/tests importent ce nom depuis la commande.
MANAGED_SUBDIRS = media_storage_audit.MANAGED_SUBDIRS


class Command(BaseCommand):
    help = "Audite le stockage documentaire local (références DB vs fichiers). Ne supprime rien."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Sortie JSON (supervision).")
        parser.add_argument("--list-missing", action="store_true",
                            help="Liste les références sans fichier.")
        parser.add_argument("--list-orphans", action="store_true",
                            help="Liste les fichiers sans référence.")
        parser.add_argument("--limit", type=int, default=20,
                            help="Nombre d'entrées listées (défaut 20).")
        parser.add_argument("--fail-on-anomaly", action="store_true",
                            help="Code de sortie non nul si une anomalie est détectée.")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        report = media_storage_audit.collect(
            missing_examples=limit if opts["list_missing"] else 0,
            orphan_examples=limit if opts["list_orphans"] else 0,
        )
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2))
        else:
            self._render_text(report)

        if opts["fail_on_anomaly"] and not media_storage_audit.is_clean(report):
            # Code de sortie exploitable par un ordonnanceur externe ou la CI.
            raise SystemExit(1)
        return None

    # ------------------------------------------------------------------ rendu
    def _render_text(self, report) -> None:
        w = self.stdout.write
        media_root = report["media_root"]
        missing_n = report["missing_file"]
        orphan_n = report["orphan_file"]

        w("Audit du stockage documentaire (ADR-001 — volume local)")
        w(f"  MEDIA_ROOT           : {media_root}"
          f"{'' if report['media_root_exists'] else '  ⚠️ INTROUVABLE'}")
        w(f"  références en base   : {report['referenced_total']}")
        w(f"  fichiers sur disque  : {report['files_on_disk']}"
          f" ({report['bytes_on_disk'] / 1e9:.2f} Go)")
        w(self.style.SUCCESS(f"  ✅ référencés + présents : {report['referenced_ok']}"))

        if missing_n:
            w(self.style.ERROR(
                f"  ❌ référence SANS fichier : {missing_n}"
                "  (documents non consultables — restauration ou ré-upload requis)"))
            for n in report.get("missing_examples", []):
                w(f"       {n}")
            if report.get("missing_truncated"):
                w(f"       … et {missing_n - len(report['missing_examples'])} autres")
        else:
            w(self.style.SUCCESS("  ✅ aucune référence sans fichier"))

        if orphan_n:
            w(self.style.WARNING(
                f"  ⚠️  fichier SANS référence : {orphan_n}  (espace occupé pour rien)"))
            for n in report.get("orphan_examples", []):
                w(f"       {n}")
            if report.get("orphan_truncated"):
                w(f"       … et {orphan_n - len(report['orphan_examples'])} autres")
        else:
            w(self.style.SUCCESS("  ✅ aucun fichier orphelin"))

        w("")
        w("Cette commande ne supprime RIEN. Aucune purge automatique n'est prévue.")

# backend/apps/audit/management/commands/purge_expired_audit_logs.py
"""Purge de rétention des journaux d'audit.

**dry-run par DÉFAUT** (aucune suppression). ``--apply`` supprime réellement.

Lot 4-D — la politique n'est plus définie ici : elle vit dans
:mod:`apps.audit.retention`, source unique partagée avec la tâche périodique
``apps.audit.tasks.purge_expired_audit_logs_task`` et avec le system check
``ragatm.E010``. Auparavant cette commande portait sa propre logique et n'était
**ordonnancée nulle part** : la rétention était documentée, pas appliquée.

Quatre catégories couvertes, dont deux d'audit de SÉCURITÉ soumises à un plancher
de rétention (cf. ``retention.SECURITY_AUDIT_MIN_DAYS``).

⚠️ Aucun mécanisme de *legal hold* / mise sous investigation n'existe dans le
schéma actuel : la commande le SIGNALE (elle ne l'invente pas). Toute exclusion
devra être ajoutée avant de dépendre de cette purge pour une obligation
contractuelle.

La suppression utilise ``QuerySet.delete()`` (court-circuite volontairement le
garde append-only unitaire des modèles d'audit).
"""
from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.audit import retention


class Command(BaseCommand):
    help = "Purge (rétention) des journaux d'audit expirés. Dry-run par défaut."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Effectue réellement la suppression (sinon dry-run).",
        )
        parser.add_argument("--json", action="store_true", help="Sortie JSON.")
        parser.add_argument(
            "--only", default=None,
            help=f"Restreint à une règle : {', '.join(r.key for r in retention.RULES)}.",
        )
        parser.add_argument(
            "--batch-size", type=int, default=None,
            help="Taille de lot (défaut : AUDIT_PURGE_BATCH_SIZE).",
        )
        parser.add_argument(
            "--max-rows", type=int, default=None,
            help="Plafond de lignes par exécution (défaut : AUDIT_PURGE_MAX_ROWS_PER_RUN).",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])

        # Une configuration invalide ne doit pas produire une purge « au mieux » :
        # c'est précisément le cas où une erreur de saisie détruirait des preuves.
        errors = retention.validate_settings()
        if errors:
            for e in errors:
                self.stderr.write(self.style.ERROR(f"  configuration invalide : {e}"))
            raise SystemExit(2)

        if apply and not bool(getattr(settings, "AUDIT_PURGE_ENABLED", True)):
            self.stderr.write(self.style.ERROR(
                "  AUDIT_PURGE_ENABLED=0 : purge suspendue par configuration. "
                "Rétablir le réglage avant de purger."
            ))
            raise SystemExit(3)

        report = retention.purge_all(
            apply=apply,
            only=opts["only"],
            batch_size=opts["batch_size"],
            max_rows=opts["max_rows"],
        )

        if opts["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            return

        mode = "APPLY" if apply else "DRY-RUN"
        self.stdout.write(f"[{mode}] purge_expired_audit_logs\n")
        for r in report["results"]:
            rule = retention.rule_by_key(r["key"])
            label = rule.label if rule else r["key"]
            line = (
                f"  {r['category']:<18} {label}\n"
                f"      rétention={r['days']}j  cutoff<{r['cutoff']}  "
                f"éligibles={r['eligible']}  supprimés={r['deleted']}"
            )
            if r.get("truncated"):
                line += "  [PLAFOND ATTEINT — reprise au prochain run]"
            if r.get("future_dated"):
                line += (
                    f"\n      ⚠ {r['future_dated']} ligne(s) datée(s) dans le FUTUR "
                    "(jamais purgées — horloge décalée ?)"
                )
            self.stdout.write(line)

        self.stdout.write(
            "\n  legal_hold: NON IMPLÉMENTÉ "
            "(aucune exclusion investigation dans le schéma actuel)"
        )
        self.stdout.write(
            "  rappel: purger la base active NE supprime PAS les données déjà "
            "présentes dans les sauvegardes historiques."
        )

        if not apply:
            self.stdout.write(self.style.WARNING(
                f"\n  dry-run : aucune suppression ({report['eligible_total']} ligne(s) "
                "éligible(s)). Utiliser --apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n  supprimés : {report['deleted_total']} ligne(s)."
            ))

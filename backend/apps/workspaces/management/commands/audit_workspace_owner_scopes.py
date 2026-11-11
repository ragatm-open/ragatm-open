"""Diagnostic (dry-run) des périmètres owner avant activation de l'isolation.

N'ÉCRIT RIEN. Inventorie chaque workspace (owners, memberships, RagSpaces,
documents, invitations), classe les anomalies avec des CODES STABLES et propose
une action de migration. Distingue trois natures :

  - integrity   : incohérence de données qui ne doit JAMAIS exister (échec CI) ;
  - decision    : nécessite une décision humaine (multi-owner, orphelin) ;
  - pending     : simplement pas encore backfillé (attendu avant migration).

Codes stables : workspace_without_owner, multiple_owners, non_owner_without_scope,
ragspace_without_scope, scope_cross_workspace, scope_target_not_owner,
access_scope_mismatch, multiple_active_accesses, inactive_owner.

Exit code : 1 si une anomalie `integrity` est détectée (ou, avec --strict, si une
anomalie `decision` subsiste). 0 sinon. Intégrable en CI / pré-déploiement.

Usage :
    python manage.py audit_workspace_owner_scopes
    python manage.py audit_workspace_owner_scopes --format json
    python manage.py audit_workspace_owner_scopes --workspace-id 3 --strict
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand

from apps.workspaces.models import Workspace
from apps.workspaces.services.owner_scope_diagnostics import (
    DECISION_CODES,
    INTEGRITY_CODES,
    diagnose_workspace,
)

# Codes ré-exportés depuis le service canonique (single source of truth), pour
# compat des imports existants.
__all__ = ["Command", "INTEGRITY_CODES", "DECISION_CODES"]


class Command(BaseCommand):
    help = "Diagnostic dry-run des périmètres owner (aucune écriture)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, default=None)
        parser.add_argument("--format", choices=["text", "json"], default="text")
        parser.add_argument("--json", action="store_true", help="Alias de --format json.")
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit≠0 aussi sur les anomalies `decision` (orphelin, multi-owner).",
        )

    def handle(self, *args, **opts):
        fmt = "json" if opts["json"] else opts["format"]
        ws_qs = Workspace.objects.all().order_by("id")
        if opts["workspace_id"] is not None:
            ws_qs = ws_qs.filter(id=opts["workspace_id"])

        report = [diagnose_workspace(ws) for ws in ws_qs]

        all_codes = {a["code"] for e in report for a in e["anomalies"]}
        has_integrity = bool(all_codes & INTEGRITY_CODES)
        has_decision = bool(all_codes & DECISION_CODES)

        if fmt == "json":
            self.stdout.write(json.dumps({"workspaces": report}, indent=2, default=str))
        else:
            self._render_text(report)

        if has_integrity or (opts["strict"] and has_decision):
            sys.exit(1)

    # -- Rendu texte ----------------------------------------------------------

    def _render_text(self, report: list[dict]) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("=== Diagnostic périmètres owner (dry-run) ==="))
        for e in report:
            codes = [a["code"] for a in e["anomalies"]]
            if not e.get("is_active", True):
                flag = "⊘ "
            elif not codes:
                flag = "OK "
            elif e["has_integrity_violation"]:
                flag = "✗ "
            else:
                flag = "⚠ "
            self.stdout.write(
                f"{flag}ws#{e['workspace_id']} '{e['workspace_name']}' — "
                f"owners={len(e['owners'])} memberships={e['memberships_total']} "
                f"ragspaces={e['ragspaces_active']} documents={e['documents']} "
                f"invitations={e.get('invitations_pending', 0)} — {e['suggested_action']}"
            )
            if codes:
                self.stdout.write(f"      anomalies: {', '.join(sorted(set(codes)))}")

        total = len(report)
        auto = sum(1 for e in report if e["migratable_automatically"])
        multi = sum(1 for e in report if any(a["code"] == "multiple_owners" for a in e["anomalies"]))
        orphan = sum(1 for e in report if any(a["code"] == "workspace_without_owner" for a in e["anomalies"]))
        integrity = sum(1 for e in report if e["has_integrity_violation"])
        self.stdout.write("")
        self.stdout.write(
            f"Total={total} | backfill auto={auto} | multi-owner={multi} | "
            f"orphelins={orphan} | INTÉGRITÉ VIOLÉE={integrity}"
        )
        if integrity:
            self.stdout.write(self.style.ERROR("Incohérence de données : CORRIGER avant toute activation (exit 1)."))
        elif auto != total:
            self.stdout.write(
                self.style.WARNING(
                    "Ne PAS activer OWNER_SCOPE_ENABLED tant qu'une ressource active reste ambiguë."
                )
            )

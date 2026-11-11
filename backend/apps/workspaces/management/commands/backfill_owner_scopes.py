"""Backfill DÉTERMINISTE des périmètres owner pour les workspaces mono-owner.

DRY-RUN par défaut. Pour chaque workspace ayant EXACTEMENT un owner : rattache
ses memberships non-owner et ses RagSpaces à cet owner. Les workspaces sans owner
ou multi-owner sont **refusés automatiquement** (`no_single_owner`) — mapping
manuel obligatoire via `assign_membership_owner_scope` / `assign_ragspace_owner_scope`.

Sélection OBLIGATOIRE : `--workspace-id N` (un workspace) ou `--all` (tous). Chaque
workspace est mutilé de façon atomique (all-or-nothing). Rapport avant/après, codes
stables, sortie JSON. Aucun flag modifié, aucun enforcement.

Usage :
    python manage.py backfill_owner_scopes --workspace-id 3            # dry-run
    python manage.py backfill_owner_scopes --workspace-id 3 --apply
    python manage.py backfill_owner_scopes --all --format json        # dry-run global
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.workspaces.models import Workspace
from apps.workspaces.services import owner_scope_migration as osm


class Command(BaseCommand):
    help = "Backfill déterministe des workspaces mono-owner (dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, default=None)
        parser.add_argument("--all", action="store_true", help="Tous les workspaces.")
        parser.add_argument("--apply", action="store_true", help="Écrit réellement (sinon dry-run).")
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        if not opts["all"] and opts["workspace_id"] is None:
            raise CommandError("Sélection obligatoire : --workspace-id N ou --all.")
        if opts["all"] and opts["workspace_id"] is not None:
            raise CommandError("--workspace-id et --all sont exclusifs.")

        ws_qs = Workspace.objects.all().order_by("id")
        if opts["workspace_id"] is not None:
            ws_qs = ws_qs.filter(id=opts["workspace_id"])
            if not ws_qs.exists():
                raise CommandError(f"workspace {opts['workspace_id']} introuvable")

        report = [osm.backfill_workspace(workspace=ws, apply=opts["apply"]) for ws in ws_qs]
        all_changes = [c for e in report for c in e["changes"]]
        errors = osm.has_errors(all_changes)

        if opts["format"] == "json":
            self.stdout.write(json.dumps({"applied": opts["apply"], "workspaces": report}, default=str))
        else:
            self._render_text(report, applied=opts["apply"])

        if errors:
            sys.exit(1)

    def _render_text(self, report, *, applied: bool) -> None:
        mode = "APPLY" if applied else "DRY-RUN"
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== Backfill owner_scope [{mode}] ==="))
        for e in report:
            if e["code"] == osm.NO_SINGLE_OWNER:
                self.stdout.write(
                    f"⤫ ws#{e['workspace_id']} — no_single_owner (owners={e['owners']}) → mapping manuel requis"
                )
                continue
            counts = osm.summarize(e["changes"])
            self.stdout.write(
                f"✓ ws#{e['workspace_id']} owner=#{e['owner_membership_id']} → "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            )
        all_changes = [c for e in report for c in e["changes"]]
        total = osm.summarize(all_changes)
        self.stdout.write("")
        self.stdout.write("Total: " + (", ".join(f"{k}={v}" for k, v in sorted(total.items())) or "aucun changement"))
        if osm.has_errors(all_changes):
            self.stdout.write(self.style.ERROR("Conflits/incohérences détectés (exit 1) — voir codes ci-dessus."))

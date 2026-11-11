"""Désactive (ou réactive avec --enable) un workspace — soft, JAMAIS de delete.

Un workspace `is_active=False` est explicitement HORS périmètre d'enforcement
(règle documentée, prise en compte par audit_workspace_owner_scopes). DRY-RUN
par défaut. Idempotent, atomique.

Usage :
    python manage.py disable_workspace --workspace-id 1            # dry-run
    python manage.py disable_workspace --workspace-id 1 --apply
    python manage.py disable_workspace --workspace-id 1 --enable --apply  # réactive
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.workspaces.models import Workspace
from apps.workspaces.services import owner_scope_migration as osm


class Command(BaseCommand):
    help = "Désactive/réactive un workspace (soft, dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, required=True)
        parser.add_argument("--enable", action="store_true", help="Réactive au lieu de désactiver.")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        ws = Workspace.objects.filter(pk=opts["workspace_id"]).first()
        if ws is None:
            raise CommandError(f"workspace {opts['workspace_id']} introuvable")

        res = osm.set_workspace_active(workspace=ws, active=opts["enable"], apply=opts["apply"])
        res["applied"] = opts["apply"]

        if opts["format"] == "json":
            self.stdout.write(json.dumps(res, default=str))
        else:
            mode = "APPLY" if opts["apply"] else "DRY-RUN"
            self.stdout.write(f"[{mode}] ws#{res['workspace_id']} → {res['code']} (was_active={res['was_active']})")

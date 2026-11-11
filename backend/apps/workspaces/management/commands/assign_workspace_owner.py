"""Crée (ou promeut) explicitement le premier owner d'un workspace orphelin.

Réservé opérateur/superuser. DRY-RUN par défaut. Ne détourne PAS la commande
générique de changement de rôle : crée un Membership(role=owner) dédié.

Usage :
    python manage.py assign_workspace_owner --workspace-id 7 --user-id 42          # dry-run
    python manage.py assign_workspace_owner --workspace-id 7 --user-id 42 --apply
    # workspace possédant déjà un owner : refus, sauf --allow-additional-owner
    # user déjà membre non-owner : refus, sauf --promote
"""
from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.workspaces.models import Workspace
from apps.workspaces.services import owner_scope_migration as osm


class Command(BaseCommand):
    help = "Crée le premier owner d'un workspace (dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, required=True)
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--allow-additional-owner", action="store_true",
                            help="Autorise l'ajout même si le workspace a déjà un owner.")
        parser.add_argument("--promote", action="store_true",
                            help="Autorise la promotion d'un membre non-owner existant.")
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        ws = Workspace.objects.filter(pk=opts["workspace_id"]).first()
        if ws is None:
            raise CommandError(f"workspace {opts['workspace_id']} introuvable")
        user = get_user_model().objects.filter(pk=opts["user_id"]).first()
        if user is None:
            raise CommandError(f"user {opts['user_id']} introuvable")

        res = osm.create_workspace_owner(
            workspace=ws,
            user=user,
            apply=opts["apply"],
            allow_additional=opts["allow_additional_owner"],
            promote=opts["promote"],
        )
        res["applied"] = opts["apply"]

        if opts["format"] == "json":
            self.stdout.write(json.dumps(res, default=str))
        else:
            mode = "APPLY" if opts["apply"] else "DRY-RUN"
            self.stdout.write(f"[{mode}] ws#{res['workspace_id']} user#{res['user_id']} → {res['code']}")

        if res["code"] in osm.ERROR_CODES:
            sys.exit(1)

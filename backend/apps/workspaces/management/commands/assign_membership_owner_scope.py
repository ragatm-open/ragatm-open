"""Rattache explicitement un membership non-owner à un périmètre owner.

DRY-RUN par défaut : n'écrit que si `--apply` est passé. Idempotent, atomique,
refuse les cibles cross-workspace ou non-owner. Ne change aucun flag.

Usage :
    python manage.py assign_membership_owner_scope --membership-id 12 --owner-membership-id 3
    python manage.py assign_membership_owner_scope --membership-id 12 --owner-membership-id 3 --apply
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.workspaces.models import Membership
from apps.workspaces.services import owner_scope_migration as osm


class Command(BaseCommand):
    help = "Rattache un membership non-owner à un owner (dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--membership-id", type=int, required=True)
        parser.add_argument("--owner-membership-id", type=int, required=True)
        parser.add_argument("--apply", action="store_true", help="Écrit réellement (sinon dry-run).")
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        membership = Membership.objects.filter(pk=opts["membership_id"]).first()
        owner = Membership.objects.filter(pk=opts["owner_membership_id"]).first()
        if membership is None:
            raise CommandError(f"membership {opts['membership_id']} introuvable")
        if owner is None:
            raise CommandError(f"owner membership {opts['owner_membership_id']} introuvable")

        res = osm.assign_membership_scope(membership=membership, owner=owner, apply=opts["apply"])
        res["applied"] = opts["apply"]

        if opts["format"] == "json":
            self.stdout.write(json.dumps(res, default=str))
        else:
            mode = "APPLY" if opts["apply"] else "DRY-RUN"
            self.stdout.write(f"[{mode}] membership#{res['id']} → {res['code']} (before={res['before']})")

        if res["code"] in osm.ERROR_CODES:
            sys.exit(1)

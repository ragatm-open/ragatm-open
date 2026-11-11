"""Inventaire EN LECTURE SEULE des accès RagSpace d'un workspace (Lot ACL).

N'ÉCRIT RIEN. Produit, pour chaque membership du workspace, les éléments
nécessaires à une décision d'octroi humaine, et une **proposition prudente** :
seul le cas non ambigu est proposé automatiquement ; tout cas ambigu reste
NON MAPPÉ (jamais deviné).

Règle de proposition :
  - owner  → aucun grant (accès implicite manage) ;
  - viewer → viewer (non ambigu : plafond D1 = viewer) → PROPOSÉ ;
  - member → editor | viewer selon l'usage → AMBIGU → non mappé ;
  - admin  → manager seulement s'il administre réellement l'espace → AMBIGU → non mappé.

Usage :
    python manage.py owner_scope_acl_inventory --workspace-id 2 --format json
    python manage.py owner_scope_acl_inventory --workspace-id 2 \
        --output-inventory acl_inventory.json --output-proposal mappings.proposed.json
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.rag_core.models import RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import (
    _ALLOWED_SPACE_ROLES_BY_WS_ROLE,
    effective_owner_scope_pk,
)

# Plafond D1 « maximal » lisible par rôle workspace.
_D1_MAX = {"owner": "manage", "admin": "manager", "member": "editor", "viewer": "viewer"}


class Command(BaseCommand):
    help = "Inventaire lecture seule des accès RagSpace + proposition prudente (aucune écriture)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, required=True)
        parser.add_argument("--output-inventory", default=None)
        parser.add_argument("--output-proposal", default=None)
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        ws = Workspace.objects.filter(pk=opts["workspace_id"]).first()
        if ws is None:
            raise CommandError(f"workspace {opts['workspace_id']} introuvable")

        members = []
        proposed_grants = []
        for m in Membership.objects.filter(workspace=ws).select_related("user"):
            scope_pk = effective_owner_scope_pk(m)
            scoped_spaces = list(
                RagSpace.objects.filter(workspace=ws, owner_scope_id=scope_pk, deleted_at__isnull=True)
            ) if scope_pk is not None else []
            active = list(
                RagSpaceAccess.objects.filter(
                    user=m.user, revoked_at__isnull=True, rag_space__workspace=ws
                ).values("rag_space_id", "space_role")
            )
            proposed, status, justif = self._propose(m.role)

            members.append({
                "user_id": m.user_id,
                "username": m.user.username,
                "email": m.user.email,
                "workspace_role": m.role,
                "owner_scope_id": scope_pk,
                "rag_spaces_in_scope": [str(s.id) for s in scoped_spaces],
                "existing_active_grants": [
                    {"rag_space_id": str(a["rag_space_id"]), "space_role": a["space_role"]} for a in active
                ],
                "d1_max_space_role": _D1_MAX.get(m.role, "viewer"),
                "proposed_space_role": proposed,
                "proposal_status": status,
                "justification": justif,
            })

            # Proposition = viewer non ambigu, uniquement pour les espaces SANS grant actif.
            if proposed is not None:
                existing_rs = {a["rag_space_id"] for a in active}
                for s in scoped_spaces:
                    if s.id not in existing_rs:
                        proposed_grants.append({
                            "user_id": m.user_id, "rag_space_id": str(s.id), "space_role": proposed,
                        })

        inventory = {"workspace_id": ws.id, "workspace_name": ws.name, "members": members,
                     "summary": self._summary(members)}
        proposal = {"workspace_id": ws.id, "grants": proposed_grants,
                    "note": "PROPOSITION NON APPLIQUÉE — valider les affectations métier avant backfill. "
                            "Les rôles ambigus (admin/member) restent NON MAPPÉS."}

        if opts["output_inventory"]:
            with open(opts["output_inventory"], "w") as fh:
                fh.write(json.dumps(inventory, indent=2, sort_keys=True, default=str))
            self.stdout.write(f"inventaire écrit : {opts['output_inventory']}")
        if opts["output_proposal"]:
            with open(opts["output_proposal"], "w") as fh:
                fh.write(json.dumps(proposal, indent=2, sort_keys=True, default=str))
            self.stdout.write(f"proposition écrite : {opts['output_proposal']}")

        if not opts["output_inventory"] and not opts["output_proposal"]:
            if opts["format"] == "json":
                self.stdout.write(json.dumps({"inventory": inventory, "proposal": proposal},
                                             indent=2, sort_keys=True, default=str))
            else:
                self._render_text(inventory, proposal)

    @staticmethod
    def _propose(ws_role: str):
        if ws_role == "owner":
            return None, "no_grant_owner_implicit", "Owner du périmètre : accès manage implicite."
        if ws_role == "viewer":
            return "viewer", "auto_unambiguous", "Plafond D1 = viewer ; proposition non ambiguë."
        if ws_role == "member":
            return None, "ambiguous_member_editor_or_viewer", "editor|viewer selon l'usage — décision humaine requise."
        if ws_role == "admin":
            return None, "ambiguous_admin_manager_if_administers", "manager seulement s'il administre l'espace — décision humaine requise."
        return None, "unknown_role", "Rôle inconnu — non mappé."

    @staticmethod
    def _summary(members) -> dict:
        d = {}
        for m in members:
            d[m["proposal_status"]] = d.get(m["proposal_status"], 0) + 1
        return d

    def _render_text(self, inventory, proposal):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Inventaire ACL ws#{inventory['workspace_id']} '{inventory['workspace_name']}' ==="))
        for m in inventory["members"]:
            self.stdout.write(
                f"  user={m['user_id']} ({m['username']}) role={m['workspace_role']} "
                f"scope={m['owner_scope_id']} spaces={len(m['rag_spaces_in_scope'])} "
                f"grants={len(m['existing_active_grants'])} -> proposed={m['proposed_space_role']} "
                f"[{m['proposal_status']}]")
        self.stdout.write("Résumé: " + ", ".join(f"{k}={v}" for k, v in sorted(inventory["summary"].items())))
        self.stdout.write(f"Propositions non ambiguës: {len(proposal['grants'])} (NON APPLIQUÉES)")

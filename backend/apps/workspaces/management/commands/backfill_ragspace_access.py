"""Backfill EXPLICITE des accès RagSpace depuis un fichier de mapping.

Aucune attribution arbitraire : chaque octroi est déclaré dans le mapping. DRY-RUN
par défaut (`--apply` obligatoire pour écrire). Transaction PAR WORKSPACE
(all-or-nothing), `select_for_update`, idempotent, codes de résultat stables,
sortie JSON, journal d'audit. **Aucun flag activé.**

Mapping (identifiants uniquement) :
    {
      "workspace_id": 2,
      "grants": [
        {"user_id": 7, "rag_space_id": "…", "space_role": "viewer"}
      ]
    }

Usage :
    python manage.py backfill_ragspace_access --workspace-id 2 --mapping-file m.json
    python manage.py backfill_ragspace_access --workspace-id 2 --mapping-file m.json --apply --format json
"""
from __future__ import annotations

import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.rag_core.models import RagSpace
from apps.workspaces.models import Workspace
from apps.workspaces.services import ragspace_access as svc


class Command(BaseCommand):
    help = "Backfill explicite des RagSpaceAccess depuis un mapping (dry-run par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-id", type=int, required=True)
        parser.add_argument("--mapping-file", required=True)
        parser.add_argument("--assigned-by-user-id", type=int, default=None)
        parser.add_argument("--allow-role-change", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--format", choices=["text", "json"], default="text")

    def handle(self, *args, **opts):
        try:
            with open(opts["mapping_file"]) as fh:
                mapping = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise CommandError(f"mapping illisible: {e}")

        ws_id = opts["workspace_id"]
        if mapping.get("workspace_id") != ws_id:
            raise CommandError(
                f"workspace_id du mapping ({mapping.get('workspace_id')}) ≠ --workspace-id ({ws_id})."
            )
        ws = Workspace.objects.filter(pk=ws_id).first()
        if ws is None:
            raise CommandError(f"workspace {ws_id} introuvable")

        assigned_by = None
        if opts["assigned_by_user_id"] is not None:
            assigned_by = get_user_model().objects.filter(pk=opts["assigned_by_user_id"]).first()
            if assigned_by is None:
                raise CommandError(f"assigned-by user {opts['assigned_by_user_id']} introuvable")

        grants = mapping.get("grants") or []
        results = self._process(ws, grants, assigned_by=assigned_by, apply=opts["apply"],
                                allow_role_change=opts["allow_role_change"])

        errors = any(r.get("code") in svc.ERROR_CODES for r in results)
        report = {"applied": opts["apply"], "workspace_id": ws_id, "results": results,
                  "summary": _summ(results)}

        if opts["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            mode = "APPLY" if opts["apply"] else "DRY-RUN"
            self.stdout.write(self.style.MIGRATE_HEADING(f"=== backfill RagSpaceAccess ws#{ws_id} [{mode}] ==="))
            for r in results:
                self.stdout.write(f"  user={r.get('user_id')} rs={str(r.get('rag_space_id'))[:8]} "
                                  f"role={r.get('space_role')} -> {r['code']}"
                                  + (f" ({r['detail']})" if r.get("detail") else ""))
            self.stdout.write("Total: " + ", ".join(f"{k}={v}" for k, v in sorted(_summ(results).items())))
            if errors:
                self.stdout.write(self.style.ERROR("Erreurs détectées (exit 1) — voir codes ci-dessus."))

        if errors:
            sys.exit(1)

    def _process(self, ws, grants, *, assigned_by, apply, allow_role_change) -> list:
        # Pré-résolution + validation d'appartenance workspace AVANT toute écriture.
        prepared, results = [], []
        for g in grants:
            base = {"user_id": g.get("user_id"), "rag_space_id": g.get("rag_space_id"),
                    "space_role": g.get("space_role")}
            rs = RagSpace.objects.filter(pk=g.get("rag_space_id")).select_related("workspace").first()
            user = get_user_model().objects.filter(pk=g.get("user_id")).first()
            if rs is None:
                results.append({**base, "code": svc.INVALID, "detail": "ragspace_not_found"}); continue
            if rs.workspace_id != ws.id:
                results.append({**base, "code": svc.INVALID, "detail": "ragspace_wrong_workspace"}); continue
            if user is None:
                results.append({**base, "code": svc.INVALID, "detail": "user_not_found"}); continue
            if g.get("space_role") not in ("manager", "editor", "viewer"):
                results.append({**base, "code": svc.INVALID, "detail": "bad_space_role"}); continue
            prepared.append((rs, user, g["space_role"]))

        if not prepared:
            return results

        def _run(apply_inner):
            out = []
            for rs, user, role in prepared:
                out.append(svc.grant_access(
                    rag_space=rs, user=user, space_role=role, assigned_by=assigned_by,
                    apply=apply_inner, allow_role_change=allow_role_change,
                ))
            return out

        if apply:
            with transaction.atomic():  # all-or-nothing par workspace
                applied = _run(True)
                if any(r.get("code") in svc.ERROR_CODES for r in applied):
                    transaction.set_rollback(True)  # aucun octroi partiel si une erreur
                self._audit(ws, applied)
            results.extend(applied)
        else:
            results.extend(_run(False))
        return results

    @staticmethod
    def _audit(ws, applied):
        try:
            from apps.audit.action_audit import emit_action_event
        except Exception:  # noqa: BLE001
            return
        for r in applied:
            if r.get("code") in (svc.GRANTED, svc.ROLE_CHANGED):
                emit_action_event(
                    event_name="ragspace_access_granted", action_name="ragspace.access.grant",
                    status="completed", workspace_id=str(ws.id), rag_space_id=r.get("rag_space_id"),
                    resource_type="ragspace_access", resource_id=r.get("grant_id"),
                    metadata={"user_id": r.get("user_id"), "space_role": r.get("space_role"), "code": r["code"]},
                )


def _summ(results) -> dict:
    d = {}
    for r in results:
        d[r["code"]] = d.get(r["code"], 0) + 1
    return d

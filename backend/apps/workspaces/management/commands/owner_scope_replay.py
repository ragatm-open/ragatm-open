"""Harnais de rejeu owner_scope (Lot 2 — phase d'observation).

Rejoue une matrice représentative de requêtes (rôles × périmètres × endpoints)
avec UNIQUEMENT les flags audit actifs, capture les événements `owner_scope.audit`,
les agrège (par endpoint / rôle / owner_scope / raison) et produit un JSON de
synthèse. **Échoue (exit 1)** si une surface attendue n'émet aucun audit.

Propriétés :
  - **non destructif** : toutes les données sont créées dans un savepoint ROLLBACK ;
  - **aucun contenu métier** dans la sortie (uniquement identifiants + décisions) ;
  - n'active que `OWNER_SCOPE_AUDIT_ONLY` (+ `RAGSPACE_ACL_AUDIT_ONLY` si demandé) ;
  - réponses HTTP inchangées (mode audit) — le harnais vérifie aussi ce point.

Usage :
    python manage.py owner_scope_replay
    python manage.py owner_scope_replay --output rapport.json --simulate-acl
"""
from __future__ import annotations

import json
import logging
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import override_settings
from rest_framework.test import APIClient

from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Membership, Workspace

# Surfaces GET attendues (legacy = require_rag_space_access, classification nette).
# Les surfaces `manage` (patch/reindex/llm) ajoutent un contrôle owner/admin que
# l'audit ne reflète pas dans `legacy_decision` — exclues du gate de silence pour
# ne pas induire en erreur (voir docs/03-backend/owner-scope.md).
EXPECTED_ENDPOINTS = {
    "ragspaces.list",
    "ragspaces.stats",
    "documents.list",
    "documents.access",
    "chat.threads.list",
}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.events: list[dict] = []

    def emit(self, record):
        msg = record.getMessage()
        if "owner_scope_audit " in msg:
            try:
                self.events.append(json.loads(msg.split(" ", 1)[1]))
            except Exception:
                pass


class Command(BaseCommand):
    help = "Rejeu owner_scope (audit-only), agrégation et rapport JSON."

    def add_arguments(self, parser):
        parser.add_argument("--output", type=str, default=None, help="Chemin du JSON de synthèse.")
        parser.add_argument("--simulate-acl", action="store_true", help="Active RAGSPACE_ACL_AUDIT_ONLY.")

    def handle(self, *args, **opts):
        cap = _Capture()
        logger = logging.getLogger("owner_scope.audit")
        prev_level = logger.level
        logger.addHandler(cap)
        logger.setLevel(logging.INFO)
        try:
            report = self._run(cap, simulate_acl=opts["simulate_acl"])
        finally:
            logger.removeHandler(cap)
            logger.setLevel(prev_level)

        payload = json.dumps(report, indent=2, sort_keys=True, default=str)
        if opts["output"]:
            with open(opts["output"], "w") as fh:
                fh.write(payload)
            self.stdout.write(f"Rapport écrit : {opts['output']}")
        else:
            self.stdout.write(payload)

        if report["endpoints_silent"] or report["http_changed"]:
            sys.exit(1)

    def _run(self, cap: _Capture, *, simulate_acl: bool) -> dict:
        User = get_user_model()
        flags = dict(
            OWNER_SCOPE_ENABLED=False,
            OWNER_SCOPE_AUDIT_ONLY=True,
            RAGSPACE_ACL_ENABLED=False,
            RAGSPACE_ACL_AUDIT_ONLY=bool(simulate_acl),
        )
        roles_by_user: dict[int, str] = {}
        http_changed: list[dict] = []

        import uuid as _uuid
        sfx = _uuid.uuid4().hex[:8]  # évite toute collision de username inter-runs

        # atomic() + set_rollback(True) : ROLLBACK réel même en autocommit (un
        # simple savepoint hors atomic laisserait les INSERT committés).
        with override_settings(**flags), transaction.atomic():
            try:
                ws = Workspace.objects.create(name=f"ZZ_REPLAY_{sfx}")
                ua = User.objects.create_user(f"zzr_{sfx}_oa", f"{sfx}_oa@t.local", "x")
                ub = User.objects.create_user(f"zzr_{sfx}_ob", f"{sfx}_ob@t.local", "x")
                um = User.objects.create_user(f"zzr_{sfx}_ma", f"{sfx}_ma@t.local", "x")
                uv = User.objects.create_user(f"zzr_{sfx}_va", f"{sfx}_va@t.local", "x")
                un = User.objects.create_user(f"zzr_{sfx}_nm", f"{sfx}_nm@t.local", "x")
                owner_a = Membership.objects.create(workspace=ws, user=ua, role="owner")
                owner_b = Membership.objects.create(workspace=ws, user=ub, role="owner")
                Membership.objects.create(workspace=ws, user=um, role="member", owner_scope=owner_a)
                Membership.objects.create(workspace=ws, user=uv, role="viewer", owner_scope=owner_a)
                for u, r in ((ua, "owner_a"), (ub, "owner_b"), (um, "member_a"), (uv, "viewer_a"), (un, "non_member")):
                    roles_by_user[u.id] = r
                rs_a = RagSpace.objects.create(workspace=ws, name="A", owner_scope=owner_a)
                rs_b = RagSpace.objects.create(workspace=ws, name="B", owner_scope=owner_b)
                doc_a = Document.objects.create(rag_space=rs_a, filename="a.pdf", sha256="a" * 64)
                doc_b = Document.objects.create(rag_space=rs_b, filename="b.pdf", sha256="b" * 64)

                spaces = {"scope_a": (rs_a, doc_a), "scope_b": (rs_b, doc_b)}
                users = [ua, ub, um, uv, un]

                for u in users:
                    client = APIClient(); client.force_authenticate(user=u)
                    # ragspaces.list (workspace-level) — un NON-MEMBRE reçoit 403
                    # (not_a_member) : refus d'accès DISTINCT du résultat vide (200 []).
                    # L'ensemble legacy attendu inclut donc les deux, comme documents.list.
                    r = client.get(f"/api/v1/rag-spaces/?workspace_id={ws.id}")
                    self._check_unchanged(http_changed, "ragspaces.list", u, r.status_code, {200, 403})
                    for _lbl, (rs, doc) in spaces.items():
                        r = client.get(f"/api/v1/rag-spaces/{rs.id}/stats/")
                        self._check_unchanged(http_changed, "ragspaces.stats", u, r.status_code, {200, 403, 404})
                        r = client.get(f"/api/v1/documents/?rag_space_id={rs.id}")
                        self._check_unchanged(http_changed, "documents.list", u, r.status_code, {200, 403})
                        r = client.get(f"/api/v1/documents/{doc.id}/")
                        self._check_unchanged(http_changed, "documents.access", u, r.status_code, {200, 404})
                        r = client.get(f"/api/v1/chat/threads/?rag_space_id={rs.id}")
                        self._check_unchanged(http_changed, "chat.threads.list", u, r.status_code, {200, 403})
            finally:
                # Ne RIEN persister : rollback de tout le jeu synthétique.
                transaction.set_rollback(True)

        return self._aggregate(cap.events, roles_by_user, http_changed)

    @staticmethod
    def _check_unchanged(acc, endpoint, user, status, allowed):
        # En audit-only, la réponse doit rester dans l'ensemble legacy attendu.
        if status not in allowed:
            acc.append({"endpoint": endpoint, "user_id": user.id, "status": status})

    def _aggregate(self, events, roles_by_user, http_changed) -> dict:
        def bump(d, k):
            d[k] = d.get(k, 0) + 1

        totals, by_endpoint, by_role, by_scope, by_reason = {}, {}, {}, {}, {}
        would_deny, would_allow, list_findings = [], [], []
        exercised = set()
        for e in events:
            ep = e.get("endpoint")
            role = roles_by_user.get(e.get("user_id"), "?")
            exercised.add(ep)

            # Événement de LISTE (ensemble legacy vs cible) : pas de champ `audit`.
            if e.get("event") == "ragspace_list_decision":
                if e.get("would_hide_count"):
                    list_findings.append({
                        "endpoint": ep, "role": role,
                        "legacy_count": e.get("legacy_count"),
                        "target_count": e.get("target_count"),
                        "would_hide_count": e.get("would_hide_count"),
                    })
                continue

            audit = e.get("audit")
            if audit is None:
                continue
            scope = e.get("owner_scope_id")
            bump(totals, audit)
            by_endpoint.setdefault(ep, {}); bump(by_endpoint[ep], audit)
            by_role.setdefault(role, {}); bump(by_role[role], audit)
            by_scope.setdefault(str(scope), {}); bump(by_scope[str(scope)], audit)
            if audit in ("would_deny", "would_allow"):
                bump(by_reason, e.get("reason") or "?")
                sample = {"endpoint": ep, "role": role, "reason": e.get("reason"),
                          "owner_scope_id": str(scope), "rag_space_id": e.get("rag_space_id")}
                (would_deny if audit == "would_deny" else would_allow).append(sample)

        return {
            "flags": {"OWNER_SCOPE_AUDIT_ONLY": True},
            "totals": totals,
            "by_endpoint": by_endpoint,
            "by_role": by_role,
            "by_owner_scope": by_scope,
            "by_reason": by_reason,
            "endpoints_exercised": sorted(x for x in exercised if x),
            "endpoints_silent": sorted(EXPECTED_ENDPOINTS - exercised),
            "would_deny": would_deny,
            "would_allow": would_allow,
            "list_findings": list_findings,
            "http_changed": http_changed,
            "http_unchanged": not http_changed,
        }

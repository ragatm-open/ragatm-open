"""Diagnostic (lecture seule) des périmètres owner — logique CANONIQUE partagée.

Extrait de la commande `audit_workspace_owner_scopes` pour être réutilisable par
l'API plateforme SANS réimplémentation parallèle. N'ÉCRIT RIEN.

`diagnose_workspace(ws)` classe les anomalies d'un workspace avec des CODES STABLES
et distingue trois natures :
  - integrity : incohérence de données qui ne doit JAMAIS exister ;
  - decision  : nécessite une décision humaine (multi-owner, orphelin) ;
  - pending   : simplement pas encore backfillé (attendu avant migration).

La commande et l'endpoint plateforme consomment tous deux ces fonctions : une seule
source de vérité.
"""
from __future__ import annotations

from apps.rag_core.models import Document, RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace, WorkspaceInvitation

INTEGRITY_CODES = {
    "scope_cross_workspace",
    "scope_target_not_owner",
    "access_scope_mismatch",
    "multiple_active_accesses",
}
DECISION_CODES = {"workspace_without_owner", "multiple_owners"}
# non_owner_without_scope / ragspace_without_scope / inactive_owner -> pending/info


def diagnose_workspace(ws: Workspace) -> dict:
    """Diagnostic complet d'un workspace (aucune écriture). Structure stable."""
    # Règle documentée : un workspace explicitement DÉSACTIVÉ (is_active=False) est
    # hors périmètre d'enforcement. TOUJOURS listé (jamais ignoré) mais code
    # informatif non bloquant. On n'exclut JAMAIS un workspace par simple vacuité.
    if not ws.is_active:
        return {
            "workspace_id": ws.id,
            "workspace_name": ws.name,
            "is_active": False,
            "owners": [{"membership_id": m.pk, "user_id": m.user_id}
                       for m in Membership.objects.filter(workspace=ws, role=Membership.ROLE_OWNER)],
            "memberships_total": Membership.objects.filter(workspace=ws).count(),
            "ragspaces_active": RagSpace.objects.filter(workspace=ws, deleted_at__isnull=True).count(),
            "documents": Document.objects.filter(rag_space__workspace=ws).count(),
            "anomalies": [{"code": "workspace_disabled"}],
            "has_integrity_violation": False,
            "migratable_automatically": False,
            "suggested_action": "désactivé — hors enforcement (règle documentée is_active=False)",
        }

    memberships = list(Membership.objects.filter(workspace=ws).select_related("owner_scope"))
    owners = [m for m in memberships if m.role == Membership.ROLE_OWNER]
    non_owners = [m for m in memberships if m.role != Membership.ROLE_OWNER]
    owner_pks = {m.pk for m in owners}
    spaces = list(RagSpace.objects.filter(workspace=ws, deleted_at__isnull=True))

    anomalies: list[dict] = []

    def add(code: str, **detail):
        anomalies.append({"code": code, **detail})

    # -- décisions
    if not owners:
        add("workspace_without_owner")
    if len(owners) > 1:
        add("multiple_owners", owner_membership_ids=[m.pk for m in owners])

    # -- owner inactif (info)
    for m in owners:
        if not getattr(m.user, "is_active", True):
            add("inactive_owner", membership_id=m.pk, user_id=m.user_id)

    # -- memberships non-owner : scope manquant (pending) ou incohérent (integrity)
    for m in non_owners:
        if m.owner_scope_id is None:
            add("non_owner_without_scope", membership_id=m.pk)
            continue
        sc = m.owner_scope
        if sc.role != Membership.ROLE_OWNER:
            add("scope_target_not_owner", membership_id=m.pk, owner_scope_id=m.owner_scope_id)
        if sc.workspace_id != ws.id:
            add("scope_cross_workspace", membership_id=m.pk, owner_scope_id=m.owner_scope_id)

    # -- RagSpaces : scope manquant (pending) ou incohérent (integrity)
    for s in spaces:
        if s.owner_scope_id is None:
            add("ragspace_without_scope", rag_space_id=str(s.id))
            continue
        if s.owner_scope_id not in owner_pks:
            sc = Membership.objects.filter(pk=s.owner_scope_id).first()
            if sc is None or sc.workspace_id != ws.id:
                add("scope_cross_workspace", rag_space_id=str(s.id), owner_scope_id=s.owner_scope_id)
            elif sc.role != Membership.ROLE_OWNER:
                add("scope_target_not_owner", rag_space_id=str(s.id), owner_scope_id=s.owner_scope_id)

    # -- RagSpaceAccess : cohérence user↔périmètre↔space + doublons actifs
    active_access = list(
        RagSpaceAccess.objects.filter(
            rag_space__workspace=ws, revoked_at__isnull=True
        ).select_related("rag_space")
    )
    seen: dict[tuple, int] = {}
    for a in active_access:
        key = (a.rag_space_id, a.user_id)
        seen[key] = seen.get(key, 0) + 1
        um = Membership.objects.filter(workspace=ws, user_id=a.user_id).first()
        eff = None
        if um is not None:
            eff = um.pk if um.role == Membership.ROLE_OWNER else um.owner_scope_id
        if um is None or a.rag_space.owner_scope_id is None or eff != a.rag_space.owner_scope_id:
            add("access_scope_mismatch", access_id=str(a.id), user_id=a.user_id, rag_space_id=str(a.rag_space_id))
    for key, n in seen.items():
        if n > 1:
            add("multiple_active_accesses", rag_space_id=str(key[0]), user_id=key[1], count=n)

    codes = {a["code"] for a in anomalies}
    if not owners:
        suggested = "assigner_owner (superuser) puis mapper — aucune migration auto"
    elif len(owners) == 1 and not (codes & INTEGRITY_CODES):
        suggested = "backfill_auto_mono_owner"
    elif codes & INTEGRITY_CODES:
        suggested = "CORRIGER incohérence AVANT toute activation"
    else:
        suggested = "mapping_manuel_multi_owner (assign_owner_scope / assign_membership_owner_scope)"

    return {
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "is_active": True,
        "owners": [{"membership_id": m.pk, "user_id": m.user_id} for m in owners],
        "memberships_total": len(memberships),
        "non_owner_memberships": len(non_owners),
        "ragspaces_active": len(spaces),
        "documents": Document.objects.filter(rag_space__workspace=ws).count(),
        "invitations_pending": WorkspaceInvitation.objects.filter(
            workspace=ws, accepted_at__isnull=True, revoked_at__isnull=True
        ).count(),
        "active_ragspace_accesses": len(active_access),
        "anomalies": anomalies,
        "has_integrity_violation": bool(codes & INTEGRITY_CODES),
        "migratable_automatically": (len(owners) == 1 and not (codes & INTEGRITY_CODES)),
        "suggested_action": suggested,
    }


def diagnose_all(workspace_id: int | None = None) -> list[dict]:
    """Diagnostic de tous les workspaces (ou d'un seul), ordonné par id."""
    qs = Workspace.objects.all().order_by("id")
    if workspace_id is not None:
        qs = qs.filter(id=workspace_id)
    return [diagnose_workspace(ws) for ws in qs]


def report_flags(report: list[dict]) -> dict:
    """Agrégats de haut niveau d'un rapport (pour exit code / dashboard)."""
    all_codes = {a["code"] for e in report for a in e["anomalies"]}
    return {
        "has_integrity": bool(all_codes & INTEGRITY_CODES),
        "has_decision": bool(all_codes & DECISION_CODES),
    }

"""API d'administration PLATEFORME — lecture seule (Lot P1).

Users et workspaces au niveau global, réservés au superuser (`IsRagATMOwner`).
Aucun contenu documentaire n'est exposé. Voir `common.py` pour la doctrine.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.models import Document, RagSpace, RagSpaceAccess
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import effective_owner_scope_pk
from apps.workspaces.services.owner_scope_diagnostics import (
    diagnose_all,
    diagnose_workspace,
    report_flags,
)

from .common import (
    PLATFORM_PERMS,
    page_envelope,
    parse_page,
    user_display_name,
    user_public_dict,
)

User = get_user_model()


def _owner_users_map(user_ids: set[int]) -> dict[int, dict]:
    """Batch : {user_id -> {id, username, email, display_name}} (anti N+1)."""
    out: dict[int, dict] = {}
    for u in User.objects.filter(id__in=user_ids):
        out[u.id] = {"id": u.id, "username": u.username or "", "email": u.email or "",
                     "display_name": user_display_name(u)}
    return out


class PlatformUserListView(APIView):
    """GET /api/v1/platform/users/ — annuaire global paginé.

    Filtres : search (username/email), is_active, is_superuser, workspace_id, role.
    """
    permission_classes = PLATFORM_PERMS

    def get(self, request):
        limit, offset = parse_page(request)
        qs = User.objects.all().order_by("id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
        act = request.query_params.get("is_active")
        if act in ("0", "1", "true", "false"):
            qs = qs.filter(is_active=act in ("1", "true"))
        su = request.query_params.get("is_superuser")
        if su in ("0", "1", "true", "false"):
            qs = qs.filter(is_superuser=su in ("1", "true"))
        wid = request.query_params.get("workspace_id")
        if wid:
            qs = qs.filter(memberships__workspace_id=wid)
        role = request.query_params.get("role")
        if role:
            qs = qs.filter(memberships__role=role)
        qs = qs.distinct()

        total = qs.count()
        page = qs.annotate(
            memberships_count=Count("memberships", distinct=True),
            owned_workspaces_count=Count(
                "memberships", filter=Q(memberships__role=Membership.ROLE_OWNER), distinct=True
            ),
        )[offset:offset + limit]

        items = []
        for u in page:
            d = user_public_dict(u)
            d["date_joined"] = u.date_joined
            d["last_login"] = u.last_login
            d["memberships_count"] = u.memberships_count
            d["owned_workspaces_count"] = u.owned_workspaces_count
            items.append(d)
        return Response(page_envelope(items, total, limit, offset), status=200)


class PlatformUserDetailView(APIView):
    """GET /api/v1/platform/users/<id>/ — fiche gouvernance (sans contenu)."""
    permission_classes = PLATFORM_PERMS

    def get(self, request, user_id):
        u = User.objects.filter(id=user_id).first()
        if u is None:
            return Response({"detail": "user_not_found"}, status=404)
        d = user_public_dict(u)
        d["date_joined"] = u.date_joined
        d["last_login"] = u.last_login

        mems = list(
            Membership.objects.filter(user=u).select_related("workspace", "owner_scope", "owner_scope__user")
        )
        memberships = []
        owned = []
        for m in mems:
            scope_pk = effective_owner_scope_pk(m)
            owner_target = None
            if m.owner_scope_id and m.owner_scope:
                owner_target = {
                    "membership_id": m.owner_scope_id,
                    "user_id": m.owner_scope.user_id,
                    "display_name": user_display_name(m.owner_scope.user),
                }
            memberships.append({
                "membership_id": m.pk,
                "workspace_id": m.workspace_id,
                "workspace_name": m.workspace.name,
                "workspace_is_active": m.workspace.is_active,
                "role": m.role,
                "owner_scope_membership_id": m.owner_scope_id,
                "effective_owner_scope_pk": scope_pk,
                "owner_membership": owner_target,
            })
            if m.role == Membership.ROLE_OWNER:
                owned.append({"workspace_id": m.workspace_id, "workspace_name": m.workspace.name})

        d["memberships"] = memberships
        d["owned_workspaces"] = owned
        # Métadonnées MINIMALES : comptage seul, jamais le contenu des grants.
        d["active_ragspace_accesses_count"] = RagSpaceAccess.objects.filter(
            user=u, revoked_at__isnull=True
        ).count()
        return Response(d, status=200)


def _workspace_list_row(ws: Workspace, diag: dict, owners_map: dict[int, dict],
                        ragspaces_total: int) -> dict:
    owners = [{**o, **owners_map.get(o["user_id"], {})} for o in diag.get("owners", [])]
    return {
        "id": ws.id,
        "name": ws.name,
        "is_active": ws.is_active,
        "created_at": ws.created_at,
        "owners": owners,
        "members_count": diag.get("memberships_total", 0),
        "ragspaces_count": ragspaces_total,
        "active_ragspaces_count": diag.get("ragspaces_active", 0),
        "documents_count": diag.get("documents", 0),
        "has_integrity_violation": diag.get("has_integrity_violation", False),
        "migratable_automatically": diag.get("migratable_automatically", False),
        "anomaly_codes": sorted({a["code"] for a in diag.get("anomalies", [])}),
        "suggested_action": diag.get("suggested_action", ""),
    }


class PlatformWorkspaceListView(APIView):
    """GET /api/v1/platform/workspaces/ — liste globale paginée + diagnostic.

    Filtres : search, is_active, without_owner, with_anomalies. Le diagnostic
    n'est calculé QUE pour la page renvoyée (borné par la pagination, cf. §perf).
    """
    permission_classes = PLATFORM_PERMS

    def get(self, request):
        limit, offset = parse_page(request)
        qs = Workspace.objects.all().order_by("id")
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        act = request.query_params.get("is_active")
        if act in ("0", "1", "true", "false"):
            qs = qs.filter(is_active=act in ("1", "true"))

        total = qs.count()
        page = list(
            qs.annotate(ragspaces_total=Count("rag_spaces", distinct=True))[offset:offset + limit]
        )

        diags = {ws.id: diagnose_workspace(ws) for ws in page}
        owner_ids = {o["user_id"] for d in diags.values() for o in d.get("owners", [])}
        owners_map = _owner_users_map(owner_ids)

        without_owner = str(request.query_params.get("without_owner") or "").lower() in ("1", "true")
        with_anom = str(request.query_params.get("with_anomalies") or "").lower() in ("1", "true")

        items = []
        for ws in page:
            d = diags[ws.id]
            row = _workspace_list_row(ws, d, owners_map, getattr(ws, "ragspaces_total", 0))
            if without_owner and row["owners"]:
                continue
            if with_anom and not row["anomaly_codes"]:
                continue
            items.append(row)
        # NB: les filtres post-diagnostic réduisent la page ; `total` reste le total
        # non filtré (le frontend gère l'affichage). Filtres serveur exacts = évolution.
        return Response(page_envelope(items, total, limit, offset), status=200)

    def post(self, request):
        # Création d'un workspace + premier owner (Lot P2). Logique déléguée au
        # module `mutations` (services canoniques + audit + transaction).
        from .mutations import perform_create_workspace

        return perform_create_workspace(request)


class PlatformWorkspaceDetailView(APIView):
    """GET /api/v1/platform/workspaces/<id>/ — fiche complète (sans contenu)."""
    permission_classes = PLATFORM_PERMS

    def get(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)

        diag = diagnose_workspace(ws)
        mems = list(
            Membership.objects.filter(workspace=ws)
            .select_related("user", "owner_scope", "owner_scope__user")
            .order_by("-role", "id")
        )
        owners = [m for m in mems if m.role == Membership.ROLE_OWNER]

        memberships = [{
            "membership_id": m.pk,
            "user": user_public_dict(m.user),
            "role": m.role,
            "owner_scope_membership_id": m.owner_scope_id,
            "effective_owner_scope_pk": effective_owner_scope_pk(m),
            "created_at": m.created_at,
        } for m in mems]

        owner_scopes = [{
            "membership_id": m.pk,
            "user": user_public_dict(m.user),
            "scoped_memberships_count": sum(1 for x in mems if x.owner_scope_id == m.pk),
        } for m in owners]

        # RagSpaces : métadonnées MINIMALES (jamais documents/prompts/chunks/chat).
        spaces = list(RagSpace.objects.filter(workspace=ws).order_by("name"))
        doc_counts = {
            row["rag_space_id"]: row["n"]
            for row in Document.objects.filter(rag_space__workspace=ws)
            .values("rag_space_id").annotate(n=Count("id"))
        }
        ragspaces = [{
            "id": str(s.id),
            "name": s.name,
            "is_active": s.deleted_at is None,
            "deleted_at": s.deleted_at,
            "owner_scope_id": s.owner_scope_id,
            "created_by_id": getattr(s, "created_by_id", None),
            "documents_count": doc_counts.get(s.id, 0),
        } for s in spaces]

        from apps.workspaces.services import quota as quota_service
        quota_usage = quota_service.get_usage(ws)

        return Response({
            "id": ws.id,
            "name": ws.name,
            "is_active": ws.is_active,
            "created_at": ws.created_at,
            "owners": [{"membership_id": m.pk, "user": user_public_dict(m.user)} for m in owners],
            "memberships": memberships,
            "owner_scopes": owner_scopes,
            "ragspaces": ragspaces,
            "quota": quota_usage,
            "counts": {
                "members": diag.get("memberships_total", 0),
                "non_owner_members": diag.get("non_owner_memberships", 0),
                "ragspaces_total": len(spaces),
                "ragspaces_active": diag.get("ragspaces_active", 0),
                "documents": diag.get("documents", 0),
                "invitations_pending": diag.get("invitations_pending", 0),
                "active_ragspace_accesses": diag.get("active_ragspace_accesses", 0),
            },
            "diagnostic": {
                "anomalies": diag.get("anomalies", []),
                "has_integrity_violation": diag.get("has_integrity_violation", False),
                "migratable_automatically": diag.get("migratable_automatically", False),
                "suggested_action": diag.get("suggested_action", ""),
            },
        }, status=200)


class PlatformDiagnosticsView(APIView):
    """GET /api/v1/platform/owner-scope-diagnostics/ — rapport complet.

    Cohérent avec la commande `audit_workspace_owner_scopes` (même service). Optionnel
    `?workspace_id=`. Read-only. `summary.exit_code` reflète l'exit de la commande.
    """
    permission_classes = PLATFORM_PERMS

    def get(self, request):
        wid = request.query_params.get("workspace_id")
        report = diagnose_all(int(wid)) if wid else diagnose_all()
        flags = report_flags(report)
        return Response({
            "workspaces": report,
            "summary": {
                "total": len(report),
                "has_integrity": flags["has_integrity"],
                "has_decision": flags["has_decision"],
                "integrity_violations": sum(1 for e in report if e["has_integrity_violation"]),
                "without_owner": sum(1 for e in report
                                     if any(a["code"] == "workspace_without_owner" for a in e["anomalies"])),
                "multiple_owners": sum(1 for e in report
                                       if any(a["code"] == "multiple_owners" for a in e["anomalies"])),
                "migratable_automatically": sum(1 for e in report if e["migratable_automatically"]),
                # Exit code équivalent CLI : 1 si intégrité violée (strict inclut décision).
                "exit_code": 1 if flags["has_integrity"] else 0,
                "strict_exit_code": 1 if (flags["has_integrity"] or flags["has_decision"]) else 0,
            },
        }, status=200)


class PlatformWorkspaceQuotaDefaultsView(APIView):
    """GET /api/v1/platform/workspace-quota-defaults/ — défauts + plafonds.

    Sert à PRÉREMPLIR la modale de création (jamais de constantes dupliquées côté
    frontend) et l'écran d'administration des défauts. Superuser only.
    """
    permission_classes = PLATFORM_PERMS

    def get(self, request):
        from apps.workspaces.services import quota as quota_service
        return Response(quota_service.platform_defaults(), status=200)


class PlatformDashboardView(APIView):
    """GET /api/v1/platform/dashboard/ — compteurs de gouvernance (sans contenu)."""
    permission_classes = PLATFORM_PERMS

    def get(self, request):
        # Compteurs bon marché (annotations / count directs).
        workspaces_without_owner = (
            Workspace.objects.annotate(
                n_owners=Count("memberships", filter=Q(memberships__role=Membership.ROLE_OWNER))
            ).filter(is_active=True, n_owners=0).count()
        )
        ragspaces_without_scope = RagSpace.objects.filter(
            deleted_at__isnull=True, owner_scope_id__isnull=True
        ).count()
        # Anomalies d'intégrité : nécessite le diagnostic (itère les workspaces).
        report = diagnose_all()
        integrity_violations = sum(1 for e in report if e["has_integrity_violation"])
        return Response({
            "users_total": User.objects.count(),
            "users_active": User.objects.filter(is_active=True).count(),
            "users_superuser": User.objects.filter(is_superuser=True, is_active=True).count(),
            "workspaces_total": Workspace.objects.count(),
            "workspaces_active": Workspace.objects.filter(is_active=True).count(),
            "workspaces_disabled": Workspace.objects.filter(is_active=False).count(),
            "workspaces_without_owner": workspaces_without_owner,
            "ragspaces_without_owner_scope": ragspaces_without_scope,
            "integrity_violations": integrity_violations,
        }, status=200)

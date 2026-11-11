"""Transfert contrôlé d'un périmètre owner-scope — Lot P3 (PRÉVISUALISATION seule).

Décision produit : l'APPLICATION du transfert est un **NO-GO explicite** dans ce lot
(opération la plus sensible : déplacement atomique de memberships + RagSpaces +
invalidation de grants). Seule la **prévisualisation read-only** est livrée. L'endpoint
`apply/` refuse délibérément (501) tant que le sous-lot dédié n'est pas implémenté et
validé. Aucune écriture ici.
"""
from __future__ import annotations

import hashlib
import json

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rag_core.models import Document, RagSpace, RagSpaceAccess
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.models.reindex_job import JOB_ACTIVE_STATUSES, RagSpaceReindexJob
from apps.workspaces.models import Membership, Workspace
from apps.workspaces.services.access import effective_owner_scope_pk
from apps.workspaces.services.owner_scope_diagnostics import diagnose_workspace

from .audit import platform_audit
from .common import PLATFORM_PERMS, user_public_dict


def _load_owner(mid, ws) -> Membership | None:
    m = Membership.objects.filter(id=mid, workspace=ws, role=Membership.ROLE_OWNER).first()
    return m


def _preview(ws: Workspace, source: Membership, target: Membership) -> dict:
    # Memberships et RagSpaces actuellement rattachés à la SOURCE → iraient à la CIBLE.
    aff_members = list(
        Membership.objects.filter(workspace=ws, owner_scope_id=source.pk).select_related("user")
    )
    aff_spaces = list(
        RagSpace.objects.filter(workspace=ws, owner_scope_id=source.pk, deleted_at__isnull=True)
    )
    aff_space_ids = {s.id for s in aff_spaces}
    aff_member_user_ids = {m.user_id for m in aff_members}

    # Signaux « en cours » (bloquants) sur les espaces affectés.
    docs_in_progress = Document.objects.filter(
        rag_space_id__in=aff_space_ids,
        status__in=[DocumentStatus.PENDING, DocumentStatus.INGESTING],
    ).count()
    active_jobs = RagSpaceReindexJob.objects.filter(
        rag_space_id__in=aff_space_ids, status__in=list(JOB_ACTIVE_STATUSES)
    ).count()

    # RagSpaceAccess : validité APRÈS transfert (space→cible si affecté, user→cible si affecté).
    grants = list(
        RagSpaceAccess.objects.filter(rag_space__workspace=ws, revoked_at__isnull=True)
        .select_related("rag_space", "user")
    )
    member_scope = {}  # user_id -> effective scope pk (courant)
    for m in Membership.objects.filter(workspace=ws):
        member_scope[m.user_id] = effective_owner_scope_pk(m)

    valid, invalid = [], []
    for g in grants:
        space_scope_after = target.pk if g.rag_space_id in aff_space_ids else g.rag_space.owner_scope_id
        user_scope_after = target.pk if g.user_id in aff_member_user_ids else member_scope.get(g.user_id)
        row = {"grant_id": str(g.id), "user_id": g.user_id, "rag_space_id": str(g.rag_space_id),
               "rag_space_name": g.rag_space.name, "space_role": g.space_role}
        if space_scope_after is not None and space_scope_after == user_scope_after:
            valid.append(row)
        else:
            invalid.append(row)

    diag = diagnose_workspace(ws)
    integrity = [a for a in diag.get("anomalies", []) if a["code"] in {
        "scope_cross_workspace", "scope_target_not_owner", "access_scope_mismatch", "multiple_active_accesses"
    }]

    blockers = []
    if docs_in_progress:
        blockers.append("documents_in_progress")
    if active_jobs:
        blockers.append("active_reindex_jobs")
    if integrity:
        blockers.append("integrity_anomalies")
    allowed = not blockers

    state = {
        "workspace_id": ws.id,
        "source": source.pk, "target": target.pk,
        "members": sorted([(m.pk, m.owner_scope_id) for m in aff_members]),
        "spaces": sorted([(str(s.id), s.owner_scope_id) for s in aff_spaces]),
        "grants": sorted([(str(g.id), g.rag_space_id and str(g.rag_space_id), g.user_id) for g in grants]),
    }
    preview_hash = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()

    return {
        "workspace_id": ws.id,
        "source_owner": {"membership_id": source.pk, "user": user_public_dict(source.user)},
        "target_owner": {"membership_id": target.pk, "user": user_public_dict(target.user)},
        "affected_memberships": [
            {"membership_id": m.pk, "user": user_public_dict(m.user), "role": m.role} for m in aff_members
        ],
        "affected_ragspaces": [
            {"id": str(s.id), "name": s.name, "owner_scope_id": s.owner_scope_id} for s in aff_spaces
        ],
        "ragspace_accesses_valid": valid,
        "ragspace_accesses_invalid": invalid,
        "documents_in_progress": docs_in_progress,
        "active_reindex_jobs": active_jobs,
        "integrity_anomalies": integrity,
        "allowed": allowed,
        "blockers": blockers,
        "preview_hash": preview_hash,
        # Application volontairement indisponible dans ce lot.
        "apply_supported": False,
    }


class PlatformOwnerScopeTransferPreviewView(APIView):
    """POST /api/v1/platform/owner-scope-transfers/preview/ — read-only.

    Payload : {workspace_id, source_owner_membership_id, target_owner_membership_id}.
    N'écrit rien. Renvoie l'impact complet + `preview_hash` + `allowed`/`blockers`.
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request):
        wid = request.data.get("workspace_id")
        ws = Workspace.objects.filter(id=wid).first()
        if ws is None:
            return Response({"detail": "workspace_not_found"}, status=404)
        source = _load_owner(request.data.get("source_owner_membership_id"), ws)
        target = _load_owner(request.data.get("target_owner_membership_id"), ws)
        if source is None or target is None:
            return Response({"detail": "owner_membership_invalid"}, status=422)
        if source.pk == target.pk:
            return Response({"detail": "source_equals_target"}, status=422)
        return Response(_preview(ws, source, target), status=200)


class PlatformOwnerScopeTransferApplyView(APIView):
    """POST /api/v1/platform/owner-scope-transfers/apply/ — NO-GO explicite (501).

    L'application effective du transfert n'est PAS livrée dans ce lot (opération la
    plus sensible : déplacement atomique + invalidation de grants). Refuse toujours.
    """
    permission_classes = PLATFORM_PERMS

    def post(self, request):
        platform_audit(request, action="owner_scope_transfer_apply_blocked", status="denied",
                       workspace_id=request.data.get("workspace_id"),
                       reason="apply_not_implemented")
        return Response({
            "detail": "transfer_apply_not_implemented",
            "message": "L'application d'un transfert de périmètre n'est pas disponible. "
                       "Utilisez la prévisualisation, puis les commandes de gestion "
                       "(assign_membership_owner_scope / assign_ragspace_owner_scope) sous contrôle.",
        }, status=501)

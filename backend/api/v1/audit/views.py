from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

from apps.audit.aggregations import aggregate_costs
from apps.audit.models import RagQueryLog
from apps.workspaces.models import Membership


def _parse_int(value, default: int, *, min_v: int | None = None, max_v: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if min_v is not None:
        n = max(min_v, n)
    if max_v is not None:
        n = min(max_v, n)
    return n


def _is_owner_admin(*, user, workspace_id: int) -> bool:
    m = Membership.objects.filter(user=user, workspace_id=workspace_id).first()
    if not m:
        return False
    return (m.role or "").lower() in ("owner", "admin")


class RagQueryLogListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspace_id_raw = request.query_params.get("workspace_id")
        rag_space_id = request.query_params.get("rag_space_id") or None

        if not workspace_id_raw:
            return Response({"detail": "workspace_id required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            workspace_id = int(workspace_id_raw)
        except (TypeError, ValueError):
            return Response({"detail": "workspace_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        limit = _parse_int(request.query_params.get("limit"), 50, min_v=1, max_v=200)

        # owner/admin only
        if not _is_owner_admin(user=request.user, workspace_id=workspace_id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        qs = RagQueryLog.objects.filter(workspace_id=workspace_id).order_by("-created_at")
        if rag_space_id:
            qs = qs.filter(rag_space_id=rag_space_id)

        rows = []
        for x in qs[:limit]:
            rows.append(
                {
                    "id": str(x.id),
                    "created_at": x.created_at.isoformat() if getattr(x, "created_at", None) else None,
                    "user_id": getattr(x, "user_id", None),
                    "rag_space_id": str(getattr(x, "rag_space_id", None)) if getattr(x, "rag_space_id", None) else None,
                    "status": getattr(x, "status", None),
                    "latency_ms": getattr(x, "latency_ms", None),
                    # compat: older/newer schemas may have different fields
                    "policy": getattr(x, "policy_name", None) if hasattr(x, "policy_name") else getattr(x, "policy", None),
                    "error_stage": getattr(x, "error_stage", None),
                    "request_id": getattr(x, "request_id", None),
                    "question": getattr(x, "question", None),
                    "sources_count": len(getattr(x, "sources", None) or []),
                    "llm_provider": getattr(x, "llm_provider", None),
                    "llm_model": getattr(x, "llm_model", None),
                    "llm_total_tokens": getattr(x, "llm_total_tokens", None),
                    "llm_cost_estimated": getattr(x, "llm_cost_estimated", None),
                }
            )

        return Response(rows, status=status.HTTP_200_OK)


class RagQueryLogDetailView(APIView):
    """
    Detail endpoint for the Audit drawer/modal.

    GET /api/v1/audit/rag-queries/<uuid:log_id>/
    Access: owner/admin of the workspace that owns the log.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, log_id):
        log = RagQueryLog.objects.filter(id=log_id).first()
        if not log:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        ws_id = getattr(log, "workspace_id", None)
        if ws_id is None:
            # Should not happen in your schema; safe guard
            return Response({"detail": "Invalid audit log (missing workspace_id)"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if not _is_owner_admin(user=request.user, workspace_id=int(ws_id)):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        data = {
            "id": str(getattr(log, "id")),
            "created_at": getattr(log, "created_at", None).isoformat() if getattr(log, "created_at", None) else None,
            "status": getattr(log, "status", None),
            "error_stage": getattr(log, "error_stage", None),
            "latency_ms": getattr(log, "latency_ms", None),
            "question": getattr(log, "question", None),
            "answer": getattr(log, "answer", None),
            "workspace_id": int(ws_id),
            "rag_space_id": str(getattr(log, "rag_space_id", None)) if getattr(log, "rag_space_id", None) else None,
            "user_id": getattr(log, "user_id", None),
            "prompt_hash": getattr(log, "prompt_hash", None),
            "sources_schema_version": getattr(log, "sources_schema_version", None),
            "sources": getattr(log, "sources", None) or [],
            # LLM fields (present in your model now; safe if missing)
            "llm_provider": getattr(log, "llm_provider", None),
            "llm_model": getattr(log, "llm_model", None),
            "llm_prompt_tokens": getattr(log, "llm_prompt_tokens", None),
            "llm_completion_tokens": getattr(log, "llm_completion_tokens", None),
            "llm_total_tokens": getattr(log, "llm_total_tokens", None),
            "llm_cost_estimated": getattr(log, "llm_cost_estimated", None),
        }

        return Response(data, status=status.HTTP_200_OK)


class LlmCostSummaryView(APIView):
    """Agrégation coûts/tokens LLM (périmètre C+).

    GET /api/v1/audit/llm-costs/?workspace_id=&rag_space_id=&days=&group_by=&period=
    - ``group_by`` : liste séparée par virgules parmi provider,model,rag_space,policy
      (défaut: provider,model) ;
    - ``period``   : day | month (bucket temporel optionnel) ;
    - ``days``     : fenêtre glissante (défaut 30, max 366).
    Accès : owner/admin du workspace.
    """

    permission_classes = [IsAuthenticated]

    _GROUP_ALIASES = {
        "provider": "llm_provider",
        "model": "llm_model",
        "rag_space": "rag_space_id",
        "policy": "policy",
        "status": "status",
    }

    def get(self, request):
        workspace_id_raw = request.query_params.get("workspace_id")
        if not workspace_id_raw:
            return Response({"detail": "workspace_id required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            workspace_id = int(workspace_id_raw)
        except (TypeError, ValueError):
            return Response({"detail": "workspace_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        if not _is_owner_admin(user=request.user, workspace_id=workspace_id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        days = _parse_int(request.query_params.get("days"), 30, min_v=1, max_v=366)
        since = timezone.now() - timedelta(days=days)

        raw_group = (request.query_params.get("group_by") or "provider,model").split(",")
        group_by = [self._GROUP_ALIASES[g.strip()] for g in raw_group if g.strip() in self._GROUP_ALIASES]
        if not group_by:
            group_by = ["llm_provider", "llm_model"]

        period = request.query_params.get("period")
        if period not in ("day", "month"):
            period = None

        rows = aggregate_costs(
            workspace_id=workspace_id,
            rag_space_id=request.query_params.get("rag_space_id") or None,
            since=since,
            group_by=group_by,
            period=period,
        )
        totals = {
            "count": sum(r["count"] for r in rows),
            "total_tokens": sum(r["total_tokens"] for r in rows),
            "total_cost_usd": round(sum(r["total_cost_usd"] for r in rows), 6),
            "cost_unknown": sum(r["cost_unknown"] for r in rows),
        }
        return Response(
            {"since": since.isoformat(), "days": days, "group_by": group_by, "period": period,
             "totals": totals, "rows": rows},
            status=status.HTTP_200_OK,
        )

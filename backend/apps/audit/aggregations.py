"""Agrégations de coûts/tokens LLM sur RagQueryLog (périmètre C+).

Regroupe par provider, modèle, RagSpace et/ou période, sur une fenêtre de temps.
Aucun secret manipulé. Le coût ``NULL`` (indéterminé) est exclu de la somme mais
compté à part (``cost_unknown``) pour rester transparent : un total faible avec un
``cost_unknown`` élevé signale une couverture tarifaire incomplète, pas un coût nul.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncDay, TruncMonth

from apps.audit.models import RagQueryLog

# Champs de regroupement autorisés (whitelist anti-injection sur values()).
_ALLOWED_GROUP_FIELDS = {
    "llm_provider",
    "llm_model",
    "rag_space_id",
    "workspace_id",
    "policy",
    "status",
}


def aggregate_costs(
    *,
    workspace_id: int | None = None,
    rag_space_id=None,
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: Iterable[str] = ("llm_provider", "llm_model"),
    period: str | None = None,
) -> list[dict]:
    """Agrège coûts/tokens. ``period`` ∈ {None, 'day', 'month'} ajoute un bucket temporel."""
    fields = [f for f in group_by if f in _ALLOWED_GROUP_FIELDS]

    qs = RagQueryLog.objects.all()
    if workspace_id is not None:
        qs = qs.filter(workspace_id=workspace_id)
    if rag_space_id is not None:
        qs = qs.filter(rag_space_id=rag_space_id)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)

    annotations = {}
    if period == "day":
        annotations["period"] = TruncDay("created_at")
        fields = ["period", *fields]
    elif period == "month":
        annotations["period"] = TruncMonth("created_at")
        fields = ["period", *fields]

    qs = qs.annotate(**annotations) if annotations else qs

    rows = (
        qs.values(*fields)
        .annotate(
            count=Count("id"),
            total_tokens=Coalesce(Sum("llm_total_tokens"), 0),
            prompt_tokens=Coalesce(Sum("llm_prompt_tokens"), 0),
            completion_tokens=Coalesce(Sum("llm_completion_tokens"), 0),
            # SUM ignore les NULL : coût indéterminé exclu, mais compté à part.
            total_cost_usd=Sum("llm_cost_estimated"),
            cost_known=Count("id", filter=Q(llm_cost_estimated__isnull=False)),
            cost_unknown=Count("id", filter=Q(llm_cost_estimated__isnull=True)),
        )
        .order_by()
    )

    out: list[dict] = []
    for r in rows:
        cost = r.get("total_cost_usd")
        item = {k: r.get(k) for k in fields}
        if "period" in item and item["period"] is not None:
            # date/datetime -> ISO pour sérialisation JSON stable
            item["period"] = item["period"].isoformat()
        item.update(
            {
                "count": r["count"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "total_tokens": r["total_tokens"],
                "total_cost_usd": round(float(cost), 6) if cost is not None else 0.0,
                "cost_known": r["cost_known"],
                "cost_unknown": r["cost_unknown"],
            }
        )
        out.append(item)

    # tri stable décroissant par coût puis tokens
    out.sort(key=lambda d: (d["total_cost_usd"], d["total_tokens"]), reverse=True)
    return out

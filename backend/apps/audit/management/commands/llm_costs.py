"""Agrégation des coûts/tokens LLM (périmètre C+) — usage ops/CLI.

Agrège RagQueryLog par provider/modèle/RagSpace/période sur une fenêtre de temps.
Le coût indéterminé (``NULL``) est exclu de la somme et reporté via ``cost_unknown``.

    python manage.py llm_costs --days 30
    python manage.py llm_costs --days 7 --group provider,model,rag_space --period day
    python manage.py llm_costs --workspace 1 --format json
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.audit.aggregations import aggregate_costs

_ALIASES = {
    "provider": "llm_provider",
    "model": "llm_model",
    "rag_space": "rag_space_id",
    "policy": "policy",
    "status": "status",
}


class Command(BaseCommand):
    help = "Agrège les coûts/tokens LLM (RagQueryLog) par provider/modèle/espace/période."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Fenêtre glissante (défaut 30).")
        parser.add_argument("--workspace", type=int, default=None, help="Filtrer par workspace_id.")
        parser.add_argument("--rag-space", default=None, help="Filtrer par rag_space_id (UUID).")
        parser.add_argument(
            "--group", default="provider,model",
            help="Regroupement: provider,model,rag_space,policy,status (défaut provider,model).",
        )
        parser.add_argument("--period", choices=["day", "month"], default=None, help="Bucket temporel.")
        parser.add_argument("--format", choices=["table", "json"], default="table")

    def handle(self, *args, **opts):
        days = max(1, int(opts["days"]))
        since = timezone.now() - timedelta(days=days)
        group_by = [_ALIASES[g.strip()] for g in opts["group"].split(",") if g.strip() in _ALIASES]
        if not group_by:
            group_by = ["llm_provider", "llm_model"]

        rows = aggregate_costs(
            workspace_id=opts.get("workspace"),
            rag_space_id=opts.get("rag_space"),
            since=since,
            group_by=group_by,
            period=opts.get("period"),
        )
        total_cost = round(sum(r["total_cost_usd"] for r in rows), 6)
        total_tokens = sum(r["total_tokens"] for r in rows)
        total_unknown = sum(r["cost_unknown"] for r in rows)

        if opts["format"] == "json":
            self.stdout.write(json.dumps(
                {"since": since.isoformat(), "days": days, "rows": rows,
                 "totals": {"total_cost_usd": total_cost, "total_tokens": total_tokens,
                            "cost_unknown": total_unknown}},
                ensure_ascii=False, indent=2,
            ))
            return

        self.stdout.write(f"Coûts LLM — {days} derniers jours (depuis {since.date()})")
        label_fields = [f for f in group_by]
        for r in rows:
            label = " / ".join(str(r.get(f) or "—") for f in label_fields)
            if "period" in r:
                label = f"{r['period']} | {label}"
            self.stdout.write(
                f"  {label}: {r['count']} req · {r['total_tokens']} tok · "
                f"${r['total_cost_usd']:.4f}"
                + (f" (coût inconnu: {r['cost_unknown']})" if r["cost_unknown"] else "")
            )
        self.stdout.write(self.style.SUCCESS(
            f"TOTAL: ${total_cost:.4f} · {total_tokens} tokens"
            + (f" · {total_unknown} requête(s) à coût indéterminé" if total_unknown else "")
        ))

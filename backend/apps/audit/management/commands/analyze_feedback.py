# backend/apps/audit/management/commands/analyze_feedback.py
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.audit.models import RagQueryLog


class Command(BaseCommand):
    help = "Analyze RAG feedback and print actionable suggestions per RagSpace."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--min", type=int, default=10, help="min feedback samples per RagSpace")

    def handle(self, *args, **opts):
        days = opts["days"]
        min_n = opts["min"]

        since = timezone.now() - timedelta(days=days)
        qs = RagQueryLog.objects.filter(created_at__gte=since).exclude(feedback_score__isnull=True)

        by_space = defaultdict(list)
        for r in qs.iterator():
            by_space[str(r.rag_space_id)].append(r)

        if not by_space:
            self.stdout.write("No feedback in period.")
            return

        for space_id, rows in by_space.items():
            if len(rows) < min_n:
                continue

            pos = sum(1 for r in rows if r.feedback_score == 1)
            neg = sum(1 for r in rows if r.feedback_score == -1)
            reasons = Counter(r.feedback_reason or "other" for r in rows if r.feedback_score == -1)

            self.stdout.write(f"\nRagSpace {space_id}: 👍 {pos} | 👎 {neg} (n={len(rows)})")
            self.stdout.write(f"  reasons: {dict(reasons)}")

            # Heuristiques de tuning très simples (et efficaces)
            # (Tu ajusteras ensuite finement par RagSpace)
            if reasons.get("missing_sources", 0) / max(1, neg) >= 0.35:
                self.stdout.write("  suggestion: increase recall → raise VECTOR_TOP_K or lower MIN_SCORE slightly")
            if reasons.get("irrelevant", 0) / max(1, neg) >= 0.35:
                self.stdout.write("  suggestion: improve precision → enable/strengthen rerank, increase MIN_SCORE")
            if reasons.get("incomplete", 0) / max(1, neg) >= 0.25:
                self.stdout.write("  suggestion: chunking may be too small → increase chunk_size and overlap")
            if reasons.get("incorrect", 0) / max(1, neg) >= 0.25:
                self.stdout.write("  suggestion: generator issue → tighten prompt, ensure only cited info is used")

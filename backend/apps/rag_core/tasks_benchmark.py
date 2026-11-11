# backend/apps/rag_core/tasks_benchmark.py
"""Tâche Celery de benchmark nightly.

NOTE (Priorité B) : le benchmark qualité *complet* (recall@k, MRR, no-answer,
sources attendues) est porté par la commande versionnée ``manage.py rag_benchmark``
sur un dataset JSONL (voir backend/benchmarks/). Cette tâche planifiée se limite
à un *smoke test* agrégé sur les ``RagBenchmarkQuestion`` actives (qui n'ont pas de
vérité terrain documentaire) : distribution des status, sources moyennes, latence.
"""
from __future__ import annotations

from django.utils import timezone

from apps.rag_core.benchmark import metrics
from apps.rag_core.models.benchmark import RagBenchmarkQuestion
from apps.rag_core.services.query import query_rag
from config.celery import app


@app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def run_benchmark(self, rag_space_id: str | None = None, limit: int = 50):
    qs = RagBenchmarkQuestion.objects.filter(is_active=True)
    if rag_space_id:
        qs = qs.filter(rag_space_id=rag_space_id)
    qs = qs.order_by("-created_at")[:limit]

    results = []
    statuses: dict[str, int] = {}
    sources_counts: list[float] = []
    for q in qs.iterator():
        out = query_rag(rag_space_id=q.rag_space_id, question=q.question, user=None, entrypoint="benchmark")
        status = str(out.get("status") or "error")
        statuses[status] = statuses.get(status, 0) + 1
        n_sources = len(out.get("sources") or [])
        sources_counts.append(float(n_sources))
        results.append(
            {
                "rag_space_id": str(q.rag_space_id),
                "question_id": q.id,
                "status": status,
                "sources_count": n_sources,
                "ran_at": timezone.now().isoformat(),
            }
        )

    return {
        "count": len(results),
        "status_distribution": statuses,
        "avg_sources": metrics.mean(sources_counts),
        "results": results,
    }

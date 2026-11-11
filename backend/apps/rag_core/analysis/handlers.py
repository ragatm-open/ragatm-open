"""Handlers d'analyse globale.

NO-AI STRUCTUREL : ce module n'importe AUCUN module de génération, d'embeddings
ou de retrieval. L'absence n'est pas une promesse — c'est une propriété du graphe
d'imports, vérifiable et testée.
"""
from __future__ import annotations

import logging

from apps.rag_core.analysis.progress import (
    raise_if_cancel_requested,
    update_job_progress,
)
from apps.rag_core.models.corpus_analysis_job import (
    PHASE_COMPUTING_SCOPE,
    PHASE_COMPUTING_STATISTICS,
    PHASE_PERSISTING_RESULT,
)

logger = logging.getLogger(__name__)


def run_coverage_snapshot(*, job, rag_space) -> dict:
    """Instantané persistant des statistiques et de la couverture (L0/L1).

    Ce type n'est PAS une analyse sémantique : il sert à éprouver toute
    l'infrastructure asynchrone (queue, progression, annulation, idempotence,
    persistance, péremption) sans dépenser un seul token.

    Renvoie ``{"result": {...}, "coverage": {...}}``. Le résultat ne contient
    aucun texte d'extrait, aucun nom de fichier, aucun contenu documentaire.
    """
    from apps.rag_core.domain.coverage import coverage_to_dict
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    total = 3

    raise_if_cancel_requested(job.id)
    update_job_progress(job.id, phase=PHASE_COMPUTING_SCOPE, current=0, total=total)

    raise_if_cancel_requested(job.id)
    stats = compute_rag_space_statistics(rag_space.id)  # SQL agrégé, 3 requêtes
    update_job_progress(job.id, phase=PHASE_COMPUTING_STATISTICS, current=1, total=total)

    raise_if_cancel_requested(job.id)
    coverage = stats["coverage"]
    last = stats.get("last_created_at")

    result = {
        "result_version": "1",
        "statistics": {
            "documents": stats["documents"],
            "content": stats["content"],
            "ratios": stats["ratios"],
            "scope": {
                "generated_at": coverage.scope.generated_at.isoformat(),
                "corpus_fingerprint": coverage.scope.corpus_fingerprint,
                "last_document_created_at": last.isoformat() if last else None,
            },
        },
    }
    update_job_progress(job.id, phase=PHASE_PERSISTING_RESULT, current=2, total=total)

    return {
        "result": result,
        "coverage": coverage_to_dict(coverage),
        "corpus_fingerprint": coverage.scope.corpus_fingerprint,
        "progress_total": total,
    }

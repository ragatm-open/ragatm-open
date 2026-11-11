"""Pré-vol : volumétrie et limites, AVANT de dépenser quoi que ce soit.

Sans pré-vol, rien n'empêche une analyse de consommer le budget mensuel en une
requête. Le calcul est du SQL agrégé (L0/L1) : aucun LLM, aucun embedding.
"""
from __future__ import annotations

from django.conf import settings

# Clés de limites : définies dans le registre (source unique). Une définition de
# type déclare celles qui LUI sont applicables — un instantané de statistiques n'a
# aucune raison d'être refusé sur un plafond de tokens LLM qu'il ne consomme pas.
from apps.rag_core.analysis.registry import (  # noqa: E402
    LIMIT_MAX_CHUNKS,
    LIMIT_MAX_DOCUMENTS,
    LIMIT_MAX_ESTIMATED_TOKENS,
    AnalysisTypeDefinition,
)

_LIMIT_SETTINGS = {
    LIMIT_MAX_DOCUMENTS: "CORPUS_ANALYTICS_MAX_DOCUMENTS",
    LIMIT_MAX_CHUNKS: "CORPUS_ANALYTICS_MAX_CHUNKS",
    LIMIT_MAX_ESTIMATED_TOKENS: "CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS",
}


def compute_preflight(*, rag_space, definition: AnalysisTypeDefinition) -> dict:
    """Volumétrie du périmètre + verdict des limites applicables à CE type."""
    from apps.rag_core.services.coverage_service import compute_rag_space_statistics

    stats = compute_rag_space_statistics(rag_space.id)
    documents = stats["documents"]
    content = stats["content"]
    coverage = stats["coverage"]

    measured = {
        LIMIT_MAX_DOCUMENTS: int(documents["total"]),
        LIMIT_MAX_CHUNKS: int(content["chunks_total"]),
        LIMIT_MAX_ESTIMATED_TOKENS: int(content["estimated_tokens_total"]),
    }

    exceeded: list[dict] = []
    for limit_key in definition.enforced_limits:
        setting_name = _LIMIT_SETTINGS.get(limit_key)
        if not setting_name:
            continue
        ceiling = int(getattr(settings, setting_name))
        value = measured.get(limit_key, 0)
        if value > ceiling:
            exceeded.append({"limit": limit_key, "value": value, "max": ceiling})

    return {
        "documents_total": int(documents["total"]),
        "documents_ready": int(documents["ready"]),
        "documents_failed": int(documents["failed"]),
        "documents_without_chunks": int(documents["without_chunks"]),
        "chunks_total": int(content["chunks_total"]),
        "pages_numbered_total": int(content["pages_numbered_total"]),
        "characters_total": int(content["characters_total"]),
        "estimated_tokens_total": int(content["estimated_tokens_total"]),
        "corpus_fingerprint": coverage.scope.corpus_fingerprint,
        "within_limits": not exceeded,
        "limits_exceeded": exceeded,
    }

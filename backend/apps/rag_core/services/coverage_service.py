"""Statistiques génériques d'un RagSpace et construction de la couverture.

Aucun LLM, aucun embedding, aucun retrieval, aucune tâche asynchrone : tout est
calculé en SQL agrégé. Aucune logique métier — le calcul est identique quel que
soit le contenu documentaire (finance, technique, presse, juridique…).

Budget SQL : 3 requêtes (documents, chunks, empreinte). Verrouillé par
`assertNumQueries` dans les tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from django.db.models import Count, Exists, Max, OuterRef, Q, Sum, TextField, Value
from django.db.models.functions import Cast, Coalesce, Concat, Length

from apps.rag_core.domain.coverage import (
    REASON_EXTRACTION_FAILED,
    REASON_NO_TEXT_CONTENT,
    REASON_NOT_READY,
    REASON_PROCESSING,
    REASON_UNKNOWN,
    Coverage,
    CoverageExclusion,
    CoverageScope,
    compute_corpus_fingerprint,
    compute_coverage_ratio,
    estimate_tokens,
)
from apps.rag_core.models.chunk import Chunk
from apps.rag_core.models.document import Document, DocumentStatus

# Un document n'est « analysé » que si son contenu textuel a réellement pu être
# parcouru : statut READY *et* au moins un chunk. Un READY sans chunk est un
# document vide du point de vue de l'analyse — il est exclu, pas compté.
#
# Tout statut hors de cette table tombe dans `unknown` : un statut inconnu est
# signalé, jamais absorbé silencieusement dans le dénominateur.
_STATUS_EXCLUSION: dict[str, str] = {
    DocumentStatus.PENDING: REASON_NOT_READY,
    DocumentStatus.INGESTING: REASON_PROCESSING,
    DocumentStatus.FAILED: REASON_EXTRACTION_FAILED,
}

# Ordre d'affichage stable des exclusions (déterminisme du rendu et des tests).
_REASON_ORDER = (
    REASON_EXTRACTION_FAILED,
    REASON_NO_TEXT_CONTENT,
    REASON_NOT_READY,
    REASON_PROCESSING,
    REASON_UNKNOWN,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _document_breakdown(rag_space_id) -> tuple[list[dict], datetime | None]:
    """(statut × présence de chunks) → compte. Une seule requête.

    `Exists` évite la jointure qui gonflerait les comptes, et permet de
    distinguer un READY exploitable d'un READY vide sans requête par document.
    """
    rows = list(
        Document.objects.filter(rag_space_id=rag_space_id)
        .annotate(has_chunks=Exists(Chunk.objects.filter(document_id=OuterRef("pk"))))
        .values("status", "has_chunks")
        .annotate(count=Count("id"), last_created_at=Max("created_at"))
        .order_by()
    )
    last = max((r["last_created_at"] for r in rows if r["last_created_at"]), default=None)
    return rows, last


def _chunk_aggregates(rag_space_id) -> dict:
    """Volumétrie textuelle. Une seule requête.

    Les pages sont comptées comme des couples distincts ``(document, page)`` :
    il n'existe pas de modèle Page, et deux documents partageant le numéro 1 ont
    bien deux pages distinctes. Les chunks sans numéro de page (`page_number IS
    NULL`) ne sont PAS comptés comme une page — compter un document non paginé
    comme « une page » serait une statistique inventée. Le numéro 0 est un numéro
    de page valide et compte normalement.
    """
    page_key = Concat(
        Cast("document_id", TextField()),
        Value(":"),
        Cast("page_number", TextField()),
        output_field=TextField(),
    )
    numbered = Q(page_number__isnull=False)
    return Chunk.objects.filter(rag_space_id=rag_space_id).aggregate(
        chunks_total=Count("id"),
        characters_total=Coalesce(Sum(Length("text")), 0),
        pages_numbered_total=Count(page_key, distinct=True, filter=numbered),
        documents_with_chunks=Count("document_id", distinct=True),
        documents_with_numbered_pages=Count("document_id", distinct=True, filter=numbered),
    )


def _fingerprint(rag_space_id) -> str:
    """Empreinte du corpus. Une seule requête, colonnes minimales.

    `sha256` est l'empreinte de contenu réellement disponible sur le modèle
    Document. Le texte des chunks n'est jamais lu.
    """
    rows: Iterable = Document.objects.filter(rag_space_id=rag_space_id).values_list(
        "id", "sha256", "status"
    )
    return compute_corpus_fingerprint(rows)


def _build_exclusions(rows: list[dict]) -> tuple[dict[str, int], int]:
    """(statut × chunks) → exclusions agrégées + nombre de documents analysés."""
    excluded: dict[str, int] = {}
    analyzed = 0

    for row in rows:
        status = row["status"]
        count = int(row["count"])
        has_chunks = bool(row["has_chunks"])

        if status == DocumentStatus.READY:
            if has_chunks:
                analyzed += count
            else:
                excluded[REASON_NO_TEXT_CONTENT] = excluded.get(REASON_NO_TEXT_CONTENT, 0) + count
            continue

        reason = _STATUS_EXCLUSION.get(status, REASON_UNKNOWN)
        excluded[reason] = excluded.get(reason, 0) + count

    return excluded, analyzed


def _ordered_exclusions(excluded: dict[str, int]) -> tuple[CoverageExclusion, ...]:
    known = [
        CoverageExclusion(reason=r, count=excluded[r])
        for r in _REASON_ORDER
        if excluded.get(r, 0) > 0
    ]
    extra = [
        CoverageExclusion(reason=r, count=c)
        for r, c in sorted(excluded.items())
        if r not in _REASON_ORDER and c > 0
    ]
    return tuple(known + extra)


def build_corpus_coverage(
    *,
    rag_space_id,
    documents_total: int,
    documents_considered: int,
    documents_analyzed: int,
    excluded: dict[str, int],
    chunks_analyzed: int,
    pages_analyzed: int,
    corpus_fingerprint: str,
    documents_with_results: int = 0,
    items_extracted: int = 0,
    items_without_source: int = 0,
    filters_applied: tuple[str, ...] = (),
    limits_applied: tuple[str, ...] = (),
    generated_at: datetime | None = None,
) -> Coverage:
    """Assemble une Coverage à partir de faits déjà mesurés.

    Ce helper ne décide RIEN de sémantique : il n'évalue pas la pertinence, ne
    reconnaît aucune notion métier, n'invente aucun élément. Il ne fait que
    rassembler des comptes et laisser les invariants de `Coverage` les valider.

    Le niveau est toujours B (textuel) dès qu'au moins un contenu a été parcouru,
    A sinon. Les niveaux C et D ne sont jamais attribués ici : ils ne sont pas
    garantissables techniquement.
    """
    exclusions = _ordered_exclusions(excluded)
    complete = (
        documents_analyzed == documents_considered
        and not exclusions
        and not limits_applied
    )
    level = "B" if chunks_analyzed > 0 else "A"

    return Coverage(
        scope=CoverageScope(
            rag_space_id=str(rag_space_id),
            generated_at=generated_at or _now(),
            corpus_fingerprint=corpus_fingerprint,
            filters_applied=tuple(filters_applied),
        ),
        documents_total=documents_total,
        documents_considered=documents_considered,
        documents_analyzed=documents_analyzed,
        documents_with_results=documents_with_results,
        documents_excluded=exclusions,
        pages_analyzed=pages_analyzed,
        chunks_analyzed=chunks_analyzed,
        items_extracted=items_extracted,
        items_without_source=items_without_source,
        coverage_level=level,
        coverage_complete=complete,
        coverage_ratio=compute_coverage_ratio(
            documents_analyzed=documents_analyzed,
            documents_considered=documents_considered,
        ),
        limits_applied=tuple(limits_applied),
    )


def compute_rag_space_statistics(rag_space_id) -> dict:
    """Statistiques complètes d'un RagSpace + couverture associée.

    Le périmètre visible est supposé DÉJÀ résolu par l'appelant (contrôle d'accès
    au niveau de la vue). Ce service ne connaît qu'un `rag_space_id` : il ne peut
    donc pas élargir le périmètre autorisé, et aucun document hors permission ne
    peut entrer dans le dénominateur.
    """
    rows, last_created_at = _document_breakdown(rag_space_id)
    agg = _chunk_aggregates(rag_space_id)
    fingerprint = _fingerprint(rag_space_id)

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + int(row["count"])

    total = sum(by_status.values())
    ready = by_status.get(DocumentStatus.READY, 0)
    pending = by_status.get(DocumentStatus.PENDING, 0)
    ingesting = by_status.get(DocumentStatus.INGESTING, 0)
    failed = by_status.get(DocumentStatus.FAILED, 0)
    other = max(0, total - (ready + pending + ingesting + failed))

    excluded, analyzed = _build_exclusions(rows)

    chunks_total = int(agg["chunks_total"] or 0)
    characters_total = int(agg["characters_total"] or 0)
    pages_numbered_total = int(agg["pages_numbered_total"] or 0)
    documents_with_chunks = int(agg["documents_with_chunks"] or 0)
    documents_with_numbered_pages = int(agg["documents_with_numbered_pages"] or 0)
    documents_without_page_numbers = max(
        0, documents_with_chunks - documents_with_numbered_pages
    )

    coverage = build_corpus_coverage(
        rag_space_id=rag_space_id,
        documents_total=total,
        documents_considered=total,
        documents_analyzed=analyzed,
        excluded=excluded,
        chunks_analyzed=chunks_total,
        pages_analyzed=pages_numbered_total,
        corpus_fingerprint=fingerprint,
    )

    def _ratio(n: int) -> float:
        # Périmètre vide : 0.0 plutôt qu'une division par zéro ou un 1.0 trompeur.
        return round(n / total, 4) if total > 0 else 0.0

    return {
        "documents": {
            "total": total,
            "ready": ready,
            "pending": pending,
            "ingesting": ingesting,
            "failed": failed,
            "other": other,
            "with_chunks": documents_with_chunks,
            "without_chunks": max(0, total - documents_with_chunks),
        },
        "content": {
            "chunks_total": chunks_total,
            "pages_numbered_total": pages_numbered_total,
            "documents_without_page_numbers": documents_without_page_numbers,
            "characters_total": characters_total,
            "estimated_tokens_total": estimate_tokens(characters_total),
        },
        "ratios": {
            "ready": _ratio(ready),
            "failed": _ratio(failed),
        },
        "coverage": coverage,
        "last_created_at": last_created_at,
    }

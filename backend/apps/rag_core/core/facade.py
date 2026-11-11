"""Façade interne RagATM_Core (PR 2).

Surface Python STABLE au-dessus des services existants (``services/query`` et
``services/ingest``). Objectif : figer un contrat réutilisable que pourront
consommer, sans changement de comportement :
  - PR 3 : les endpoints HTTP ``/core/*`` (exposition only) ;
  - PR 4 : RagATM App (chat_service) derrière un flag.

Garanties / non-objectifs de cette PR :
  - délégation PURE : aucun changement de l'ingestion, du retrieval, de la
    génération, du modèle d'embeddings, ni des providers ;
  - ``retrieve`` ne génère JAMAIS la réponse finale (cf. RetrievalResult) ;
  - AUCUNE vérification de permission ici : le contrôle d'accès workspace/ragspace
    reste de la responsabilité de l'appelant (vue HTTP en PR 3 via
    ``require_rag_space_access``). La façade ne doit pas être exposée sans ce garde.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.llm_router.capabilities import provider_capabilities
from apps.rag_core.models import Document
from apps.rag_core.repositories.document_repository import count_chunks_for_document
from apps.rag_core.services.ingest import (
    IngestResult,
    ingest_document_from_text,
    ingest_document_from_upload,
)
from apps.rag_core.services.query.orchestrator import query_rag

from .dto import DocumentStatusResult, HealthResult, RetrievalResult

__all__ = [
    "retrieve",
    "ingest_text",
    "ingest_upload",
    "document_status",
    "health",
    "RetrievalResult",
    "DocumentStatusResult",
    "HealthResult",
    "IngestResult",
]


def retrieve(
    *,
    rag_space_id,
    question: str,
    user=None,
    rag_space=None,
    request_id: str | None = None,
    conversation_context: str | None = None,
    entrypoint: str = "core_api",
    answer_language: str | None = None,
    chunk_allowlist=None,
) -> RetrievalResult:
    """Récupère des passages pertinents — SANS générer de réponse.

    Délègue à ``query_rag`` (recherche FTS/vecteur + sélection), qui retourne les
    sources et le prompt assemblé mais n'appelle aucun LLM de génération.

    ``entrypoint`` (P1-A0) identifie l'appelant pour l'observabilité. Défaut
    ``core_api`` (l'usage direct de la façade) ; le chat le surcharge à
    ``chat_sync`` / ``chat_stream``.

    ``chunk_allowlist`` (CORPUS-SNAPSHOT-HARDENING, optionnel) borne le
    retrieval à un ensemble FIGÉ de fragments (``rag_core.domain.chunk_allowlist``).
    Il n'est transmis que s'il est fourni : un appel sans lui est l'appel d'avant.
    """
    bounded = {"chunk_allowlist": chunk_allowlist} if chunk_allowlist is not None else {}
    payload: dict[str, Any] = query_rag(
        rag_space_id=rag_space_id,
        question=question,
        user=user,
        rag_space=rag_space,
        request_id=request_id,
        conversation_context=conversation_context,
        entrypoint=entrypoint,
        answer_language=answer_language,
        **bounded,
    )
    return RetrievalResult(
        status=payload.get("status", ""),
        sources=payload.get("sources") or [],
        prompt=payload.get("prompt"),
        log_id=payload.get("log_id"),
        answer_kind=payload.get("answer_kind"),
        raw=payload,
    )


def ingest_text(
    *,
    rag_space_id,
    filename: str,
    text: str,
    content_type: str = "text/plain",
    request_id: str | None = None,
    async_mode: bool | None = None,
) -> IngestResult:
    """Ingestion de texte brut — délègue à ``ingest_document_from_text``."""
    return ingest_document_from_text(
        rag_space_id=rag_space_id,
        filename=filename,
        text=text,
        content_type=content_type,
        request_id=request_id,
        async_mode=async_mode,
    )


def ingest_upload(
    *,
    rag_space_id,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    request_id: str | None = None,
    async_mode: bool | None = None,
) -> IngestResult:
    """Ingestion d'un fichier uploadé — délègue à ``ingest_document_from_upload``."""
    return ingest_document_from_upload(
        rag_space_id=rag_space_id,
        filename=filename,
        content_type=content_type,
        file_bytes=file_bytes,
        request_id=request_id,
        async_mode=async_mode,
    )


def document_status(*, rag_space_id, document_id) -> DocumentStatusResult:
    """État d'un document, scopé par ``rag_space_id`` (isolation préservée)."""
    doc = Document.objects.filter(rag_space_id=rag_space_id, id=document_id).first()
    if doc is None:
        return DocumentStatusResult(document_id=str(document_id), found=False)
    return DocumentStatusResult(
        document_id=str(doc.id),
        found=True,
        status=doc.status,
        document_status=doc.document_status,
        chunks_count=count_chunks_for_document(document=doc),
        error_message=doc.error_message or "",
        filename=doc.filename or "",
    )


def health() -> HealthResult:
    """Santé du moteur (providers + dimension d'embedding), sans secret."""
    return HealthResult(
        ok=True,
        embedding_dim=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)),
        providers=provider_capabilities(),
    )

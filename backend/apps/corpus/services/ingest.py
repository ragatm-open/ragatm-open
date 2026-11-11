# backend/apps/corpus/services/ingest.py
from __future__ import annotations

"""Ingestion des documents de corpus métier (Phase 5).

PRINCIPE : on RÉUTILISE intégralement les garde-fous d'ingestion documentaire du
privé (couche ``apps.rag_core.services.ingest.extraction`` : validation MIME,
allowlist, refus exécutables/macros, anti zip-bomb, limites taille/PDF, OCR local)
ainsi que le chunking déterministe et le calcul d'embeddings. SEULE la cible
d'écriture change : ``DomainCorpusDocument`` / ``DomainCorpusChunk`` au lieu de
``Document`` / ``Chunk``. Aucune table privée n'est jamais lue ni écrite ici.

L'ingestion est synchrone (déterministe, testable). Le provider/modèle d'embeddings
proviennent du corpus (jamais d'un RagSpace) : les vecteurs d'un corpus partagé
sont stockés une seule fois et restent comparables entre eux.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.embeddings import embed_texts
from apps.rag_core.services.chunking import chunk_text_deterministic
from apps.rag_core.services.ingest.extraction import (
    ExtractionError,
    ExtractionResult,
    ExtractionStatus,
    extract_document,
)
from apps.rag_core.services.ingest.extraction.base import RejectedFile, UnsupportedFormat
from apps.rag_core.services.ingest.utils import settings_int, sha256_bytes

from apps.corpus.domain.embedding_identity import (
    EmbeddingIdentity,
    normalize_model,
    normalize_provider,
)
from apps.corpus.models import DomainCorpus, DomainCorpusChunk, DomainCorpusDocument
from apps.corpus.models.document import CorpusDocumentStatus

logger = logging.getLogger(__name__)


@dataclass
class CorpusIngestResult:
    document_id: str
    status: str
    chunks_created: int
    deduped: bool


def resolve_corpus_embedding(corpus: DomainCorpus) -> tuple[str | None, str | None]:
    """Provider/modèle d'embeddings d'un corpus (source de vérité de son indexation).

    Strictement analogue à ``resolve_space_embedding`` pour le privé. Le stub
    global d'embeddings (tests/dev) prime de toute façon dans ``embed_texts``.
    """
    from apps.corpus.services.retrieval import CorpusEmbeddingConfigurationError

    provider = (getattr(corpus, "embedding_provider", "") or "").strip().lower() or None
    model = (getattr(corpus, "embedding_model", "") or "").strip() or None
    # Refus SYMÉTRIQUE de celui du retrieval : un vecteur ne doit pas naître avec une
    # provenance ambiguë, sinon la question posée plus tard ne pourra pas être encodée
    # dans le même espace de façon vérifiable.
    if provider and not model:
        raise CorpusEmbeddingConfigurationError(getattr(corpus, "id", None), provider)
    return provider, model


def _find_existing_non_failed(*, corpus: DomainCorpus, sha256: str) -> DomainCorpusDocument | None:
    if not sha256:
        return None
    return (
        DomainCorpusDocument.objects.filter(corpus=corpus, sha256=sha256)
        .exclude(status=CorpusDocumentStatus.FAILED)
        .order_by("-created_at")
        .first()
    )


def index_corpus_extraction_result(
    *,
    document: DomainCorpusDocument,
    result: ExtractionResult,
    chunk_size: int,
    overlap: int,
    dim: int,
) -> CorpusIngestResult:
    """Chunk + embed + persiste les ``DomainCorpusChunk`` d'un résultat d'extraction.

    Document vide => jamais indexé : FAILED + error_message='empty_document'. La
    réindexation supprime d'abord les chunks EXISTANTS de CE document uniquement.
    """
    corpus = document.corpus
    filename = document.original_filename or document.display_title

    if result.status == ExtractionStatus.EMPTY or not (result.text or "").strip():
        DomainCorpusChunk.objects.filter(document_id=document.id).delete()
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message="empty_document"
        )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)

    try:
        with transaction.atomic():
            DomainCorpusDocument.objects.filter(id=document.id).update(
                status=CorpusDocumentStatus.INGESTING, error_message=""
            )
            DomainCorpusChunk.objects.filter(document_id=document.id).delete()

            if result.pages:
                specs = []
                global_offset = 0
                next_idx = 0
                for page_number, page_text in result.pages:
                    page_raw = page_text or ""
                    page_specs = chunk_text_deterministic(
                        page_raw,
                        source=filename,
                        chunk_size=chunk_size,
                        overlap=overlap,
                        page_number=page_number,
                        base_char_offset=global_offset,
                        start_chunk_index=next_idx,
                    )
                    specs.extend(page_specs)
                    if page_specs:
                        next_idx = page_specs[-1].chunk_index + 1
                    global_offset += len(page_raw) + 1
            else:
                specs = chunk_text_deterministic(
                    result.text,
                    source=filename,
                    chunk_size=chunk_size,
                    overlap=overlap,
                    page_number=None,
                    base_char_offset=0,
                    start_chunk_index=0,
                )

            if not specs:
                raise ExtractionError("no_valid_chunks")

            texts = [s.text for s in specs]
            provider, model = resolve_corpus_embedding(corpus)
            # Exception RECENSÉE (cf. LlmExecutionContext.for_domain_corpus) : le
            # corpus métier n'a pas de champ de consentement ; son propre
            # `embedding_provider` fait autorité. Le contexte rend cette sortie
            # EXPLICITE et auditable au lieu de la laisser passer sans garde.
            vectors = embed_texts(
                texts, dim=dim, provider=provider, model=model,
                context=LlmExecutionContext.for_domain_corpus(
                    corpus, operation="embedding.document"
                ),
            )
            # Identité RÉELLEMENT utilisée pour produire ces vecteurs. Construite APRÈS
            # l'appel, jamais avant : une identité écrite en amont décrirait une
            # intention, pas un fait.
            identity = EmbeddingIdentity(
                provider=normalize_provider(provider), model=normalize_model(model),
                dimension=int(dim),
            )

            chunks = [
                DomainCorpusChunk(
                    corpus=corpus,
                    document=document,
                    chunk_index=s.chunk_index,
                    text=s.text,
                    embedding=vec,
                    page_number=s.page_number,
                    meta={
                        "chunk_index": s.chunk_index,
                        "page_number": s.page_number,
                        "source": s.source,
                        "char_start": s.char_start,
                        "char_end": s.char_end,
                    },
                )
                for s, vec in zip(specs, vectors, strict=False)
            ]
            DomainCorpusChunk.objects.bulk_create(chunks, batch_size=200)
            # Empreinte et statut READY posés dans la MÊME transaction que les chunks :
            # un document déclaré cohérent l'est nécessairement. En cas d'échec, le
            # `except` supprime les chunks et pose FAILED — jamais de document
            # faussement cohérent, jamais de mélange silencieux d'anciens et de
            # nouveaux vecteurs.
            DomainCorpusDocument.objects.filter(id=document.id).update(
                status=CorpusDocumentStatus.READY, error_message="",
                embedding_provider=identity.provider,
                embedding_model=identity.model,
                embedding_dimension=identity.dimension,
                embedding_fingerprint=identity.fingerprint,
                reindex_required=False,
            )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.READY, len(chunks), False)

    except Exception as e:
        DomainCorpusChunk.objects.filter(document_id=document.id).delete()
        # L'empreinte est EFFACÉE avec les chunks : un document en échec ne doit pas
        # conserver la preuve d'une indexation qui n'existe plus.
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message=str(e)[:1000],
            embedding_provider="", embedding_model="", embedding_dimension=None,
            embedding_fingerprint="", reindex_required=False,
        )
        logger.warning("corpus index failed (doc=%s): %s", document.id, e.__class__.__name__)
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)


def _extract_and_index(*, document: DomainCorpusDocument, file_bytes: bytes, content_type: str) -> CorpusIngestResult:
    chunk_size = settings_int("RAG_CHUNK_SIZE_CHARS", 2500)
    overlap = settings_int("RAG_CHUNK_OVERLAP_CHARS", 250)
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))
    filename = document.original_filename or "document.bin"

    try:
        result = extract_document(
            file_bytes or b"",
            filename=filename,
            content_type=content_type or "",
            # 🔒 L'ingestion du corpus métier est 100 % SYNCHRONE (aucune tâche Celery,
            # cf. docs/follow-ups/corpus/02-async-celery-corpus-ingestion.md). Un gros
            # classeur ou une présentation lourde y bloquerait une requête HTTP plusieurs
            # secondes. Les deux formats y restent refusés tant que le corpus n'est pas
            # asynchrone — les deux sont forcés en async ailleurs (cf. async_policy).
            allow_excel=False,
            allow_pptx=False,
        )
    except (RejectedFile, UnsupportedFormat) as e:
        DomainCorpusChunk.objects.filter(document_id=document.id).delete()
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message=f"{e.code}: {e.detail}"[:1000]
        )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)
    except ExtractionError as e:
        DomainCorpusChunk.objects.filter(document_id=document.id).delete()
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message=f"{getattr(e, 'code', 'extraction_error')}"[:1000]
        )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)

    return index_corpus_extraction_result(
        document=document, result=result, chunk_size=chunk_size, overlap=overlap, dim=dim
    )


def ingest_corpus_document_from_upload(
    *,
    corpus: DomainCorpus,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    title: str = "",
    source_url: str = "",
    source_reference: str = "",
    version_label: str = "",
) -> CorpusIngestResult:
    """Upload + indexation d'un document dans un corpus métier (synchrone).

    Dédup par sha256 DANS le corpus : un doc identique non-FAILED renvoie le
    document existant (``deduped=True``) sans réindexer. Le même fichier reste
    autorisé dans un AUTRE corpus (choix produit assumé).
    """
    file_bytes = file_bytes or b""
    sha = sha256_bytes(file_bytes)

    existing = _find_existing_non_failed(corpus=corpus, sha256=sha)
    if existing is not None:
        return CorpusIngestResult(
            document_id=str(existing.id),
            status=existing.status,
            chunks_created=DomainCorpusChunk.objects.filter(document_id=existing.id).count(),
            deduped=True,
        )

    document = DomainCorpusDocument.objects.create(
        corpus=corpus,
        title=title or "",
        original_filename=filename or "document.bin",
        mime_type=content_type or "",
        size_bytes=len(file_bytes),
        sha256=sha,
        status=CorpusDocumentStatus.PENDING,
        source_url=source_url or "",
        source_reference=source_reference or "",
        version_label=version_label or "",
    )
    # Conserver l'original (réindexation ultérieure sans re-upload).
    try:
        document.storage_file.save(filename or "document.bin", ContentFile(file_bytes), save=True)
    except Exception as e:  # pragma: no cover - stockage best-effort
        logger.warning("corpus storage_file save failed (doc=%s): %s", document.id, e)

    return _extract_and_index(document=document, file_bytes=file_bytes, content_type=content_type)


def reindex_corpus_document(*, document: DomainCorpusDocument) -> CorpusIngestResult:
    """Réindexe UN document de corpus depuis son fichier source.

    N'affecte que les chunks de ce document. Ne touche jamais le privé.
    """
    sf = getattr(document, "storage_file", None)
    if not sf or not getattr(sf, "name", ""):
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message="no_storage_file"
        )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)

    try:
        with document.storage_file.open("rb") as fh:
            file_bytes = fh.read()
    except Exception as e:
        DomainCorpusDocument.objects.filter(id=document.id).update(
            status=CorpusDocumentStatus.FAILED, error_message=f"storage_read_failed: {e}"[:1000]
        )
        return CorpusIngestResult(str(document.id), CorpusDocumentStatus.FAILED, 0, False)

    return _extract_and_index(document=document, file_bytes=file_bytes, content_type=document.mime_type or "")


def reindex_corpus(*, corpus: DomainCorpus) -> dict:
    """Réindexe TOUS les documents réindexables d'un corpus.

    Ne touche que les chunks de CE corpus (``DomainCorpusChunk``). Aucune table
    privée (``rag_document`` / ``rag_chunk``) n'est lue ou modifiée.
    """
    reindexed = 0
    failed = 0
    skipped = 0
    qs = DomainCorpusDocument.objects.filter(corpus=corpus)
    for doc in qs.iterator():
        sf = getattr(doc, "storage_file", None)
        if not sf or not getattr(sf, "name", ""):
            skipped += 1
            continue
        res = reindex_corpus_document(document=doc)
        if res.status == CorpusDocumentStatus.READY:
            reindexed += 1
        else:
            failed += 1
    return {"reindexed": reindexed, "failed": failed, "skipped": skipped, "total": qs.count()}

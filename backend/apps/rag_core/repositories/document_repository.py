# backend/apps/rag_core/repositories/document_repository.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union
from uuid import UUID

from django.db import IntegrityError, connection, transaction

from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus


@dataclass(frozen=True)
class CreateOrGetResult:
    document: Document
    deduped: bool


#: Nom EXACT de l'index unique partiel (``rag_document(rag_space_id, sha256)
#: WHERE sha256 <> '' AND status <> 'FAILED'``). Seule une collision sur CETTE
#: contrainte est une collision de contenu ; toute autre ``IntegrityError`` reste
#: une erreur à propager.
DUPLICATE_CONTENT_CONSTRAINT = "uniq_rag_document_space_sha256_not_failed"


def find_existing_by_sha(*, space: RagSpace, sha256: str) -> Document | None:
    """Identité de contenu dans un RagSpace : ``same space + same sha256``.

    1. un document **actif** (non FAILED) portant ce contenu : c'est lui, le plus
       récent (il n'en existe qu'un : index unique partiel) ;
    2. à défaut, un document **FAILED** portant ce contenu : la **première**
       occurrence (``created_at`` puis ``id`` croissants — celle qui porte la
       provenance d'import). Elle est RÉUTILISÉE comme cible de reprise.

    Historiquement les FAILED étaient ignorés « pour permettre retry/re-upload » :
    chaque ré-import créait alors une nouvelle ligne de même contenu, et dès qu'une
    de ces lignes redevenait active, les autres ne pouvaient plus jamais quitter
    FAILED (collision sur la contrainte). Un FAILED n'est pas une impasse : c'est
    la ligne à relancer.
    """
    sha256 = (sha256 or "").strip()
    if not sha256:
        return None

    qs = Document.objects.filter(rag_space=space, sha256=sha256)
    active = qs.exclude(status=DocumentStatus.FAILED).order_by("-created_at").first()
    if active is not None:
        return active
    return qs.filter(status=DocumentStatus.FAILED).order_by("created_at", "id").first()


def find_active_duplicate(*, space_id, sha256: str, exclude_id=None) -> Document | None:
    """Document ACTIF (non FAILED) portant ce contenu dans l'espace, hors ``exclude_id``.

    C'est le « gagnant » d'une collision sur ``DUPLICATE_CONTENT_CONSTRAINT``.
    """
    sha256 = (sha256 or "").strip()
    if not sha256:
        return None
    qs = Document.objects.filter(rag_space_id=space_id, sha256=sha256).exclude(status=DocumentStatus.FAILED)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return qs.order_by("-created_at").first()


def is_duplicate_content_collision(exc: BaseException) -> bool:
    """Vrai si ``exc`` est une ``IntegrityError`` sur EXACTEMENT la contrainte de
    contenu. Décision sur le nom de contrainte rapporté par PostgreSQL
    (``diag.constraint_name``), à défaut sur sa présence dans le message. Toute
    autre violation d'intégrité rend ``False`` : elle ne doit jamais être
    requalifiée en doublon de contenu.
    """
    if not isinstance(exc, IntegrityError):
        return False
    cause = getattr(exc, "__cause__", None)
    diag = getattr(cause, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return name == DUPLICATE_CONTENT_CONSTRAINT
    return DUPLICATE_CONTENT_CONSTRAINT in str(exc)


def count_chunks_for_document(
    document: Optional[Document] = None,
    *,
    document_id: Optional[Union[UUID, str]] = None,
) -> int:
    """
    Backward/forward compatible:

    - count_chunks_for_document(document)                 # OK (positional doc)
    - count_chunks_for_document(document=document)        # OK
    - count_chunks_for_document(document_id=document.id)  # OK
    """
    did: Optional[Union[UUID, str]] = None
    try:
        did = document.id if document is not None else document_id
    except Exception:
        did = document_id

    if not did:
        return 0

    try:
        return Chunk.objects.filter(document_id=did).count()
    except Exception:
        return 0


def _norm_str(v: object, default: str = "") -> str:
    s = (v or default) if v is not None else default
    return str(s).strip()


def _norm_size(v: object) -> int:
    try:
        iv = int(v)  # type: ignore[arg-type]
        return max(0, iv)
    except Exception:
        return 0


def _norm_limit(v: object, default: int = 200, max_limit: int = 1000) -> int:
    try:
        iv = int(v)  # type: ignore[arg-type]
        iv = default if iv <= 0 else iv
        return min(iv, max_limit)
    except Exception:
        return default


def create_or_get_by_sha(
    *,
    space: RagSpace,
    filename: str,
    content_type: str,
    size_bytes: int | None,
    sha256: str,
    status: str = DocumentStatus.PENDING,
) -> CreateOrGetResult:
    """
    Create a new Document, or return an existing one if:
    - soft dedup finds it (same sha256 : document actif, sinon première occurrence FAILED)
    - hard dedup unique index triggers under concurrency (IntegrityError sur la
      contrainte de contenu EXACTEMENT ; les autres IntegrityError sont propagées)

    IMPORTANT:
    - This implementation is aligned with the prod Document schema:
      filename, content_type, size_bytes, sha256, status, storage_file, error_message, created_at.
    - It never writes legacy fields like name/mime_type (which would raise FieldError).

    Assumes UNIQUE partial index exists:
      (rag_space_id, sha256) WHERE sha256 != '' AND status != 'failed'
    """
    sha256 = _norm_str(sha256)
    filename = _norm_str(filename, default="upload")
    content_type = _norm_str(content_type, default="application/octet-stream")
    size_bytes_i = _norm_size(size_bytes)

    # fast path (soft dedup)
    existing = find_existing_by_sha(space=space, sha256=sha256)
    if existing is not None:
        try:
            existing.refresh_from_db()
        except Exception:
            pass
        return CreateOrGetResult(document=existing, deduped=True)

    try:
        with transaction.atomic():
            doc = Document.objects.create(
                rag_space=space,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes_i,
                sha256=sha256,
                status=status,
            )
            return CreateOrGetResult(document=doc, deduped=False)
    except IntegrityError as exc:
        # Course entre deux créateurs : l'index unique partiel a tranché. Seule une
        # collision sur CETTE contrainte est résolue en relisant le gagnant ; toute
        # autre violation d'intégrité est propagée telle quelle.
        if not is_duplicate_content_collision(exc):
            raise
        existing2 = find_existing_by_sha(space=space, sha256=sha256)
        if existing2 is not None:
            try:
                existing2.refresh_from_db()
            except Exception:
                pass
            return CreateOrGetResult(document=existing2, deduped=True)
        raise


# ---------------------------------------------------------------------
# Document "listing/search" helpers (for queries like: "liste des docs sur Nordexia")
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class DocumentHit:
    document_id: UUID
    filename: str
    hits: int
    pages: list[int]


def list_documents_mentioning_term(
    *,
    space_id: Union[UUID, str],
    term: str,
    limit: int = 200,
) -> list[DocumentHit]:
    """
    Return documents in a RagSpace that mention `term` (case-insensitive),
    with count of matching chunks and distinct page numbers.

    Uses ILIKE (works everywhere). For large corpora, prefer FTS (below)
    once you have the GIN index on to_tsvector('simple', text).
    """
    q = _norm_str(term)
    if not q:
        return []

    lim = _norm_limit(limit, default=200, max_limit=1000)

    sql = """
        SELECT
          d.id,
          d.filename,
          COUNT(*) AS hits,
          ARRAY_AGG(DISTINCT c.page_number ORDER BY c.page_number) AS pages
        FROM rag_document d
        JOIN rag_chunk c ON c.document_id = d.id
        JOIN rag_space s ON s.id = d.rag_space_id
        WHERE d.rag_space_id = %(space)s
          AND d.status = 'READY'                  -- 🔒 éligibilité documentaire
          AND s.deleted_at IS NULL                -- 🔒 espace non supprimé
          AND c.text ILIKE %(pat)s
        GROUP BY d.id, d.filename
        ORDER BY hits DESC, d.filename ASC
        LIMIT %(limit)s;
    """
    params: dict[str, Any] = {"space": str(space_id), "pat": f"%{q}%", "limit": lim}

    out: list[DocumentHit] = []
    with connection.cursor() as cur:
        cur.execute(sql, params)
        for doc_id, filename, hits, pages in cur.fetchall():
            try:
                pages_list = [int(p) for p in (pages or []) if p is not None]
            except Exception:
                pages_list = []
            out.append(
                DocumentHit(
                    document_id=doc_id,
                    filename=str(filename or ""),
                    hits=int(hits or 0),
                    pages=pages_list,
                )
            )
    return out


def list_documents_mentioning_term_fts(
    *,
    space_id: Union[UUID, str],
    term: str,
    limit: int = 200,
    language: str = "simple",
) -> list[DocumentHit]:
    """
    Same as list_documents_mentioning_term but uses Postgres full-text search.

    Requires index like:
      CREATE INDEX ... ON rag_chunk USING GIN (to_tsvector('simple', text));

    We use plainto_tsquery for safe parsing of user input.
    """
    q = _norm_str(term)
    if not q:
        return []

    lim = _norm_limit(limit, default=200, max_limit=1000)
    lang = _norm_str(language, default="simple") or "simple"

    sql = f"""
        SELECT
          d.id,
          d.filename,
          COUNT(*) AS hits,
          ARRAY_AGG(DISTINCT c.page_number ORDER BY c.page_number) AS pages
        FROM rag_document d
        JOIN rag_chunk c ON c.document_id = d.id
        JOIN rag_space s ON s.id = d.rag_space_id
        WHERE d.rag_space_id = %(space)s
          AND d.status = 'READY'                  -- 🔒 éligibilité documentaire
          AND s.deleted_at IS NULL                -- 🔒 espace non supprimé
          AND to_tsvector(%(lang)s, c.text) @@ plainto_tsquery(%(lang)s, %(q)s)
        GROUP BY d.id, d.filename
        ORDER BY hits DESC, d.filename ASC
        LIMIT %(limit)s;
    """
    params: dict[str, Any] = {
        "space": str(space_id),
        "lang": lang,
        "q": q,
        "limit": lim,
    }

    out: list[DocumentHit] = []
    with connection.cursor() as cur:
        cur.execute(sql, params)
        for doc_id, filename, hits, pages in cur.fetchall():
            try:
                pages_list = [int(p) for p in (pages or []) if p is not None]
            except Exception:
                pages_list = []
            out.append(
                DocumentHit(
                    document_id=doc_id,
                    filename=str(filename or ""),
                    hits=int(hits or 0),
                    pages=pages_list,
                )
            )
    return out


def list_document_page_snippets_for_term(
    *,
    space_id: Union[UUID, str],
    term: str,
    limit: int = 200,
    max_snippet_chars: int = 260,
) -> list[dict[str, Any]]:
    """
    Returns (filename, page_number, snippet) rows for quick UI preview / debugging.
    Uses ILIKE to match; keeps isolation by rag_space_id.
    """
    q = _norm_str(term)
    if not q:
        return []

    lim = _norm_limit(limit, default=200, max_limit=2000)
    snippet_len = _norm_limit(max_snippet_chars, default=260, max_limit=2000)

    sql = """
        SELECT
          d.filename,
          c.page_number,
          c.text
        FROM rag_document d
        JOIN rag_chunk c ON c.document_id = d.id
        JOIN rag_space s ON s.id = d.rag_space_id
        WHERE d.rag_space_id = %(space)s
          AND d.status = 'READY'                  -- 🔒 éligibilité documentaire
          AND s.deleted_at IS NULL                -- 🔒 espace non supprimé
          AND c.text ILIKE %(pat)s
        ORDER BY d.filename ASC, c.page_number ASC
        LIMIT %(limit)s;
    """
    params: dict[str, Any] = {"space": str(space_id), "pat": f"%{q}%", "limit": lim}

    rows: list[dict[str, Any]] = []
    with connection.cursor() as cur:
        cur.execute(sql, params)
        for filename, page_number, text in cur.fetchall():
            t = (text or "")
            rows.append(
                {
                    "filename": str(filename or ""),
                    "page_number": int(page_number) if page_number is not None else None,
                    "snippet": str(t[:snippet_len]),
                }
            )
    return rows

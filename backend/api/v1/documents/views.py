# backend/api/v1/documents/views.py
from __future__ import annotations

"""
Backward-compatible re-exports.

This module used to contain all document API views in a single file.
It now re-exports the same public symbols from smaller modules, so external
imports (e.g. api.v1.urls) keep working without changes.
"""
from .views_read import DocumentsSearchView 
# Shared helpers (kept importable for legacy internal imports/tests)
from .views_common import (  # noqa: F401
    logger,
    _boolish,
    _should_async,
    _enqueue_ingest,
    _safe_str,
    _guess_content_type,
    _serialize_document,
    _ensure_storage_file_or_fail,
    reindex_existing_document,
    ingest_document_task,
    _sha256_bytes,
    _sha256_text,
)

# Views
from .views_read import (  # noqa: F401
    DocumentListView,
    DocumentDetailView,
    DocumentDownloadView,
)

from .views_write import (  # noqa: F401
    DocumentUploadView,
    DocumentAudioUploadView,
    DocumentIngestView,
    DocumentReindexView,
    DocumentDeleteView,
    DocumentSummaryView,
)

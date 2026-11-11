# backend/apps/rag_core/services/ingest_document.py
from __future__ import annotations

"""
Backward-compatible shim.

The ingestion logic has been split into apps.rag_core.services.ingest.*
This module re-exports the original public API so existing imports do not break.
"""

from .ingest import (  # noqa: F401
    IngestResult,
    IngestError,
    extract_text_from_txt_bytes,
    extract_text_pages_from_pdf_bytes,
    ingest_document_from_text,
    ingest_document_from_upload,
    reindex_existing_document,
    sha256_bytes as _sha256_bytes,
    sha256_text as _sha256_text,
)

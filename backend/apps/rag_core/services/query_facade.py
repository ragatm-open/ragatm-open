# backend/apps/rag_core/services/query.py
from __future__ import annotations

# façade compat: ne casse aucun import existant
from apps.rag_core.services.query import query_rag, SOURCES_SCHEMA_VERSION, normalize_sources_for_audit

__all__ = ["query_rag", "SOURCES_SCHEMA_VERSION", "normalize_sources_for_audit"]

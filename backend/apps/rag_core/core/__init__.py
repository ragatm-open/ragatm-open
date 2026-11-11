"""RagATM_Core — façade interne stable (PR 2).

Surface publique : ``from apps.rag_core import core`` puis ``core.retrieve(...)``,
``core.ingest_text(...)``, ``core.ingest_upload(...)``, ``core.document_status(...)``,
``core.health()``.

Cette façade ne fait que déléguer aux services existants. Voir ``facade`` pour les
garanties (aucun changement de comportement, pas de génération, pas de contrôle
d'accès intégré).
"""
from .dto import DocumentStatusResult, HealthResult, RetrievalResult
from .facade import (
    IngestResult,
    document_status,
    health,
    ingest_text,
    ingest_upload,
    retrieve,
)

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

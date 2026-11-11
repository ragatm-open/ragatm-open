# backend/apps/rag_core/services/ingest/__init__.py
from .types import IngestResult, IngestError
from .utils import sha256_bytes, sha256_text
from .extractors import extract_text_from_txt_bytes, extract_text_pages_from_pdf_bytes
from .pipeline import ingest_document_from_text, ingest_document_from_upload
from .reindex import reindex_existing_document
from .extraction import extract_document, ExtractionResult, ExtractionStatus


def ingest_plain_text(*, rag_space_id, filename: str, text: str) -> str:
    """Rétro-compat (ancienne API service) : ingère du texte brut, renvoie le document_id.

    Défini ici (et non importé depuis ingest_facade) pour éviter un import circulaire
    ingest_facade -> ingest_document -> ingest(package).
    """
    res = ingest_document_from_text(
        rag_space_id=rag_space_id,
        filename=filename,
        text=text,
        content_type="text/plain",
    )
    return res.document_id


__all__ = [
    "IngestResult",
    "IngestError",
    "sha256_bytes",
    "sha256_text",
    "extract_text_from_txt_bytes",
    "extract_text_pages_from_pdf_bytes",
    "ingest_document_from_text",
    "ingest_document_from_upload",
    "reindex_existing_document",
    "ingest_plain_text",
    "extract_document",
    "ExtractionResult",
    "ExtractionStatus",
]

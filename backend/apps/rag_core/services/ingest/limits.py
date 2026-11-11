# backend/apps/rag_core/services/ingest/limits.py
from __future__ import annotations

"""
Limites applicatives d'upload / d'extraction.

Centralise la lecture des settings (surchargeables via .env) et les controles :
- taille maximale de fichier ;
- nombre maximal de pages PDF ;
- timeout d'extraction.

Les controles levent une exception explicite (`UploadLimitExceeded` /
`PdfExtractionTimeout`) avec un message lisible, pour que les vues renvoient un
code HTTP clair et que les pipelines async marquent le document FAILED avec une
raison comprehensible.
"""

import concurrent.futures
import logging
from io import BytesIO

from django.conf import settings

logger = logging.getLogger(__name__)


class UploadLimitExceeded(Exception):
    """Un upload viole une limite applicative (taille, pages, ...)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)

    def __reduce__(self):
        # `super().__init__(detail)` ne place qu'UN argument dans `args`, alors que
        # `__init__` en exige deux : le dépicklage échouerait avec un TypeError. Sans
        # cela, cette exception ne pourrait pas franchir une frontière de processus, et
        # un « PDF trop volumineux » reviendrait à l'appelant comme une erreur générique.
        return (self.__class__, (self.code, self.detail))


class PdfExtractionTimeout(Exception):
    """L'extraction de texte d'un PDF a depasse le timeout configure."""

    code = "pdf_extraction_timeout"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------
# Lecture des limites (defauts raisonnables si settings absents)
# ---------------------------------------------------------------------
def _registry_upload_default_mb() -> float:
    """Défaut DÉCLARÉ au registre — jamais un littéral local : un repli codé en dur à 25
    a divergé du registre (50) et a fait refuser par un worker périmé des fichiers que
    l'API venait d'accepter (staging, 21/09/2026)."""
    from config import registry

    return float(registry.require("UPLOAD_MAX_FILE_SIZE_MB").default)


def max_file_size_mb() -> float:
    return float(getattr(settings, "UPLOAD_MAX_FILE_SIZE_MB", None) or _registry_upload_default_mb())


def max_file_size_bytes() -> int:
    # Derive depuis UPLOAD_MAX_FILE_SIZE_MB (la variable documentee / surchargeable),
    # pour que toute surcharge soit prise en compte.
    return int(max_file_size_mb() * 1024 * 1024)


def max_pdf_pages() -> int:
    return int(getattr(settings, "UPLOAD_PDF_MAX_PAGES", 500))


def extraction_timeout_s() -> int:
    return int(getattr(settings, "UPLOAD_EXTRACTION_TIMEOUT_S", 30))


# ---------------------------------------------------------------------
# Controles
# ---------------------------------------------------------------------
def check_file_size(size_bytes: int | None) -> None:
    """Leve UploadLimitExceeded si la taille depasse la limite."""
    limit = max_file_size_bytes()
    if limit and size_bytes and int(size_bytes) > limit:
        raise UploadLimitExceeded(
            "file_too_large",
            f"Fichier trop volumineux : {int(size_bytes)} octets "
            f"(limite : {limit} octets ~ {max_file_size_mb():g} Mo). "
            "Ajustez UPLOAD_MAX_FILE_SIZE_MB si necessaire.",
        )


def count_pdf_pages(raw: bytes) -> int | None:
    """Nombre de pages d'un PDF, ou None si illisible (sans lever)."""
    raw = raw or b""
    if not raw:
        return 0
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return None
    try:
        reader = PdfReader(BytesIO(raw))
        return len(reader.pages)
    except Exception:
        return None


def check_pdf_pages(raw: bytes) -> None:
    """Leve UploadLimitExceeded si le PDF a trop de pages.

    Si le PDF n'est pas lisible (count None), on ne bloque pas ici : l'extraction
    en aval gerera l'echec (et le timeout protege contre les fichiers pieges).
    """
    limit = max_pdf_pages()
    if not limit:
        return
    n = count_pdf_pages(raw)
    if n is not None and n > limit:
        raise UploadLimitExceeded(
            "pdf_too_many_pages",
            f"PDF trop volumineux : {n} pages (limite : {limit}). "
            "Ajustez UPLOAD_PDF_MAX_PAGES si necessaire.",
        )


def run_with_timeout(fn, timeout_s: int | None = None):
    """Execute `fn()` avec un timeout (secondes).

    Leve PdfExtractionTimeout si depasse. Le thread de travail n'est pas tue
    (Python ne le permet pas) : on rend la main immediatement et le thread
    orphelin se termine en arriere-plan. C'est un timeout "souple" suffisant
    pour ne pas bloquer la requete / le worker.
    """
    if timeout_s is None:
        timeout_s = extraction_timeout_s()
    if not timeout_s or timeout_s <= 0:
        return fn()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        raise PdfExtractionTimeout(
            f"Extraction PDF interrompue : delai depasse (> {timeout_s}s). "
            "Ajustez UPLOAD_EXTRACTION_TIMEOUT_S si necessaire."
        )
    finally:
        # Ne pas attendre le thread orphelin (sinon le timeout serait inutile).
        executor.shutdown(wait=False)

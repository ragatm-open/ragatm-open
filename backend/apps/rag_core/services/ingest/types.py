# backend/apps/rag_core/services/ingest/types.py
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    status: str
    chunks_created: int
    deduped: bool = False
    #: Le contenu existait déjà dans l'espace sur une ligne FAILED : cette ligne a été
    #: RÉUTILISÉE comme cible de reprise (même ``document_id``, aucune ligne créée).
    retried: bool = False


class IngestError(Exception):
    pass


#: Code métier d'une collision de contenu : un document NON-FAILED portant le même
#: ``sha256`` existe déjà dans le RagSpace (contrainte
#: ``uniq_rag_document_space_sha256_not_failed``). Définitif pour cette ligne : la
#: rejouer reproduirait la collision tant que l'autre document reste actif.
DUPLICATE_CONTENT = "duplicate_content"


class DuplicateContentError(IngestError):
    """La transition FAILED → INGESTING a heurté la contrainte d'unicité de contenu.

    ``duplicate_of`` : identifiant du document ACTIF qui porte déjà ce contenu dans
    l'espace. Jamais transitoire : aucun marqueur de ``_RETRYABLE_MARKERS`` ne s'y
    applique, et ``retry_strategy`` le classe non retryable.
    """

    def __init__(self, duplicate_of: str, *, document_id: str = "") -> None:
        self.duplicate_of = str(duplicate_of)
        self.document_id = str(document_id)
        super().__init__(f"{DUPLICATE_CONTENT}: contenu identique déjà actif dans l'espace (document {self.duplicate_of})")


class RetryableIngestError(IngestError):
    """Erreur d'ingestion **transitoire** (provider embeddings indisponible, 429,
    5xx, timeout, connexion reset/refused, network unreachable…).

    Sous-classe d'``IngestError`` mais traitée à part par le worker Celery :
    elle déclenche ``self.retry(...)`` au lieu d'un ``FAILED`` définitif. En mode
    synchrone (sans tâche Celery), la couche d'indexation la dégrade proprement
    en ``Document(FAILED)`` (cf. ``indexing.index_extraction_result`` + ``allow_retry``).
    """


# Marqueurs (sur le message d'exception) identifiant une erreur transitoire.
# Source UNIQUE : réutilisée par ``tasks`` (décision de retry) ET ``indexing``
# (décision de relancer/dégrader). Ne pas dupliquer ailleurs.
_IDENTIFIER_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,}")

_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "temporar",
    "temporary",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "network unreachable",
    "name resolution",
    "dns",
    "502",
    "503",
    "504",
    "429",
    "rate limit",
    "too many requests",
    "insufficient_quota",
    "service unavailable",
    "provider unavailable",
    "bad gateway",
    "gateway timeout",
)


def is_retryable_error(exc: Exception) -> bool:
    """Heuristique unique : retry sur erreurs transitoires (réseau, timeout, 429,
    5xx, provider embeddings indisponible…). On se base sur le message car les
    exceptions varient selon le provider HTTP.

    NB : une ``RetryableIngestError`` est toujours retryable par construction.
    """
    if isinstance(exc, RetryableIngestError):
        return True
    if isinstance(exc, DuplicateContentError):
        return False  # typée et déterministe : un doublon ne disparaît pas en réessayant
    # Les identifiants (UUID, empreintes hexadécimales) ne sont jamais des codes HTTP : sans ce retrait, un
    # UUID contenant « 429 » ou « 503 » rendait une erreur définitive « transitoire » au hasard.
    msg = _IDENTIFIER_RE.sub(" ", (str(exc) or "").lower())
    return any(m in msg for m in _RETRYABLE_MARKERS)

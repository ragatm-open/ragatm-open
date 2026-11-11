# backend/apps/corpus/services/document_counts.py
from __future__ import annotations

"""Comptage des documents d'un corpus métier — définition UNIQUE.

Le problème que ce module résout
--------------------------------
Les écrans « Business corpora » et « Corpus admin » affichaient un unique
``documents_count``, obtenu par ``Count("documents")`` **sans filtre de statut**. Un
corpus dont les douze documents ont échoué à l'ingestion affichait donc « 12 documents »
— exactement comme un corpus pleinement indexé. Ce compteur a servi de preuve
d'exploitabilité lors d'un diagnostic ; il n'en est pas une.

La règle
--------
``ready`` utilise EXACTEMENT la définition d'éligibilité du retrieval
(``ELIGIBLE_DOCUMENT_STATUS``). Un compteur d'exploitabilité qui divergerait du filtre
réellement appliqué en SQL serait pire qu'absent : il mentirait avec autorité.

Par construction de l'ingestion (``services/ingest.index_corpus_extraction_result``), un
document sans aucun chunk est marqué ``FAILED`` — jamais ``READY``. Le statut suffit donc
à qualifier l'exploitabilité, et aucune jointure sur les chunks n'est nécessaire.

``documents_count`` est CONSERVÉ : les clients existants continuent de le lire.
"""

from django.db.models import Count, Q

from apps.rag_core.domain.eligibility import ELIGIBLE_DOCUMENT_STATUS

from apps.corpus.models.document import CorpusDocumentStatus

#: Statuts « en cours » : le document existe, il n'est pas encore interrogeable, et
#: ce n'est pas une erreur — un rafraîchissement suffira.
PROCESSING_STATUSES: tuple[str, ...] = (
    CorpusDocumentStatus.PENDING,
    CorpusDocumentStatus.INGESTING,
)

#: Clés exposées par l'API. Ordre = ordre de lecture naturel (total, puis détail).
COUNT_KEYS: tuple[str, ...] = (
    "documents_count",
    "ready_documents_count",
    "processing_documents_count",
    "failed_documents_count",
)


def document_count_annotations(prefix: str) -> dict[str, Count]:
    """Annotations agrégées pour un queryset atteignant les documents via ``prefix``.

    ``prefix`` est le chemin ORM vers ``DomainCorpusDocument`` (``"documents"`` depuis
    ``DomainCorpus``, ``"corpus__documents"`` depuis ``WorkspaceDomainCorpusAccess``).
    Les quatre compteurs sont produits par UNE seule requête, via des agrégats filtrés :
    aucune requête par corpus, aucun N+1.
    """
    status_field = f"{prefix}__status"
    return {
        "documents_count": Count(prefix, distinct=True),
        "ready_documents_count": Count(
            prefix, filter=Q(**{status_field: ELIGIBLE_DOCUMENT_STATUS}), distinct=True
        ),
        "processing_documents_count": Count(
            prefix, filter=Q(**{f"{status_field}__in": list(PROCESSING_STATUSES)}), distinct=True
        ),
        "failed_documents_count": Count(
            prefix, filter=Q(**{status_field: CorpusDocumentStatus.FAILED}), distinct=True
        ),
    }


def counts_from_annotated(obj) -> dict[str, int]:
    """Extrait les compteurs d'un objet annoté par :func:`document_count_annotations`."""
    return {key: int(getattr(obj, key, 0) or 0) for key in COUNT_KEYS}


def corpus_document_counts(corpus_id) -> dict[str, int]:
    """Compteurs d'UN corpus (vue détail), en une seule agrégation."""
    from apps.corpus.models import DomainCorpusDocument

    row = DomainCorpusDocument.objects.filter(corpus_id=corpus_id).aggregate(
        documents_count=Count("id"),
        ready_documents_count=Count("id", filter=Q(status=ELIGIBLE_DOCUMENT_STATUS)),
        processing_documents_count=Count("id", filter=Q(status__in=list(PROCESSING_STATUSES))),
        failed_documents_count=Count("id", filter=Q(status=CorpusDocumentStatus.FAILED)),
    )
    return {key: int(row.get(key) or 0) for key in COUNT_KEYS}

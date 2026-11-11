# backend/apps/rag_core/domain/provenance.py
from __future__ import annotations

"""Provenance d'une source de réponse — énumération CANONIQUE.

Le problème que ce module résout
--------------------------------
Trois modules définissaient chacun leur paire de constantes
(``services/query/audit.py``, ``services/query/candidate_retriever.py``,
``chat/services/chat_sources.py``), et **tous** appliquaient le même repli :
provenance absente ⇒ ``workspace_document``. Une source dont l'origine était perdue
en amont était donc affichée « Workspace » avec la même assurance qu'un vrai document
privé. Ce n'est pas un défaut d'affichage : c'est une affirmation fausse sur l'origine
d'une information citée, et elle était indétectable.

La règle
--------
La provenance est AFFIRMÉE par le composant qui la connaît — le repository privé pour
``rag_chunk``, le service corpus pour ``domain_corpus_chunk``, le leg temporel pour ses
propres candidats. Tout ce qui arrive sans provenance reconnue est ``unknown`` : un
état nommé, affichable et cherchable, jamais un privé par défaut.

``unknown`` n'est pas un mode dégradé silencieux : c'est le contraire. Il rend visible
exactement ce que le repli masquait.
"""

from enum import Enum


class SourceProvenance(str, Enum):
    """Origine documentaire d'une source citée."""

    WORKSPACE_DOCUMENT = "workspace_document"
    DOMAIN_CORPUS = "domain_corpus"
    UNKNOWN = "unknown"


SOURCE_TYPE_WORKSPACE = SourceProvenance.WORKSPACE_DOCUMENT.value
SOURCE_TYPE_DOMAIN_CORPUS = SourceProvenance.DOMAIN_CORPUS.value
SOURCE_TYPE_UNKNOWN = SourceProvenance.UNKNOWN.value

#: Valeurs ACCEPTÉES dans le contrat de sources. Toute autre valeur — absente, vide,
#: inconnue d'une version future du client — est normalisée en ``unknown``.
KNOWN_SOURCE_TYPES: frozenset[str] = frozenset(
    {SOURCE_TYPE_WORKSPACE, SOURCE_TYPE_DOMAIN_CORPUS}
)


def resolve_source_type(candidate: dict | None, source: dict | None = None) -> str:
    """Provenance EFFECTIVE d'un candidat : racine, puis ``source``, sinon ``unknown``.

    Point d'application unique du contrat. Ne devine jamais : une provenance manquante
    reste manquante.
    """
    root = candidate.get("source_type") if isinstance(candidate, dict) else None
    if root in KNOWN_SOURCE_TYPES:
        return root
    nested = source if isinstance(source, dict) else (
        candidate.get("source") if isinstance(candidate, dict) else None
    )
    if isinstance(nested, dict):
        value = nested.get("source_type")
        if value in KNOWN_SOURCE_TYPES:
            return value
    return SOURCE_TYPE_UNKNOWN

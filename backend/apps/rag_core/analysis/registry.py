"""Registre FERMÉ des types d'analyse globale.

Fermé côté serveur : le client ne fournit qu'une **clé**, jamais un chemin
d'import. Aucun callable n'est résolu dynamiquement depuis une entrée utilisateur,
et le callable n'est jamais exposé par l'API.

Un type non enregistré est rejeté de façon déterministe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Clés — n'enregistrer QUE ce qui existe réellement. Annoncer un type non
# implémenté reproduirait exactement le problème de confiance que ces lots
# cherchent à supprimer.
COVERAGE_SNAPSHOT = "corpus.coverage_snapshot"
GLOBAL_SEARCH = "corpus.global_search"


class UnknownAnalysisType(ValueError):
    """Clé absente du registre."""


@dataclass(frozen=True)
class AnalysisTypeDefinition:
    key: str
    handler: Callable
    requires_llm: bool
    requires_embeddings: bool
    supports_cancel: bool
    result_version: str
    default_progress_total: int = 1
    #: Limites de pré-vol RÉELLEMENT applicables à ce type. Une analyse sans IA
    #: n'a aucune raison d'être refusée sur un plafond de tokens LLM : appliquer
    #: les mêmes plafonds à tous les types serait un refus arbitraire.
    enforced_limits: tuple[str, ...] = field(default=())
    #: Ce type occupe-t-il une place dans le quota de concurrence ? Un instantané
    #: SQL instantané n'a pas à consommer le quota des analyses coûteuses.
    counts_against_concurrency: bool = False


# Clés de limites de pré-vol (dupliquées ici pour éviter un cycle d'import avec
# `preflight`, qui importe ce module).
LIMIT_MAX_DOCUMENTS = "max_documents"
LIMIT_MAX_CHUNKS = "max_chunks"
LIMIT_MAX_ESTIMATED_TOKENS = "max_estimated_tokens"


_REGISTRY: dict[str, AnalysisTypeDefinition] = {}


def register_analysis_type(defn: AnalysisTypeDefinition) -> None:
    if defn.key in _REGISTRY:
        raise ValueError(f"type d'analyse déjà enregistré : {defn.key}")
    _REGISTRY[defn.key] = defn


def get_analysis_type(key: str) -> AnalysisTypeDefinition:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownAnalysisType(key) from None


def is_known_analysis_type(key: str) -> bool:
    return key in _REGISTRY


def list_analysis_types() -> tuple[dict, ...]:
    """Vue sérialisable — **sans le callable**."""
    return tuple(
        {
            "key": d.key,
            "requires_llm": d.requires_llm,
            "requires_embeddings": d.requires_embeddings,
            "supports_cancel": d.supports_cancel,
            "result_version": d.result_version,
        }
        for d in sorted(_REGISTRY.values(), key=lambda x: x.key)
    )


def _register_builtin_types() -> None:
    """Import différé : évite un cycle registry <-> handlers."""
    from apps.rag_core.analysis.global_search import run_global_search
    from apps.rag_core.analysis.handlers import run_coverage_snapshot

    register_analysis_type(
        AnalysisTypeDefinition(
            key=COVERAGE_SNAPSHOT,
            handler=run_coverage_snapshot,
            # Instantané de statistiques : SQL agrégé seulement.
            requires_llm=False,
            requires_embeddings=False,
            supports_cancel=True,
            result_version="1",
            default_progress_total=1,
            # Aucun plafond IA ne s'applique : ce type ne consomme aucun token.
            enforced_limits=(),
            # Instantané instantané : aucune raison de le soumettre au quota des
            # analyses coûteuses.
            counts_against_concurrency=False,
        )
    )

    register_analysis_type(
        AnalysisTypeDefinition(
            key=GLOBAL_SEARCH,
            handler=run_global_search,
            # Traite les candidats par lots via le LLM…
            requires_llm=True,
            # …mais AUCUN embedding : le préfiltre est purement lexical (FTS).
            requires_embeddings=False,
            supports_cancel=True,
            result_version="1",
            default_progress_total=100,
            # Plafonds de volumétrie du corpus : une analyse LLM sur un corpus
            # démesuré est refusée AVANT de dépenser quoi que ce soit.
            enforced_limits=(LIMIT_MAX_DOCUMENTS, LIMIT_MAX_CHUNKS),
            counts_against_concurrency=True,
        )
    )


if not _REGISTRY:  # idempotent à l'import
    _register_builtin_types()

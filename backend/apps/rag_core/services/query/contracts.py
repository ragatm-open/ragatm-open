"""Contrats internes (DTO) de l'orchestrateur de retrieval (Lot P1-B1).

But : formaliser les frontières de ``query_rag`` (entrée, requête préparée, politique,
candidats, résultat) par des types EXPLICITES, en préparation du découpage P1-B2 —
**sans** déplacer d'étape ni modifier le comportement observable.

Principes stricts :
  - ces DTO sont **INTERNES** : jamais sérialisés dans une réponse API, jamais un
    modèle DB / serializer DRF / schéma frontend ;
  - **source de vérité unique** : le dict historique reste le contrat public
    (``QueryResult`` n'en est qu'une VUE typée immuable — cf. ``to_legacy_dict``) ;
  - **pas de conversion en boucle chaude** : ``CandidateSet`` / ``RankedCandidateSet``
    référencent les listes de dicts existantes (aucune copie de texte) ;
    ``RetrievalCandidate`` n'est appliqué qu'à l'unité (tests / futur P1-B2) ;
  - les enums P1-A0 (``RetrievalEntrypoint``/``RetrievalLegStatus``/``RetrievalOutcome``)
    et les erreurs typées P1-A1 sont RÉUTILISÉES, jamais dupliquées ;
  - ``repr`` ne divulgue aucun contenu sensible (question/texte) — seulement des tailles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from .errors import RetrievalConfigurationError, RetrievalInvariantError
from .trace import (
    LEG_BUSINESS_CORPUS,
    LEG_FTS,
    LEG_RERANK,
    LEG_TEMPORAL,
    LEG_VECTOR,
    RetrievalEntrypoint,
    RetrievalTraceBuilder,
    coerce_entrypoint,
)

# Version de contrat des DTO internes (indépendante du schéma de trace P1-A0).
QUERY_CONTRACTS_VERSION = 1


class QueryMode(str, Enum):
    """Mode PRIMAIRE dérivé de la question (valeurs stables, snake_case).

    NB : dans ``query_rag`` les modes sont des booléens INDÉPENDANTS
    (inventory/explanatory/comparative/news). ``QueryMode`` en donne le mode primaire par
    PRÉCÉDENCE documentée (news > comparative > explanatory > inventory > standard) pour
    l'observabilité ; ``PreparedQuery`` conserve les drapeaux bruts.

    ``COMPARATIVE`` passe AVANT ``EXPLANATORY`` : « compare X et Y en détail » est d'abord une
    comparaison — le gabarit de réponse comparatif est le plus contraignant des deux, et le
    préambule explicatif ne saurait pas exiger la distinction ressemblances / différences.
    """

    STANDARD = "standard"
    INVENTORY = "inventory"
    EXPLANATORY = "explanatory"
    COMPARATIVE = "comparative"
    NEWS = "news"

    @classmethod
    def from_flags(
        cls, *, inventory: bool, explanatory: bool, news: bool, comparative: bool = False
    ) -> "QueryMode":
        if news:
            return cls.NEWS
        if comparative:
            return cls.COMPARATIVE
        if explanatory:
            return cls.EXPLANATORY
        if inventory:
            return cls.INVENTORY
        return cls.STANDARD


class CandidateSourceLeg(str, Enum):
    """Provenance d'un candidat — miroir des noms de legs de la trace P1-A0."""

    FTS = LEG_FTS
    VECTOR = LEG_VECTOR
    BUSINESS_CORPUS = LEG_BUSINESS_CORPUS
    TEMPORAL = LEG_TEMPORAL
    RERANK = LEG_RERANK


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


# ===========================================================================
# Entrée
# ===========================================================================
@dataclass(frozen=True)
class QueryRequest:
    """Contrat d'ENTRÉE de ``query_rag`` (immuable).

    Propriétaire : orchestrateur (frontière d'entrée). Ne lit AUCUN setting, ne porte
    aucune logique métier. ``rag_space_id=None`` est VALIDE (mène à ``no_answer``, pas à
    une erreur) : la validation ne change donc pas le comportement historique.
    """

    question: str
    rag_space_id: Any | None
    user: Any | None
    rag_space: Any | None
    request_id: str | None
    conversation_context: str | None
    entrypoint: str
    #: Langue de réponse RÉSOLUE (``"fr"|"en"``) ou ``None`` (politique désactivée →
    #: prompts français historiques). N'influence QUE la construction du prompt final ;
    #: jamais le retrieval, l'answerability ni les citations.
    answer_language: str | None = None

    @classmethod
    def from_legacy_args(
        cls,
        *,
        rag_space_id,
        question: str,
        user=None,
        rag_space=None,
        request_id: str | None = None,
        conversation_context: str | None = None,
        entrypoint: str = "internal",
        answer_language: str | None = None,
    ) -> "QueryRequest":
        if not isinstance(question, str):
            raise RetrievalInvariantError("question must be a str")
        return cls(
            question=question,
            rag_space_id=rag_space_id,
            user=user,
            rag_space=rag_space,
            request_id=request_id,
            conversation_context=conversation_context,
            entrypoint=coerce_entrypoint(entrypoint),
            answer_language=answer_language,
        )

    @property
    def entrypoint_enum(self) -> RetrievalEntrypoint:
        return RetrievalEntrypoint(self.entrypoint)

    def __repr__(self) -> str:  # jamais le contenu de la question
        return (
            f"QueryRequest(entrypoint={self.entrypoint!r}, rag_space_id={self.rag_space_id!r}, "
            f"has_user={self.user is not None}, question_chars={len(self.question or '')}, "
            f"has_context={bool(self.conversation_context)})"
        )


# ===========================================================================
# Requête préparée + politique
# ===========================================================================
@dataclass(frozen=True)
class PreparedQuery:
    """Valeurs RÉSOLUES par la phase de préparation (immuable).

    Propriétaire : phase de préparation. Distingue la question ORIGINALE de la/les
    requête(s) de RETRIEVAL. N'inclut ni le prompt final ni les sources.
    """

    original_question: str
    retrieval_queries: tuple[str, ...]
    primary_retrieval_query: str
    rewritten_query: str | None
    inventory: bool
    explanatory: bool
    news: bool
    has_temporal_intent: bool
    # Question de COMPARAISON (différences, points communs, avantages/inconvénients).
    # Valeur par défaut : le contrat reste construit par les appels existants sans rupture.
    comparative: bool = False

    @property
    def mode(self) -> QueryMode:
        return QueryMode.from_flags(
            inventory=self.inventory,
            explanatory=self.explanatory,
            news=self.news,
            comparative=self.comparative,
        )

    @classmethod
    def from_resolved(
        cls,
        *,
        original_question: str,
        queries_for_search: Sequence[str],
        primary_retrieval_query: str,
        inventory: bool,
        explanatory: bool,
        news: bool,
        has_temporal_intent: bool,
        comparative: bool = False,
    ) -> "PreparedQuery":
        qs = tuple(queries_for_search or ())
        # `rewritten_query` = 2e requête si présente (contrat historique inchangé).
        rewritten = qs[1] if len(qs) >= 2 else None
        return cls(
            original_question=original_question,
            retrieval_queries=qs,
            primary_retrieval_query=primary_retrieval_query,
            rewritten_query=rewritten,
            inventory=bool(inventory),
            explanatory=bool(explanatory),
            news=bool(news),
            has_temporal_intent=bool(has_temporal_intent),
            comparative=bool(comparative),
        )

    def __repr__(self) -> str:
        return (
            f"PreparedQuery(mode={self.mode.value}, n_queries={len(self.retrieval_queries)}, "
            f"has_rewrite={self.rewritten_query is not None}, "
            f"temporal={self.has_temporal_intent})"
        )


@dataclass(frozen=True)
class RetrievalPolicy:
    """Politique/budget RÉSOLUS (flags + seuils), immuable.

    Propriétaire : phase de résolution de politique. Objectif P1-B2 : éviter que les
    legs relisent les settings. Valeurs déjà calculées par ``query_rag``.
    """

    fts_required: bool
    hybrid_enabled: bool
    vector_available: bool
    temporal_enabled: bool
    rerank_enabled: bool
    diversity_enabled: bool
    min_sources: int
    min_text_rank: float
    min_score: float
    top_k: int
    vector_top_k: int
    candidate_k: int
    lexical_weight: float
    # Déclencheurs de reranking (défaut 0 : les appels historiques restent valides ;
    # `query_rag` renseigne les vraies valeurs depuis `RagPolicy`).
    rerank_trigger_min_candidates: int = 0
    rerank_top_k: int = 0
    #: Fusion bi-source unifiée (Lot 4). OFF => assemblage historique (privé fusionné
    #: d'abord, corpus ajouté ensuite, donc sans `fusion_score`). ON => les legs privés
    #: et corpus votent dans la MÊME fusion de rangs.
    unified_bi_source_fusion: bool = False
    #: Poids du VOTE des legs corpus dans la fusion unifiée. Les legs privés votent
    #: toujours à 1.0. α ∈ ]0, 1] : 1.0 = fusion unifiée non pondérée.
    corpus_rrf_leg_weight: float = 1.0
    #: Porte d'admission calibrée des corpus métier. MÉCANISME DISTINCT de α :
    #: l'admission décide si un corpus participe, α le poids de son vote une fois admis.
    calibrated_corpus_admission: bool = False
    #: Seuil sur la PROBABILITÉ calibrée d'admission, ∈ [0, 1]. Ce n'est PAS un seuil sur
    #: le score brut du signal : la probabilité est la seule grandeur comparable d'un
    #: corpus à l'autre (cf. `domain/corpus_admission.py`).
    corpus_admission_probability_threshold: float = 0.65

    def __post_init__(self) -> None:
        if int(self.top_k) <= 0:
            raise RetrievalConfigurationError(f"top_k must be > 0 (got {self.top_k})")
        if not _is_finite_number(self.min_score):
            raise RetrievalConfigurationError("min_score must be a finite number")
        if not _is_finite_number(self.min_text_rank):
            raise RetrievalConfigurationError("min_text_rank must be a finite number")
        # Le poids du leg corpus gouverne l'ORDRE des candidats : une valeur invalide
        # ne doit pas être ramenée en silence à 1.0 — ce serait activer la fusion non
        # pondérée en croyant l'avoir atténuée. On refuse, explicitement.
        if not _is_finite_number(self.corpus_rrf_leg_weight):
            raise RetrievalConfigurationError(
                "corpus_rrf_leg_weight must be a finite number"
            )
        if not (0.0 < float(self.corpus_rrf_leg_weight) <= 1.0):
            raise RetrievalConfigurationError(
                "corpus_rrf_leg_weight must be in ]0, 1] "
                f"(got {self.corpus_rrf_leg_weight})"
            )
        # Un seuil d'admission invalide déciderait de la participation d'une source
        # entière. Même règle que pour α : refus explicite, jamais de repli.
        if not _is_finite_number(self.corpus_admission_probability_threshold):
            raise RetrievalConfigurationError(
                "corpus_admission_probability_threshold must be a finite number"
            )
        if not (0.0 <= float(self.corpus_admission_probability_threshold) <= 1.0):
            raise RetrievalConfigurationError(
                "corpus_admission_probability_threshold must be in [0, 1] "
                f"(got {self.corpus_admission_probability_threshold})"
            )


# ===========================================================================
# Candidats
# ===========================================================================
@dataclass(frozen=True)
class RetrievalCandidate:
    """Contrat d'UN candidat (immuable). Adaptateur autour du dict historique.

    IMPORTANT (performance) : n'est PAS appliqué à chaque chunk dans le pipeline de
    production (les helpers historiques consomment des dicts). Il sert à la validation
    unitaire et au futur P1-B2. ``from_dict`` NE COPIE PAS le texte inutilement.
    """

    chunk_id: str
    document_id: Any | None
    rag_space_id: Any | None
    score: float
    source_leg: str | None
    text: str
    metadata: Mapping[str, Any]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, validate: bool = True) -> "RetrievalCandidate":
        if not isinstance(d, Mapping):
            raise RetrievalInvariantError("candidate must be a mapping")
        src = d.get("source") if isinstance(d.get("source"), Mapping) else {}
        chunk_id = d.get("chunk_id")
        document_id = d.get("document_id") or (src.get("document_id") if src else None)
        score = d.get("score")
        cand = cls(
            chunk_id=chunk_id,
            document_id=document_id,
            rag_space_id=d.get("rag_space_id") or (src.get("rag_space_id") if src else None),
            score=score if _is_finite_number(score) else 0.0,
            source_leg=d.get("retrieval_leg") or d.get("source_type"),
            text=d.get("text") or "",
            metadata=src if src else {},
        )
        if validate:
            cand.validate()
        return cand

    def validate(self) -> "RetrievalCandidate":
        if not self.chunk_id:
            raise RetrievalInvariantError("candidate.chunk_id is required")
        if not _is_finite_number(self.score):
            raise RetrievalInvariantError("candidate.score must be a finite number")
        return self

    def __repr__(self) -> str:  # pas de texte
        return (
            f"RetrievalCandidate(chunk_id={self.chunk_id!r}, leg={self.source_leg!r}, "
            f"score={self.score!r}, text_chars={len(self.text or '')})"
        )


@dataclass
class CandidateSet:
    """Vue agrégée des candidats par leg (RÉFÉRENCES, aucune copie de texte).

    Propriétaire : phase de retrieval. ``merged`` reflète le pool après fusion +
    bi-source (les candidats corpus y sont fondus). Mutable par construction (le
    pipeline poursuit sur ``merged``), mais N'ALTÈRE PAS les dicts référencés.
    """

    fts: list[dict[str, Any]] = field(default_factory=list)
    vector: list[dict[str, Any]] = field(default_factory=list)
    corpus: list[dict[str, Any]] = field(default_factory=list)
    merged: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "fts": len(self.fts),
            "vector": len(self.vector),
            "corpus": len(self.corpus),
            "merged": len(self.merged),
        }

    def typed(self, which: str = "merged") -> list[RetrievalCandidate]:
        """Vue TYPÉE à la demande (hors boucle chaude — tests / diagnostics)."""
        return [RetrievalCandidate.from_dict(c, validate=False) for c in getattr(self, which)]

    def __repr__(self) -> str:
        return f"CandidateSet(counts={self.counts})"


@dataclass(frozen=True)
class RankedCandidateSet:
    """Candidats ORDONNÉS après ranking (immuable).

    INVARIANT CENTRAL : l'ORDRE de ``candidates`` EST le contrat de ranking. Aucun
    consommateur ne doit re-trier ni recalculer les scores (préserve
    ``test_rerank_order_preserved``).
    """

    candidates: tuple[dict[str, Any], ...]
    rerank_applied: bool
    fallback_used: bool

    @classmethod
    def from_selected(
        cls, selected: Sequence[dict[str, Any]], *, rerank_applied: bool, fallback_used: bool
    ) -> "RankedCandidateSet":
        return cls(
            candidates=tuple(selected or ()),
            rerank_applied=bool(rerank_applied),
            fallback_used=bool(fallback_used),
        )

    def __len__(self) -> int:
        return len(self.candidates)

    def __repr__(self) -> str:
        return (
            f"RankedCandidateSet(n={len(self.candidates)}, "
            f"rerank_applied={self.rerank_applied}, fallback_used={self.fallback_used})"
        )


# ===========================================================================
# Résultat interne (VUE typée du dict — source de vérité unique)
# ===========================================================================
@dataclass(frozen=True)
class QueryResult:
    """VUE typée immuable du résultat de ``query_rag``.

    SOURCE DE VÉRITÉ UNIQUE : le dict ``raw``. Les propriétés ne font que le LIRE ;
    ``to_legacy_dict`` le renvoie VERBATIM. Nommé ``QueryResult`` (et non
    ``RetrievalResult``) pour ne pas entrer en collision avec le DTO PUBLIC de la façade
    ``apps.rag_core.core.dto.RetrievalResult``.
    """

    raw: dict[str, Any]

    @classmethod
    def from_legacy_dict(cls, d: Mapping[str, Any]) -> "QueryResult":
        if not isinstance(d, Mapping):
            raise RetrievalInvariantError("query result must be a mapping")
        if "status" not in d:
            raise RetrievalInvariantError("query result must carry a 'status'")
        return cls(raw=dict(d))

    def to_legacy_dict(self) -> dict[str, Any]:
        return self.raw

    # --- propriétés en LECTURE SEULE (dérivées de raw) ---------------------
    @property
    def status(self) -> str:
        return self.raw.get("status", "")

    @property
    def answer_kind(self) -> str | None:
        return self.raw.get("answer_kind")

    @property
    def sources(self) -> list[dict[str, Any]]:
        return self.raw.get("sources") or []

    @property
    def prompt(self) -> str | None:
        return self.raw.get("prompt")

    @property
    def log_id(self) -> str | None:
        return self.raw.get("log_id")

    @property
    def retrieval_degraded(self) -> bool:
        return bool(self.raw.get("retrieval_degraded"))

    @property
    def retrieval_trace(self) -> dict[str, Any] | None:
        return self.raw.get("retrieval_trace")

    def __repr__(self) -> str:  # pas de prompt/sources en clair
        return (
            f"QueryResult(status={self.status!r}, answer_kind={self.answer_kind!r}, "
            f"n_sources={len(self.sources)}, degraded={self.retrieval_degraded})"
        )


@dataclass(frozen=True)
class PromptBuildResult:
    """Sortie typée du PromptBuilder (Lot P1-B2), immuable.

    Évite une tuple ambiguë ``(prompt, selected, answer_kind)``. ``sources`` = sélection
    APRÈS budget (références). ``repr`` ne divulgue ni le prompt ni les sources.
    """

    prompt: str | None
    sources: list[dict[str, Any]]
    answer_kind: str
    preamble_kind: str
    #: Comptabilité du budget de prompt : coût fixe des consignes, budget réellement
    #: disponible pour les sources, retraits et troncature éventuelle. Défaut ``None``
    #: pour rester compatible avec les appelants antérieurs à ce champ.
    budget: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"PromptBuildResult(answer_kind={self.answer_kind!r}, "
            f"preamble_kind={self.preamble_kind!r}, n_sources={len(self.sources)}, "
            f"prompt_chars={len(self.prompt or '')})"
        )


# ===========================================================================
# Contexte d'exécution (transversal — PAS un god-object)
# ===========================================================================
@dataclass
class QueryExecutionContext:
    """Contexte transversal d'un appel ``query_rag``.

    Contient UNIQUEMENT des données transversales (requête, trace, chrono) et les
    contrats résolus au fil de l'exécution. N'HÉBERGE PAS de repository, de service
    global, ni de ``dict`` fourre-tout. Prépare P1-B2 (passage d'un contexte unique aux
    étapes extraites) sans encore les extraire.
    """

    request: QueryRequest
    trace: RetrievalTraceBuilder
    started_at: float
    prepared: PreparedQuery | None = None
    policy: RetrievalPolicy | None = None
    candidates: CandidateSet | None = None
    ranked: RankedCandidateSet | None = None

    @classmethod
    def create(
        cls, request: QueryRequest, *, started_at: float, trace: RetrievalTraceBuilder
    ) -> "QueryExecutionContext":
        return cls(request=request, trace=trace, started_at=started_at)

    def __repr__(self) -> str:
        return (
            f"QueryExecutionContext(entrypoint={self.request.entrypoint!r}, "
            f"prepared={self.prepared is not None}, policy={self.policy is not None}, "
            f"candidates={self.candidates is not None}, ranked={self.ranked is not None})"
        )

"""Trace structurée du pipeline de retrieval (Lot P1-A0 — instrumentation).

Objectif : rendre les dégradations du retrieval EXPLICITEMENT observables, sans
modifier la politique de réponse, les scores, l'ordre des sources ni les contrats
publics. Cette structure est une donnée d'OBSERVABILITÉ, pas une seconde
représentation du résultat.

Distinction centrale portée par ce module :

    no_answer + retrieval_degraded=False  -> absence LÉGITIME de preuve
    no_answer + retrieval_degraded=True   -> réponse vide obtenue PENDANT une dégradation

Confidentialité (impératif) : la trace ne doit JAMAIS contenir la question, le
prompt, des chunks, du contenu documentaire, un secret, un identifiant à forte
cardinalité, un détail SQL ou un ``repr`` d'exception. Seule la CATÉGORIE d'erreur
(nom de classe) est conservée via :func:`safe_error_kind`.

Toutes les valeurs d'enum sont un CONTRAT STABLE en snake_case (``schema_version``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Version du schéma de trace. Incrémenter à tout changement INCOMPATIBLE
#: (renommage/suppression de champ, changement de sémantique d'une valeur).
RETRIEVAL_TRACE_SCHEMA_VERSION = 1


#: Valeurs admises pour les seules clés TEXTUELLES de la comptabilité du budget (V4).
_CLOSED_VOCABULARY_KEYS: dict[str, frozenset[str]] = {
    "rules_kind": frozenset({"none", "excel", "table", "excel+table"}),
    "prompt_limit_exception_reason": frozenset(
        {"minimum_source_protection", "unaccounted_overflow"}
    ),
}


class RetrievalOutcome(str, Enum):
    """Verdict global de l'exécution du retrieval."""

    SUCCESS = "success"
    EMPTY_LEGITIMATE = "empty_legitimate"
    DEGRADED = "degraded"
    ERROR = "error"


class RetrievalLegStatus(str, Enum):
    """État d'un leg (étape) du retrieval.

    Les cinq états sont volontairement DISTINCTS :
      - ``NOT_REQUESTED`` : le leg n'était pas demandé pour cette requête ;
      - ``DISABLED``      : le leg est désactivé par configuration ;
      - ``SUCCEEDED``     : exécuté, au moins un candidat ;
      - ``EMPTY``         : exécuté normalement, aucun candidat ;
      - ``DEGRADED``      : demandé mais a échoué (le pipeline a continué) ;
      - ``FAILED``        : demandé mais impossible à exécuter.
    """

    NOT_REQUESTED = "not_requested"
    DISABLED = "disabled"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    DEGRADED = "degraded"
    FAILED = "failed"


class CorpusLegModalityStatus(str, Enum):
    """État d'UNE modalité (lexicale ou vectorielle) du leg corpus métier."""

    NOT_RUN = "not_run"
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


class CorpusDropStage(str, Enum):
    """Étape à laquelle le corpus métier a cessé d'être représenté.

    Réponse à UNE question opérationnelle : « pourquoi ce corpus n'a-t-il pas été
    cité ? ». Les états sont volontairement ordonnés du plus AMONT au plus AVAL, et
    l'absence de candidat n'est jamais confondue avec l'élimination d'un candidat —
    c'est précisément la confusion qui avait rendu D1 et D2 indiscernables.
    """

    NOT_ASSOCIATED = "not_associated"        # aucun corpus associé au workspace
    DISABLED = "disabled"                    # associé, mais association ou corpus inactif
    NO_READY_DOCUMENTS = "no_ready_documents"
    FTS_ERROR = "fts_error"
    VECTOR_ERROR = "vector_error"
    FTS_NO_MATCH = "fts_no_match"            # exécuté, aucune correspondance
    ADMISSION = "admission"                  # candidats produits, corpus NON admis (porte)
    MERGE = "merge"                          # candidats produits, absents du pool fusionné
    DIVERSIFICATION = "diversification"
    RERANKING = "reranking"
    PROMPT_BUDGET = "prompt_budget"          # retenus au rang, coupés par le budget de prompt
    THRESHOLD = "threshold"                  # aucune source retenue, toutes provenances
    SELECTED = "selected"


class RerankGatewayStatus(str, Enum):
    """État du reranking via l'AI Gateway, pour UNE requête.

    Pourquoi une énumération distincte de :class:`RetrievalLegStatus` : celle-ci dit
    ce qui est arrivé à l'APPEL, avec sa cause. Un exploitant ne corrige pas de la
    même façon une Gateway lente et une réponse non conforme, et les compteurs
    agrégés — qui distinguaient déjà les deux — ne permettaient de les rattacher à
    aucune requête.

    ``FALLBACK`` n'est volontairement PAS un état : le repli est une CONSÉQUENCE, pas
    une cause, et il est porté par un booléen séparé. Sous ``fail_open_rrf``, une
    requête peut parfaitement réussir pour l'utilisateur pendant que le reranking a
    échoué — confondre les deux est exactement le défaut que ce leg corrige.

    ``NOT_RECORDED`` n'est jamais ÉCRIT : c'est la lecture d'une trace antérieure à ce
    leg. « Le reranking n'a pas eu lieu » et « on ne sait pas s'il a eu lieu » sont
    deux affirmations différentes, et la seconde ne doit pas se déguiser en première.
    """

    NOT_REQUESTED = "not_requested"
    SUCCESS = "success"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    FAILURE = "failure"
    NOT_RECORDED = "not_recorded"


class RetrievalEntrypoint(str, Enum):
    """Point d'entrée appelant le noyau de retrieval."""

    CHAT_SYNC = "chat_sync"
    CHAT_STREAM = "chat_stream"
    CORE_API = "core_api"
    BENCHMARK = "benchmark"
    HOLDOUT = "holdout"
    INTERNAL = "internal"


# --- Noms de legs (centralisés, snake_case, stables) ------------------------
LEG_FTS = "fts"
LEG_VECTOR = "vector"
LEG_BUSINESS_CORPUS = "business_corpus"
LEG_TEMPORAL = "temporal"
#: Reranking HISTORIQUE (`RAG_RERANK_ENABLED`, modèle chargé dans le process). Le nom
#: reste `rerank` : des consommateurs le lisent déjà, et le renommer casserait la
#: lecture des traces existantes sans rien apprendre à personne.
LEG_RERANK = "rerank"
#: Reranking via l'AI GATEWAY (`RAG_RERANKER_ENABLED`, `POST /v1/rerank`). Leg
#: SÉPARÉ : les deux mécanismes sont distincts, et un seul état partagé ne pourrait
#: pas dire lequel a tourné.
LEG_RERANK_GATEWAY = "rerank_gateway"

#: Statuts qui signifient « demandé/nécessaire mais rompu, pipeline poursuivi ».
#: Ce sont EXACTEMENT les statuts qui rendent ``retrieval_degraded`` vrai.
_DEGRADING_STATUSES = frozenset({RetrievalLegStatus.DEGRADED, RetrievalLegStatus.FAILED})


def coerce_entrypoint(value: Any) -> str:
    """Normalise une valeur d'entrypoint vers une valeur stable connue.

    Toute valeur inconnue retombe sur ``internal`` (sûr), pour ne jamais casser un
    appelant existant qui n'aurait pas été mis à jour.
    """
    if isinstance(value, RetrievalEntrypoint):
        return value.value
    try:
        return RetrievalEntrypoint(str(value)).value
    except (ValueError, TypeError):
        return RetrievalEntrypoint.INTERNAL.value


def safe_error_kind(exc: BaseException | None) -> str | None:
    """Catégorie d'erreur NON SENSIBLE : nom de la classe uniquement.

    Ne renvoie JAMAIS ``str(exc)`` / ``repr(exc)`` : ceux-ci peuvent contenir des
    données (fragment de requête SQL, valeur, chemin).
    """
    if exc is None:
        return None
    try:
        return type(exc).__name__
    except Exception:  # pragma: no cover - défensif pur
        return "UnknownError"


@dataclass
class RetrievalLegTrace:
    """Trace d'un leg unique."""

    name: str
    status: RetrievalLegStatus
    candidate_count: int = 0
    duration_ms: int | None = None
    fallback_used: bool = False
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "candidate_count": int(self.candidate_count),
            "duration_ms": self.duration_ms,
            "fallback_used": bool(self.fallback_used),
            "error_kind": self.error_kind,
        }


@dataclass
class RerankGatewayTrace:
    """Ce que le reranking Gateway a fait pour UNE requête.

    Construite à partir de l'``info`` déjà rendue par
    ``rerank_gateway.rerank_chunks`` : aucun classement n'est recalculé, aucun appel
    n'est refait, aucun candidat n'est touché.
    """

    status: RerankGatewayStatus
    #: L'appel a-t-il été TENTÉ ? Distinct de `status is SUCCESS` : un pool vide ne
    #: déclenche aucun appel, et ce n'est pas un échec.
    attempted: bool = False
    #: Le pipeline est-il reparti sur l'ordre RRF ? Sous `fail_open_rrf`, la requête
    #: aboutit quand même : c'est la distinction que ce champ existe pour porter.
    fallback: bool = False
    failure_policy: str | None = None
    #: Catégorie d'erreur du client, déjà bornée et non sensible.
    error_kind: str | None = None
    model_logical: str | None = None
    #: Révision OBSERVÉE du modèle physique, telle que rendue par la Gateway.
    model_revision: str | None = None
    pool_k: int | None = None
    documents_submitted: int = 0
    documents_returned: int | None = None
    duration_ms: float | None = None
    #: Mouvements de rang RRF -> reranking. Bornés par `pool_k` (≤ 64 au registre).
    movements: list[dict[str, Any]] = field(default_factory=list)
    #: Nombre de candidats reranké survivant au contexte final. `None` tant que la
    #: sélection finale n'est pas connue — jamais 0, qui affirmerait une élimination.
    survived_in_final_context: int | None = None

    @property
    def moved_count(self) -> int:
        return sum(1 for m in self.movements if m.get("delta"))

    @property
    def top1_changed(self) -> bool:
        """Le premier candidat soumis est-il resté premier ?

        Le signal le plus lisible d'un reranking qui agit : si le top-1 ne bouge
        jamais, le mécanisme coûte sans rien déplacer là où ça compte.
        """
        for m in self.movements:
            if m.get("after_rank") == 1:
                return m.get("before_rank") != 1
        return False

    def to_dict(self, *, with_movements: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status.value,
            "attempted": bool(self.attempted),
            "fallback": bool(self.fallback),
            "failure_policy": self.failure_policy,
            "error_kind": self.error_kind,
            "model_logical": self.model_logical,
            "model_revision": self.model_revision,
            "pool_k": self.pool_k,
            "documents_submitted": int(self.documents_submitted),
            "documents_returned": self.documents_returned,
            "duration_ms": self.duration_ms,
            "moved_count": self.moved_count,
            "top1_changed": self.top1_changed,
            "survived_in_final_context": self.survived_in_final_context,
        }
        if with_movements and self.movements:
            out["movements"] = [dict(m) for m in self.movements]
        return out


#: Clé du leg Gateway dans les représentations sérialisées.
RERANK_GATEWAY_KEY = "rerank_gateway"
ADMISSION_SHADOW_KEY = "admission_shadow"
CANDIDATE_TRACE_KEY = "candidate_trace"
VISUAL_NEIGHBORS_KEY = "visual_neighbors"


def read_gateway_rerank(trace_dict: Any) -> dict[str, Any]:
    """Lit le leg Gateway d'une trace SÉRIALISÉE, y compris ancienne.

    Une trace produite avant ce leg n'en porte pas — et l'absence d'information ne
    doit pas se lire comme « le reranking n'a pas eu lieu ». On rend donc
    ``not_recorded``, qui est vrai, plutôt que ``not_requested``, qui serait une
    affirmation que rien ne soutient.

    Ne lève jamais : un lecteur d'audit ne doit pas planter sur une trace ancienne,
    tronquée ou d'un schéma qu'il ne connaît pas.
    """
    if not isinstance(trace_dict, dict):
        return {"status": RerankGatewayStatus.NOT_RECORDED.value, "recorded": False}
    leg = trace_dict.get(RERANK_GATEWAY_KEY)
    if not isinstance(leg, dict) or not leg.get("status"):
        return {"status": RerankGatewayStatus.NOT_RECORDED.value, "recorded": False}
    return {**leg, "recorded": True}


@dataclass
class RetrievalTrace:
    """Trace complète d'une exécution du retrieval (observabilité)."""

    schema_version: int
    request_id: str | None
    entrypoint: str
    outcome: RetrievalOutcome
    degraded: bool
    legs: list[RetrievalLegTrace] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    final_candidate_count: int = 0
    source_count: int = 0
    #: Détail du leg corpus métier. ``None`` quand le workspace n'a aucun corpus
    #: associé ET que rien n'a été observé — l'absence de clé reste un état valide
    #: pour les consommateurs antérieurs à ce champ (ajout rétro-compatible).
    corpus: "CorpusTrace | None" = None
    #: Détail du reranking Gateway. ``None`` quand rien n'a été observé — l'absence
    #: de clé reste un état valide pour les consommateurs antérieurs à ce champ.
    rerank_gateway: "RerankGatewayTrace | None" = None
    #: Décision de la policy d'admission en mode FANTÔME. ``None`` quand le drapeau est
    #: éteint. Purement observationnelle : aucune valeur de ce champ n'influence la
    #: réponse servie — cf. ``apps.rag_core.domain.admission_shadow``.
    admission_shadow: "dict[str, Any] | None" = None
    #: Trace détaillée des candidats du pool. ``None`` quand le drapeau
    #: ``RAG_CANDIDATE_TRACE_ENABLED`` est éteint — c'est le défaut. Diagnostic pur :
    #: aucune décision ne la lit, et aucun texte métier n'y figure. Voir
    #: ``apps.rag_core.services.query.candidate_trace``.
    candidate_trace: "dict[str, Any] | None" = None
    #: Visuels de page placés derrière leur passage natif (VISUAL-RETRIEVAL-QUALITY-1B).
    #: ``None`` quand aucune promotion n'a eu lieu : la clé reste alors absente.
    #: Identifiants et positions seulement, borné par la limite de passages natifs.
    visual_neighbors: "list[dict[str, Any]] | None" = None

    def to_dict(self) -> dict[str, Any]:
        """Représentation JSON complète (sérialisable, non sensible)."""
        out = {
            "schema_version": int(self.schema_version),
            "request_id": self.request_id,
            "entrypoint": self.entrypoint,
            "outcome": self.outcome.value,
            "degraded": bool(self.degraded),
            "legs": [leg.to_dict() for leg in self.legs],
            "fallback_reasons": list(self.fallback_reasons),
            "final_candidate_count": int(self.final_candidate_count),
            "source_count": int(self.source_count),
        }
        if self.corpus is not None:
            out["business_corpus"] = self.corpus.to_dict()
        if self.rerank_gateway is not None:
            # SANS les mouvements. Cette représentation part dans la RÉPONSE de
            # `query_rag` — donc, en chat, dans la charge utile rendue à chaque
            # requête. Quarante mouvements y ajouteraient ~7 ko pour un consommateur
            # qui n'en a aucun usage : l'état et les agrégats suffisent à savoir ce
            # que le reranking a fait. Le détail par candidat vit dans le résumé
            # PERSISTÉ, où une campagne peut le relire (cf. `summary`).
            out[RERANK_GATEWAY_KEY] = self.rerank_gateway.to_dict(with_movements=False)
        if self.admission_shadow is not None:
            # Une dizaine de champs scalaires : la charge utile n'en souffre pas, et
            # un consommateur qui veut savoir ce que la policy AURAIT décidé doit
            # pouvoir le lire sans attendre la ligne d'audit.
            out[ADMISSION_SHADOW_KEY] = dict(self.admission_shadow)
        if self.candidate_trace is not None:
            # Volumétrie bornée par `MAX_CANDIDATS_TRACES` : la trace de diagnostic
            # ne doit avoir aucune croissance libre, même sur une requête large.
            out[CANDIDATE_TRACE_KEY] = dict(self.candidate_trace)
        if self.visual_neighbors:
            out[VISUAL_NEIGHBORS_KEY] = [dict(p) for p in self.visual_neighbors]
        return out

    def summary(self) -> dict[str, Any]:
        """Représentation PERSISTÉE (``RagQueryLog.retrieval``).

        ``legs`` est réduit à ``{nom: statut}`` — suffisant pour distinguer les états
        A..J de la mission sans surcharger la ligne d'audit.

        Une exception assumée au « compact » : le leg de reranking Gateway y porte ses
        mouvements de rang. C'est délibéré et c'est le seul endroit où ils survivent —
        un audit qui ne garde que « le reranking a réussi » ne permet toujours pas de
        dire ce qu'il a déplacé, ce qui était précisément le point aveugle.
        """
        out = {
            "schema_version": int(self.schema_version),
            "entrypoint": self.entrypoint,
            "outcome": self.outcome.value,
            "degraded": bool(self.degraded),
            "legs": {leg.name: leg.status.value for leg in self.legs},
        }
        if self.corpus is not None:
            out["business_corpus"] = self.corpus.summary()
        if self.rerank_gateway is not None:
            # AVEC les mouvements — seul endroit où ils sont conservés. Ce résumé est
            # ce que `RagQueryLog.retrieval` persiste : c'est donc la seule surface
            # depuis laquelle une campagne pourra, plus tard, mesurer les
            # déplacements de rang qu'elle n'aura pas observés en direct. La
            # volumétrie reste bornée par `pool_k` (≤ 64 au registre) : ~7 ko à 40.
            out[RERANK_GATEWAY_KEY] = self.rerank_gateway.to_dict(with_movements=True)
        if self.admission_shadow is not None:
            out[ADMISSION_SHADOW_KEY] = dict(self.admission_shadow)
        if self.candidate_trace is not None:
            out[CANDIDATE_TRACE_KEY] = dict(self.candidate_trace)
        if self.visual_neighbors:
            out[VISUAL_NEIGHBORS_KEY] = [dict(p) for p in self.visual_neighbors]
        return out


class CorpusLegRecorder:
    """Collecteur d'état du leg corpus métier, passé au service corpus.

    Collaboration EXPLICITE (pas de variable globale) : le corpus remplit ce
    recorder, l'orchestrateur le lit et le reporte dans la trace principale. Duck-typé
    volontairement — le module corpus n'importe pas ce fichier ; il appelle seulement
    ``note_*`` (et tolère l'absence d'une méthode, cf. ``corpus.services.retrieval._note``).

    Tous les champs sont des COMPTEURS et des CATÉGORIES : ni question, ni tsquery, ni
    nom de document, ni extrait n'y entrent jamais.
    """

    def note_embedding_groups(self, groupes: Any) -> None:
        """Consigne les couples (provider, modèle) réellement employés.

        Appelée par le service corpus avec ce qu'il vient de calculer pour grouper
        ses requêtes vectorielles : aucune requête supplémentaire, aucune dérivation.
        """
        vus: list[dict[str, Any]] = []
        for entree in groupes or ():
            try:
                provider, model, ids = entree
            except (TypeError, ValueError):
                continue
            vus.append({
                "provider": str(provider) if provider else None,
                "model": str(model) if model else None,
                "corpus_count": len(list(ids or ())),
            })
        self.embedding_groups = vus

    def note_embedding_dimension(self, provider: Any, model: Any, dimension: Any) -> None:
        """Complète un groupe avec la dimension du vecteur RÉELLEMENT calculé.

        Appelée après l'encodage de la question, jamais avant : c'est la longueur du
        vecteur obtenu qui fait foi, pas le ``dim`` demandé — un provider peut rendre
        une autre dimension que celle réclamée, et c'est précisément ce qu'une
        identité d'embedding doit permettre de constater.
        """
        try:
            n = int(dimension)
        except (TypeError, ValueError):
            return
        if n <= 0:
            return
        cible = (str(provider) if provider else None, str(model) if model else None)
        for groupe in self.embedding_groups:
            if (groupe.get("provider"), groupe.get("model")) == cible:
                groupe["dimension"] = n
                return

    def __init__(self) -> None:
        self.requested: bool = False
        self.fts_error_kind: str | None = None
        self.vector_error_kind: str | None = None
        self.candidate_count: int = 0
        # --- Périmètre (D1/D2 : distinguer non-associé, désactivé, sans document) ---
        self.associated_corpus_count: int = 0
        self.active_corpus_count: int = 0
        self.ready_document_count: int = 0
        # --- Exécution du plan FTS (D1) -----------------------------------------
        #: Identité d'embedding du corpus métier, TELLE QUE LE RETRIEVAL L'A UTILISÉE
        #: pour grouper ses requêtes vectorielles — jamais reconstruite depuis un nom.
        #: Liste de `{provider, model, corpus_count}` : un workspace peut autoriser
        #: plusieurs corpus qui n'utilisent pas le même modèle, et masquer ce fait par
        #: un choix arbitraire rendrait la trace trompeuse.
        self.embedding_groups: list[dict[str, Any]] = []
        # --- Exécution du plan FTS (D1) -----------------------------------------
        self.fts_plan_index: int | None = None
        self.fts_plan_size: int = 0
        self.fts_attempt_count: int = 0
        self.fts_fallback_used: bool = False
        # --- Candidats par modalité ---------------------------------------------
        self.text_candidate_count: int = 0
        self.vector_candidate_count: int = 0
        self.vector_requested: bool = True
        #: Code d'erreur STABLE du leg vectoriel (ex. `corpus_embedding_model_missing`).
        #: Un nom de classe d'exception dit qu'il y a eu un problème ; un code dit
        #: LEQUEL, et se cherche dans les journaux.
        self.vector_error_code: str | None = None
        #: Cohérence d'empreinte par corpus (``{corpus_id: ConsistencyReport}``).
        self.consistency_reports: dict = {}
        #: Vrai si le leg vectoriel a été REFUSÉ faute de document exploitable.
        self.vector_incoherent: bool = False

    # -- messages émis par le service corpus ---------------------------------
    def note_scope(self, *, associated: int, allowed: int) -> None:
        self.associated_corpus_count = int(associated or 0)
        self.active_corpus_count = int(allowed or 0)

    def note_requested(self) -> None:
        self.requested = True

    def note_ready_documents(self, count: int) -> None:
        self.ready_document_count = int(count or 0)

    def note_fts_plan(self, execution: Any) -> None:
        """Retient la tentative de plan la plus AVANCÉE observée sur les multi-passes.

        Une question réécrite produit plusieurs passes FTS ; ce qui intéresse
        l'exploitation, c'est de savoir si le repli a servi, pas de tenir le journal
        de chaque passe.
        """
        self.fts_plan_size = max(self.fts_plan_size, int(getattr(execution, "plan_size", 0) or 0))
        self.fts_attempt_count += int(getattr(execution, "attempt_count", 0) or 0)
        index = getattr(execution, "plan_index", None)
        if index is not None:
            self.fts_plan_index = index if self.fts_plan_index is None else max(self.fts_plan_index, index)
            self.fts_fallback_used = self.fts_fallback_used or bool(index > 0)

    def note_vector_disabled(self) -> None:
        self.vector_requested = False

    def note_leg_counts(self, *, text: int, vector: int) -> None:
        self.text_candidate_count = int(text or 0)
        self.vector_candidate_count = int(vector or 0)

    def note_fts_error(self, exc: BaseException) -> None:
        self.fts_error_kind = safe_error_kind(exc)

    def note_vector_error(self, exc: BaseException) -> None:
        self.vector_error_kind = safe_error_kind(exc)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            self.vector_error_code = code

    def note_vector_error_code(self, code: str) -> None:
        self.vector_error_code = str(code) if code else None

    def note_corpus_consistency(self, reports: Any) -> None:
        """Rapports de cohérence d'empreinte, PAR CORPUS (duck-typés, cf. corpus)."""
        if isinstance(reports, dict):
            self.consistency_reports.update(reports)

    def note_vector_incoherent(self) -> None:
        """Aucun corpus du groupe n'a de document exploitable : le leg n'a PAS tourné.

        Distinct d'un leg vide : ici on a refusé de comparer, ce n'est pas l'absence de
        correspondance. Sans cette distinction, une incohérence d'indexation se lirait
        comme un corpus sans réponse — deux causes opposées.
        """
        self.vector_incoherent = True

    def note_candidate_count(self, count: int) -> None:
        self.candidate_count = int(count or 0)

    # -- lecture -------------------------------------------------------------
    @property
    def failed(self) -> bool:
        return bool(self.fts_error_kind or self.vector_error_kind)

    @property
    def error_kind(self) -> str | None:
        return self.fts_error_kind or self.vector_error_kind

    @property
    def lexical_status(self) -> CorpusLegModalityStatus:
        if not self.requested:
            return CorpusLegModalityStatus.NOT_RUN
        if self.fts_error_kind:
            return CorpusLegModalityStatus.ERROR
        return (
            CorpusLegModalityStatus.OK
            if self.text_candidate_count > 0
            else CorpusLegModalityStatus.EMPTY
        )

    @property
    def vector_status(self) -> CorpusLegModalityStatus:
        if not self.requested or not self.vector_requested:
            return CorpusLegModalityStatus.NOT_RUN
        if self.vector_error_kind:
            return CorpusLegModalityStatus.ERROR
        return (
            CorpusLegModalityStatus.OK
            if self.vector_candidate_count > 0
            else CorpusLegModalityStatus.EMPTY
        )

    def leg_status(self) -> RetrievalLegStatus:
        """Traduit l'état collecté en statut de leg.

        - non demandé (aucun corpus autorisé) -> ``NOT_REQUESTED`` ;
        - une passe a échoué                  -> ``DEGRADED`` ;
        - candidats présents                  -> ``SUCCEEDED`` ;
        - exécuté sans candidat               -> ``EMPTY``.
        """
        if not self.requested:
            return RetrievalLegStatus.NOT_REQUESTED
        if self.failed:
            return RetrievalLegStatus.DEGRADED
        if self.candidate_count > 0:
            return RetrievalLegStatus.SUCCEEDED
        return RetrievalLegStatus.EMPTY


# --- Provenance des candidats (comptage d'observabilité) --------------------
SOURCE_TYPE_DOMAIN_CORPUS = "domain_corpus"

#: Nombre maximal de corpus détaillés dans la trace. Au-delà, seuls les agrégats
#: subsistent : une trace n'est pas un inventaire.
MAX_TRACED_CORPORA = 10


def is_corpus_candidate(candidate: Any) -> bool:
    """Vrai si le candidat provient d'un corpus métier (provenance EXPLICITE)."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get("source_type") == SOURCE_TYPE_DOMAIN_CORPUS:
        return True
    src = candidate.get("source")
    return isinstance(src, dict) and src.get("source_type") == SOURCE_TYPE_DOMAIN_CORPUS


def _document_id_of(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    src = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    value = src.get("document_id") or candidate.get("document_id")
    return str(value) if value else None


def _corpus_id_of(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    src = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    value = src.get("corpus_id") or candidate.get("corpus_id")
    return str(value) if value else None


def _first_ranks(candidates: Any) -> tuple[int | None, int | None]:
    """``(rang du premier corpus, rang du premier privé)``, 1-indexés, sinon ``None``."""
    first_corpus = first_private = None
    for index, candidate in enumerate(candidates or [], start=1):
        if is_corpus_candidate(candidate):
            if first_corpus is None:
                first_corpus = index
        elif first_private is None:
            first_private = index
        if first_corpus is not None and first_private is not None:
            break
    return first_corpus, first_private


def split_by_provenance(candidates: Any) -> tuple[int, int]:
    """``(privés, corpus)`` — ne conserve AUCUNE référence aux candidats."""
    private = corpus = 0
    for c in candidates or []:
        if is_corpus_candidate(c):
            corpus += 1
        else:
            private += 1
    return private, corpus


@dataclass
class CorpusTrace:
    """Trace du leg corpus métier : périmètre, exécution, survie par étape."""

    associated_corpus_count: int = 0
    active_corpus_count: int = 0
    ready_document_count: int = 0
    lexical_status: CorpusLegModalityStatus = CorpusLegModalityStatus.NOT_RUN
    vector_status: CorpusLegModalityStatus = CorpusLegModalityStatus.NOT_RUN
    vector_error_code: str | None = None
    fts_plan_index: int | None = None
    fts_plan_size: int = 0
    fts_attempt_count: int = 0
    fts_fallback_used: bool = False
    candidate_count: int = 0
    text_candidate_count: int = 0
    vector_candidate_count: int = 0
    unified_fusion: bool = False
    corpus_leg_weight: float = 1.0
    workspace_leg_weight: float = 1.0
    rrf_k: int = 0
    # Survie par étape (comptes de candidats CORPUS)
    pool_count: int = 0
    pre_rerank_count: int = 0
    post_rerank_count: int = 0
    final_count: int = 0
    # Contexte privé, pour lire les comptes ci-dessus sans deviner
    private_pool_count: int = 0
    private_pre_rerank_count: int = 0
    private_post_rerank_count: int = 0
    private_final_count: int = 0
    # Rang du PREMIER candidat de chaque provenance, par étape. Un compte dit combien
    # ont survécu ; un rang dit s'ils sont lisibles par le modèle — le premier rang est
    # ce qui décide qu'une source est citée ou noyée.
    first_corpus_rank_pool: int | None = None
    first_private_rank_pool: int | None = None
    first_corpus_rank_final: int | None = None
    first_private_rank_final: int | None = None
    # Budget de prompt : ce que la sélection perd entre le classement et le contexte
    # réellement transmis au modèle.
    prompt_budget_observed: bool = False
    prompt_budget_dropped_corpus: int = 0
    prompt_budget_dropped_private: int = 0
    #: Comptabilité du budget : ce que les CONSIGNES coûtent, et ce qu'il reste aux
    #: sources. Sans ces valeurs, l'élargissement d'un gabarit retire des sources sans
    #: qu'aucun compteur ne le dise — c'est exactement ce qui s'est produit.
    prompt_budget_accounting: dict[str, Any] = field(default_factory=dict)
    distinct_documents_final: int = 0
    distinct_corpora_final: int = 0
    diversity_applied: bool = False
    rerank_applied: bool = False
    selection_observed: bool = False
    per_corpus: list[dict[str, Any]] = field(default_factory=list)
    # --- Porte d'admission calibrée -----------------------------------------
    # Un corpus peut désormais être ÉCARTÉ avant toute fusion. Sans ces champs, ce rejet
    # serait indiscernable d'un corpus sans correspondance — deux causes opposées.
    admission_enabled: bool = False
    admission_signal_name: str | None = None
    admission_signal_value: float | None = None
    admission_calibrator_type: str | None = None
    admission_calibration_version: int | None = None
    admission_probability: float | None = None
    admission_probability_threshold: float | None = None
    corpus_admitted: bool = True
    corpus_admission_reason: str | None = None
    corpus_admission_error_code: str | None = None
    corpus_candidate_count_before_admission: int = 0
    corpus_candidate_count_after_admission: int = 0
    admission_per_corpus: list[dict[str, Any]] = field(default_factory=list)
    # --- Cohérence d'empreinte d'indexation, PAR CORPUS ----------------------
    # Un corpus écarté faute d'empreinte cohérente et un corpus sans correspondance
    # produisent le même silence. Ces champs les séparent : deux causes opposées,
    # deux actions opposées de l'exploitant.
    embedding_consistency: list[dict[str, Any]] = field(default_factory=list)
    vector_incoherent: bool = False

    @property
    def drop_stage(self) -> CorpusDropStage:
        """Étape à laquelle le corpus a disparu — calculée, jamais devinée."""
        if self.active_corpus_count == 0:
            return (
                CorpusDropStage.NOT_ASSOCIATED
                if self.associated_corpus_count == 0
                else CorpusDropStage.DISABLED
            )
        if self.ready_document_count == 0:
            return CorpusDropStage.NO_READY_DOCUMENTS
        if self.candidate_count == 0:
            if self.lexical_status is CorpusLegModalityStatus.ERROR:
                return CorpusDropStage.FTS_ERROR
            if self.vector_status is CorpusLegModalityStatus.ERROR:
                return CorpusDropStage.VECTOR_ERROR
            return CorpusDropStage.FTS_NO_MATCH
        if self.admission_enabled and not self.corpus_admitted:
            # Des candidats existaient : le corpus n'a pas été écarté faute de matière,
            # mais par DÉCISION. Sans cette étape, ce rejet se lirait `merge`, c'est-à-dire
            # exactement le défaut de classement que la porte n'a pas.
            return CorpusDropStage.ADMISSION
        if not self.selection_observed:
            # Le retrieval s'est arrêté avant la sélection (abstention immédiate).
            return CorpusDropStage.THRESHOLD
        if self.final_count > 0:
            return CorpusDropStage.SELECTED
        if self.private_final_count == 0:
            # Personne n'a été retenu : c'est la politique de réponse, pas le classement.
            return CorpusDropStage.THRESHOLD
        if self.post_rerank_count > 0:
            return CorpusDropStage.PROMPT_BUDGET
        if self.pre_rerank_count > 0 and self.rerank_applied:
            return CorpusDropStage.RERANKING
        if self.pool_count > 0:
            return CorpusDropStage.DIVERSIFICATION
        return CorpusDropStage.MERGE

    def to_dict(self) -> dict[str, Any]:
        return {
            "associated_corpus_count": int(self.associated_corpus_count),
            "active_corpus_count": int(self.active_corpus_count),
            "ready_corpus_document_count": int(self.ready_document_count),
            "corpus_lexical_status": self.lexical_status.value,
            "corpus_vector_status": self.vector_status.value,
            "corpus_vector_error_code": self.vector_error_code,
            "corpus_fts_plan_index": self.fts_plan_index,
            "corpus_fts_plan_size": int(self.fts_plan_size),
            "corpus_fts_attempt_count": int(self.fts_attempt_count),
            "corpus_fts_fallback_used": bool(self.fts_fallback_used),
            "corpus_candidate_count": int(self.candidate_count),
            "corpus_text_candidate_count": int(self.text_candidate_count),
            "corpus_vector_candidate_count": int(self.vector_candidate_count),
            "unified_fusion": bool(self.unified_fusion),
            "corpus_rrf_leg_weight": float(self.corpus_leg_weight),
            "workspace_rrf_leg_weight": float(self.workspace_leg_weight),
            "rrf_k": int(self.rrf_k),
            "private_candidate_count": int(self.private_pool_count),
            "private_selected_count": int(self.private_final_count),
            "corpus_pool_count": int(self.pool_count),
            "corpus_pre_rerank_count": int(self.pre_rerank_count),
            "corpus_post_rerank_count": int(self.post_rerank_count),
            "corpus_selected_count": int(self.final_count),
            # Survie par ÉTAPE, provenance par provenance. `after_prompt_budget` est la
            # seule ligne qui décrit ce que le modèle a réellement lu.
            "stage_counts": {
                "after_rrf": {
                    "workspace": int(self.private_pool_count),
                    "corpus": int(self.pool_count),
                },
                "after_diversification": {
                    "workspace": int(self.private_pre_rerank_count),
                    "corpus": int(self.pre_rerank_count),
                },
                "after_reranking": {
                    "workspace": int(self.private_post_rerank_count),
                    "corpus": int(self.post_rerank_count),
                },
                "after_prompt_budget": {
                    "workspace": int(self.private_final_count),
                    "corpus": int(self.final_count),
                },
            },
            "prompt_budget": {
                "observed": bool(self.prompt_budget_observed),
                "dropped_workspace": int(self.prompt_budget_dropped_private),
                "dropped_corpus": int(self.prompt_budget_dropped_corpus),
                **dict(self.prompt_budget_accounting or {}),
            },
            "first_rank": {
                "corpus_after_rrf": self.first_corpus_rank_pool,
                "workspace_after_rrf": self.first_private_rank_pool,
                "corpus_after_prompt_budget": self.first_corpus_rank_final,
                "workspace_after_prompt_budget": self.first_private_rank_final,
            },
            # Porte d'admission : la décision, ses ENTRÉES et son effet mesuré. Les
            # coefficients ne sont pas exposés — la version identifie la calibration,
            # c'est ce qu'il faut pour rejouer une décision, et cela n'alourdit rien.
            "admission": {
                "calibrated_admission_enabled": bool(self.admission_enabled),
                "admission_signal_name": self.admission_signal_name,
                "admission_signal_value": self.admission_signal_value,
                "admission_calibrator_type": self.admission_calibrator_type,
                "admission_calibration_version": self.admission_calibration_version,
                "admission_probability": self.admission_probability,
                "admission_probability_threshold": self.admission_probability_threshold,
                "corpus_admitted": bool(self.corpus_admitted),
                "corpus_admission_reason": self.corpus_admission_reason,
                "corpus_admission_error_code": self.corpus_admission_error_code,
                "corpus_candidate_count_before_admission": int(
                    self.corpus_candidate_count_before_admission
                ),
                "corpus_candidate_count_after_admission": int(
                    self.corpus_candidate_count_after_admission
                ),
                "per_corpus": list(self.admission_per_corpus),
            },
            "embedding_consistency": {
                "vector_refused_for_incoherence": bool(self.vector_incoherent),
                "per_corpus": list(self.embedding_consistency),
            },
            "distinct_documents_final": int(self.distinct_documents_final),
            "distinct_corpora_final": int(self.distinct_corpora_final),
            "diversity_applied": bool(self.diversity_applied),
            "rerank_applied": bool(self.rerank_applied),
            "corpus_drop_stage": self.drop_stage.value,
            "per_corpus": list(self.per_corpus),
        }

    def summary(self) -> dict[str, Any]:
        """Vue COMPACTE (audit, Core API) : l'essentiel du diagnostic, rien de plus."""
        return {
            "active_corpus_count": int(self.active_corpus_count),
            "ready_corpus_document_count": int(self.ready_document_count),
            "corpus_candidate_count": int(self.candidate_count),
            "corpus_selected_count": int(self.final_count),
            "corpus_fts_plan_index": self.fts_plan_index,
            "corpus_drop_stage": self.drop_stage.value,
            # Le résumé porte la DÉCISION et sa raison, pas les valeurs intermédiaires :
            # « le corpus a-t-il été admis, et pourquoi » est diagnostique à lui seul.
            "corpus_admitted": bool(self.corpus_admitted),
            "corpus_admission_reason": self.corpus_admission_reason,
            # Le budget de prompt est la DERNIÈRE étape qui retire des sources. Sa
            # comptabilité existait, exposée par l'API, mais ne survivait pas dans
            # l'audit : une éviction de sources y était donc indétectable après coup.
            # Constaté le 23/08/2026 en lisant un journal réel — on y voyait « 1
            # source » sans pouvoir dire ce qui l'avait réduite à une.
            "prompt_budget": self._prompt_budget_summary(),
        }

    def _prompt_budget_summary(self) -> dict[str, Any]:
        """Ce que le budget a retiré, et ce qui l'a mangé. Compact et sans texte."""
        a = self.prompt_budget_accounting or {}
        out: dict[str, Any] = {
            "observed": bool(self.prompt_budget_observed),
            "dropped_workspace": int(self.prompt_budget_dropped_private),
            "dropped_corpus": int(self.prompt_budget_dropped_corpus),
        }
        for cle in ("prompt_overhead_chars", "source_budget_chars",
                    "sources_dropped_by_budget", "prompt_budget_exhausted",
                    "final_source_truncated", "conversation_context_requested_chars",
                    "conversation_context_kept_chars", "conversation_context_truncated",
                    "min_sources_reserved",
                    # Compteurs V4 (présents seulement sous
                    # `RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED`) : nombres, booléens
                    # et vocabulaire FERMÉ — jamais de texte.
                    "render_conformance_v1", "rules_kind", "conditional_rules_chars",
                    "source_context_chars", "headers_deduplicated",
                    "prompt_total_chars", "prompt_limit_chars",
                    "prompt_remaining_chars", "prompt_limit_exceeded",
                    "prompt_limit_exception_reason"):
            if cle in a:
                out[cle] = a[cle]
        return out


def _consistency_rows(reports: Any) -> list[dict[str, Any]]:
    """Rapports de cohérence -> lignes de trace, bornées et sans contenu.

    Un identifiant technique de corpus et des compteurs : ni nom de fichier, ni texte,
    ni empreinte complète (l'empreinte est un hash, mais elle n'apporte rien à un
    diagnostic et alourdirait chaque trace).
    """
    if not isinstance(reports, dict) or not reports:
        return []
    rows: list[dict[str, Any]] = []
    for corpus_id, report in sorted(reports.items())[:MAX_TRACED_CORPORA]:
        payload = report.to_dict() if hasattr(report, "to_dict") else {}
        rows.append({"corpus_id": str(corpus_id), **payload})
    return rows


class RetrievalTraceBuilder:
    """Accumulateur mutable de legs, utilisé par ``query_rag``.

    Ne calcule ``outcome`` / ``degraded`` qu'au :meth:`build` final. Ré-enregistrer un
    même leg écrase la valeur précédente (dernier état gagne) tout en conservant
    l'ordre d'apparition.
    """

    def __init__(self, *, entrypoint: Any, request_id: str | None) -> None:
        self.entrypoint = coerce_entrypoint(entrypoint)
        self.request_id = request_id or None
        self._legs: dict[str, RetrievalLegTrace] = {}
        #: Décision d'admission (lot 4). `None` tant qu'aucune n'a été prise.
        self._admission: dict | None = None
        self._order: list[str] = []
        self.fallback_reasons: list[str] = []
        self.corpus: CorpusTrace | None = None
        self.rerank_gateway: RerankGatewayTrace | None = None
        self.admission_shadow: dict[str, Any] | None = None
        self.candidate_trace: dict[str, Any] | None = None
        self.visual_neighbors: list[dict[str, Any]] | None = None
        #: Identité d'embedding PAR MAGASIN. Alimentée par les couches qui
        #: la CONNAISSENT déjà — le retrieval privé la reçoit de la Gateway, le
        #: service corpus l'a calculée pour grouper ses requêtes. Rien n'est dérivé
        #: ni deviné ici : une identité absente reste absente.
        self.store_embedding_identities: dict[str, dict[str, Any]] = {}

    def record_store_embedding_identity(self, store: str, **champs: Any) -> None:
        """Consigne l'identité d'embedding d'un magasin. Ne dérive rien.

        Les champs à ``None`` sont écartés plutôt qu'inscrits : « inconnu » et
        « absent » se lisent alors pareil, ce qui est le fait, tandis qu'un champ
        présent et nul se lirait comme une valeur mesurée.
        """
        utiles = {k: v for k, v in champs.items() if v not in (None, "")}
        if utiles:
            self.store_embedding_identities[str(store)] = utiles

    # --- Leg corpus métier -------------------------------------------------
    def record_corpus_state(
        self,
        recorder: CorpusLegRecorder,
        *,
        unified_fusion: bool,
        corpus_leg_weight: float = 1.0,
        workspace_leg_weight: float = 1.0,
    ) -> None:
        """Recopie l'état collecté par le service corpus (compteurs et catégories)."""
        from .merge import _rrf_k

        self.corpus = CorpusTrace(
            associated_corpus_count=recorder.associated_corpus_count,
            active_corpus_count=recorder.active_corpus_count,
            ready_document_count=recorder.ready_document_count,
            lexical_status=recorder.lexical_status,
            vector_status=recorder.vector_status,
            vector_error_code=recorder.vector_error_code,
            fts_plan_index=recorder.fts_plan_index,
            fts_plan_size=recorder.fts_plan_size,
            fts_attempt_count=recorder.fts_attempt_count,
            fts_fallback_used=recorder.fts_fallback_used,
            candidate_count=recorder.candidate_count,
            text_candidate_count=recorder.text_candidate_count,
            vector_candidate_count=recorder.vector_candidate_count,
            embedding_consistency=_consistency_rows(
                getattr(recorder, "consistency_reports", None)
            ),
            vector_incoherent=bool(getattr(recorder, "vector_incoherent", False)),
            unified_fusion=bool(unified_fusion),
            corpus_leg_weight=float(corpus_leg_weight),
            workspace_leg_weight=float(workspace_leg_weight),
            rrf_k=_rrf_k(),
        )

    def record_corpus_admission(
        self,
        outcome: Any,
        *,
        enabled: bool,
        threshold: float,
    ) -> None:
        """Reporte la décision de la porte d'admission — décision, entrées, effet.

        La décision est prise PAR CORPUS ; la trace expose donc le détail (borné) et une
        vue agrégée. Le représentant agrégé est le corpus dont la probabilité est la plus
        élevée : sur un rejet, c'est celui qui a failli passer, donc le seul intéressant
        pour régler un seuil.

        Rien de sensible n'entre ici : un identifiant technique de corpus, une valeur de
        signal, une probabilité, une raison — ni question, ni extrait, ni coefficient.
        """
        if self.corpus is None:
            self.corpus = CorpusTrace()
        c = self.corpus
        c.admission_enabled = bool(enabled)
        c.admission_probability_threshold = float(threshold) if enabled else None
        decisions = dict(getattr(outcome, "decisions", None) or {})
        c.corpus_candidate_count_before_admission = int(
            getattr(outcome, "candidate_count_before", 0) or 0
        )
        c.corpus_candidate_count_after_admission = int(
            getattr(outcome, "candidate_count_after", 0) or 0
        )
        if not enabled:
            c.corpus_admitted = True
            return
        c.corpus_admitted = bool(getattr(outcome, "any_admitted", False))
        c.admission_per_corpus = [
            {
                "corpus_id": cid,
                "admitted": bool(d.admitted),
                "reason": d.reason,
                "probability": d.probability,
                "signal_value": d.signal_value,
                "error_code": d.error_code,
            }
            for cid, d in sorted(decisions.items())[:MAX_TRACED_CORPORA]
        ]
        representative = self._representative_decision(decisions)
        if representative is None:
            # La porte est active mais aucun corpus n'a produit de candidat : il n'y a
            # pas de décision à rapporter, et prétendre le contraire serait faux.
            c.corpus_admitted = False
            c.corpus_admission_reason = None
            return
        c.admission_signal_name = representative.signal_name
        c.admission_signal_value = representative.signal_value
        c.admission_probability = representative.probability
        c.admission_calibrator_type = representative.calibrator_type
        c.admission_calibration_version = representative.calibration_version
        c.corpus_admission_reason = representative.reason
        c.corpus_admission_error_code = representative.error_code

    @staticmethod
    def _representative_decision(decisions: dict[str, Any]) -> Any:
        """Décision la plus informative : admise si possible, sinon la plus proche du seuil."""
        if not decisions:
            return None
        admitted = [d for d in decisions.values() if d.admitted]
        pool = admitted or list(decisions.values())
        return max(pool, key=lambda d: (d.probability if d.probability is not None else -1.0))

    def record_bi_source_selection(
        self,
        *,
        pool: Any,
        pre_rerank: Any,
        post_rerank: Any,
        final: Any,
        diversity_applied: bool,
        rerank_applied: bool,
    ) -> None:
        """Compte la survie des candidats CORPUS à chaque étape de sélection.

        Aucune référence n'est conservée : seuls des entiers sortent d'ici. C'est ce
        qui rend l'étape d'élimination lisible en production sans jamais journaliser
        un extrait, un nom de fichier ou une question.
        """
        if self.corpus is None:
            self.corpus = CorpusTrace()
        c = self.corpus
        c.private_pool_count, c.pool_count = split_by_provenance(pool)
        c.private_pre_rerank_count, c.pre_rerank_count = split_by_provenance(pre_rerank)
        c.private_post_rerank_count, c.post_rerank_count = split_by_provenance(post_rerank)
        c.private_final_count, c.final_count = split_by_provenance(final)
        c.first_corpus_rank_pool, c.first_private_rank_pool = _first_ranks(pool)
        c.first_corpus_rank_final, c.first_private_rank_final = _first_ranks(final)
        c.diversity_applied = bool(diversity_applied)
        c.rerank_applied = bool(rerank_applied)
        c.selection_observed = True
        c.per_corpus = self._per_corpus_counts(pool, final)
        c.distinct_documents_final = len(
            {d for d in (_document_id_of(x) for x in (final or [])) if d}
        )
        c.distinct_corpora_final = len(
            {d for d in (_corpus_id_of(x) for x in (final or [])) if d}
        )

    def record_prompt_budget(
        self, *, before: Any, after: Any, accounting: Any = None
    ) -> None:
        """Ce que le BUDGET DE PROMPT a retiré, provenance par provenance, et POURQUOI.

        Étape distincte et souvent décisive : c'est elle qui décide de ce que le modèle
        lit réellement, et elle était jusqu'ici invisible. Un classement excellent dont
        les bonnes sources tombent ici produit exactement la même réponse qu'un mauvais
        classement — sans qu'aucun compteur ne le dise.

        ``accounting`` ajoute la comptabilité du budget : coût FIXE des consignes, place
        réellement laissée aux sources, retraits, et troncature éventuelle de la dernière
        source. Sans elle, l'élargissement d'un gabarit de consignes retire une source à
        la quasi-totalité des requêtes sans qu'aucun compteur ne bouge — mesuré, et
        resté invisible jusqu'à une comparaison de versions.
        """
        if self.corpus is None:
            self.corpus = CorpusTrace()
        c = self.corpus
        before_private, before_corpus = split_by_provenance(before)
        after_private, after_corpus = split_by_provenance(after)
        c.prompt_budget_observed = True
        c.prompt_budget_dropped_private = max(0, before_private - after_private)
        c.prompt_budget_dropped_corpus = max(0, before_corpus - after_corpus)
        if isinstance(accounting, dict):
            # Uniquement des entiers et des booléens : ni texte de source, ni extrait,
            # ni question. Deux clés à VOCABULAIRE FERMÉ font exception (V4) : un
            # identifiant de catégorie n'est pas un texte, et la liste blanche interdit
            # qu'une chaîne libre s'y glisse.
            c.prompt_budget_accounting = {
                k: v for k, v in accounting.items()
                if isinstance(v, (int, float, bool)) or v is None
                or (k in _CLOSED_VOCABULARY_KEYS and v in _CLOSED_VOCABULARY_KEYS[k])
            }

    @staticmethod
    def _per_corpus_counts(pool: Any, final: Any) -> list[dict[str, Any]]:
        """Détail par corpus (UUID technique + compteurs), borné et sans contenu."""
        counts: dict[str, dict[str, int]] = {}
        for candidate in pool or []:
            if not is_corpus_candidate(candidate):
                continue
            cid = _corpus_id_of(candidate)
            if cid is None:
                continue
            counts.setdefault(cid, {"candidates": 0, "selected": 0})["candidates"] += 1
        for candidate in final or []:
            if not is_corpus_candidate(candidate):
                continue
            cid = _corpus_id_of(candidate)
            if cid is None or cid not in counts:
                continue
            counts[cid]["selected"] += 1
        rows = [{"corpus_id": cid, **vals} for cid, vals in sorted(counts.items())]
        return rows[:MAX_TRACED_CORPORA]

    def record_admission(self, decision) -> None:
        """Consigne la décision d'admission (lot 4).

        Portée dans la trace et non seulement dans les logs : c'est la seule façon
        de savoir, en relisant un `RagQueryLog`, POURQUOI une requête n'a pas
        répondu — un refus de politique et une absence de candidats produisent tous
        deux zéro source, et sont deux incidents différents.
        """
        self._admission = {
            "status": getattr(decision, "status", ""),
            "reason": getattr(decision, "reason", ""),
            "policy": getattr(decision, "policy_name", ""),
            "thresholds": dict(getattr(decision, "thresholds", {}) or {}),
            "observed": dict(getattr(decision, "observed_scores", {}) or {}),
        }

    # --- Leg reranking AI Gateway -----------------------------------------
    def record_admission_shadow(self, decision: Any) -> None:
        """Consigne la décision FANTÔME de la policy d'admission.

        Ne recalcule rien, n'appelle rien : elle range une structure déjà produite par
        ``domain.admission_shadow.evaluate`` — qui lit lui-même le score déjà rendu par
        le reranking. Aucun appel modèle, aucune requête réseau.

        Enregistrer une décision ne la rend pas agissante : rien dans le pipeline ne lit
        ce champ. C'est le contrat du mode fantôme, et un test le verrouille en comparant
        les réponses drapeau ON et drapeau OFF.
        """
        self.admission_shadow = dict(decision) if isinstance(decision, dict) else None

    def record_candidate_trace(self, trace: Any) -> None:
        """Consigne la trace détaillée des candidats.

        Ne construit rien : la structure arrive déjà bâtie par
        ``services.query.candidate_trace.construire``, une fonction pure qui ne
        relit que des champs posés par le pipeline. Aucun appel modèle, aucune
        requête réseau, aucun recalcul de score.

        Consigner n'est pas agir : rien dans le pipeline ne lit ce champ.
        """
        self.candidate_trace = dict(trace) if isinstance(trace, dict) else None

    def record_visual_neighbors(self, promotions: Any) -> None:
        """Consigne les voisins visuels de page promus (VISUAL-RETRIEVAL-QUALITY-1B).

        Ne décide rien : la liste arrive déjà construite par
        ``services.query.visual_neighbors``. Identifiants et positions, aucun texte.
        """
        rows = [p.to_dict() if hasattr(p, "to_dict") else dict(p) for p in (promotions or [])]
        self.visual_neighbors = rows or None

    def record_rerank_gateway(self, info: Any) -> RerankGatewayTrace:
        """Consigne ce que le reranking Gateway a fait, depuis son ``info``.

        Pourquoi cette méthode existe. L'orchestrateur l'appelait déjà — derrière un
        ``hasattr(trace, "record_rerank_gateway")`` — alors qu'elle n'a jamais existé.
        Le garde était donc toujours faux, l'appel toujours sans effet, et l'audit
        affirmait ``rerank: not_requested`` sur des requêtes RÉELLEMENT rerankées.
        Ce n'était pas une métrique manquante : c'était une trace qui disait le
        contraire de ce qui s'était passé.

        Elle ne recalcule aucun classement, ne refait aucun appel, ne touche aucun
        candidat : elle lit une structure déjà produite.

        Le leg ``rerank_gateway`` est enregistré à côté du leg historique ``rerank``,
        jamais à sa place. Un repli est marqué ``DEGRADED``, exactement comme le fait
        déjà le leg historique : servir l'ordre RRF quand on croyait servir un
        classement reranké EST une dégradation, et la trace doit le dire.
        """
        from apps.rag_core.services.rerank_gateway import (
            OUTCOME_INVALID_RESPONSE,
            OUTCOME_NOT_ATTEMPTED,
            OUTCOME_SUCCESS,
            OUTCOME_TIMEOUT,
            classify_outcome,
        )

        data = info if isinstance(info, dict) else {}
        outcome = classify_outcome(data)
        status = {
            OUTCOME_SUCCESS: RerankGatewayStatus.SUCCESS,
            OUTCOME_TIMEOUT: RerankGatewayStatus.TIMEOUT,
            OUTCOME_INVALID_RESPONSE: RerankGatewayStatus.INVALID_RESPONSE,
            OUTCOME_NOT_ATTEMPTED: RerankGatewayStatus.NOT_REQUESTED,
        }.get(outcome, RerankGatewayStatus.FAILURE)

        movements = data.get("movements")
        trace = RerankGatewayTrace(
            status=status,
            attempted=bool(data.get("attempted")),
            fallback=bool(data.get("fallback")),
            failure_policy=data.get("policy"),
            error_kind=data.get("error_kind"),
            model_logical=data.get("model_logical"),
            model_revision=data.get("model_revision"),
            pool_k=data.get("pool_k"),
            documents_submitted=int(data.get("documents") or 0),
            documents_returned=data.get("returned"),
            duration_ms=data.get("duration_ms"),
            movements=list(movements) if isinstance(movements, list) else [],
        )
        self.rerank_gateway = trace

        if status is RerankGatewayStatus.NOT_REQUESTED:
            leg_status = RetrievalLegStatus.NOT_REQUESTED
        elif status is RerankGatewayStatus.SUCCESS:
            leg_status = RetrievalLegStatus.SUCCEEDED
        else:
            leg_status = RetrievalLegStatus.DEGRADED
        self.record_leg(
            LEG_RERANK_GATEWAY,
            leg_status,
            candidate_count=int(trace.documents_returned or trace.documents_submitted or 0),
            duration_ms=int(trace.duration_ms) if trace.duration_ms is not None else None,
            fallback_used=trace.fallback,
            error_kind=trace.error_kind if leg_status is RetrievalLegStatus.DEGRADED else None,
        )
        if trace.fallback:
            self.add_fallback_reason("rerank_gateway_fallback")
        return trace

    def record_rerank_gateway_survival(self, final_chunk_ids: Any) -> None:
        """Combien de candidats rerankés atteignent le contexte final.

        Appelée APRÈS la sélection, avec les identifiants réellement transmis. Elle ne
        rejoue rien : le reranking a lieu avant la diversification, et c'est
        précisément l'écart entre les deux que cette valeur rend mesurable.

        Sans effet si aucun reranking Gateway n'a été consigné — la survie d'un
        ensemble vide n'est pas 0, elle n'existe pas.
        """
        if self.rerank_gateway is None or not self.rerank_gateway.movements:
            return
        finals = {str(x) for x in (final_chunk_ids or [])}
        self.rerank_gateway.survived_in_final_context = sum(
            1 for m in self.rerank_gateway.movements if str(m.get("chunk_id")) in finals
        )

    def record_leg(
        self,
        name: str,
        status: RetrievalLegStatus,
        *,
        candidate_count: int = 0,
        duration_ms: int | None = None,
        fallback_used: bool = False,
        error_kind: str | None = None,
    ) -> RetrievalLegTrace:
        leg = RetrievalLegTrace(
            name=name,
            status=status,
            candidate_count=int(candidate_count or 0),
            duration_ms=duration_ms,
            fallback_used=bool(fallback_used),
            error_kind=error_kind,
        )
        if name not in self._legs:
            self._order.append(name)
        self._legs[name] = leg
        return leg

    def add_fallback_reason(self, reason: str | None) -> None:
        if reason and reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    @property
    def degraded(self) -> bool:
        return any(leg.status in _DEGRADING_STATUSES for leg in self._legs.values())

    def _compute_outcome(self, *, source_count: int, error: bool) -> RetrievalOutcome:
        if error:
            return RetrievalOutcome.ERROR
        if self.degraded:
            return RetrievalOutcome.DEGRADED
        if source_count > 0:
            return RetrievalOutcome.SUCCESS
        return RetrievalOutcome.EMPTY_LEGITIMATE

    def build(
        self,
        *,
        final_candidate_count: int,
        source_count: int,
        error: bool = False,
    ) -> RetrievalTrace:
        legs = [self._legs[name] for name in self._order]
        return RetrievalTrace(
            schema_version=RETRIEVAL_TRACE_SCHEMA_VERSION,
            request_id=self.request_id,
            entrypoint=self.entrypoint,
            outcome=self._compute_outcome(source_count=source_count, error=error),
            degraded=self.degraded,
            legs=legs,
            fallback_reasons=list(self.fallback_reasons),
            final_candidate_count=int(final_candidate_count or 0),
            source_count=int(source_count or 0),
            corpus=self.corpus,
            rerank_gateway=self.rerank_gateway,
            admission_shadow=self.admission_shadow,
            candidate_trace=self.candidate_trace,
            visual_neighbors=self.visual_neighbors,
        )

"""CandidateRetriever — constitution du pool de candidats (Lot P1-B2).

Responsabilité UNIQUE : produire l'ensemble des candidats (FTS + vectoriel + corpus
métier) et leur FUSION, en instrumentant chaque leg (trace P1-A0) et en appliquant la
politique d'erreur typée (P1-A1). Ne fait PAS : reranking, prompt, answerability,
audit, génération, transport HTTP.

Entrées : ``QueryExecutionContext`` (request + prepared + policy + trace) + les fonctions
de recherche INJECTÉES (``fts_search``/``vector_search``) — l'injection depuis
``query_rag`` préserve les points de monkeypatch existants
(``orchestrator.search_chunks_hybrid`` / ``...vector``).
Sortie : la liste FUSIONNÉE ``merged`` (référence) ; ``ctx.candidates`` (CandidateSet)
est renseigné en effet de bord.

Le code a été déplacé VERBATIM depuis ``orchestrator.query_rag`` (P1-B2). Il a depuis
gagné UN point de variation, explicite et réversible : l'assemblage bi-source a deux
modes, gouvernés par ``RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED``.

- OFF (défaut) — ``_merge_bi_source_legacy`` : le privé est fusionné (RRF) d'abord, le
  corpus est ajouté ensuite. Les candidats corpus n'ont donc pas de ``fusion_score``, et
  la diversification — qui classe sur ``(fusion_score, score)`` — les range derrière tout
  candidat privé, quelle que soit leur pertinence.
- ON — ``merge_rrf`` : les quatre listes classées (privé lexical, privé vectoriel, corpus
  lexical, corpus vectoriel) votent dans la MÊME fusion de rangs. Un rang est sans
  dimension : c'est ce qui rend un ``ts_rank`` corpus comparable à un ``ts_rank`` privé.

Aucun quota de provenance dans un mode ni dans l'autre : les boosts restent la seule
préférence, appliqués à une étape unique (après la fusion, avant la diversification).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from django.conf import settings

from apps.llm_router.embeddings import embed_texts
from apps.rag_core.domain.provenance import (
    SOURCE_TYPE_DOMAIN_CORPUS as _SOURCE_TYPE_DOMAIN_CORPUS,
    SOURCE_TYPE_WORKSPACE as _SOURCE_TYPE_WORKSPACE,
)

from .contracts import CandidateSet, QueryExecutionContext
from .errors import (
    RetrievalError,
    RetrievalInvariantError,
    RetrievalLegUnavailable,
    raise_if_programming_error,
)
from .hygiene import clean_candidates
from .merge import MODALITY_TEXT, MODALITY_VECTOR, FusionLeg, merge_candidates, merge_hybrid, merge_rrf
from .trace import (
    LEG_BUSINESS_CORPUS,
    LEG_FTS,
    LEG_VECTOR,
    CorpusLegModalityStatus,
    CorpusLegRecorder,
    RetrievalLegStatus,
    RetrievalTraceBuilder,
    is_corpus_candidate,
    safe_error_kind,
)

logger = logging.getLogger(__name__)

# Énumération CANONIQUE : importée, jamais redéfinie (cf. domain/provenance.py).
SOURCE_TYPE_WORKSPACE = _SOURCE_TYPE_WORKSPACE
SOURCE_TYPE_DOMAIN_CORPUS = _SOURCE_TYPE_DOMAIN_CORPUS


# ---------------------------------------------------------------------------
# Helpers bi-source (déplacés depuis orchestrator.py — zéro référence externe)
# ---------------------------------------------------------------------------
def _tag_private_chunk(ch: dict[str, Any], *, workspace_id, rag_space_id) -> dict[str, Any]:
    """Marque un chunk privé : ``source_type=workspace_document`` + provenance."""
    if not isinstance(ch, dict):
        return ch
    ch.setdefault("source_type", SOURCE_TYPE_WORKSPACE)
    src = ch.get("source")
    if isinstance(src, dict):
        src.setdefault("source_type", SOURCE_TYPE_WORKSPACE)
        src.setdefault("workspace_id", str(workspace_id) if workspace_id is not None else None)
        src.setdefault("rag_space_id", str(rag_space_id) if rag_space_id is not None else None)
    return ch


def _apply_boost(ch: dict[str, Any], boost: float) -> dict[str, Any]:
    if boost and boost != 1.0:
        try:
            ch["score"] = float(ch.get("score") or 0.0) * float(boost)
        except Exception as exc:
            # P1-A1 : le boost modifie le SCORE donc l'ORDRE. Un bug (ex. `ch` non
            # dict -> TypeError) se propage ; une donnée de score invalide reste
            # best-effort mais loggée (plus de `pass` silencieux).
            raise_if_programming_error(exc, leg="ranking_boost")
            logger.warning("ranking boost skipped (invalid score) error_kind=%s", type(exc).__name__)
    return ch


#: Poids du vote des legs PRIVÉS. Constante : ce lot n'arbitre que le poids corpus, et
#: garder le privé à 1.0 rend α directement lisible comme « part de voix du corpus ».
WORKSPACE_RRF_LEG_WEIGHT = 1.0


def unified_fusion_enabled() -> bool:
    """Flag de la fusion bi-source unifiée (Lot 4).

    OFF par défaut : le chemin historique reste actif à l'octet près. ON, les
    candidats corpus votent dans la MÊME fusion de rangs que les candidats privés.
    Rollback = repasser le flag à 0, sans migration ni réindexation.
    """
    return bool(getattr(settings, "RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED", False))


def calibrated_admission_enabled() -> bool:
    """Flag de la porte d'admission calibrée. OFF par défaut, partout.

    Admission et pondération sont deux mécanismes DISTINCTS, et l'ordre importe :
    l'admission décide si le corpus participe, α décide du poids de son vote une fois
    admis. Un corpus rejeté n'a pas de poids ; un corpus admis n'a aucune place réservée.
    """
    return bool(getattr(settings, "RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED", False))


def corpus_admission_probability_threshold() -> float:
    """Seuil sur la PROBABILITÉ calibrée — jamais sur le score brut.

    Valeur brute, non corrigée : ``RetrievalPolicy.__post_init__`` refuse ce qui est
    hors [0, 1] ou non fini. Confondre ce seuil avec le seuil brut observé (≈ 0,645 sur
    ``corpus_vector.top10_mean`` pour un corpus donné) reviendrait à comparer une
    probabilité à une similarité.
    """
    raw = getattr(settings, "RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD", 0.65)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("nan")


def corpus_rrf_leg_weight() -> float:
    """Poids α du vote des legs corpus. Valeur brute, NON corrigée.

    La validation est faite par ``RetrievalPolicy.__post_init__`` : une valeur hors
    ]0, 1] doit faire échouer la requête de façon explicite, pas être ramenée en
    silence à 1.0 — ce qui reviendrait à exécuter la fusion non pondérée en croyant
    l'avoir atténuée. On rend donc ici ce que la configuration dit, tel quel.
    """
    raw = getattr(settings, "RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT", 1.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Non convertible : on laisse passer une valeur que la politique refusera avec
        # un message clair, plutôt que d'inventer un repli ici.
        return float("nan")


def _apply_source_boosts(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Applique les boosts de provenance — étape UNIQUE et explicitement située.

    Après la fusion, donc sur des scores déjà comparables ; avant la diversification,
    donc en amont de la sélection. Le boost agit sur ``score`` (seuils, confiance,
    départage), jamais sur ``fusion_score`` : c'est le rang qui classe.
    """
    ws_boost = float(getattr(settings, "RAG_WORKSPACE_SOURCE_BOOST", 1.10) or 1.0)
    corpus_boost = float(getattr(settings, "RAG_DOMAIN_CORPUS_SOURCE_BOOST", 1.0) or 1.0)
    return [
        _apply_boost(c, corpus_boost if is_corpus_candidate(c) else ws_boost)
        for c in (chunks or [])
        if isinstance(c, dict)
    ]


def _admit_corpus_legs(legs, *, recorder, enabled: bool, threshold: float):
    """Applique la porte d'admission calibrée aux legs corpus. Retourne l'issue.

    Le leg vectoriel est la CONDITION du signal : s'il n'a pas tourné proprement, aucune
    décision n'est possible et le corpus est rejeté — y compris son leg lexical. Laisser
    le lexical entrer seul contournerait la porte par le leg le moins informatif
    (``corpus_fts`` mesuré ANTI-corrélé à la pertinence, ROC-AUC 0.42).

    Conséquence assumée et documentée : activer la porte alors que le vectoriel est
    désactivé (``RAG_HYBRID_ENABLED=0``) retire le corpus métier de la fusion. C'est un
    refus visible, pas une dégradation muette.

    Le signal est calculé sur le leg vectoriel BRUT (``vector_raw``), avant le filtrage
    au seuil ``RAG_MIN_SCORE`` : sinon un corpus dont toutes les similarités sont basses
    — précisément le cas que la porte doit rattraper — n'aurait plus aucun candidat,
    donc plus de signal, et « faible » deviendrait « inconnu ».
    """
    from apps.corpus.services.admission import (
        apply_admission,
        corpus_ids_in_legs,
        evaluate_admission,
    )
    from apps.rag_core.domain.corpus_admission import AdmissionDecision, REASON_FEATURE_DISABLED

    if not enabled:
        return apply_admission(
            legs, {"*": AdmissionDecision(admitted=True, reason=REASON_FEATURE_DISABLED)}
        )

    vector_healthy = recorder.vector_status is CorpusLegModalityStatus.OK or (
        recorder.vector_status is CorpusLegModalityStatus.EMPTY
    )
    decisions = evaluate_admission(
        corpus_ids=corpus_ids_in_legs(legs),
        vector_candidates=getattr(legs, "vector_raw", None) or legs.vector,
        vector_leg_healthy=vector_healthy,
        enabled=True,
        threshold=threshold,
        embedding_dimension=int(getattr(settings, "RAG_EMBEDDING_DIM", 768)),
    )
    return apply_admission(legs, decisions)


def _retrieve_corpus_legs(
    *,
    workspace_id,
    queries_for_search: list[str],
    top_k: int,
    vector_top_k: int,
    hybrid_enabled: bool,
    min_text_rank: float,
    admission_enabled: bool,
    admission_threshold: float,
    trace: "RetrievalTraceBuilder | None",
    rag_space=None,
):
    """Exécute le leg corpus métier et reporte son état. Ne classe rien.

    Renvoie les candidats par modalité (``text``/``vector``) et leur vue fusionnée
    historique (``merged``). Des legs VIDES couvrent les trois cas où le corpus ne
    contribue pas — non autorisé, sans correspondance, ou panne de dépendance absorbée :
    l'appelant fusionne alors le privé seul, comme auparavant. Le statut, lui, distingue
    ces cas dans la trace.

    P1-A0 : le leg est instrumenté via un ``CorpusLegRecorder`` passé au service
    corpus (collaboration explicite, testée — pas de variable globale).
    """
    from apps.corpus.services.retrieval import CorpusCandidateLegs, retrieve_corpus_candidate_legs

    recorder = CorpusLegRecorder()

    def _record_corpus_leg(status: RetrievalLegStatus, *, error_kind: str | None = None) -> None:
        if trace is None:
            return
        trace.record_leg(
            LEG_BUSINESS_CORPUS,
            status,
            candidate_count=recorder.candidate_count,
            error_kind=error_kind,
        )
        trace.record_corpus_state(
            recorder,
            unified_fusion=unified_fusion_enabled(),
            corpus_leg_weight=corpus_rrf_leg_weight(),
            workspace_leg_weight=WORKSPACE_RRF_LEG_WEIGHT,
        )
        if status is RetrievalLegStatus.DEGRADED:
            trace.add_fallback_reason("corpus_error")

    try:
        legs = retrieve_corpus_candidate_legs(
            workspace_id=workspace_id,
            queries=queries_for_search or [],
            top_k=top_k,
            vector_top_k=vector_top_k,
            hybrid_enabled=hybrid_enabled,
            min_text_rank=min_text_rank,
            recorder=recorder,
            rag_space=rag_space,
        )
    except RetrievalError:
        # P1-A1 : erreur déjà TYPÉE par le service corpus (bug/invariant) — on la
        # propage telle quelle (FAIL CLOSED), on ne masque pas par un repli privé.
        _record_corpus_leg(RetrievalLegStatus.FAILED, error_kind="RetrievalInvariantError")
        raise
    except Exception as exc:
        logger.exception("corpus retrieval failed; falling back to private-only")
        # Un BUG de programmation se propage ; une panne de DÉPENDANCE attendue est
        # dégradable — le corpus métier est OPTIONNEL, le repli privé est légitime.
        raise_if_programming_error(exc, leg=LEG_BUSINESS_CORPUS)
        _record_corpus_leg(RetrievalLegStatus.DEGRADED, error_kind=safe_error_kind(exc))
        # Aucune décision d'admission n'a pu être prise : la trace doit le dire, plutôt
        # que de laisser croire à une admission par défaut.
        legs = CorpusCandidateLegs()
    else:
        # Statut du leg d'après ce que le service a réellement observé (demandé/vide/
        # dégradé). Reporté même quand des candidats existent : la dégradation d'une passe
        # ne doit pas être avalée par le succès d'une autre.
        _record_corpus_leg(recorder.leg_status(), error_kind=recorder.error_kind)

    # l'identité du magasin MÉTIER remonte du recorder vers la trace
    # principale. Un workspace peut autoriser plusieurs corpus aux modèles différents :
    # la liste les expose tous plutôt que d'en élire un.
    if trace is not None and getattr(recorder, "embedding_groups", None):
        _groupes = recorder.embedding_groups
        _premier = _groupes[0]
        trace.record_store_embedding_identity(
            "business_corpus",
            provider=_premier.get("provider"),
            model=_premier.get("model"),
            # Absente quand le leg vectoriel n'a pas tourné (hybride éteint,
            # configuration refusée) : aucune question n'a alors été encodée, donc
            # aucune dimension n'a été observée. Absente plutôt que devinée.
            dimension=_premier.get("dimension"),
            # `pipeline_family` n'existe PAS pour le corpus métier : la table
            # `domain_corpus_chunk` ne porte pas cette colonne, et l'identité
            # gouvernée est propre aux espaces privés. Absente, donc, plutôt que nulle.
            groups=_groupes if len(_groupes) > 1 else None,
        )

    # Porte d'admission APRÈS l'exécution des legs (elle a besoin du signal) et AVANT
    # toute fusion (un corpus rejeté ne doit pas voter, même faiblement).
    outcome = _admit_corpus_legs(
        legs, recorder=recorder, enabled=admission_enabled, threshold=admission_threshold,
    )
    if trace is not None:
        trace.record_corpus_admission(
            outcome, enabled=admission_enabled, threshold=admission_threshold,
        )
    return legs


def _merge_bi_source_legacy(
    *,
    private_chunks: list[dict[str, Any]],
    corpus_chunks: list[dict[str, Any]],
    workspace_id,
    rag_space_id,
    top_k: int,
    vector_top_k: int,
) -> list[dict[str, Any]]:
    """Assemblage HISTORIQUE : typage, boosts, puis fusion par score brut.

    Conservé à l'identique pour que la désactivation du flag de fusion unifiée soit un
    rollback réel, et non une seconde implémentation « équivalente ».

    Limite connue et documentée : les candidats corpus arrivent APRÈS ``merge_hybrid``
    et n'ont donc pas de ``fusion_score``. La diversification, qui classe sur
    ``(fusion_score, score)``, les range derrière tous les candidats privés — c'est le
    défaut D2, que seule la fusion unifiée corrige.
    """
    ws_boost = float(getattr(settings, "RAG_WORKSPACE_SOURCE_BOOST", 1.10) or 1.0)
    corpus_boost = float(getattr(settings, "RAG_DOMAIN_CORPUS_SOURCE_BOOST", 1.0) or 1.0)

    private = [
        _apply_boost(_tag_private_chunk(c, workspace_id=workspace_id, rag_space_id=rag_space_id), ws_boost)
        for c in (private_chunks or [])
        if isinstance(c, dict)
    ]
    if not corpus_chunks:
        return private

    corpus = [_apply_boost(c, corpus_boost) for c in corpus_chunks if isinstance(c, dict)]
    return merge_candidates(a=private, b=corpus, top_k=max(top_k, vector_top_k) + len(corpus))


def _families_present(**kwargs: Any) -> dict[Any, int]:
    """Familles de pipeline présentes dans le corpus privé de l'espace.

    Import PARESSEUX, comme les autres accès SQL de ce module : il garde le point de
    monkeypatch sur le dépôt et évite d'importer la couche de persistance au chargement
    du service. En cas de défaillance, on rend un inventaire VIDE — le diagnostic
    n'aboutit pas, la recherche reste vide, mais on n'invente pas une dérive et on ne
    transforme pas une panne de diagnostic en panne de recherche.
    """
    from apps.rag_core.repositories.chunk_repository import pipeline_families_present

    try:
        return pipeline_families_present(**kwargs)
    except Exception:
        logger.exception("retrieval_family_probe_failed")
        return {}


# ---------------------------------------------------------------------------
# Composant principal
# ---------------------------------------------------------------------------
def retrieve_candidates(
    ctx: QueryExecutionContext,
    *,
    fts_search: Callable[..., list[dict[str, Any]]] | None,
    vector_search: Callable[..., list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Constitue et FUSIONNE le pool de candidats. Renseigne ``ctx.candidates``.

    Toutes les valeurs proviennent des DTO threadés (``request``/``prepared``/
    ``policy``) — les legs ne relisent plus les settings déjà résolus.
    """
    request = ctx.request
    prepared = ctx.prepared
    policy = ctx.policy
    trace = ctx.trace
    assert prepared is not None and policy is not None  # garanti par query_rag

    question = request.question
    rag_space_id = request.rag_space_id
    rag_space = request.rag_space
    rid = trace.request_id or ""
    queries_for_search = list(prepared.retrieval_queries)

    pool_k = policy.candidate_k
    vector_pool_k = max(policy.vector_top_k, policy.candidate_k)
    effective_min_score = policy.min_score
    min_text_rank = policy.min_text_rank
    lexical_weight = policy.lexical_weight

    # 1) FTS multi-pass (required)
    if not callable(fts_search):
        raise ImportError("chunk_repository.search_chunks_hybrid is missing (cannot retrieve sources)")

    text_chunks: list[dict[str, Any]] = []
    _fts_start = time.perf_counter()
    _fts_error_kind: str | None = None
    _fts_dependency_failed = False
    _fts_exc: Exception | None = None
    for qx in (queries_for_search or [question]):
        try:
            t = fts_search(
                rag_space_id=rag_space_id,
                question=qx,
                top_k=pool_k,
                min_text_rank=min_text_rank,
            )
            # Liste FTS mono-modalité (score = ts_rank) : on filtre au seuil
            # LEXICAL, pas au seuil vectoriel RAG_MIN_SCORE (sinon on jette
            # des résultats lexicaux pertinents à faible ts_rank avant fusion).
            t = clean_candidates(t, min_score=min_text_rank)
            text_chunks = merge_candidates(a=text_chunks, b=t, top_k=pool_k)
        except Exception as exc:
            _fts_error_kind = safe_error_kind(exc)
            # La question de l'utilisateur n'est PLUS journalisée en clair
            # (constat P0-C #13) : seule sa longueur est conservée.
            logger.exception("FTS retrieval failed (query_chars=%d)", len(qx or ""))
            logger.error(
                "retrieval_leg_degraded",
                extra={
                    "request_id": rid,
                    "entrypoint": trace.entrypoint,
                    "rag_space_id": str(rag_space_id),
                    "leg": LEG_FTS,
                    "error_kind": _fts_error_kind,
                    "policy": "error",
                },
            )
            # P1-A1 : un BUG de programmation (TypeError, AttributeError, SQL
            # ProgrammingError, contrat repo violé...) ne doit JAMAIS devenir un
            # no_answer -> propagation immédiate (FAIL CLOSED).
            raise_if_programming_error(exc, leg=LEG_FTS)
            # Panne de DÉPENDANCE attendue (backend FTS indisponible) : inutile de
            # retenter les autres passes. La décision error/degraded dépend de la
            # présence d'un fallback vectoriel VALIDE (tranchée après le leg vectoriel).
            _fts_dependency_failed = True
            _fts_exc = exc
            break

    chunks = text_chunks

    if _fts_error_kind is not None:
        _fts_status = RetrievalLegStatus.DEGRADED
    elif text_chunks:
        _fts_status = RetrievalLegStatus.SUCCEEDED
    else:
        _fts_status = RetrievalLegStatus.EMPTY
    trace.record_leg(
        LEG_FTS,
        _fts_status,
        candidate_count=len(text_chunks),
        duration_ms=int((time.perf_counter() - _fts_start) * 1000),
        error_kind=_fts_error_kind,
    )
    if _fts_error_kind is not None:
        trace.add_fallback_reason("fts_error")

    # 2) Vector multi-pass (optional)
    hybrid_enabled = policy.hybrid_enabled
    vector_available = callable(vector_search)
    # Fallback vectoriel VALIDE = leg vectoriel demandé ET exécuté sans erreur.
    # Sert à trancher le sort d'un FTS (requis) tombé en panne de dépendance.
    _vector_ran_clean = False
    # Déclaré ICI : en fusion unifiée, la liste vectorielle privée doit rester
    # accessible APRÈS le bloc conditionnel pour voter dans la fusion commune.
    vector_chunks: list[dict[str, Any]] = []
    unified = bool(policy.unified_bi_source_fusion)

    if not hybrid_enabled:
        # Désactivé par configuration : ce n'est PAS une dégradation.
        trace.record_leg(LEG_VECTOR, RetrievalLegStatus.DISABLED)
    elif not vector_available:
        # Demandé mais fonction absente (déploiement) : dégradation observable.
        logger.warning("RAG_HYBRID_ENABLED=True but search_chunks_vector is missing -> vector disabled")
        trace.record_leg(
            LEG_VECTOR, RetrievalLegStatus.DEGRADED, error_kind="MissingVectorFunction",
        )
        trace.add_fallback_reason("vector_unavailable")

    if hybrid_enabled and vector_available:
        _vector_start = time.perf_counter()
        _vector_error_kind: str | None = None
        # La question DOIT être encodée avec le même provider/modèle d'embeddings
        # que les documents de l'espace, sinon les vecteurs sont incomparables.
        from apps.llm_router.embedding_pipeline import TASK_QUERY
        from apps.llm_router.egress import LlmExecutionContext
        from apps.llm_router.embeddings import (
            embed_texts_with_identity,
            resolve_space_embedding,
            space_is_governed,
        )

        _emb_provider, _emb_model = (
            resolve_space_embedding(rag_space) if rag_space is not None else (None, None)
        )
        # Lot 3 : un espace GOUVERNÉ (provider `ai_gateway`) filtre en plus sur la
        # famille de pipeline. Un espace historique garde son comportement exact.
        _governed = rag_space is not None and space_is_governed(rag_space)
        # Politique d'egress de l'espace, résolue UNE fois hors de la boucle de
        # reformulations. Le P0 corrigé ne concerne pas que l'indexation : une
        # QUESTION utilisateur envoyée à un provider externe sort du périmètre
        # exactement comme un document.
        #
        # Un refus lève et sera capté par le `except Exception` ci-dessous, qui
        # DÉGRADE le leg vectoriel (comportement historique de ce bloc pour toute
        # défaillance d'embedding). Le refus reste tracé : `safe_error_kind` porte
        # `ExternalEgressDenied` dans la trace de retrieval, et l'événement d'egress
        # `status=denied` est écrit. Rendre ce cas FATAL serait un changement de
        # comportement du retrieval, hors périmètre de ce lot.
        _emb_ctx = (
            LlmExecutionContext.for_rag_space(rag_space, operation="embedding.query")
            if rag_space is not None
            else None
        )
        try:
            for qx in (queries_for_search or [question]):
                emb_batch = embed_texts_with_identity(
                    [qx],
                    dim=settings.RAG_EMBEDDING_DIM,
                    provider=_emb_provider,
                    model=_emb_model,
                    # `query` et non `document` : le modèle gouverné encode une
                    # question différemment d'un passage, et l'identité rendue le dit.
                    task=TASK_QUERY,
                    rag_space_id=rag_space_id,
                    context=_emb_ctx,
                )
                emb = emb_batch.vectors[0]
                _pipeline_family = None
                if _governed:
                    # La famille est DÉCOUPÉE de l'identité rendue par la Gateway ;
                    # elle n'est jamais reconstruite à partir du modèle. Absente, on
                    # refuse de chercher : une recherche gouvernée sans filtre
                    # comparerait la question à des chunks d'un autre pipeline.
                    _pipeline_family = emb_batch.family
                    if not _pipeline_family:
                        # `RetrievalInvariantError` et non une erreur générique : le
                        # `except Exception` en aval DÉGRADE (vector_chunks = []) tout
                        # ce qui n'est pas classé comme invariant brisé. Une recherche
                        # gouvernée qui perd son filtre ne doit pas devenir un repli
                        # lexical silencieux — elle doit échouer (FAIL CLOSED).
                        raise RetrievalInvariantError(
                            "espace gouverné: famille de pipeline absente de la réponse "
                            "d'embeddings (refus de comparer des vecteurs non identifiés)",
                            leg=LEG_VECTOR,
                        )
                # identité du magasin PRIVÉ, telle qu'employée à l'instant
                # même pour filtrer — provider et modèle bornent la recherche, la
                # famille vient de la Gateway. Aucune reconstruction, aucune requête.
                if trace is not None:
                    trace.record_store_embedding_identity(
                        "private_ragspace",
                        provider=_emb_provider,
                        model=_emb_model,
                        pipeline_family=_pipeline_family,
                        dimension=len(emb) if emb is not None else None,
                    )
                v = vector_search(
                    rag_space_id=rag_space_id,
                    query_embedding=emb,
                    top_k=vector_pool_k,
                    # P1-D : ne comparer qu'aux documents encodés avec le modèle
                    # COURANT de l'espace (les incohérents sont exclus du vectoriel).
                    embedding_provider=_emb_provider,
                    embedding_model=_emb_model,
                    # Lot 3 : None hors mode gouverné -> requête inchangée.
                    pipeline_family=_pipeline_family,
                )
                v = clean_candidates(v, min_score=effective_min_score)
                vector_chunks = merge_candidates(a=vector_chunks, b=v, top_k=vector_pool_k)
        except Exception as exc:
            _vector_error_kind = safe_error_kind(exc)
            logger.exception("Vector retrieval disabled due to embeddings/vector error")
            logger.error(
                "retrieval_leg_degraded",
                extra={
                    "request_id": rid,
                    "entrypoint": trace.entrypoint,
                    "rag_space_id": str(rag_space_id),
                    "leg": LEG_VECTOR,
                    "error_kind": _vector_error_kind,
                    "policy": "error",
                },
            )
            # P1-A1 : un BUG (TypeError/AttributeError/ProgrammingError SQL) ou une
            # erreur de CONFIGURATION du vectoriel ne doit pas devenir un repli
            # lexical silencieux -> propagation (FAIL CLOSED). Une panne de
            # DÉPENDANCE attendue (provider indispo/timeout) reste dégradable : le
            # FTS demeure le leg requis.
            raise_if_programming_error(exc, leg=LEG_VECTOR)
            vector_chunks = []

        vector_chunks = clean_candidates(vector_chunks, min_score=effective_min_score)

        # Dérive de FAMILLE (23/08/2026). La garde ci-dessus refuse une famille
        # ABSENTE ; elle ne dit rien d'une famille PRÉSENTE mais différente de celle
        # du corpus. Or la Gateway peut changer les poids d'un modèle sans changer son
        # nom : le `pipeline_id` porte alors un autre digest, le filtre SQL ne trouve
        # plus rien, et un vectoriel VIDE est indistinguable d'un espace sans réponse.
        # Le silence était total — recall zéro, aucun signal.
        #
        # Le diagnostic n'est déclenché que sur le cas suspect (gouverné, sans erreur,
        # zéro candidat) : il ne coûte rien tant que la recherche trouve quelque chose.
        # Et il ne conclut que si des chunks comparables existent VRAIMENT sous une
        # autre famille — un espace vide reste un espace vide, pas une dérive.
        if _governed and _vector_error_kind is None and not vector_chunks and _pipeline_family:
            _familles = _families_present(
                rag_space_id=rag_space_id,
                embedding_provider=_emb_provider,
                embedding_model=_emb_model,
            )
            _autres = {f: n for f, n in _familles.items() if f and f != _pipeline_family}
            if _autres and _pipeline_family not in _familles:
                logger.error(
                    "retrieval_pipeline_family_drift",
                    extra={
                        "request_id": rid,
                        "rag_space_id": str(rag_space_id),
                        "query_family": _pipeline_family,
                        "corpus_families": {str(f): n for f, n in _autres.items()},
                    },
                )
                # FAIL CLOSED, comme la famille absente : rendre zéro source ici
                # produirait une réponse « je ne sais pas » parfaitement crédible sur
                # un corpus qui contient la réponse. Un mensonge plausible est pire
                # qu'une erreur.
                raise RetrievalInvariantError(
                    "espace gouverné: la famille de pipeline de la question "
                    f"({_pipeline_family}) n'existe dans aucun fragment comparable "
                    f"(présentes : {sorted(str(f) for f in _autres)}) — corpus à "
                    "réencoder ou modèle changé côté Gateway",
                    leg=LEG_VECTOR,
                )

        if _vector_error_kind is not None:
            _vector_status = RetrievalLegStatus.DEGRADED
            trace.add_fallback_reason("vector_error")
        elif vector_chunks:
            _vector_status = RetrievalLegStatus.SUCCEEDED
        else:
            _vector_status = RetrievalLegStatus.EMPTY
        _vector_ran_clean = _vector_error_kind is None
        trace.record_leg(
            LEG_VECTOR,
            _vector_status,
            candidate_count=len(vector_chunks),
            duration_ms=int((time.perf_counter() - _vector_start) * 1000),
            error_kind=_vector_error_kind,
        )
        if not unified:
            # Fusion RRF sur le POOL élargi : la troncature au top_k n'a lieu qu'APRÈS
            # diversification. La formule RRF et les rangs sont inchangés.
            #
            # En fusion UNIFIÉE, cette fusion n'a pas lieu ici : elle est repoussée
            # après le leg corpus, pour que les quatre listes votent ENSEMBLE. Fusionner
            # d'abord le privé puis « ajouter » le corpus est exactement ce qui privait
            # les candidats corpus de `fusion_score`.
            chunks = merge_hybrid(
                text_chunks=text_chunks, vector_chunks=vector_chunks, top_k=pool_k,
                lexical_weight=lexical_weight, vector_weight=1.0,
            )
            chunks = clean_candidates(chunks, min_score=effective_min_score)

    # P1-A1 : le FTS est le leg REQUIS. S'il est tombé en panne de DÉPENDANCE et
    # qu'aucun fallback vectoriel VALIDE n'a tourné (vectoriel désactivé, absent, ou
    # lui-même en erreur), on refuse de présenter un `no_answer` trompeur : on
    # remonte une erreur retrieval contrôlée (FAIL CLOSED -> outcome=error, audit
    # status=error, 500/SSE error à la frontière). Un bug FTS a déjà été propagé
    # plus haut (`raise_if_programming_error`).
    if _fts_dependency_failed and not _vector_ran_clean:
        raise RetrievalLegUnavailable(
            "FTS (leg requis) indisponible et aucun fallback vectoriel valide",
            leg=LEG_FTS,
        ) from _fts_exc

    # 2bis) Bi-source : enrichit avec les corpus métier AUTORISÉS du workspace.
    # Typage des sources (workspace_document / domain_corpus) + boosts de provenance.
    # No-op (chunks privés seulement) si aucun corpus n'est associé au workspace.
    workspace_id = getattr(rag_space, "workspace_id", None)
    corpus_legs = _retrieve_corpus_legs(
        workspace_id=workspace_id,
        queries_for_search=queries_for_search or [question],
        top_k=pool_k,
        vector_top_k=vector_pool_k,
        hybrid_enabled=(hybrid_enabled and vector_available),
        min_text_rank=min_text_rank,
        admission_enabled=bool(policy.calibrated_corpus_admission),
        admission_threshold=float(policy.corpus_admission_probability_threshold),
        trace=trace,
        rag_space=rag_space,
    )

    if unified:
        # Les QUATRE listes classées votent dans la même fusion de rangs. Un rang est
        # sans dimension : c'est ce qui rend un ts_rank corpus comparable à un ts_rank
        # privé — et ce que l'ancien assemblage ne faisait pas.
        private_text = [
            _tag_private_chunk(c, workspace_id=workspace_id, rag_space_id=rag_space_id)
            for c in text_chunks
            if isinstance(c, dict)
        ]
        private_vector = [
            _tag_private_chunk(c, workspace_id=workspace_id, rag_space_id=rag_space_id)
            for c in vector_chunks
            if isinstance(c, dict)
        ]
        fusion_pool_k = max(pool_k, vector_pool_k) + len(corpus_legs.merged)
        # α atténue le VOTE des deux legs corpus, sans réserver aucune place et sans
        # toucher aux rangs privés. La pondération lexicale conditionnelle (P1.5) reste
        # multipliée par α côté corpus : les deux mécanismes sont indépendants et se
        # composent, comme n'importe quels poids de legs.
        alpha = float(policy.corpus_rrf_leg_weight)
        chunks = merge_rrf(
            legs=[
                FusionLeg(MODALITY_TEXT, private_text,
                          weight=lexical_weight * WORKSPACE_RRF_LEG_WEIGHT, origin="private"),
                FusionLeg(MODALITY_VECTOR, private_vector,
                          weight=WORKSPACE_RRF_LEG_WEIGHT, origin="private"),
                FusionLeg(MODALITY_TEXT, corpus_legs.text,
                          weight=lexical_weight * alpha, origin="business_corpus"),
                FusionLeg(MODALITY_VECTOR, corpus_legs.vector,
                          weight=alpha, origin="business_corpus"),
            ],
            top_k=fusion_pool_k,
        )
        chunks = clean_candidates(chunks, min_score=effective_min_score)
        chunks = _apply_source_boosts(chunks)
    else:
        chunks = _merge_bi_source_legacy(
            private_chunks=chunks,
            corpus_chunks=corpus_legs.merged,
            workspace_id=workspace_id,
            rag_space_id=rag_space_id,
            top_k=pool_k,
            vector_top_k=vector_pool_k,
        )

    # --- Contrat CANDIDATS (P1-B1, réellement consommé en P1-B2) ---------------
    # Vue agrégée par RÉFÉRENCE (aucune copie de texte). `merged` = pool après
    # fusion + bi-source (les candidats corpus y sont fondus). N'altère aucun dict.
    ctx.candidates = CandidateSet(
        fts=text_chunks,
        vector=vector_chunks,
        corpus=corpus_legs.merged,
        merged=chunks,
    )
    return chunks

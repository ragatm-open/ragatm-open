# backend/apps/rag_core/services/query/orchestrator.py
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date
from typing import Any, Callable, Optional

from django.conf import settings

from apps.audit.models import RagQueryLog
from apps.rag_core.domain.policies import RagPolicy, should_answer  # noqa: F401 (legacy)
from apps.rag_core.services.query.admission import admit, expected_family_for
from apps.rag_core.domain.ranking import tie_break_key
from apps.rag_core.models import RagSpace
from apps.rag_core.services.rerank import rerank_chunks_observed

from .audit import (
    SOURCES_SCHEMA_VERSION,
    extract_request_id,
    normalize_request_id,
    normalize_sources_for_audit,
)
from .diversify import (
    build_diversity_group,
    diversify_by_document,
    diversify_ranked_results,
)
from .heuristics import (
    detect_comparative_intent,
    is_explanatory_question,
    is_inventory_question,
)
from .lowconf import LOWCONF_ENABLED, LOWCONF_MIN_SOURCES, low_conf_threshold
from .passage_shape import apply_structural_penalty
from .rewrite import build_queries_for_search
from apps.llm_router.egress import email_scoped as _email_scoped
from .strong_signals import (
    StrongQuerySignals,
    apply_temporal_operator,
    canonical_date_terms,
    extract_signals,
    extract_temporal_intent,
)
from .space_policy import min_score_audit, resolve_min_score
from .temporal_candidates import (
    TEMPORAL_LEG,
    fetch_temporal_candidates,
    temporal_leg_enabled,
)
from .settings import setting, setting_bool, setting_float, setting_int

#: Le mode « news » n'est PAS branché : `RAG_ENABLE_NEWS_HEURISTIC` était lu par
#: `getattr(settings, ...)` sans qu'aucun settings ne le projette — le poser n'a
#: jamais rien activé. La constante rend cet état explicite. Les branches `news`
#: qui suivent sont donc inatteignables ; elles sont conservées telles quelles
#: (leur retrait est une refonte de l'orchestrateur, hors du présent lot) mais
#: aucune configuration ne peut plus laisser croire qu'on les allume.
NEWS_HEURISTIC_ENABLED = False
NEWS_TOP_K = 0
NEWS_VECTOR_TOP_K = 0
from .candidate_reranker import rank_candidates
from .candidate_retriever import (
    calibrated_admission_enabled,
    corpus_admission_probability_threshold,
    corpus_rrf_leg_weight,
    retrieve_candidates,
    unified_fusion_enabled,
)
from .prompt_builder import build_prompt_stage
from .visual_neighbors import promote_same_page_visuals
from .contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
    QueryResult,
    RetrievalPolicy,
)
from .errors import (
    RetrievalInvariantError,
    raise_if_programming_error,
)
from .trace import (
    LEG_TEMPORAL,
    RetrievalLegStatus,
    RetrievalTraceBuilder,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Safe repository imports (avoid ImportError at Django boot)
# ---------------------------------------------------------------------

try:
    from apps.rag_core.repositories import chunk_repository as _chunk_repo
except Exception as e:
    _chunk_repo = None
    logger.exception("Failed to import apps.rag_core.repositories.chunk_repository: %s", e)

search_chunks_hybrid: Callable[..., list[dict[str, Any]]] | None = None
search_chunks_vector: Callable[..., list[dict[str, Any]]] | None = None

if _chunk_repo is not None:
    search_chunks_hybrid = getattr(_chunk_repo, "search_chunks_hybrid", None)
    search_chunks_vector = getattr(_chunk_repo, "search_chunks_vector", None)

# ---------------------------------------------------------------------
# Helpers: document_date enrichment (from Document table)
# ---------------------------------------------------------------------


def _model_has_field(model: Any, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = _ISO_DATE_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


_NEWS_Q_RE = re.compile(
    r"\b("
    r"news|actu|actualit(?:é|e)s?|récent(?:e|es|s)?|dernier(?:e|es|s)?|"
    r"à la une|quoi de neuf|ce mois-ci|cette semaine|mise à jour|updates?"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_news_question(q: str) -> bool:
    s = (q or "").strip()
    if not s:
        return False
    return bool(_NEWS_Q_RE.search(s))


def _extract_doc_id(source_like: dict[str, Any]) -> str | None:
    if not isinstance(source_like, dict):
        return None

    for k in ("document_id", "document", "doc_id"):
        v = source_like.get(k)
        if v:
            return str(v)

    src = source_like.get("source")
    if isinstance(src, dict):
        for k in ("document_id", "document", "doc_id"):
            v = src.get(k)
            if v:
                return str(v)

    return None


def _fetch_document_dates_map(*, rag_space_id, document_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not document_ids:
        return out

    try:
        from apps.rag_core.models.document import Document
    except Exception:
        return out

    has_doc_date = _model_has_field(Document, "document_date")
    has_meta = _model_has_field(Document, "meta")
    has_metadata = _model_has_field(Document, "metadata")

    fields = ["id"]
    if has_doc_date:
        fields.append("document_date")
    if has_meta:
        fields.append("meta")
    if has_metadata:
        fields.append("metadata")

    try:
        qs = Document.objects.filter(rag_space_id=rag_space_id, id__in=document_ids).values(*fields)
        for row in qs:
            did = str(row.get("id"))
            iso: str | None = None

            if has_doc_date and row.get("document_date"):
                try:
                    iso = row["document_date"].isoformat()
                except Exception:
                    iso = None

            if not iso and has_meta:
                meta = row.get("meta")
                if isinstance(meta, dict):
                    iso = str(meta.get("document_date") or "").strip() or None

            if not iso and has_metadata:
                md = row.get("metadata")
                if isinstance(md, dict):
                    iso = str(md.get("document_date") or "").strip() or None

            if iso and _parse_iso_date(iso):
                out[did] = iso
    except Exception:
        return out

    return out


def _document_id_of(chunk: dict[str, Any]) -> Optional[str]:
    source = chunk.get("source") if isinstance(chunk, dict) else None
    value = (source or {}).get("document_id") if isinstance(source, dict) else None
    return str(value) if value else None


def _apply_temporal_leg(*, chunks, intent, rag_space_id):
    """Fusionne les candidats du leg temporel avec le pool sémantique.

    Stratégie retenue : FUSION DOCUMENTAIRE avec priorité ordinale (§12, stratégie A
    et B combinées au niveau document). Le leg produit une liste de documents ORDONNÉE
    par date ; ces documents passent en tête, les autres candidats suivent dans leur
    ordre d'origine.

    Déduplication PAR DOCUMENT, pas par chunk : si un document sélectionné sur sa date
    avait aussi été retrouvé par similarité, on remonte le chunk DÉJÀ SCORÉ plutôt que
    d'ajouter le chunk d'en-tête en double. Le document n'apparaît donc jamais deux
    fois, et l'on ne perd pas le meilleur passage quand il existe.

    Portée :

    - opérateurs GLOBAUX : la sélection porte sur tout le RagSpace autorisé — c'est le
      seul cas où la date prime sur la pertinence, parce qu'il n'y a pas de thème ;
    - opérateurs THÉMATIQUES : la sélection est RESTREINTE aux documents déjà jugés
      pertinents (phase A). La date ne peut donc pas rendre pertinent un document hors
      sujet. Si la phase A n'a rien trouvé, le leg s'abstient — il ne bascule jamais
      silencieusement en global.

    Les opérateurs ancrés sur une date SANS thème explicite (« les notes après le
    18 mai ») portent sur l'espace : le filtre de date est lui-même une borne, ce
    n'est pas un tri de récence sur tout le corpus.
    """
    restrict_to_topic = (not intent.is_global) and intent.has_explicit_topic
    document_ids = None
    if restrict_to_topic:
        document_ids = list(dict.fromkeys(
            doc_id for doc_id in (_document_id_of(c) for c in (chunks or [])) if doc_id
        ))

    candidates, audit = fetch_temporal_candidates(
        rag_space_id=rag_space_id, intent=intent, document_ids=document_ids,
    )
    logger.info(
        "retrieval.temporal_leg applied=%s operator=%s scope=%s topic_terms=%s "
        "metadata_candidates=%d topic_documents=%d selected=%d limit=%d reason=%s "
        "duration_ms=%.2f",
        audit["temporal_leg_applied"], audit["temporal_operator"], audit["temporal_scope"],
        audit["topic_terms_present"], audit["metadata_candidates_count"],
        audit["topic_documents_count"], audit["selected_document_count"],
        audit["candidate_limit"], audit["reason"], audit["duration_ms"],
    )
    if not candidates:
        return chunks, audit

    best_by_document: dict[str, dict[str, Any]] = {}
    for chunk in chunks or []:
        doc_id = _document_id_of(chunk)
        if doc_id and doc_id not in best_by_document:
            best_by_document[doc_id] = chunk

    head, promoted = [], set()
    for candidate in candidates:
        doc_id = _document_id_of(candidate)
        if not doc_id or doc_id in promoted:
            continue
        promoted.add(doc_id)
        existing = best_by_document.get(doc_id)
        if existing is None:
            head.append(candidate)
            continue
        # Document retrouvé par les DEUX voies : on conserve le chunk scoré et son
        # score, on ne fait que le placer selon l'ordre temporel.
        merged = dict(existing)
        merged["retrieval_leg"] = TEMPORAL_LEG
        merged["temporal_reason"] = candidate.get("temporal_reason")
        merged["temporal_priority"] = candidate.get("temporal_priority")
        head.append(merged)

    tail = [c for c in (chunks or []) if _document_id_of(c) not in promoted]
    return head + tail, audit


def _enrich_sources_with_document_date(*, rag_space_id, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        doc_ids: list[str] = []
        for s in sources or []:
            did = _extract_doc_id(s)
            if did:
                doc_ids.append(did)
        doc_ids = list(dict.fromkeys(doc_ids))
        m = _fetch_document_dates_map(rag_space_id=rag_space_id, document_ids=doc_ids)
        if not m:
            return sources

        for s in sources or []:
            did = _extract_doc_id(s)
            if not did:
                continue

            iso = m.get(did)
            if not iso:
                continue

            if not s.get("document_date"):
                s["document_date"] = iso

            src = s.get("source")
            if isinstance(src, dict):
                if not src.get("document_date"):
                    src["document_date"] = iso
                meta = src.get("meta")
                if isinstance(meta, dict):
                    meta.setdefault("document_date", iso)
                elif meta is None:
                    src["meta"] = {"document_date": iso}

        return sources
    except Exception as exc:
        # P1-A1 : enrichissement de date best-effort, MAIS il alimente le tri « news »
        # (donc l'ordre). Un bug se propage ; une panne data reste best-effort + loggée.
        raise_if_programming_error(exc, leg="ranking_enrich_date")
        logger.warning("document-date enrichment skipped error_kind=%s", type(exc).__name__)
        return sources


def _sort_sources_for_news(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _get_iso(s: dict[str, Any]) -> str | None:
        v = s.get("document_date")
        if isinstance(v, str) and v.strip():
            return v.strip()
        src = s.get("source")
        if isinstance(src, dict):
            v2 = src.get("document_date")
            if isinstance(v2, str) and v2.strip():
                return v2.strip()
            meta = src.get("meta")
            if isinstance(meta, dict):
                v3 = meta.get("document_date")
                if isinstance(v3, str) and v3.strip():
                    return v3.strip()
        return None

    def _key(s: dict[str, Any]):
        d = _parse_iso_date(_get_iso(s))
        score = float(s.get("score") or 0.0)
        # Tri CROISSANT : les négations portent l'ordre décroissant de la date et du
        # score. Le départage documentaire est lui aussi croissant : il se concatène
        # donc directement (pas de `sort_candidates`, qui suppose `reverse=True`).
        #
        # Indispensable ici : la date est à granularité JOUR. Tous les documents d'une
        # même journée sont ex æquo sur les deux premières clés, et le score les
        # départage rarement. Sans ordre total, la numérotation [1] [2] du prompt —
        # donc le contexte envoyé au modèle — varierait d'un appel à l'autre.
        return (d is None, -(d.toordinal() if d else 0), -score) + tie_break_key(s)

    try:
        return sorted(sources, key=_key)
    except Exception as exc:
        # P1-A1 : le tri « news » modifie l'ORDRE des sources. Un bug se propage ;
        # une donnée inattendue reste best-effort (ordre non trié) mais loggée.
        raise_if_programming_error(exc, leg="ranking_news_sort")
        logger.warning("news source sort skipped error_kind=%s", type(exc).__name__)
        return sources


# ---------------------------------------------------------------------
# Prompt safety helpers (P1 vector: also prevents Ollama prompt blow-ups)
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Bi-source : documents privés du workspace + corpus métier autorisés (Phase 6)
# ---------------------------------------------------------------------

# Constitution du pool de candidats (FTS + vectoriel + corpus + fusion) et ses
# helpers bi-source : voir apps.rag_core.services.query.candidate_retriever (Lot P1-B2).


def _max_group_count(chunks: list[dict[str, Any]]) -> int:
    """Plus grand nombre de chunks partageant un même groupe de diversité (observabilité)."""
    counts: dict[str, int] = {}
    for c in chunks:
        g = build_diversity_group(c)
        counts[g] = counts.get(g, 0) + 1
    return max(counts.values(), default=0)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def _minimize_log_text(text: str, store_flag_name: str) -> str:
    """Minimisation optionnelle de RagQueryLog (lot 5, §15).

    Défaut (flag=True) : texte inchangé (comportement production préservé). Flag=False :
    on ne stocke PAS le texte, mais un marqueur redacté portant hash SHA-256 court +
    longueur — sans nouvelle colonne (utilise le TextField existant)."""
    if bool(getattr(settings, store_flag_name, True)):
        return text or ""
    t = text or ""
    digest = hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]
    return f"[redacted sha256:{digest} len:{len(t)}]"


def _record_gateway_rerank(trace, info) -> None:
    """Consigne le reranking Gateway dans la trace. N'interrompt JAMAIS la requête.

    Deux défaillances doivent être traitées différemment, et l'ancien code les
    confondait sous un `hasattr` silencieux :

    - **méthode absente au développement** — un défaut de code. Il ne doit pas
      s'installer en silence : la suite de tests le rend rouge (un test lit la trace
      consignée, un autre interdit le motif `hasattr` autour de cette méthode).
    - **échec d'écriture à l'exécution** — une donnée d'audit SECONDAIRE. Elle ne
      justifie pas de transformer une réponse valide en 500 : le pipeline a fait son
      travail, seule sa description a échoué. On journalise et on continue, comme le
      fait déjà la capture diagnostique.
    """
    try:
        trace.record_rerank_gateway(info)
    except Exception:  # noqa: BLE001 - télémétrie : jamais au prix de la réponse
        logger.exception("retrieval.rerank_gateway trace non consignée")


def _record_candidate_trace(
    trace, pool_avant, pool_apres, info, *, reranker_disponible, ordre_canonique
) -> None:
    """Consigne la trace des candidats. N'interrompt JAMAIS la requête.

    `except` large et délibéré : une capacité d'OBSERVATION qui ferait échouer une
    requête serait pire que son absence — c'est le même contrat que le mode fantôme.
    """
    try:
        from .candidate_trace import construire

        identites = {}
        par_magasin = getattr(trace, "store_embedding_identities", None)
        if par_magasin:
            identites["embedding_by_store"] = dict(par_magasin)
        trace.record_candidate_trace(construire(
            pool_avant=pool_avant,
            pool_apres=pool_apres,
            info_rerank=info,
            reranker_disponible=reranker_disponible,
            identites=identites,
            ordre_canonique=ordre_canonique,
        ))
    except Exception:  # noqa: BLE001 - l'observabilité ne coûte pas une réponse
        logger.exception("retrieval.candidate_trace non consignée")


def _record_gateway_rerank_survival(trace, selected) -> None:
    """Marque combien de candidats rerankés atteignent le contexte final."""
    try:
        trace.record_rerank_gateway_survival(
            [c.get("chunk_id") for c in (selected or [])]
        )
    except Exception:  # noqa: BLE001 - même doctrine que ci-dessus
        logger.exception("retrieval.rerank_gateway survie non consignée")


def _capture_feedback_trace(
    *,
    log,
    rag_space,
    pool: list[dict[str, Any]] | None,
    selected_pre_rerank: list[dict[str, Any]] | None,
    selected_post_rerank: list[dict[str, Any]] | None,
    selected_final: list[dict[str, Any]] | None,
    top_k: int,
    rerank_applied: bool,
    diversity_applied: bool,
    answerability_policy: str,
    policy_obj=None,
    structural_audit: dict[str, Any] | None = None,
    trace_sink=None,
) -> None:
    """Capture d'observabilité pour le diagnostic d'incident (lot feedback).

    Appelée à l'instant précis où le pool de candidats existe encore. Elle
    n'influence RIEN : ni score, ni ordre, ni sélection, ni politique de réponse.
    Le drapeau est à 0 par défaut, l'import est tardif, et toute exception est
    avalée — un défaut d'observabilité ne doit jamais coûter une réponse.
    """
    if log is None and trace_sink is None:
        return
    try:
        from apps.feedback.diagnostics.hook import capture_retrieval_trace

        # Les candidats pénalisés portent la trace de leur score d'origine :
        # on les identifie par cette clé plutôt qu'en reclassant le texte.
        penalised = {
            str(c.get("chunk_id"))
            for c in (pool or [])
            if c.get("fusion_score_before_penalty") is not None and c.get("chunk_id")
        }

        thresholds = {}
        if policy_obj is not None:
            thresholds = {
                "min_sources": getattr(policy_obj, "min_sources", None),
                "min_text_rank": getattr(policy_obj, "min_text_rank", None),
                "min_score": getattr(policy_obj, "min_score", None),
                "rerank_enabled": getattr(policy_obj, "rerank_enabled", None),
                "rerank_top_k": getattr(policy_obj, "rerank_top_k", None),
            }

        capture_retrieval_trace(
            log_id=str(log.id) if log is not None else "",
            pool=pool,
            selected_pre_rerank=selected_pre_rerank,
            selected_post_rerank=selected_post_rerank,
            selected_final=selected_final,
            top_k=int(top_k or 0),
            rerank_applied=bool(rerank_applied),
            diversity_applied=bool(diversity_applied),
            answerability_policy=answerability_policy,
            thresholds=thresholds,
            budgets={
                "top_k": int(top_k or 0),
                "max_prompt_chars": getattr(settings, "RAG_MAX_PROMPT_CHARS", None),
                "snippet_max_chars": getattr(settings, "RAG_SNIPPET_MAX_CHARS", None),
            },
            embedding_model=str(getattr(rag_space, "embedding_model", "") or ""),
            structural_penalised_ids=penalised,
            created_at=log.created_at.isoformat() if getattr(log, "created_at", None) else "",
            sink=trace_sink,
        )
        if structural_audit:
            # Compteur informatif : la pénalité structurelle explique un
            # déclassement, elle n'écarte aucun candidat.
            logger.debug(
                "feedback.capture structural_penalized=%s",
                structural_audit.get("candidates_penalized", 0),
            )
    except Exception as exc:  # noqa: BLE001 - jamais bloquant
        logger.warning(
            "feedback.capture skipped error_code=%s", type(exc).__name__
        )


@_email_scoped
def query_rag(
    *,
    rag_space_id,
    question: str,
    user=None,
    rag_space=None,
    request_id: str | None = None,
    conversation_context: str | None = None,
    entrypoint: str = "internal",
    answer_language: str | None = None,
    trace_sink=None,
    chunk_allowlist=None,
) -> dict:
    """``trace_sink`` (optionnel) : observateur de la trace de candidats.

    Strictement ADDITIF — absent, le comportement est inchangé au bit près. Il
    existe parce que le pool de candidats n'est visible qu'ici : le replay d'un
    incident en a besoin, et n'a aucun autre moyen de l'obtenir.

    ``chunk_allowlist`` (optionnel, ``rag_core.domain.chunk_allowlist.ChunkAllowlist``) :
    CORPUS-SNAPSHOT-HARDENING. Absent, rien ne change — c'est le corps ci-dessous,
    tel quel. Présent, l'appel est délégué à ``_query_rag_bounded`` : ce même corps,
    exécuté sous la liste blanche (chaque leg SQL joint la table), puis une garde finale
    vérifie que toute source rendue lui appartient (FAIL CLOSED).
    """
    if chunk_allowlist is not None:
        return _query_rag_bounded(
            chunk_allowlist=chunk_allowlist,
            rag_space_id=rag_space_id,
            question=question,
            user=user,
            rag_space=rag_space,
            request_id=request_id,
            conversation_context=conversation_context,
            entrypoint=entrypoint,
            answer_language=answer_language,
            trace_sink=trace_sink,
        )
    start = time.perf_counter()
    prompt_text: str | None = None
    selected_for_log: list[dict[str, Any]] = []

    # --- Contrat d'ENTRÉE typé (Lot P1-B1) -------------------------------------
    # `query_rag` reste la façade publique COMPATIBLE : elle normalise ses arguments
    # historiques vers un DTO interne immuable, puis poursuit sur le corps existant
    # (aucune étape déplacée). Les variables locales sont réassignées depuis le DTO —
    # même valeur, mais frontière d'entrée désormais explicite et validée.
    # L'espace est résolu AVANT la construction du DTO, et non après.
    #
    # Il l'était après, et la conséquence était invisible : `QueryExecutionContext`
    # capture `request.rag_space`, si bien qu'un appelant ne passant que
    # `rag_space_id` (tout l'outillage de benchmark et de holdout) faisait voyager
    # `rag_space=None` jusqu'au `candidate_retriever`. Celui-ci en déduisait
    # `embedding_provider=None`, `embedding_model=None`, `pipeline_family=None` :
    # la garde de cohérence P1-D **et** le filtre de famille du pipeline gouverné
    # étaient tous deux inertes. Un espace gouverné voyait donc sa recherche
    # vectorielle comparer la question à des chunks d'un AUTRE pipeline, et à des
    # documents marqués `reindex_required`.
    #
    # Le chat n'était pas touché : il passe toujours l'objet (cf.
    # `apps.rag_core.core.facade.retrieve`). Mais les benchmarks, eux, ne mesuraient
    # pas ce que la production exécute — ce qui rend leurs comparaisons ininterprétables
    # sur un espace gouverné. Constat du lot GOVERNED-EMBEDDING-PIPELINE-ENABLEMENT.
    if rag_space is None and rag_space_id is not None:
        rag_space = RagSpace.objects.select_related("workspace").filter(id=rag_space_id).first()

    request = QueryRequest.from_legacy_args(
        rag_space_id=rag_space_id,
        question=question,
        user=user,
        rag_space=rag_space,
        request_id=request_id,
        conversation_context=conversation_context,
        entrypoint=entrypoint,
        answer_language=answer_language,
    )
    rag_space_id = request.rag_space_id
    question = request.question
    user = request.user
    rag_space = request.rag_space
    conversation_context = request.conversation_context
    entrypoint = request.entrypoint

    rid = normalize_request_id(request_id) or (
        normalize_request_id(extract_request_id(user)) if user is not None else ""
    )

    # --- Instrumentation P1-A0 : trace structurée du retrieval -----------------
    # Donnée d'OBSERVABILITÉ uniquement. N'influence NI les scores, NI l'ordre, NI la
    # sélection, NI la politique de réponse. Voir services/query/trace.py.
    trace = RetrievalTraceBuilder(entrypoint=entrypoint, request_id=rid or None)
    # Contexte transversal (transporte requête + trace + contrats résolus). Prépare
    # P1-B2 (passage d'un contexte unique aux étapes extraites) — ici seulement peuplé.
    ctx = QueryExecutionContext.create(request, started_at=start, trace=trace)

    inventory_mode = is_inventory_question(question)
    explanatory_mode = is_explanatory_question(question)
    # Question comparative : DEUX objets explicitement mis en relation (« différences entre
    # X et Y », « compare X à Y », « X vs Y »). Un terme de constat isolé (« écart »,
    # « avantage ») ne suffit pas — cf. la doctrine et sa mesure dans `heuristics`.
    # La décision porte aussi les deux objets, qui servent à l'expansion déterministe.
    comparative_intent = detect_comparative_intent(question)
    comparative_mode = comparative_intent.is_comparative
    news_mode = bool(NEWS_HEURISTIC_ENABLED and _looks_like_news_question(question))

    # Les questions comparatives héritent des BUDGETS analytiques (top_k, seuils) : comparer
    # deux objets demande mécaniquement plus d'extraits qu'une question factuelle. Ces budgets
    # sont désormais DÉCLARÉS dans les settings, et leurs défauts valent les budgets standards
    # (cf. docs/10-reference/variables-environnement.md) : aucun élargissement implicite.
    analytical_mode = explanatory_mode or comparative_mode

    # Expansion de requête : DÉTERMINISTE pour une comparaison explicite (les deux objets
    # sont déjà connus, aucun appel LLM), historique sinon.
    queries_for_search = build_queries_for_search(
        question=question,
        explanatory_mode=analytical_mode,
        comparative_objects=comparative_intent.objects if comparative_mode else None,
    )

    # --- Signaux forts (P3) ---------------------------------------------------
    #
    # Une question peut porter une contrainte EXPLICITE qui identifie un document
    # mieux que la similarité sémantique — typiquement une date. Sur un corpus de
    # documents datés au vocabulaire proche, « la note du 18 mai 2026 » n'a aucun
    # token commun avec la forme indexée `2026-05-18` : le lexical ne peut pas les
    # rapprocher, et le vectoriel les trouve tous également proches.
    #
    # On ajoute donc une requête de recherche SUPPLÉMENTAIRE portant la forme
    # canonique. Strictement additif : aucune requête existante n'est modifiée ni
    # retirée, et en l'absence de date détectée la liste est inchangée — une
    # question sans signal suit exactement le chemin historique.
    # Drapeau d'activation : la porte doit pouvoir se refermer sans redéploiement,
    # et le comparatif de qualification exige de mesurer les deux états à code égal.
    strong_signals_enabled = bool(getattr(settings, "RAG_STRONG_SIGNALS_ENABLED", True))
    strong_signals = extract_signals(question) if strong_signals_enabled else StrongQuerySignals()
    # `q_search_primary` est calculée AVANT l'ajout des termes de date. Elle prend le
    # DERNIER élément de la liste : y ajouter la forme ISO d'abord la faisait devenir
    # la requête primaire — donc « 2026-05-18 » tout court partait au reranker à la
    # place de la question. L'ajout des dates doit rester strictement additif pour la
    # recherche, sans redéfinir la requête de référence.
    q_search_primary = (
        queries_for_search[-1]
        if len(queries_for_search) >= 2
        else (queries_for_search[0] if queries_for_search else question)
    )
    date_terms = canonical_date_terms(strong_signals)
    if date_terms:
        queries_for_search = list(queries_for_search) + [" ".join(date_terms)]

    # P3-c : intention temporelle (portée globale ou thématique). Détection PURE ici ;
    # le leg documentaire n'est interrogé qu'après le retrieval, plus bas.
    temporal_intent = (
        extract_temporal_intent(question)
        if (strong_signals_enabled and temporal_leg_enabled())
        else None
    )

    # --- Contrat de REQUÊTE PRÉPARÉE (Lot P1-B1) -------------------------------
    # Snapshot immuable des valeurs déjà résolues par la préparation. Source unique de
    # `rewritten_query` renvoyé plus bas. Les legs continuent d'utiliser leurs locales
    # (le threading complet est du ressort de P1-B2).
    ctx.prepared = PreparedQuery.from_resolved(
        original_question=question,
        queries_for_search=queries_for_search,
        primary_retrieval_query=q_search_primary,
        inventory=inventory_mode,
        explanatory=explanatory_mode,
        news=news_mode,
        comparative=comparative_mode,
        has_temporal_intent=temporal_intent is not None,
    )

    def _latency_ms() -> int:
        return int((time.perf_counter() - start) * 1000)

    def _prompt_hash() -> str:
        return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() if prompt_text else ""

    def _create_log(
        status: str,
        sources_selected: list[dict[str, Any]],
        *,
        error_stage: str = "",
        answer_text: str | None = None,
        policy: str = "",
        rerank: dict[str, Any] | None = None,
        retrieval: dict[str, Any] | None = None,
    ):
        if user is None or rag_space is None:
            return None

        sources_json = normalize_sources_for_audit(sources_selected)

        return RagQueryLog.objects.create(
            user=user,
            workspace=rag_space.workspace,
            rag_space=rag_space,
            question=_minimize_log_text(question, "RAG_QUERY_LOG_STORE_QUESTION"),
            answer=_minimize_log_text(answer_text or "", "RAG_QUERY_LOG_STORE_ANSWER"),
            status=status,
            sources=sources_json,
            sources_schema_version=SOURCES_SCHEMA_VERSION,
            prompt_hash=_prompt_hash(),
            latency_ms=_latency_ms(),
            error_stage=error_stage,
            request_id=rid,
            policy=policy,
            rerank=rerank or {},
            # Résumé COMPACT et non sensible de la trace de retrieval (P1-A0).
            # Distingue en audit un no_answer LÉGITIME d'une DÉGRADATION.
            retrieval=retrieval or {},
        )

    try:
        if rag_space_id is None:
            no_space_trace = trace.build(final_candidate_count=0, source_count=0)
            log = _create_log(
                RagQueryLog.STATUS_NO_ANSWER,
                [],
                answer_text="Je ne sais pas.",
                policy="no_rag_space",
                retrieval=no_space_trace.summary(),
            )
            return QueryResult.from_legacy_dict({
                "status": "no_answer",
                "sources": [],
                "prompt": None,
                "log_id": str(log.id) if log else None,
                "retrieval_degraded": no_space_trace.degraded,
                "retrieval_trace": no_space_trace.to_dict(),
            }).to_legacy_dict()

        # Seuil de pertinence vectorielle : celui de l'espace s'il en porte un ET que le
        # flag est ouvert, sinon le défaut global — inchangé. Voir space_policy pour la
        # raison : le seuil absolu dépend du modèle d'embedding et de la langue du fonds.
        effective_min_score = resolve_min_score(rag_space)
        min_score_info = min_score_audit(rag_space, effective_min_score)

        policy_obj = RagPolicy(
            min_sources=settings.RAG_MIN_SOURCES,
            min_text_rank=settings.RAG_MIN_TEXT_RANK,
            min_score=effective_min_score,
            rerank_enabled=settings.RAG_RERANK_ENABLED,
            rerank_trigger_min_candidates=settings.RAG_RERANK_TRIGGER_MIN_CANDIDATES,
            rerank_top_k=settings.RAG_RERANK_TOP_K,
        )

        if analytical_mode:
            explain_min_text_rank = setting("RAG_EXPLAIN_MIN_TEXT_RANK")
            explain_min_score = setting("RAG_EXPLAIN_MIN_SCORE")
            explain_rerank_trigger = setting("RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES")

            if explain_min_text_rank is not None:
                policy_obj.min_text_rank = min(float(policy_obj.min_text_rank), float(explain_min_text_rank))
            if explain_min_score is not None:
                policy_obj.min_score = min(float(policy_obj.min_score), float(explain_min_score))
            if explain_rerank_trigger is not None:
                policy_obj.rerank_trigger_min_candidates = min(
                    int(policy_obj.rerank_trigger_min_candidates), int(explain_rerank_trigger)
                )

        top_k = int(getattr(settings, "RAG_TOP_K", 4))
        vector_top_k = int(getattr(settings, "RAG_VECTOR_TOP_K", 20))

        # Surcharges de budget par mode : `max(courant, surcharge)` — un budget de
        # mode ne peut qu'ÉLARGIR. Défaut du registre = 0 (« aucune surcharge »),
        # strictement équivalent à l'ancien défaut d'appel `top_k`.
        if inventory_mode:
            top_k = max(top_k, setting_int("RAG_INVENTORY_TOP_K"))
            vector_top_k = max(vector_top_k, setting_int("RAG_INVENTORY_VECTOR_TOP_K"))

        if analytical_mode:
            top_k = max(top_k, setting_int("RAG_EXPLAIN_TOP_K"))
            vector_top_k = max(vector_top_k, setting_int("RAG_EXPLAIN_VECTOR_TOP_K"))

        if news_mode:
            top_k = max(top_k, NEWS_TOP_K)
            vector_top_k = max(vector_top_k, NEWS_VECTOR_TOP_K)

        # candidate_k : pool ÉLARGI récupéré AVANT fusion et diversification. On tronque
        # au top_k final seulement APRÈS avoir diversifié. Sans cela, un chunk au rang
        # lexical 11 ne pouvait jamais entrer, même si une feuille dense occupait à elle
        # seule les 8 premières places.
        diversity_enabled = bool(getattr(settings, "RAG_RETRIEVAL_DIVERSITY_ENABLED", False))
        if diversity_enabled:
            explicit_k = setting_int("RAG_RETRIEVAL_CANDIDATE_K")
            mult = max(1, setting_int("RAG_RETRIEVAL_CANDIDATE_MULTIPLIER"))
            candidate_k = explicit_k if explicit_k > 0 else top_k * mult
            candidate_k = max(candidate_k, top_k)          # jamais plus étroit que top_k
        else:
            candidate_k = top_k
        # Pondération lexicale CONDITIONNELLE (P1.5). Renforcée uniquement quand la
        # question porte un identifiant fort (« Series A-1 », « 2020-1 ») : là, le signal
        # lexical exact est plus fiable que le vectoriel sémantique. Sans identifiant, les
        # poids restent (1, 1) et la fusion est historique, bit pour bit — aucune requête
        # sémantique ne peut donc régresser (neutralité STRUCTURELLE, pas statistique).
        #
        # On réutilise le détecteur de la tokenisation : pas de seconde regex à maintenir.
        lexical_weight = 1.0
        if bool(getattr(settings, "RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED", False)):
            try:
                from apps.rag_core.repositories.chunk_repository import extract_identifiers
                has_strong_id = bool(extract_identifiers(question))
            except Exception as exc:
                # P1-A1 : détermine la pondération lexicale conditionnelle -> l'ORDRE.
                # Un bug (import/attribut/signature) se propage ; sinon best-effort loggé.
                raise_if_programming_error(exc, leg="ranking_strong_id")
                logger.warning("strong-identifier detection skipped error_kind=%s", type(exc).__name__)
                has_strong_id = False
            if has_strong_id:
                lexical_weight = setting_float("RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT")
                logger.info("retrieval.conditional_rrf applied lexical_weight=%.2f", lexical_weight)

        # Le pool lexical/vectoriel/fusion travaille sur candidate_k ; top_k reste la
        # taille FINALE, appliquée après diversification. Les pools effectifs sont
        # dérivés de la politique DANS le CandidateRetriever (P1-B2).

        # --- Contrat de POLITIQUE/BUDGET résolu (Lot P1-B1) -------------------
        # Snapshot des flags + seuils déjà calculés (le FTS est requis). Objectif P1-B2 :
        # éviter que les legs relisent les settings. Ici, uniquement peuplé (validation
        # légère des invariants : top_k > 0, seuils finis).
        ctx.policy = RetrievalPolicy(
            fts_required=True,
            hybrid_enabled=bool(getattr(settings, "RAG_HYBRID_ENABLED", False)),
            vector_available=callable(search_chunks_vector),
            temporal_enabled=temporal_intent is not None,
            rerank_enabled=bool(policy_obj.rerank_enabled),
            diversity_enabled=diversity_enabled,
            min_sources=int(policy_obj.min_sources),
            min_text_rank=float(policy_obj.min_text_rank),
            min_score=float(effective_min_score),
            top_k=int(top_k),
            vector_top_k=int(vector_top_k),
            candidate_k=int(candidate_k),
            lexical_weight=float(lexical_weight),
            rerank_trigger_min_candidates=int(policy_obj.rerank_trigger_min_candidates),
            rerank_top_k=int(policy_obj.rerank_top_k),
            unified_bi_source_fusion=unified_fusion_enabled(),
            corpus_rrf_leg_weight=corpus_rrf_leg_weight(),
            calibrated_corpus_admission=calibrated_admission_enabled(),
            corpus_admission_probability_threshold=corpus_admission_probability_threshold(),
        )

        # --- CandidateRetriever (Lot P1-B2) : FTS + vectoriel + corpus + fusion ---
        # Les fonctions de recherche sont INJECTÉES depuis le namespace du module
        # (résolues à l'appel) : les points de monkeypatch existants restent effectifs.
        chunks = retrieve_candidates(
            ctx,
            fts_search=search_chunks_hybrid,
            vector_search=search_chunks_vector,
        )

        if inventory_mode and chunks:
            inv_min_unique = setting_int("RAG_INVENTORY_MIN_UNIQUE_DOCS") or min(10, max(4, top_k // 2))
            chunks = diversify_by_document(chunks, top_k=top_k, min_unique_docs=inv_min_unique)

        # Opérateur temporel relatif (« la plus récente », « la première ») : on
        # réordonne les CANDIDATS déjà retenus, sans élargir ni filtrer le corpus.
        # Conditionnel : sans opérateur détecté, `chunks` est renvoyé tel quel.
        if strong_signals.temporal_operator:
            chunks = apply_temporal_operator(chunks, strong_signals.temporal_operator)

        # P3-c) Leg documentaire borné sur les métadonnées de date.
        #
        # Il intervient APRÈS le nettoyage : c'est tout l'objet du lot. Un candidat
        # sélectionné sur sa date n'a pas de score sémantique, et le passer dans la
        # politique de score le supprimerait — c'est précisément ce qui faisait
        # disparaître « la note la plus récente » avant la fusion finale.
        temporal_audit = None
        if temporal_intent is not None:
            chunks, temporal_audit = _apply_temporal_leg(
                chunks=chunks,
                intent=temporal_intent,
                rag_space_id=rag_space_id,
            )

        # Leg temporel : informatif (jamais `degraded` en P1-A0 — les erreurs de date
        # sont best-effort/cosmétiques, cf. trace.py & doc). `NOT_REQUESTED` quand la
        # question ne porte aucune intention temporelle.
        if temporal_intent is None:
            trace.record_leg(LEG_TEMPORAL, RetrievalLegStatus.NOT_REQUESTED)
        elif temporal_audit and temporal_audit.get("temporal_leg_applied"):
            trace.record_leg(LEG_TEMPORAL, RetrievalLegStatus.SUCCEEDED)
        else:
            trace.record_leg(LEG_TEMPORAL, RetrievalLegStatus.EMPTY)

        # --- Admission du retrieval (lot 4) ---------------------------------
        # `should_answer` reste la source de vérité du refus STRICT tant que la
        # politique vaut `legacy` : `admit` la reproduit alors à l'octet près. Toute
        # autre politique décide à sa place, et la trace dit laquelle.
        #
        # `UNCERTAIN` n'est PAS un refus : il autorise une réponse prudente. Le
        # traiter comme un refus supprimerait l'intérêt du troisième état.
        _admission = admit(
            chunks,
            rag_space=rag_space,
            # Politique DÉJÀ résolue pour cette requête (budgets par mode,
            # seuil par espace) : l'admission ne doit pas la réinventer.
            base_policy=policy_obj,
            expected_pipeline_family=expected_family_for(rag_space),
            request_id=str(request_id or ""),
        )
        strict_refusal = not _admission.is_answerable
        trace.record_admission(_admission)
        if strict_refusal and not chunks:
            # Abstention immédiate : la sélection n'a pas eu lieu. On l'enregistre
            # explicitement (listes vides) pour que l'étape de disparition du corpus
            # soit calculée, et non laissée indéterminée.
            trace.record_bi_source_selection(
                pool=chunks, pre_rerank=[], post_rerank=[], final=[],
                diversity_applied=False, rerank_applied=False,
            )
            no_answer_trace = trace.build(final_candidate_count=len(chunks), source_count=0)
            log = _create_log(
                RagQueryLog.STATUS_NO_ANSWER, [], answer_text="Je ne sais pas.",
                policy="no_answer", retrieval=no_answer_trace.summary(),
            )
            # Abstention : c'est LE cas qu'on cherche à diagnostiquer (« I don't
            # know » alors que le terme est indexé). Une trace vide n'est pas une
            # trace inutile — elle prouve qu'aucun candidat n'a survécu au
            # rappel, ce qui oriente vers les seuils et non vers la génération.
            _capture_feedback_trace(
                log=log,
                rag_space=rag_space,
                pool=chunks,
                selected_pre_rerank=[],
                selected_post_rerank=[],
                selected_final=[],
                top_k=top_k,
                rerank_applied=False,
                diversity_applied=False,
                answerability_policy="no_answer",
                policy_obj=policy_obj,
                trace_sink=trace_sink,
            )
            # La provenance du seuil est exposée AUSSI sur le refus : c'est là
            # qu'elle sert le plus. Sans elle, un refus causé par un seuil mal
            # calibré est indiscernable d'un refus causé par un corpus qui ne
            # contient pas la réponse.
            return QueryResult.from_legacy_dict(
                {"status": "no_answer", "sources": [], "prompt": None,
                 "log_id": str(log.id) if log else None,
                 "min_score": min_score_info,
                 "retrieval_degraded": no_answer_trace.degraded,
                 "retrieval_trace": no_answer_trace.to_dict()}
            ).to_legacy_dict()

        # 3bis) Diversification du pool élargi -> top_k final.
        #
        # C'est ici que le chunk au rang 11 peut enfin entrer. On plafonne SOUPLEMENT
        # chaque groupe (document + feuille + groupe de colonnes) : passe 1 stricte,
        # passe 2 de complétion sans plafond. Le classement RRF reste la vérité — on ne
        # fait que CHOISIR un sous-ensemble, jamais recalculer un score.
        #
        # L'inventaire a déjà sa propre diversité (par document) juste au-dessus : on ne
        # la double pas.
        #
        # Le leg temporel non plus. Il a déjà produit une sélection DOCUMENTAIRE
        # déterministe — un chunk par document, ordonnés par date — donc l'objectif de
        # la diversification (empêcher un document de monopoliser le top_k) est atteint
        # par construction. Pire, la diversification reclasserait sur le score de
        # fusion, et les candidats temporels ont un score sémantique NUL par
        # construction : ils couleraient, et « la note la plus récente » cesserait de
        # renvoyer la plus récente.
        #
        # Le défaut existait déjà mais restait invisible : la garde `len(chunks) > top_k`
        # n'était jamais franchie tant que la politique de score maintenait le pool
        # sous top_k. Il apparaît dès que davantage de candidats survivent — corpus plus
        # fourni, ou seuil calibré par espace. Mesuré : sur un corpus mixte de
        # 14 documents, l'ordre final devenait 03-13, 03-09, 03-12… au lieu de 06-03.
        temporal_leg_applied = bool(temporal_audit and temporal_audit.get("temporal_leg_applied"))

        # --- Bruit STRUCTUREL (P0) : questions et titres du corpus ------------
        #
        # Point d'application UNIQUE, ici et nulle part ailleurs : après la fusion et
        # APRÈS `should_answer`, juste avant la sélection. Conséquences voulues :
        #
        # - la pénalité n'agit que sur `fusion_score`, la clé de classement de la
        #   diversification. `score` reste intact, donc les SEUILS (`should_answer`) et
        #   la CONFIANCE FAIBLE restent inchangés par construction ;
        # - aucun candidat n'est supprimé : `len(chunks)` est conservé ;
        # - l'inventaire et le leg temporel portent un ordre DOCUMENTAIRE délibéré, que
        #   la pénalité n'a pas à réécrire — on la saute dans ces deux cas.
        #
        # Mesuré : questions et titres occupaient 19 % du top-8 alors qu'ils n'énoncent
        # aucun fait ; dans le pool lexical, les questions du corpus se classaient même
        # MIEUX que les réponses, parce qu'elles reprennent les mots de la requête.
        structural_audit: dict[str, Any] = {}
        if (
            bool(getattr(settings, "RAG_STRUCTURAL_NOISE_PENALTY_ENABLED", False))
            and not inventory_mode
            and not temporal_leg_applied
            and chunks
        ):
            chunks, structural_audit = apply_structural_penalty(
                chunks,
                factor=getattr(settings, "RAG_STRUCTURAL_NOISE_PENALTY_FACTOR", 0.75),
                top_k=top_k,
            )
            logger.info(
                "retrieval.structural_noise factor=%.2f candidates=%d question=%d "
                "title_like=%d penalized=%d top_k_noise_before=%s top_k_noise_after=%s",
                structural_audit.get("factor", 1.0),
                structural_audit.get("candidates", 0),
                structural_audit.get("question_detected", 0),
                structural_audit.get("title_like_detected", 0),
                structural_audit.get("candidates_penalized", 0),
                structural_audit.get("top_k_noise_before"),
                structural_audit.get("top_k_noise_after"),
            )

        # --- Reranking de passages via l'AI Gateway (sous drapeau, OFF par défaut) ---
        # Agit APRÈS l'admission et AVANT la diversification : il ne fait revenir aucun
        # candidat, il ne change que l'ordre de sélection. Le pool soumis est prélevé sur
        # l'ordre RRF CANONIQUE — la liste `chunks` est dans l'ordre d'hygiène, et un
        # `chunks[:N]` naïf soumettrait un autre sous-ensemble (régression mesurée).
        #
        # Le préfixe `gateway_` n'est pas cosmétique : la trace du reranker HISTORIQUE
        # s'appelait `rerank_info` elle aussi, et son affectation (plus bas) écrasait
        # celle-ci dans la même portée. Deux traces distinctes sous un seul nom, dont
        # l'une survit à l'autre selon l'ordre des lignes : c'est la forme même du
        # défaut d'observabilité qu'un jour de panne révèle. Les deux mécanismes ne
        # peuvent plus être actifs ensemble (system check `rag_core.E008`), mais le nom
        # doit dire lequel des deux il décrit — la garde et la lisibilité sont deux
        # protections distinctes, pas deux fois la même.
        #
        # Les réglages de ce chemin sont lus par `services/rerank_gateway.py`, pas ici :
        # le gel de campagne (`benchmark/holdout_config.py`, schéma v2) les découvre
        # donc dans ce module-là, par analyse syntaxique des lectures réelles.
        gateway_rerank_info: dict[str, Any] = {}
        try:
            from apps.rag_core.services import rerank_gateway as _rrg
            _rrg_enabled = _rrg.reranker_enabled()
        except Exception:  # pragma: no cover - un défaut d'import ne coûte pas une réponse
            _rrg, _rrg_enabled = None, False
        # Pool AVANT reranking, dans l'ordre RRF CANONIQUE — celui que le reranker
        # verra réellement. Prélevé UNIQUEMENT si la trace est demandée : drapeau
        # éteint, ce bloc ne coûte pas même un tri. On garde des RÉFÉRENCES, jamais
        # une copie des textes.
        _pool_avant, _ordre_canonique = None, True
        if chunks and setting_bool("RAG_CANDIDATE_TRACE_ENABLED"):
            try:
                _tete, _queue = _rrg.select_rrf_pool(chunks)
                _pool_avant = _tete + _queue
            except Exception:  # noqa: BLE001 - reranker absent : on trace quand même
                _pool_avant, _ordre_canonique = list(chunks), False
        if _rrg_enabled and chunks:
            chunks, gateway_rerank_info = _rrg.rerank_chunks(question=question, chunks=chunks)
            # Consigné DANS TOUS LES CAS, succès comme repli. La version précédente
            # n'enregistrait que les succès — et derrière un `hasattr` portant sur une
            # méthode qui n'existait pas, donc jamais. L'audit affirmait alors
            # `rerank: not_requested` sur des requêtes réellement rerankées : une trace
            # qui dit le contraire de ce qui s'est passé est pire qu'une trace absente.
            #
            # Plus de `hasattr` : la méthode est contractuelle. Si elle disparaît, les
            # tests doivent rougir, pas le silence s'installer.
            _record_gateway_rerank(trace, gateway_rerank_info)

        # --- Trace des CANDIDATS ----------------------------------------
        # Placée ICI parce que c'est le seul instant où les deux états coexistent :
        # le pool tel que le reranker l'a vu, et l'ordre qu'il en a fait. Une ligne
        # plus bas, la diversification remanie `chunks` et le rang « après » cesse
        # d'être celui du reranking.
        #
        # Consignée aussi quand le reranker est éteint ou a replié : c'est
        # précisément l'état qu'un diagnostic doit pouvoir distinguer, et la version antérieure ne
        # le pouvait pas.
        if _pool_avant is not None:
            _record_candidate_trace(
                trace, _pool_avant, chunks, gateway_rerank_info,
                reranker_disponible=bool(_rrg_enabled),
                ordre_canonique=_ordre_canonique,
            )

        # --- Policy d'admission en mode FANTÔME ---------------------------
        # Placée ICI, et pas ailleurs : le score cross-encoder existe à partir de cette
        # ligne, et cesse d'être disponible dès que la diversification remanie `chunks`.
        # Elle RELIT `gateway_rerank_info` — aucun appel supplémentaire au reranker, à la
        # Gateway, aux embeddings ou à un LLM.
        #
        # Rien en aval ne lit sa sortie. C'est le contrat du mode fantôme : avec le
        # drapeau ON ou OFF, la réponse servie est identique, et un test compare les deux
        # exécutions plutôt que de faire confiance à cette phrase.
        #
        # Un `except` large parce qu'une décision d'OBSERVATION ne doit jamais coûter une
        # réponse. Une policy fantôme qui fait échouer une requête serait pire que son
        # absence — et pire que le défaut qu'elle est censée mesurer.
        try:
            if setting_bool("RAG_ADMISSION_SHADOW_ENABLED"):
                from apps.rag_core.domain import admission_shadow as _shadow

                trace.record_admission_shadow(_shadow.evaluate(
                    gateway_rerank_info,
                    enabled=True,
                    candidate_count=len(chunks),
                ))
        except Exception:  # pragma: no cover - jamais au prix d'une réponse
            logger.exception("retrieval.admission_shadow non consignée")

        diversity_applied = False
        if diversity_enabled and not inventory_mode and not temporal_leg_applied and len(chunks) > top_k:
            max_per_group = max(1, setting_int("RAG_RETRIEVAL_MAX_PER_GROUP"))
            # Le plafond du chemin reranké est SÉPARÉ : drapeau OFF => plafond historique.
            if gateway_rerank_info.get("success"):
                max_per_group = max(1, _rrg.max_per_group())
            before_max = _max_group_count(chunks[:top_k])
            selected = diversify_ranked_results(
                chunks, top_k=top_k, max_per_group=max_per_group,
            )
            diversity_applied = True
            logger.info(
                "retrieval.diversity applied candidate_k=%d pool=%d top_k=%d "
                "max_per_group=%d groups=%d max_group_before=%d max_group_after=%d",
                candidate_k, len(chunks), top_k, max_per_group,
                len({build_diversity_group(c) for c in chunks}),
                before_max, _max_group_count(selected),
            )
        else:
            selected = chunks[:top_k]

        # État de la sélection AVANT reranking. Conservé par référence (aucune
        # copie de contenu) : c'est la comparaison entre les états successifs qui
        # permet de dire pourquoi un candidat a disparu — `rerank_cut` plutôt
        # qu'une raison devinée. Voir apps.feedback.diagnostics.capture.
        selected_pre_rerank = list(selected)

        # --- Reranker HISTORIQUE (Lot P1-B2) : ordre autoritaire, repli tracé ---
        # Chemin DISTINCT du reranking Gateway ci-dessus : modèle chargé dans le
        # process, trace `RerankInfo` — un objet, pas le dict de la Gateway. C'est cet
        # objet-là que l'audit `RagQueryLog.rerank` persiste, et lui seul. Les deux
        # chemins ne peuvent pas être actifs ensemble : voir `checks_rerank.py`
        # (`rag_core.E008`), qui nomme les deux drapeaux.
        # `rerank_chunks_observed` injecté depuis le namespace du module (monkeypatch OK).
        selected, legacy_rerank_info = rank_candidates(
            ctx, chunks=chunks, selected=selected, rerank_fn=rerank_chunks_observed,
        )

        # --- Voisinage visuel de page (VISUAL-RETRIEVAL-QUALITY-1B) -----------
        # APRÈS la sélection (diversification + reranker historique), AVANT le budget
        # de prompt : le visuel de la même page qu'un passage natif retenu est placé
        # juste derrière lui, pris dans le pool DÉJÀ récupéré. Aucun score, aucune
        # requête ; la séquence garde sa longueur. Hors inventaire et leg temporel,
        # dont la sélection a sa propre sémantique.
        if not inventory_mode and not temporal_leg_applied:
            selected, _visual_promotions = promote_same_page_visuals(selected, chunks)
            if _visual_promotions:
                trace.record_visual_neighbors(_visual_promotions)

        selected = _enrich_sources_with_document_date(rag_space_id=rag_space_id, sources=selected)
        if news_mode:
            selected = _sort_sources_for_news(selected)

        best_score = max((float(c.get("score") or 0.0) for c in selected), default=0.0)
        low_thr = low_conf_threshold(policy_obj)
        low_confidence = bool(LOWCONF_ENABLED) and len(selected) >= int(LOWCONF_MIN_SOURCES) and best_score < low_thr
        # Un document retenu par le leg temporel l'a été sur une métadonnée EXACTE, pas
        # sur une similarité. Son score sémantique est nul par construction : le juger
        # « peu fiable » sur ce score reviendrait à pénaliser la réponse la plus sûre
        # du lot. La confiance est donc laissée intacte quand le leg a tranché.
        if low_confidence and any(c.get("retrieval_leg") == TEMPORAL_LEG for c in selected):
            low_confidence = False

        # --- PromptBuilder (Lot P1-B2) : préambules + mémoire + budget ---------
        # `low_confidence`/`strict_refusal` (answerability) restent décidés ci-dessus.
        prompt_result = build_prompt_stage(
            ctx, selected=selected, low_confidence=low_confidence, strict_refusal=strict_refusal,
        )
        prompt_text = prompt_result.prompt
        selected_post_rerank = selected
        selected = prompt_result.sources
        policy_label = prompt_result.answer_kind

        selected_for_log = selected
        rerank_audit = legacy_rerank_info.to_dict() if legacy_rerank_info is not None else {}
        # Survie des candidats CORPUS à chaque étape de sélection (compteurs seuls).
        # C'est ce qui permet de dire, en production, si un corpus a disparu à la
        # diversification, au reranking ou au budget de prompt — plutôt que de le
        # déduire d'une absence.
        trace.record_bi_source_selection(
            pool=chunks,
            pre_rerank=selected_pre_rerank,
            post_rerank=selected_post_rerank,
            final=selected,
            diversity_applied=diversity_applied,
            rerank_applied=legacy_rerank_info is not None,
        )
        # Le budget de prompt est la DERNIÈRE étape qui retire des sources, et la seule
        # qui décrive ce que le modèle a réellement lu. `selected_post_rerank` est le
        # classement ; `selected` est ce qui est entré dans le contexte.
        trace.record_prompt_budget(
            before=selected_post_rerank, after=selected,
            accounting=getattr(prompt_result, "budget", None),
        )
        # Survie des candidats rerankés au contexte final. Ici, et pas plus tôt : le
        # reranking a lieu AVANT la diversification et le budget de prompt, et c'est
        # l'écart entre son classement et ce qui est réellement transmis qui intéresse.
        # Aucun recalcul : on lit les identifiants de `selected`.
        _record_gateway_rerank_survival(trace, selected)
        # `ctx.ranked` (RankedCandidateSet) est renseigné par le Reranker (P1-B2).
        ok_trace = trace.build(final_candidate_count=len(chunks), source_count=len(selected))
        log = _create_log(
            RagQueryLog.STATUS_OK,
            selected_for_log,
            answer_text="",
            policy=policy_label,
            rerank=rerank_audit,
            retrieval=ok_trace.summary(),
        )

        # --- Capture diagnostique (lot 2) -------------------------------------
        # ICI, et nulle part ailleurs : c'est le dernier instant où le pool de
        # candidats existe encore. Au `return`, `chunks` disparaît, et aucune
        # tâche déclenchée plus tard ne pourra le reconstituer.
        #
        # Strictement observationnel : aucune valeur de retour n'est lue, aucune
        # exception ne remonte, et le drapeau est à 0 par défaut.
        _capture_feedback_trace(
            log=log,
            rag_space=rag_space,
            pool=chunks,
            selected_pre_rerank=selected_pre_rerank,
            selected_post_rerank=selected_post_rerank,
            selected_final=selected,
            top_k=top_k,
            rerank_applied=legacy_rerank_info is not None,
            diversity_applied=diversity_applied,
            answerability_policy=policy_label,
            policy_obj=policy_obj,
            structural_audit=structural_audit,
            trace_sink=trace_sink,
        )

        # Source unique : la valeur résolue par le contrat de requête préparée (P1-B1).
        # Identique au calcul historique (`queries_for_search[1]` si présent).
        rewritten_query = ctx.prepared.rewritten_query if ctx.prepared else (
            queries_for_search[1] if len(queries_for_search) >= 2 else None
        )

        # Frontière de SORTIE (P1-B1) : le dict historique reste la SOURCE DE VÉRITÉ ;
        # `QueryResult` n'en est qu'une vue typée immuable et `to_legacy_dict()` le
        # renvoie VERBATIM — la sortie publique est strictement identique.
        return QueryResult.from_legacy_dict({
            "status": "ok",
            "sources": selected,
            "prompt": prompt_text,
            "log_id": str(log.id) if log else None,
            "answer_kind": policy_label,
            "rewritten_query": rewritten_query,
            "rerank": rerank_audit,
            # Agrégats de la pénalité structurelle (compteurs uniquement, jamais de
            # texte documentaire). La clé n'apparaît QUE si le mécanisme s'est exécuté :
            # à l'état livré (désactivé), le contrat public est inchangé au bit près.
            **({"structural_noise": structural_audit} if structural_audit else {}),
            # Observabilité du leg temporel. `None` quand la question ne porte aucun
            # opérateur — c'est le cas normal, et il se distingue d'un leg exécuté
            # sans résultat (`temporal_no_dated_document`).
            "temporal": temporal_audit,
            # Provenance du seuil appliqué (aucune donnée, que de la configuration).
            "min_score": min_score_info,
            # Instrumentation P1-A0 (additif, non sensible). `retrieval_degraded`
            # distingue un `no_answer`/résultat vide LÉGITIME d'une DÉGRADATION interne.
            "retrieval_degraded": ok_trace.degraded,
            "retrieval_trace": ok_trace.to_dict(),
        }).to_legacy_dict()

    except Exception:
        # P1-A0 : le contrat est INCHANGÉ (log ERROR puis `raise`). On enrichit
        # seulement l'audit d'un résumé de trace `outcome=error` pour distinguer une
        # panne du pipeline d'un vide légitime.
        error_trace = trace.build(
            final_candidate_count=0, source_count=0, error=True,
        )
        _create_log(
            RagQueryLog.STATUS_ERROR,
            selected_for_log,
            error_stage=RagQueryLog.ERROR_STAGE_RETRIEVAL,
            answer_text="",
            policy="error",
            retrieval=error_trace.summary(),
        )
        raise


def _query_rag_bounded(*, chunk_allowlist, **kwargs) -> dict:
    """``query_rag`` sous liste blanche, puis garde finale sur les sources rendues."""
    from apps.rag_core.domain.chunk_allowlist import chunk_allowlist_scope

    with chunk_allowlist_scope(chunk_allowlist):
        payload = _run_unbounded(**kwargs)
    _enforce_chunk_allowlist(payload, chunk_allowlist)
    return payload


def _run_unbounded(**kwargs) -> dict:
    """Le corps historique de ``query_rag`` (point d'injection unique pour les tests)."""
    return query_rag(**kwargs)

def _enforce_chunk_allowlist(payload: dict, allowlist) -> None:
    """Garde finale : aucune source hors liste blanche ne sort, quel que soit le leg.

    Fusion, déduplication, reranking, voisins visuels ou repli ne font que RÉORDONNER
    des candidats issus des legs bornés ; cette garde le PROUVE à chaque appel plutôt
    que de le supposer. Une source de provenance inconnue est aussi un refus : on ne
    peut pas prouver qu'elle appartient à la liste.
    """
    from apps.rag_core.domain.chunk_allowlist import STORE_CORPUS, STORE_PRIVATE
    from apps.rag_core.domain.provenance import (
        SOURCE_TYPE_DOMAIN_CORPUS,
        SOURCE_TYPE_WORKSPACE,
    )

    wanted: dict[str, set[str]] = {STORE_PRIVATE: set(), STORE_CORPUS: set()}
    for source in (payload or {}).get("sources") or []:
        if not isinstance(source, dict):
            raise RetrievalInvariantError("liste blanche: source illisible")
        nested = source.get("source") if isinstance(source.get("source"), dict) else {}
        source_type = source.get("source_type") or nested.get("source_type")
        chunk_id = str(source.get("chunk_id") or "")
        if not chunk_id:
            raise RetrievalInvariantError("liste blanche: source sans chunk_id")
        if source_type == SOURCE_TYPE_WORKSPACE:
            wanted[STORE_PRIVATE].add(chunk_id)
        elif source_type == SOURCE_TYPE_DOMAIN_CORPUS:
            wanted[STORE_CORPUS].add(chunk_id)
        else:
            raise RetrievalInvariantError(
                f"liste blanche: provenance non bornable ({source_type!r})"
            )
    for store, ids in wanted.items():
        if not ids:
            continue
        outside = ids - allowlist.members(store=store, chunk_ids=ids)
        if outside:
            logger.error(
                "retrieval_chunk_allowlist_violation",
                extra={"store": store, "outside_count": len(outside)},
            )
            raise RetrievalInvariantError(
                f"liste blanche violée: {len(outside)} fragment(s) {store} hors corpus figé"
            )

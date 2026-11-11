# backend/apps/corpus/services/retrieval.py
from __future__ import annotations

import json
import logging

"""Retrieval sur les chunks de corpus métier (Phase 6).

ÉTANCHÉITÉ ABSOLUE : on ne recherche JAMAIS dans tous les corpus. Toute requête
est filtrée par une liste EXPLICITE d'ids de corpus autorisés, calculée à partir
de la table d'association ``WorkspaceDomainCorpusAccess`` (enabled + corpus actif)
pour le workspace courant. Un workspace non associé à un corpus ne peut récupérer
aucun de ses chunks.

Le format de sortie est strictement compatible avec le retrieval privé (mêmes clés
``chunk_id``/``text``/``score``/``source``) pour passer dans merge/rerank, AVEC en
plus un ``source_type="domain_corpus"`` et les métadonnées corpus/domaine.

Stratégie FTS : la recherche lexicale exécute le MÊME plan de requêtes, avec le MÊME
repli, que le leg privé — la stratégie vit dans ``rag_core.domain.fts_plan`` et n'est
recopiée nulle part. C'est le correctif D1 : le leg corpus n'exécutait auparavant que
``build_tsquery_plan(question)[0]`` et renvoyait zéro candidat dès que la requête la
plus sélective ne matchait pas, sans jamais tenter le repli large.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from django.conf import settings
from django.db import connection, transaction

from apps.llm_router.egress import EXTERNAL_PROVIDERS, domain_corpus_context, normalize_provider
from apps.rag_core.domain.chunk_allowlist import STORE_CORPUS, allowlist_join
from apps.rag_core.domain.eligibility import ELIGIBLE_DOCUMENT_STATUS as ELIGIBLE_STATUS
from apps.rag_core.domain.fts_plan import FtsPlanExecution, execute_fts_plan
from apps.rag_core.domain.ranking import SQL_TIE_BREAK

# On réutilise les helpers FTS/vector du privé (source unique, pas de duplication).
from apps.rag_core.repositories.chunk_repository import (
    _build_label,
    _vector_literal,
    build_tsquery_plan,
    build_tsquery_plan_with_telemetry,
    fts_config,
    fts_vector_sql,
)

logger = logging.getLogger(__name__)



SOURCE_TYPE_DOMAIN_CORPUS = "domain_corpus"

#: Code d'erreur STABLE, consommé par la trace et l'exploitation.
CORPUS_EMBEDDING_MODEL_MISSING = "corpus_embedding_model_missing"


class CorpusEmbeddingConfigurationError(ValueError):
    """Le modèle d'embeddings d'un corpus vectorisé n'est pas renseigné.

    Pourquoi c'est une ERREUR et non un repli.

    Un corpus dont ``embedding_model`` est vide voyait son modèle résolu, à
    l'exécution, par une cascade de variables GLOBALES (modèle du fournisseur global,
    puis ``EMBEDDINGS_MODEL``, puis un défaut de code) qui ne le concernent pas. Deux
    conséquences, toutes deux observées ou permises :

    - la cascade résout un modèle INEXISTANT pour le provider : l'appel échoue, le leg
      vectoriel du corpus est dégradé **en silence**, et le corpus ne vote plus qu'à
      moitié. Mesuré : un modèle d'un autre fournisseur envoyé au provider, 404, leg
      vide, aucune alerte ;
    - pire, la cascade résout un modèle VALIDE mais différent de celui qui a produit les
      vecteurs stockés : la question est alors encodée dans un espace incomparable, et
      le leg répond — avec des résultats plausibles et faux.

    La provenance des vecteurs doit être portée par le corpus lui-même. Sans modèle
    explicite, on refuse avant tout calcul.
    """

    def __init__(self, corpus_id: Any, provider: str | None) -> None:
        self.code = CORPUS_EMBEDDING_MODEL_MISSING
        self.corpus_id = str(corpus_id)
        self.provider = provider
        super().__init__(
            f"{CORPUS_EMBEDDING_MODEL_MISSING}: corpus={self.corpus_id} "
            f"provider={provider!r} — renseigner DomainCorpus.embedding_model."
        )


def _note(recorder, method: str, *args, **kwargs) -> None:
    """Appelle ``recorder.<method>`` s'il existe (recorder DUCK-TYPÉ, cf. trace.py).

    Le module corpus n'importe pas ``rag_core.services.query.trace`` : il ne connaît
    du recorder que les messages qu'il lui envoie. Un recorder plus ancien (ou un
    double de test minimal) ne doit pas faire échouer le retrieval.
    """
    if recorder is None:
        return
    fn = getattr(recorder, method, None)
    if callable(fn):
        fn(*args, **kwargs)


def _meta_as_dict(meta):
    """`jsonb` lu en SQL BRUT -> dict (le curseur ne désérialise pas)."""
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, (str, bytes)):
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


# ---------------------------------------------------------------------------
# Périmètre d'autorisation
# ---------------------------------------------------------------------------
def allowed_corpus_ids_for_workspace(workspace_id) -> list[str]:
    """Liste des ids de corpus AUTORISÉS (associés + activés) pour un workspace.

    Unique source d'autorisation du retrieval corpus. Renvoie ``[]`` si aucun
    corpus n'est associé (le workspace conserve alors le comportement privé pur).
    """
    if workspace_id is None:
        return []
    from apps.corpus.models import WorkspaceDomainCorpusAccess

    return [
        str(cid)
        for cid in WorkspaceDomainCorpusAccess.objects.filter(
            workspace_id=workspace_id, enabled=True, corpus__is_active=True
        ).values_list("corpus_id", flat=True)
    ]


def associated_corpus_count(workspace_id) -> int:
    """Nombre de corpus ASSOCIÉS au workspace, activés ou non.

    Ne sert qu'à l'observabilité, et seulement quand aucun corpus n'est autorisé : c'est
    le seul moment où la distinction compte. « Aucun corpus associé » et « corpus associé
    mais désactivé » réclament deux actions opposées de l'exploitant ; les confondre,
    c'est renvoyer l'incident au point de départ. Une agrégation indexée, jamais dans le
    cas nominal.
    """
    if workspace_id is None:
        return 0
    from apps.corpus.models import WorkspaceDomainCorpusAccess

    return WorkspaceDomainCorpusAccess.objects.filter(workspace_id=workspace_id).count()


def ready_document_count(corpus_ids: Sequence[str]) -> int:
    """Documents RÉELLEMENT interrogeables des corpus donnés.

    Même définition d'éligibilité que le SQL de retrieval (``ELIGIBLE_STATUS``) : un
    compteur d'observabilité qui divergerait du filtre réel ne servirait qu'à égarer.
    """
    ids = [str(c) for c in (corpus_ids or [])]
    if not ids:
        return 0
    from apps.corpus.models import DomainCorpusDocument

    return DomainCorpusDocument.objects.filter(
        corpus_id__in=ids, status=ELIGIBLE_STATUS
    ).count()


# ---------------------------------------------------------------------------
# Recherche
# ---------------------------------------------------------------------------
def _row_to_result(row) -> dict[str, Any]:
    (
        chunk_id, text, page_number, chunk_index, meta,
        document_id, title, original_filename,
        corpus_id, corpus_name, domain_id, domain_name,
        score,
    ) = row
    filename = title or original_filename
    return {
        "chunk_id": chunk_id,
        "text": text,
        "score": float(score or 0.0),
        # Position documentaire au niveau racine : départage des ex æquo en aval,
        # sans lire le JSON `meta`. Cf. rag_core/domain/ranking.py.
        "chunk_index": chunk_index,
        "source_type": SOURCE_TYPE_DOMAIN_CORPUS,
        "source": {
            "source_type": SOURCE_TYPE_DOMAIN_CORPUS,
            "document_id": document_id,
            "filename": filename,
            "page_number": page_number,
            "label": _build_label(filename, page_number, None),
            # jsonb lu en SQL brut -> chaîne. Cf. chunk_repository._meta_as_dict.
            "meta": _meta_as_dict(meta),
            # Métadonnées corpus/domaine (typage des sources, audit, UX).
            "corpus_id": corpus_id,
            "corpus_name": corpus_name,
            "business_domain_id": domain_id,
            "business_domain_name": domain_name,
        },
    }


def _search_corpus_chunks_fts(
    *, corpus_ids: Sequence[str], tsq: str, top_k: int, min_text_rank: float
) -> list[dict]:
    """UNE requête FTS sur ``domain_corpus_chunk``, restreinte aux corpus autorisés."""
    if not tsq or not corpus_ids:
        return []

    cfg = fts_config()
    tsv = fts_vector_sql("c")
    # Liste blanche (corpus figé d'un run d'analyse) : JOIN indexable. Hors liste
    # blanche : fragment vide, requête inchangée.
    allow_join, allow_params = allowlist_join(alias="c", store=STORE_CORPUS)
    sql = f"""
    SELECT
      c.id::text as chunk_id,
      c.text,
      c.page_number,
      c.chunk_index,
      c.meta,
      d.id::text as document_id,
      d.title,
      d.original_filename,
      co.id::text as corpus_id,
      co.name as corpus_name,
      bd.id::text as domain_id,
      bd.name as domain_name,
      ts_rank(
        {tsv},
        to_tsquery('{cfg}', %(tsq)s)
      ) AS text_rank
    FROM domain_corpus_chunk c
    JOIN domain_corpus_document d ON d.id = c.document_id
    JOIN domain_corpus co ON co.id = c.corpus_id
    JOIN business_domain bd ON bd.id = co.domain_id
{allow_join}    WHERE c.corpus_id = ANY(%(ids)s::uuid[])
      AND d.corpus_id = ANY(%(ids)s::uuid[])        -- 🔒 verrou anti-fuite
      AND d.status = '{ELIGIBLE_STATUS}'            -- 🔒 éligibilité documentaire
      AND {tsv} @@ to_tsquery('{cfg}', %(tsq)s)
      AND ts_rank({tsv}, to_tsquery('{cfg}', %(tsq)s)) >= %(min_rank)s
    ORDER BY text_rank DESC, {SQL_TIE_BREAK}
    LIMIT %(k)s;
    """

    with connection.cursor() as cur:
        cur.execute(
            sql,
            {
                "ids": list(corpus_ids),
                "tsq": tsq,
                "k": int(top_k),
                "min_rank": float(min_text_rank),
                **allow_params,
            },
        )
        rows = cur.fetchall()
    return [_row_to_result(r) for r in rows]


def search_corpus_chunks_hybrid(
    *,
    corpus_ids: Sequence[str],
    question: str,
    top_k: int,
    min_text_rank: float,
    execution_sink=None,
) -> list[dict]:
    """Recherche FTS corpus **avec REPLI**, sur le plan complet de la question.

    Correctif D1 : cette fonction exécutait auparavant la seule requête la plus
    sélective du plan (``_build_tsquery``) et renvoyait ``[]`` sans jamais tenter la
    requête large — là où le leg privé, lui, repliait. La stratégie est désormais
    partagée (``domain/fts_plan.execute_fts_plan``), donc identique par construction.
    """
    ids = [str(c) for c in (corpus_ids or [])]
    if not ids:
        return []

    plan, mode, raw_n, norm_n = build_tsquery_plan_with_telemetry(question)
    rows, execution = execute_fts_plan(
        plan=plan,
        query_mode=mode,
        raw_term_count=raw_n,
        normalized_term_count=norm_n,
        runner=lambda tsq: _search_corpus_chunks_fts(
            corpus_ids=ids, tsq=tsq, top_k=top_k, min_text_rank=min_text_rank
        ),
    )
    if execution_sink is not None:
        execution_sink(execution)
    return rows


def search_corpus_chunks_vector(
    *,
    corpus_ids: Sequence[str],
    query_embedding: Sequence[float] | Iterable[float],
    top_k: int,
    embedding_fingerprint: str | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
) -> list[dict]:
    """Recherche vectorielle (cosine) sur ``domain_corpus_chunk``, corpus autorisés.

    ⚠️ L'appelant DOIT n'inclure dans ``corpus_ids`` que des corpus partageant le
    même provider/modèle d'embeddings que ``query_embedding`` (vecteurs comparables).

    ``embedding_fingerprint`` — quand il est fourni, seuls les chunks des documents
    portant CETTE empreinte et non marqués ``reindex_required`` sont comparés. Le filtre
    est appliqué en SQL, dans la même requête que le tri vectoriel : charger tous les
    candidats pour les écarter ensuite en Python ferait payer le coût du calcul de
    distance sur des vecteurs qu'on sait incomparables, et laisserait la fenêtre
    ``LIMIT`` se remplir de documents à exclure.
    """
    corpus_ids = [str(c) for c in (corpus_ids or [])]
    if not corpus_ids or query_embedding is None:
        return []

    k = max(1, int(top_k or 10))
    emb_lit = _vector_literal(query_embedding)
    # Les quatre conditions du filtre ORM sont reprises telles quelles : l'empreinte
    # suffirait (elle hache le triplet), mais deux chemins qui décident la même chose
    # doivent le décider de la même façon — sinon un compteur annonce « incohérent »
    # pendant que le SQL continue de comparer.
    fingerprint_clause = (
        "      AND d.embedding_fingerprint = %(fingerprint)s\n"
        "      AND lower(d.embedding_provider) = %(fp_provider)s\n"
        "      AND d.embedding_model = %(fp_model)s\n"
        "      AND d.embedding_dimension = %(fp_dimension)s\n"
        "      AND d.reindex_required = FALSE\n"
        if embedding_fingerprint
        else ""
    )
    allow_join, allow_params = allowlist_join(alias="c", store=STORE_CORPUS)
    sql = f"""
    SELECT
      c.id::text as chunk_id,
      c.text,
      c.page_number,
      c.chunk_index,
      c.meta,
      d.id::text as document_id,
      d.title,
      d.original_filename,
      co.id::text as corpus_id,
      co.name as corpus_name,
      bd.id::text as domain_id,
      bd.name as domain_name,
      (1.0 / (1.0 + (c.embedding <=> (%(emb)s)::vector))) AS sim
    FROM domain_corpus_chunk c
    JOIN domain_corpus_document d ON d.id = c.document_id
    JOIN domain_corpus co ON co.id = c.corpus_id
    JOIN business_domain bd ON bd.id = co.domain_id
{allow_join}    WHERE c.corpus_id = ANY(%(ids)s::uuid[])
      AND d.corpus_id = ANY(%(ids)s::uuid[])        -- 🔒 verrou anti-fuite
      AND d.status = '{ELIGIBLE_STATUS}'            -- 🔒 éligibilité documentaire
{fingerprint_clause}      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> (%(emb)s)::vector ASC, {SQL_TIE_BREAK}
    LIMIT %(k)s;
    """
    ef_search = int(getattr(settings, "RAG_HNSW_EF_SEARCH", 40) or 40)
    params = {"ids": corpus_ids, "emb": emb_lit, "k": k, **allow_params}
    if embedding_fingerprint:
        params.update({
            "fingerprint": embedding_fingerprint,
            "fp_provider": (embedding_provider or "").strip().lower(),
            "fp_model": (embedding_model or "").strip(),
            "fp_dimension": int(embedding_dimension or 0),
        })
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = %s", [ef_search])
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row_to_result(r) for r in rows]


def fingerprint_enforcement_enabled() -> bool:
    """Le filtrage vectoriel par empreinte est-il CONTRAIGNANT ?

    OFF (défaut) : les empreintes sont calculées, comptées et tracées, mais n'excluent
    rien — comportement historique à l'octet près. ON : seuls les documents portant
    l'empreinte attendue sont comparés.

    Pourquoi un flag plutôt qu'un basculement atomique. À la sortie de la migration,
    AUCUN document existant ne porte d'empreinte : un filtrage immédiat exclurait 100 %
    du corpus de production, tuerait son leg vectoriel et, par ricochet, son admission
    calibrée. La séquence sûre est donc : déployer OFF, prouver les empreintes par
    `backfill_corpus_embedding_fingerprints`, puis activer. Le flag disparaîtra une fois
    les corpus déployés tous prouvés.
    """
    return bool(getattr(settings, "RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED", False))


def _corpus_fingerprints(corpus_ids, *, provider, model, dim) -> dict:
    """Empreinte attendue du groupe et corpus RÉELLEMENT exploitables, un par un.

    Renvoie aussi le rapport de cohérence de chaque corpus : c'est lui que la trace
    expose, et c'est lui qui explique pourquoi un corpus a disparu du leg vectoriel.
    Aucune écriture, deux agrégations indexées par corpus.
    """
    from apps.corpus.domain.embedding_identity import EmbeddingIdentity
    from apps.corpus.models import DomainCorpus
    from apps.corpus.services.embedding_consistency import consistency_report

    identity = EmbeddingIdentity(provider=provider or "", model=model or "", dimension=int(dim))
    usable, reports = [], {}
    for corpus in DomainCorpus.objects.filter(id__in=list(corpus_ids)):
        report = consistency_report(corpus, identity=identity)
        reports[str(corpus.id)] = report
        if report.vector_retrieval_available:
            usable.append(str(corpus.id))
    return {"fingerprint": identity.fingerprint, "usable_ids": sorted(usable), "reports": reports}


def _group_corpus_ids_by_embedding(corpus_ids: Sequence[str]) -> list[tuple[str | None, str | None, list[str]]]:
    """Regroupe les corpus par (provider, model) d'embeddings.

    La recherche vectorielle exige des vecteurs comparables : on encode la question
    une fois PAR groupe, jamais en mélangeant des providers/modèles incompatibles.

    Le `.order_by("id")` n'est PAS cosmétique : sans lui, PostgreSQL ne garantit aucun
    ordre de lignes, donc l'ordre des groupes varie. Or `retrieve_corpus_candidates`
    fusionne les groupes SÉQUENTIELLEMENT (`merge_candidates` cumulatif, qui tronque à
    chaque passe) : un ordre de groupes différent produit un ensemble de candidats
    différent à score égal. Les groupes sont ensuite triés eux-mêmes, pour ne dépendre
    ni de l'ordre de la base, ni de l'ordre d'insertion du dict.
    """
    from apps.corpus.models import DomainCorpus

    groups: dict[tuple[str | None, str | None], list[str]] = {}
    rows = DomainCorpus.objects.filter(id__in=list(corpus_ids)).order_by("id").values_list(
        "id", "embedding_provider", "embedding_model"
    )
    for cid, provider, model in rows:
        norm_provider = (provider or "").strip().lower() or None
        norm_model = (model or "").strip() or None
        # Un corpus VECTORISÉ doit porter son modèle. Sans lui, la cascade globale
        # déciderait à sa place — cf. CorpusEmbeddingConfigurationError.
        if norm_provider and not norm_model:
            raise CorpusEmbeddingConfigurationError(cid, norm_provider)
        key = (norm_provider, norm_model)
        groups.setdefault(key, []).append(str(cid))
    return [
        (p, m, sorted(ids))
        for (p, m), ids in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or ""))
    ]


# ---------------------------------------------------------------------------
# Candidats
# ---------------------------------------------------------------------------
@dataclass
class CorpusCandidateLegs:
    """Candidats corpus, exposés PAR MODALITÉ **et** sous leur forme historique.

    ``merged`` est l'accumulation historique (FTS puis vectoriel dans un seul cumul) :
    le chemin de fusion legacy la consomme telle quelle, à l'octet près.

    ``text`` / ``vector`` sont les mêmes candidats, tenus séparés. Leur RANG dans leur
    propre liste est ce qui permet à la fusion unifiée (RRF) de les comparer aux legs
    privés : un rang est sans dimension, un ``ts_rank`` ne l'est pas.
    """

    merged: list[dict] = field(default_factory=list)
    text: list[dict] = field(default_factory=list)
    vector: list[dict] = field(default_factory=list)
    #: Leg vectoriel AVANT le filtrage au seuil ``RAG_MIN_SCORE``. Sert uniquement au
    #: signal d'admission, jamais au classement.
    #:
    #: Pourquoi cette liste existe. Le signal est la moyenne des dix meilleures
    #: similarités du corpus pour la question ; la calculer après le seuil la ferait
    #: dépendre d'un réglage sans rapport, et surtout : un corpus dont TOUTES les
    #: similarités sont basses — le cas même que la porte doit rattraper — n'aurait plus
    #: aucun candidat, donc plus de signal du tout. « Faible » deviendrait « inconnu ».
    vector_raw: list[dict] = field(default_factory=list)

    @property
    def requested(self) -> bool:
        return True


EMPTY_CORPUS_LEGS = CorpusCandidateLegs()


def _email_space(rag_space) -> bool:
    """L'espace demandeur est-il alimenté par un connecteur de messagerie ? (toujours non en édition Open)"""
    # RAGatm Open : aucun connecteur de messagerie — jamais une « boîte » rattachée.
    return False


def retrieve_corpus_candidate_legs(
    *,
    workspace_id,
    queries: Sequence[str],
    top_k: int,
    vector_top_k: int,
    hybrid_enabled: bool,
    min_text_rank: float,
    recorder=None,
    rag_space=None,
) -> CorpusCandidateLegs:
    """Candidats corpus métier (FTS + vector optionnel) pour un workspace.

    ``rag_space`` (espace DEMANDEUR, optionnel) : conservé pour le contrat d'appel. En
    édition Open, aucun groupe d'embeddings n'est externe et aucun connecteur de messagerie
    n'alimente un espace — la requête ne quitte jamais l'infrastructure.

    Renvoie des legs VIDES si le workspace n'a aucun corpus autorisé : le pipeline
    privé reste alors strictement inchangé. Toutes les sources portent ``source_type``.

    ``recorder`` (optionnel, duck-typé — cf. ``query.trace.CorpusLegRecorder``) reçoit
    l'état d'exécution du leg (P1-A0). Le comportement fonctionnel est INCHANGÉ : sur
    erreur, on continue toujours, mais le silence total (``except: pass``) est remplacé
    par un log structuré non sensible + la remontée de la catégorie d'erreur.
    """
    corpus_ids = allowed_corpus_ids_for_workspace(workspace_id)
    # Une agrégation indexée supplémentaire, assumée : sans elle, « aucun corpus
    # associé » et « corpus associé mais désactivé » sont indiscernables — deux
    # situations qui appellent deux actions opposées de l'exploitant.
    _note(
        recorder,
        "note_scope",
        associated=associated_corpus_count(workspace_id),
        allowed=len(corpus_ids),
    )

    if not corpus_ids:
        # Aucun corpus autorisé : leg NON demandé (état distinct d'un corpus vide).
        return CorpusCandidateLegs()

    _note(recorder, "note_requested")
    _note(recorder, "note_ready_documents", ready_document_count(corpus_ids))

    # Imports paresseux : évite tout cycle corpus <-> rag_core.query.
    from apps.llm_router.embeddings import embed_texts
    from apps.rag_core.services.query.errors import raise_if_programming_error
    from apps.rag_core.services.query.hygiene import clean_candidates
    from apps.rag_core.services.query.merge import merge_candidates

    qlist = list(queries or [])
    pool_k = max(top_k, vector_top_k)
    legs = CorpusCandidateLegs()

    # 1) FTS multi-pass sur l'ensemble des corpus autorisés, AVEC repli de plan (D1).
    for qx in qlist:
        try:
            t = search_corpus_chunks_hybrid(
                corpus_ids=corpus_ids,
                question=qx,
                top_k=top_k,
                min_text_rank=min_text_rank,
                execution_sink=lambda ex: _note(recorder, "note_fts_plan", ex),
            )
            t = clean_candidates(t, min_score=min_text_rank)
            legs.text = merge_candidates(a=legs.text, b=t, top_k=pool_k)
            legs.merged = merge_candidates(a=legs.merged, b=t, top_k=pool_k)
        except Exception as exc:
            if recorder is not None:
                recorder.note_fts_error(exc)
            logger.exception("corpus FTS retrieval failed (query_chars=%d)", len(qx or ""))
            # P1-A1 : un BUG de programmation se propage (l'appelant décidera error) ;
            # une panne de DÉPENDANCE attendue reste dégradable (corpus optionnel).
            raise_if_programming_error(exc, leg="business_corpus")

    # 2) Vectoriel optionnel, GROUPÉ par provider/modèle d'embeddings.
    if hybrid_enabled:
        dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))
        try:
            embedding_groups = _group_corpus_ids_by_embedding(corpus_ids)
        except CorpusEmbeddingConfigurationError as exc:
            # Configuration de corpus invalide : le leg vectoriel est refusé, le leg
            # lexical déjà calculé reste valable. L'erreur est NOMMÉE dans la trace.
            _note(recorder, "note_vector_error_code", exc.code)
            if recorder is not None:
                recorder.note_vector_error(exc)
            logger.error(
                "corpus_vector_configuration_error",
                extra={"error_code": exc.code, "corpus_id": exc.corpus_id,
                       "embedding_provider": exc.provider},
            )
            embedding_groups = []
        # l'identité d'embedding du corpus est CELLE-CI — celle qui vient de
        # servir à grouper les requêtes vectorielles. La consigner ne coûte aucune
        # requête et évite qu'un consommateur ait à la redériver depuis un corpus_id.
        _note(recorder, "note_embedding_groups", embedding_groups)
        enforce = fingerprint_enforcement_enabled()
        for provider, model, ids in embedding_groups:
            # Cohérence documentaire ÉVALUÉE PAR CORPUS : dans un groupe, un corpus
            # cohérent doit pouvoir participer même si un autre ne l'est pas. Une
            # décision de groupe punirait le corpus sain pour la faute du voisin.
            fingerprints = _corpus_fingerprints(ids, provider=provider, model=model, dim=dim)
            _note(recorder, "note_corpus_consistency", fingerprints["reports"])
            usable_ids = fingerprints["usable_ids"] if enforce else list(ids)
            if enforce and not usable_ids:
                _note(recorder, "note_vector_incoherent")
                continue
            # Contexte d'egress du GROUPE : le groupe est justement constitué par
            # (provider, modèle), donc la décision est la même pour tous ses corpus.
            # Résolu une fois par groupe, pas par reformulation.
            if normalize_provider(provider) in EXTERNAL_PROVIDERS and _email_space(rag_space):
                _note(recorder, "note_vector_error_code", "email_space_external_egress_refused")
                logger.warning("corpus_vector_external_refused_for_email_space",
                               extra={"embedding_provider": provider, "corpus_count": len(ids)})
                continue
            group_ctx = domain_corpus_context(
                corpus_id=(list(ids)[0] if ids else ""),
                provider=provider,
                operation="embedding.query",
            )
            try:
                for qx in qlist:
                    emb = embed_texts(
                        [qx], dim=dim, provider=provider, model=model,
                        # Une QUESTION posée au corpus sort par le même chemin que
                        # ses documents : même contexte, même décision.
                        context=group_ctx,
                    )[0]
                    # la dimension de l'identité est celle du vecteur
                    # OBTENU, pas celle demandée — un provider peut rendre autre
                    # chose, et l'identité doit dire ce qui a réellement servi.
                    _note(recorder, "note_embedding_dimension", provider, model, len(emb))
                    v = search_corpus_chunks_vector(
                        corpus_ids=usable_ids, query_embedding=emb, top_k=vector_top_k,
                        embedding_fingerprint=fingerprints["fingerprint"] if enforce else None,
                        embedding_provider=provider, embedding_model=model,
                        embedding_dimension=dim,
                    )
                    # Conservé AVANT le seuil : c'est la matière du signal d'admission.
                    legs.vector_raw = merge_candidates(a=legs.vector_raw, b=v, top_k=pool_k)
                    v = clean_candidates(v)
                    legs.vector = merge_candidates(a=legs.vector, b=v, top_k=pool_k)
                    legs.merged = merge_candidates(a=legs.merged, b=v, top_k=pool_k)
            except Exception as exc:
                if recorder is not None:
                    recorder.note_vector_error(exc)
                logger.exception("corpus vector retrieval failed (query_chars=%d)", len(qx or ""))
                raise_if_programming_error(exc, leg="business_corpus")
    else:
        _note(recorder, "note_vector_disabled")

    _note(recorder, "note_leg_counts", text=len(legs.text), vector=len(legs.vector))
    if recorder is not None:
        recorder.note_candidate_count(len(legs.merged))

    return legs


def retrieve_corpus_candidates(
    *,
    workspace_id,
    queries: Sequence[str],
    top_k: int,
    vector_top_k: int,
    hybrid_enabled: bool,
    min_text_rank: float,
    recorder=None,
) -> list[dict]:
    """Vue HISTORIQUE des candidats corpus (liste unique déjà fusionnée).

    Conservée telle quelle : c'est le contrat consommé par le chemin de fusion legacy
    et par les appelants externes. Cf. ``retrieve_corpus_candidate_legs`` pour la vue
    par modalité, nécessaire à la fusion unifiée.
    """
    return retrieve_corpus_candidate_legs(
        workspace_id=workspace_id,
        queries=queries,
        top_k=top_k,
        vector_top_k=vector_top_k,
        hybrid_enabled=hybrid_enabled,
        min_text_rank=min_text_rank,
        recorder=recorder,
    ).merged

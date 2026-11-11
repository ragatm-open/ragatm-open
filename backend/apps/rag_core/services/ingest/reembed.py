"""Recalculer les vecteurs d'un corpus existant SANS toucher à son texte.

## Pourquoi cette primitive existe

Le seul chemin de réindexation disponible était ``extract -> chunk -> embed -> index``.
Il recalcule tout, y compris ce qui ne dépend pas du modèle d'embedding. Or migrer un
espace de ``ollama``/``nomic-embed-text`` vers ``ai_gateway``/``legacy-768`` ne change
que la **provenance** des vecteurs : mêmes poids amont, identité désormais vérifiée par
digest. Le texte, le découpage et les identifiants n'ont aucune raison de bouger.

Les faire bouger quand même a un coût qui n'est pas seulement du temps machine : un
corpus redécoupé n'est plus comparable à celui d'avant, si bien qu'on ne peut plus
mesurer ce que la migration a changé — tout a changé en même temps.

Cette primitive fait donc exactement une chose :

    fragments existants -> nouveaux vecteurs -> nouvelle provenance, en un seul commit

## Ce qu'elle ne fait PAS, et comment on le sait

- **aucune extraction** : elle n'importe ni n'appelle `extract_document`, l'OCR ou un
  parseur. Un test lit la source de ce module et échoue si un tel appel apparaît ;
- **aucun redécoupage** : elle n'appelle pas `resolve_chunk_budget` ni le découpeur.
  Elle VÉRIFIE que chaque fragment tient sous le plafond du modèle cible — vérifier
  n'est pas découper ;
- **aucune écriture du RagSpace** : la gouvernance d'un espace est une décision
  distincte de la provenance de ses fragments. Les confondre ferait basculer un espace
  entier au premier document traité.

## Fail-closed

Contrairement au reranking, une migration d'embeddings ne peut pas être fail-open : un
vecteur manquant ou d'une autre dimension ne dégrade pas la réponse, il produit un
corpus dont une partie est incomparable — invisible au retrieval, qui ne verrait qu'un
rappel en baisse. Toute anomalie lève AVANT la moindre écriture.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.llm_router.capabilities import governed_model_max_input_chars
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.embedding_pipeline import TASK_DOCUMENT
from apps.llm_router.embeddings import embed_texts_with_identity, resolve_space_embedding
from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services import ops_counters

logger = logging.getLogger("rag.reembed")

#: Compteurs d'exploitation. Cardinalité BORNÉE : aucun identifiant de document,
#: d'espace ou de fragment n'y entre.
COUNTER_NAMES = (
    "rag_reembed_attempted_total",
    "rag_reembed_success_total",
    "rag_reembed_failure_total",
    "rag_reembed_skipped_total",
    "rag_reembed_chunks_total",
    "rag_reembed_duration_ms_total",
)

#: Motifs d'inéligibilité. Stables : ils sont consommés par le préflight et les rapports.
INELIGIBLE_NOT_READY = "not_ready"
INELIGIBLE_NO_CHUNKS = "no_chunks"
INELIGIBLE_REINDEX_REQUIRED = "reindex_required"
INELIGIBLE_CHUNK_TOO_LONG = "chunk_too_long"
INELIGIBLE_UNKNOWN_MODEL_LIMIT = "unknown_model_limit"

SKIPPED_ALREADY_TARGET = "already_target_identity"


class ReembedError(RuntimeError):
    """Échec de ré-embedding. Levée AVANT toute écriture, ou dans la transaction."""

    def __init__(self, message: str, *, code: str = "reembed_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocumentPreflight:
    """Ce qu'on peut dire d'un document SANS rien écrire ni appeler la Gateway."""

    document_id: str
    chunk_count: int
    current_provider: str
    current_model: str
    current_families: tuple[str, ...]
    target_provider: str
    target_model: str
    max_chunk_chars: int
    target_max_chars: int | None
    eligible: bool
    reason: str = ""
    already_target: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_count": self.chunk_count,
            "current_identity": f"{self.current_provider or '<vide>'}/{self.current_model or '<vide>'}",
            "current_families": list(self.current_families),
            "target_identity": f"{self.target_provider}/{self.target_model}",
            "max_chunk_chars": self.max_chunk_chars,
            "target_max_chars": self.target_max_chars,
            "eligible": self.eligible,
            "reason": self.reason,
            "already_target": self.already_target,
        }


@dataclass(frozen=True)
class ReembedResult:
    document_id: str
    chunks: int
    pipeline_id: str
    pipeline_family: str
    duration_ms: float
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class SpacePreflight:
    """Agrégat LECTURE SEULE pour un espace entier."""

    rag_space_id: str
    target_provider: str
    target_model: str
    space_identity_matches_target: bool
    documents_total: int = 0
    documents_eligible: int = 0
    documents_already_target: int = 0
    chunks_eligible: int = 0
    max_chunk_chars: int = 0
    chunks_over_limit: int = 0
    ineligible_by_reason: dict[str, int] = field(default_factory=dict)
    identity_distribution: dict[str, int] = field(default_factory=dict)
    estimated_embedding_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rag_space_id": self.rag_space_id,
            "target_identity": f"{self.target_provider}/{self.target_model}",
            "space_identity_matches_target": self.space_identity_matches_target,
            "documents_total": self.documents_total,
            "documents_eligible": self.documents_eligible,
            "documents_already_target": self.documents_already_target,
            "chunks_eligible": self.chunks_eligible,
            "max_chunk_chars": self.max_chunk_chars,
            "chunks_over_limit": self.chunks_over_limit,
            "ineligible_by_reason": dict(sorted(self.ineligible_by_reason.items())),
            "identity_distribution": dict(sorted(self.identity_distribution.items())),
            "estimated_embedding_calls": self.estimated_embedding_calls,
        }


def _batch_size() -> int:
    return max(1, int(getattr(settings, "RAG_EMBED_BATCH_SIZE", 64)))


def _target(space, provider: str | None, model: str | None) -> tuple[str, str]:
    """Identité visée. Par défaut celle de l'ESPACE — un ré-embedding sans cible
    explicite recalcule ce que l'espace déclare, ce qui est le cas de reprise."""
    sp, sm = resolve_space_embedding(space)
    return ((provider or sp or "").strip().lower(), (model or sm or "").strip())


# --------------------------------------------------------------------------- #
# Préflight — lecture seule, aucune écriture, aucun appel Gateway
# --------------------------------------------------------------------------- #

def preflight_document(document, *, provider: str | None = None,
                       model: str | None = None) -> DocumentPreflight:
    # On relit l'état PERSISTÉ plutôt que de faire confiance à l'objet du caller.
    # Un appelant qui garde une instance en mémoire après un premier ré-embedding
    # verrait une empreinte périmée, conclurait à tort que le travail reste à faire,
    # et recalculerait tout — c'est exactement le cas de reprise que l'idempotence
    # doit couvrir.
    document = Document.objects.get(pk=document.pk)
    space = document.rag_space
    tp, tm = _target(space, provider, model)

    chunks = Chunk.objects.filter(document=document)
    n = chunks.count()
    longueurs = list(chunks.values_list("text", flat=True)) if n else []
    max_chars = max((len(t or "") for t in longueurs), default=0)
    familles = tuple(sorted({f or "<null>" for f in chunks.values_list("pipeline_family", flat=True)}))

    plafond = governed_model_max_input_chars(tm) if tp == "ai_gateway" else None

    def _ko(motif: str) -> DocumentPreflight:
        return DocumentPreflight(
            document_id=str(document.id), chunk_count=n,
            current_provider=document.embedding_provider or "",
            current_model=document.embedding_model or "",
            current_families=familles, target_provider=tp, target_model=tm,
            max_chunk_chars=max_chars, target_max_chars=plafond,
            eligible=False, reason=motif,
        )

    if document.status != DocumentStatus.READY:
        return _ko(INELIGIBLE_NOT_READY)
    if n == 0:
        return _ko(INELIGIBLE_NO_CHUNKS)
    if document.reindex_required:
        # Un document marqué à réindexer a un problème que le ré-embedding ne règle
        # pas : son texte lui-même peut être absent ou périmé. Il relève du chemin
        # de réindexation complet.
        return _ko(INELIGIBLE_REINDEX_REQUIRED)
    if tp == "ai_gateway":
        if plafond is None:
            return _ko(INELIGIBLE_UNKNOWN_MODEL_LIMIT)
        if max_chars > plafond:
            # Refus EXPLICITE : jamais de troncature silencieuse. Tronquer produirait
            # un vecteur qui ne décrit pas le fragment qu'il prétend décrire.
            return _ko(INELIGIBLE_CHUNK_TOO_LONG)

    deja = (
        (document.embedding_provider or "").strip().lower() == tp
        and (document.embedding_model or "").strip() == tm
        and "<null>" not in familles
    )
    return DocumentPreflight(
        document_id=str(document.id), chunk_count=n,
        current_provider=document.embedding_provider or "",
        current_model=document.embedding_model or "",
        current_families=familles, target_provider=tp, target_model=tm,
        max_chunk_chars=max_chars, target_max_chars=plafond,
        eligible=True, already_target=deja,
    )


def preflight_space(space, *, provider: str | None = None,
                    model: str | None = None) -> SpacePreflight:
    tp, tm = _target(space, provider, model)
    sp, sm = resolve_space_embedding(space)
    agg = SpacePreflight(
        rag_space_id=str(space.id), target_provider=tp, target_model=tm,
        space_identity_matches_target=((sp or "").lower() == tp and (sm or "") == tm),
    )
    lots = _batch_size()
    for doc in Document.objects.filter(rag_space=space).iterator():
        agg.documents_total += 1
        pf = preflight_document(doc, provider=tp, model=tm)
        cle = f"{pf.current_provider or '<vide>'}/{pf.current_model or '<vide>'}"
        agg.identity_distribution[cle] = agg.identity_distribution.get(cle, 0) + 1
        agg.max_chunk_chars = max(agg.max_chunk_chars, pf.max_chunk_chars)
        if pf.target_max_chars and pf.max_chunk_chars > pf.target_max_chars:
            agg.chunks_over_limit += Chunk.objects.filter(
                document=doc, text__regex=r"^.{%d,}$" % (pf.target_max_chars + 1)
            ).count()
        if not pf.eligible:
            agg.ineligible_by_reason[pf.reason] = agg.ineligible_by_reason.get(pf.reason, 0) + 1
            continue
        agg.documents_eligible += 1
        if pf.already_target:
            agg.documents_already_target += 1
            continue
        agg.chunks_eligible += pf.chunk_count
        agg.estimated_embedding_calls += (pf.chunk_count + lots - 1) // lots
    return agg


# --------------------------------------------------------------------------- #
# La primitive
# --------------------------------------------------------------------------- #

def reembed_document(document, *, provider: str | None = None, model: str | None = None,
                     force: bool = False) -> ReembedResult:
    """Recalcule les vecteurs des fragments EXISTANTS de ce document.

    Préserve, au bit près : identifiants de fragments, texte, index, page, `meta`,
    et donc la colonne FTS générée (``text_tsv_french`` est
    ``GENERATED ALWAYS AS to_tsvector('french', text) STORED`` — PostgreSQL interdit
    de l'écrire, et elle ne peut pas diverger d'un texte inchangé).

    Change : le vecteur, `pipeline_id`, `pipeline_family`, et l'empreinte
    d'indexation du document (`embedding_provider`/`embedding_model`).

    ## Atomicité

    Tous les vecteurs sont calculés et validés AVANT la première écriture, puis écrits
    en une seule transaction. Un échec au dernier lot laisse donc la base strictement
    dans son état d'origine — pas un document à moitié migré, dont la moitié des
    fragments serait devenue invisible au retrieval sans qu'aucune erreur ne subsiste.
    """
    debut = time.perf_counter()
    document = Document.objects.get(pk=document.pk)
    space = document.rag_space
    tp, tm = _target(space, provider, model)

    pf = preflight_document(document, provider=tp, model=tm)
    if not pf.eligible:
        ops_counters.incr("rag_reembed_failure_total")
        raise ReembedError(
            f"document {document.id} inéligible au ré-embedding : {pf.reason} "
            f"(fragments={pf.chunk_count}, plus long={pf.max_chunk_chars} car., "
            f"plafond cible={pf.target_max_chars})",
            code=pf.reason,
        )
    if pf.already_target and not force:
        # Idempotence : rejouer un ré-embedding déjà abouti ne doit ni coûter, ni
        # réécrire. C'est ce qui rend une reprise après échec sûre.
        ops_counters.incr("rag_reembed_skipped_total")
        return ReembedResult(
            document_id=str(document.id), chunks=pf.chunk_count, pipeline_id="",
            pipeline_family=(pf.current_families[0] if pf.current_families else ""),
            duration_ms=(time.perf_counter() - debut) * 1000.0,
            skipped=True, skip_reason=SKIPPED_ALREADY_TARGET,
        )

    ops_counters.incr("rag_reembed_attempted_total")
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))

    # Ordre STABLE et explicite : les vecteurs sont appariés aux fragments par
    # position. Un ordre implicite ferait dépendre l'appariement du plan PostgreSQL.
    fragments = list(
        Chunk.objects.filter(document=document).order_by("chunk_index", "id")
        .values_list("id", "text")
    )
    ids = [c[0] for c in fragments]
    textes = [c[1] or "" for c in fragments]

    ctx = LlmExecutionContext.for_rag_space(space, operation="embedding.document")
    lots = _batch_size()
    vecteurs: list[Any] = []
    pid: str | None = None
    famille: str | None = None
    try:
        for i in range(0, len(textes), lots):
            lot = embed_texts_with_identity(
                textes[i:i + lots], dim=dim, provider=tp, model=tm,
                task=TASK_DOCUMENT, rag_space_id=getattr(space, "id", None),
                document_id=getattr(document, "id", None), context=ctx,
            )
            if pid is None:
                pid = lot.pipeline_id
                # Famille portée par le lot (attestée), jamais découpée de l'identifiant.
                famille = lot.family or None
            elif lot.pipeline_id != pid or (lot.family or None) != famille:
                # Même garde que l'indexation : un rechargement de modèle en cours de
                # document produirait un document dont une moitié est comparable et
                # l'autre non.
                raise ReembedError(
                    "identité de pipeline changée en cours de document — aucun "
                    "fragment écrit", code="embedding_pipeline_changed_mid_document",
                )
            vecteurs.extend(lot.vectors)
    except ReembedError:
        ops_counters.incr("rag_reembed_failure_total")
        raise
    except Exception as exc:
        ops_counters.incr("rag_reembed_failure_total")
        raise ReembedError(
            f"échec d'embedding pour le document {document.id} : "
            f"{type(exc).__name__} — aucun fragment écrit",
            code="embedding_failed",
        ) from exc

    # Validations AVANT écriture. Un vecteur d'une autre dimension ne dégrade pas la
    # recherche, il la rend fausse : pgvector comparerait des espaces différents.
    if len(vecteurs) != len(ids):
        ops_counters.incr("rag_reembed_failure_total")
        raise ReembedError(
            f"{len(vecteurs)} vecteurs rendus pour {len(ids)} fragments — aucun écrit",
            code="vector_count_mismatch",
        )
    for rang, v in enumerate(vecteurs):
        if len(v) != dim:
            ops_counters.incr("rag_reembed_failure_total")
            raise ReembedError(
                f"vecteur de dimension {len(v)} au rang {rang}, attendu {dim} — "
                "aucun fragment écrit", code="dimension_mismatch",
            )

    with transaction.atomic():
        # Même discipline que l'ingestion : le ré-embedding a été préparé sous une
        # identité cible explicite ; si l'espace a bougé pendant les appels Gateway,
        # publier ce résultat produirait un document incohérent avec son espace.
        from apps.rag_core.services.ragspace_mutation import (
            lock_ragspace_for_mutation,
        )

        lock_ragspace_for_mutation(space.id)

        objets = [
            Chunk(id=cid, embedding=vec, pipeline_id=pid or None, pipeline_family=famille)
            for cid, vec in zip(ids, vecteurs, strict=True)
        ]
        Chunk.objects.bulk_update(objets, ["embedding", "pipeline_id", "pipeline_family"],
                                  batch_size=200)
        Document.objects.filter(id=document.id).update(
            embedding_provider=tp, embedding_model=tm, reindex_required=False,
        )

    duree = (time.perf_counter() - debut) * 1000.0
    ops_counters.incr_many({
        "rag_reembed_success_total": 1,
        "rag_reembed_chunks_total": len(ids),
        "rag_reembed_duration_ms_total": int(duree),
    })
    # Jamais de texte de fragment dans les journaux.
    logger.info(
        "reembed.document ok chunks=%s model=%s duration_ms=%.0f",
        len(ids), tm, duree,
        extra={"document_id": str(document.id), "pipeline_family": famille},
    )
    return ReembedResult(
        document_id=str(document.id), chunks=len(ids), pipeline_id=pid or "",
        pipeline_family=famille or "", duration_ms=duree,
    )


def counters_snapshot() -> dict[str, int]:
    return ops_counters.snapshot(list(COUNTER_NAMES))

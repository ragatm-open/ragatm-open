"""Migrer l'identité d'embedding d'un RagSpace ENTIER, atomiquement.

## Le défaut que cette primitive corrige

Le ré-embedding par document (`reembed.py`) est atomique **par document**. Cela ne
suffit pas à migrer un espace : la garde de cohérence P1-D compare l'empreinte du
DOCUMENT à l'identité de l'ESPACE, et un document ne porte qu'une empreinte. Il ne peut
donc pas satisfaire l'ancienne et la nouvelle identité à la fois.

Mesuré le 23/08/2026 sur un espace jetable — le lecteur voyait ``4 → 0 → 4`` :

    avant                        ollama/nomic-embed-text      4 fragments servis
    après ré-embedding           ollama/nomic-embed-text      0
    après bascule de l'espace    ai_gateway/legacy-768        4
    après rollback de la bascule ollama/nomic-embed-text      0

Aucun ORDRE de deux opérations séparées n'évite cette fenêtre : ``A → B`` cache les
documents pendant toute la phase A, ``B → A`` les marque `reindex_required` et produit
la même fenêtre à l'envers. La seule issue est qu'il n'y ait **plus deux opérations**.

## Le modèle : préparer optimiste, commiter pessimiste

    PREPARE   lecture seule · calcule tous les vecteurs · scelle l'état source
              (hors transaction : aucun appel réseau ne doit tenir un verrou)
    COMMIT    transaction courte · verrouille · revalide le sceau · écrit
              fragments + empreintes documentaires + ligne RagSpace, en UN commit

Un lecteur ne peut donc observer que deux mondes : l'ancien complet, ou le nouveau
complet. Jamais d'intermédiaire.

## Mémoire

Les vecteurs sont conservés en `array('f')` et non en `list[float]`. Mesuré pour 8 713
vecteurs de dimension 768 : **28 Mo** contre **221 Mo** — une liste Python paie huit
fois le prix des données. C'est ce qui rend la préparation en mémoire praticable et
évite d'inventer une table temporaire.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from array import array
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.embedding_pipeline import TASK_DOCUMENT
from apps.llm_router.embeddings import embed_texts_with_identity, resolve_space_embedding
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services import ops_counters
from apps.rag_core.services.ingest.reembed import preflight_document

logger = logging.getLogger("rag.space_migration")

COUNTER_NAMES = (
    "rag_space_embedding_migration_attempted_total",
    "rag_space_embedding_migration_prepared_total",
    "rag_space_embedding_migration_success_total",
    "rag_space_embedding_migration_abort_total",
    "rag_space_embedding_migration_failure_total",
    "rag_space_embedding_migration_chunks_total",
)

ABORT_SOURCE_CHANGED = "source_state_changed"
ABORT_ALREADY_TARGET = "already_target"


class SpaceMigrationError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class SourceStateChanged(SpaceMigrationError):
    """L'état source a changé entre PREPARE et COMMIT. Aucune écriture n'a lieu."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ABORT_SOURCE_CHANGED)


@dataclass(frozen=True)
class TargetIdentity:
    provider: str
    model: str
    dimension: int
    pipeline_id: str
    pipeline_family: str


@dataclass(frozen=True)
class SpaceMigrationPlan:
    """Plan IMMUABLE. Les vecteurs y vivent en mémoire, jamais sérialisés."""

    rag_space_id: str
    source_provider: str
    source_model: str
    source_governed: bool
    target: TargetIdentity
    document_ids: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    vectors: tuple[array, ...] = field(repr=False, default=())
    source_state_hash: str = ""
    excluded_documents: tuple[dict[str, Any], ...] = ()
    documents_already_target: tuple[str, ...] = ()
    prepared_at_monotonic: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Vue journalisable. Ne contient AUCUN vecteur ni texte."""
        return {
            "rag_space_id": self.rag_space_id,
            "source": f"{self.source_provider}/{self.source_model}",
            "source_governed": self.source_governed,
            "target": f"{self.target.provider}/{self.target.model}",
            "target_pipeline_family": self.target.pipeline_family,
            "target_dimension": self.target.dimension,
            "documents": len(self.document_ids),
            "documents_already_target": len(self.documents_already_target),
            "chunks": len(self.chunk_ids),
            "excluded_documents": len(self.excluded_documents),
            "source_state_hash": self.source_state_hash,
        }


@dataclass(frozen=True)
class SpaceMigrationResult:
    rag_space_id: str
    chunks_written: int
    documents_written: int
    target_pipeline_family: str
    commit_duration_ms: float


# --------------------------------------------------------------------------- #
# Sceau de l'état source
# --------------------------------------------------------------------------- #

def source_state_hash(space) -> str:
    """Empreinte de tout ce qui rendrait un plan préparé caduc.

    Couvre l'identité de l'espace, l'ensemble des documents avec leur empreinte et leur
    statut, et l'ensemble ORDONNÉ des fragments avec le hachage de leur texte. Un ajout,
    une suppression, une réindexation ou une modification de texte change ce hachage.

    Le texte est haché, jamais inclus : un sceau ne doit pas devenir un stockage.
    """
    space = RagSpace.objects.get(pk=space.pk)
    documents = list(
        Document.objects.filter(rag_space=space)
        .order_by("id")
        .values_list("id", "status", "embedding_provider", "embedding_model", "reindex_required")
    )
    fragments = list(
        Chunk.objects.filter(rag_space=space)
        .order_by("document_id", "chunk_index", "id")
        .values_list("id", "text", "pipeline_family")
    )
    h = hashlib.sha256()
    h.update(json.dumps({
        "space": [str(space.id), space.embedding_provider or "", space.embedding_model or ""],
        "documents": [[str(d[0]), d[1], d[2] or "", d[3] or "", bool(d[4])] for d in documents],
    }, sort_keys=True, separators=(",", ":")).encode())
    for cid, texte, famille in fragments:
        h.update(str(cid).encode())
        h.update(hashlib.sha256((texte or "").encode()).digest())
        h.update((famille or "").encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# PRÉFLIGHT — lecture seule ET bon marché (aucun appel Gateway)
# --------------------------------------------------------------------------- #

#: Coût mémoire d'un vecteur conservé en `array('f')`, mesuré le 23/08/2026 :
#: 8 713 vecteurs de dimension 768 tiennent en 28 Mo, contre 221 Mo en
#: `list[float]`. C'est ce facteur 8 qui décide de la faisabilité en mémoire.
OCTETS_PAR_COMPOSANTE = 4


def preflight_space_migration(space, *, provider: str, model: str) -> dict[str, Any]:
    """Chiffre la migration SANS calculer un seul vecteur.

    Distinct de `prepare_space_migration`, qui appelle la Gateway : on doit pouvoir
    décider d'une migration — volumes, éligibilité, mémoire — sans en payer le calcul,
    et sans qu'une Gateway indisponible empêche de préparer une décision.
    """
    space = RagSpace.objects.get(pk=space.pk)
    sp, sm = resolve_space_embedding(space)
    tp, tm = (provider or "").strip().lower(), (model or "").strip()
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))
    lots = max(1, int(getattr(settings, "RAG_EMBED_BATCH_SIZE", 64)))

    eligibles = deja = fragments = 0
    exclus: dict[str, int] = {}
    servis_non_migrables = 0
    for doc in Document.objects.filter(rag_space=space).iterator():
        pf = preflight_document(doc, provider=tp, model=tm)
        if not pf.eligible:
            exclus[pf.reason] = exclus.get(pf.reason, 0) + 1
            servis_non_migrables += Chunk.objects.filter(
                document=doc, embedding__isnull=False,
                document__status=DocumentStatus.READY, document__reindex_required=False,
            ).count()
            continue
        if pf.already_target:
            deja += 1
            continue
        eligibles += 1
        fragments += pf.chunk_count

    return {
        "rag_space_id": str(space.id),
        "source": f"{sp or '<vide>'}/{sm or '<vide>'}",
        "target": f"{tp}/{tm}",
        "documents_eligible": eligibles,
        "documents_already_target": deja,
        "documents_excluded": dict(sorted(exclus.items())),
        "retrievable_chunks_not_migratable": servis_non_migrables,
        "chunks_to_migrate": fragments,
        "estimated_gateway_calls": (fragments + lots - 1) // lots,
        "estimated_vector_memory_mb": round(fragments * dim * OCTETS_PAR_COMPOSANTE / 1e6, 1),
        "source_state_hash": source_state_hash(space),
        "migratable": servis_non_migrables == 0 and (eligibles > 0 or deja > 0),
    }


# --------------------------------------------------------------------------- #
# PREPARE — aucune écriture
# --------------------------------------------------------------------------- #

def prepare_space_migration(space, *, provider: str, model: str) -> SpaceMigrationPlan:
    """Calcule tout ce qu'il faut écrire, sans rien écrire.

    Refuse AVANT tout calcul si un fragment actuellement servi appartient à un document
    inéligible : basculer l'espace le rendrait définitivement invisible.
    """
    ops_counters.incr("rag_space_embedding_migration_attempted_total")
    space = RagSpace.objects.get(pk=space.pk)
    sp, sm = resolve_space_embedding(space)
    tp, tm = (provider or "").strip().lower(), (model or "").strip()
    if not tp or not tm:
        raise SpaceMigrationError(
            "identité cible incomplète : provider et modèle sont requis. Laisser le "
            "modèle implicite ferait dépendre la migration d'un défaut d'environnement.",
            code="target_incomplete",
        )

    sceau = source_state_hash(space)
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))

    eligibles: list[Document] = []
    deja: list[str] = []
    exclus: list[dict[str, Any]] = []
    for doc in Document.objects.filter(rag_space=space).order_by("id"):
        pf = preflight_document(doc, provider=tp, model=tm)
        if pf.eligible:
            if pf.already_target:
                # État hybride : ce document porte déjà la cible alors que l'espace ne
                # l'a pas encore. Il est donc INVISIBLE aujourd'hui (l'empreinte du
                # document ne correspond pas à celle de l'espace) et la bascule le
                # rendra visible. Rien à recalculer, mais il compte dans le rapport.
                deja.append(str(doc.id))
            else:
                eligibles.append(doc)
            continue
        exclus.append({"document_id": str(doc.id), "reason": pf.reason,
                       "chunk_count": pf.chunk_count, "max_chunk_chars": pf.max_chunk_chars})

    # §43 — porte fondamentale. Un fragment SERVI aujourd'hui qui n'aurait pas
    # l'identité cible après bascule disparaîtrait du corpus, sans erreur.
    ids_exclus = [e["document_id"] for e in exclus]
    servis_exclus = Chunk.objects.filter(
        rag_space=space, document_id__in=ids_exclus, embedding__isnull=False,
        document__status=DocumentStatus.READY, document__reindex_required=False,
    ).count() if ids_exclus else 0
    if servis_exclus:
        ops_counters.incr("rag_space_embedding_migration_abort_total")
        raise SpaceMigrationError(
            f"{servis_exclus} fragment(s) actuellement servis appartiennent à des "
            "documents inéligibles : la bascule les rendrait invisibles. Traiter ces "
            "documents avant de migrer l'espace.",
            code="retrievable_chunk_not_migratable",
        )

    if not eligibles:
        raise SpaceMigrationError(
            "aucun document à migrer" + (
                " — l'espace porte déjà l'identité cible" if deja and (sp, sm) == (tp, tm)
                else " (état hybride : des documents portent déjà la cible mais pas l'espace)"
                if deja else ""),
            code=ABORT_ALREADY_TARGET if (sp, sm) == (tp, tm) else "nothing_to_migrate",
        )

    # --- Calcul des vecteurs, HORS transaction -----------------------------
    ctx = LlmExecutionContext.for_rag_space(space, operation="embedding.document")
    lots = max(1, int(getattr(settings, "RAG_EMBED_BATCH_SIZE", 64)))
    chunk_ids: list[str] = []
    vecteurs: list[array] = []
    pid: str | None = None
    famille: str = ""

    for doc in eligibles:
        fragments = list(
            Chunk.objects.filter(document=doc).order_by("chunk_index", "id")
            .values_list("id", "text")
        )
        textes = [t or "" for _, t in fragments]
        produits: list[Any] = []
        for i in range(0, len(textes), lots):
            lot = embed_texts_with_identity(
                textes[i:i + lots], dim=dim, provider=tp, model=tm, task=TASK_DOCUMENT,
                rag_space_id=space.id, document_id=doc.id, context=ctx,
            )
            if pid is None:
                pid = lot.pipeline_id
                # Famille portée par le lot (attestée), jamais découpée de l'identifiant.
                famille = lot.family or ""
            elif lot.pipeline_id != pid or (lot.family or "") != famille:
                # §37/§39 : la révision du modèle a changé pendant la préparation.
                # Mélanger deux révisions produirait un corpus dont une part est
                # comparable et l'autre non — invisible au retrieval.
                ops_counters.incr("rag_space_embedding_migration_abort_total")
                raise SpaceMigrationError(
                    f"identité de pipeline changée pendant la préparation "
                    f"({pid!r} puis {lot.pipeline_id!r}) — plan abandonné",
                    code="target_revision_drift",
                )
            produits.extend(lot.vectors)
        if len(produits) != len(fragments):
            raise SpaceMigrationError(
                f"{len(produits)} vecteurs pour {len(fragments)} fragments "
                f"(document {doc.id}) — plan abandonné", code="vector_count_mismatch",
            )
        for (cid, _), v in zip(fragments, produits, strict=True):
            if len(v) != dim:
                raise SpaceMigrationError(
                    f"dimension {len(v)} au lieu de {dim} — plan abandonné",
                    code="dimension_mismatch",
                )
            if not all(math.isfinite(x) for x in v):
                # Un NaN ne lève pas à l'écriture : il produit une distance NULL et le
                # fragment sort silencieusement de tout classement.
                raise SpaceMigrationError(
                    "vecteur non fini (NaN/inf) — plan abandonné", code="non_finite_vector",
                )
            chunk_ids.append(str(cid))
            vecteurs.append(array("f", v))

    if not famille:
        raise SpaceMigrationError(
            "famille de pipeline non résolue depuis l'identité rendue par la Gateway",
            code="pipeline_family_unresolved",
        )

    plan = SpaceMigrationPlan(
        rag_space_id=str(space.id), source_provider=sp or "", source_model=sm or "",
        source_governed=(sp or "").lower() == "ai_gateway",
        target=TargetIdentity(tp, tm, dim, pid or "", famille),
        document_ids=tuple(str(d.id) for d in eligibles),
        chunk_ids=tuple(chunk_ids), vectors=tuple(vecteurs),
        source_state_hash=sceau, excluded_documents=tuple(exclus),
        documents_already_target=tuple(deja), prepared_at_monotonic=time.monotonic(),
    )
    ops_counters.incr("rag_space_embedding_migration_prepared_total")
    logger.info("space_migration.prepared %s", json.dumps(plan.summary(), ensure_ascii=False))
    return plan


# --------------------------------------------------------------------------- #
# COMMIT — une transaction, courte
# --------------------------------------------------------------------------- #

def commit_space_migration(plan: SpaceMigrationPlan) -> SpaceMigrationResult:
    """Écrit fragments, empreintes documentaires et identité de l'espace, en UN commit.

    Aucun appel réseau n'a lieu ici : tout est déjà calculé. La transaction se limite à
    des écritures, ce qui la garde courte — une transaction qui tiendrait pendant les
    appels Gateway maintiendrait des verrous et un instantané MVCC pour rien.
    """
    debut = time.perf_counter()
    try:
        with transaction.atomic():
            # Verrou de l'espace, par la primitive de domaine — la MÊME que tous les
            # autres écrivains, avec le même ordre canonique. On ne revalide pas
            # l'identité ici : c'est le sceau d'état source, plus large, qui s'en
            # charge quelques lignes plus bas.
            from apps.rag_core.services.ragspace_mutation import (
                lock_ragspace_for_mutation,
            )

            space = lock_ragspace_for_mutation(plan.rag_space_id)
            # Verrou des documents concernés : un UPDATE/DELETE concurrent sur ces
            # lignes attendra notre commit plutôt que de s'y superposer.
            list(Document.objects.select_for_update()
                 .filter(id__in=list(plan.document_ids)).values_list("id", flat=True))

            actuel = source_state_hash(space)
            if actuel != plan.source_state_hash:
                raise SourceStateChanged(
                    "l'état source a changé depuis la préparation "
                    f"(scellé {plan.source_state_hash[:12]}…, observé {actuel[:12]}…) — "
                    "aucune écriture. Le plan doit être recalculé."
                )

            objets = [
                Chunk(id=cid, embedding=list(vec), pipeline_id=plan.target.pipeline_id,
                      pipeline_family=plan.target.pipeline_family)
                for cid, vec in zip(plan.chunk_ids, plan.vectors, strict=True)
            ]
            Chunk.objects.bulk_update(
                objets, ["embedding", "pipeline_id", "pipeline_family"], batch_size=500)

            Document.objects.filter(id__in=list(plan.document_ids)).update(
                embedding_provider=plan.target.provider,
                embedding_model=plan.target.model,
                reindex_required=False,
            )
            # L'identité de l'espace est écrite EN DERNIER et dans la MÊME transaction :
            # c'est elle qui rend le nouveau monde visible, et elle ne doit le devenir
            # qu'une fois tout le reste en place.
            RagSpace.objects.filter(pk=space.pk).update(
                embedding_provider=plan.target.provider,
                embedding_model=plan.target.model,
            )
    except SourceStateChanged:
        ops_counters.incr("rag_space_embedding_migration_abort_total")
        raise
    except Exception:
        ops_counters.incr("rag_space_embedding_migration_failure_total")
        raise

    duree = (time.perf_counter() - debut) * 1000.0
    ops_counters.incr_many({
        "rag_space_embedding_migration_success_total": 1,
        "rag_space_embedding_migration_chunks_total": len(plan.chunk_ids),
    })
    logger.info(
        "space_migration.committed chunks=%s documents=%s family=%s duration_ms=%.0f",
        len(plan.chunk_ids), len(plan.document_ids), plan.target.pipeline_family, duree,
    )
    return SpaceMigrationResult(
        rag_space_id=plan.rag_space_id, chunks_written=len(plan.chunk_ids),
        documents_written=len(plan.document_ids),
        target_pipeline_family=plan.target.pipeline_family, commit_duration_ms=duree,
    )


def counters_snapshot() -> dict[str, int]:
    return ops_counters.snapshot(list(COUNTER_NAMES))

"""Service des jobs de réindexation durable d'un RagSpace.

Création atomique + exclusivité (contrainte partielle « un job actif par espace »),
recomptage des compteurs par AGRÉGATION (jamais d'incrément naïf non atomique),
relance des échecs (nouveau job lié), et récupération des items « stale ».

L'ENQUEUE Celery se fait toujours via ``transaction.on_commit`` : jamais mettre en
file une tâche qui référence des lignes non encore commitées.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.reindex_job import (
    ERROR_INTERRUPTED,
    ITEM_UNFINISHED_STATUSES,
    JOB_ACTIVE_STATUSES,
    RagSpaceReindexJob,
    RagSpaceReindexJobItem,
    ReindexItemStatus,
    ReindexJobKind,
    ReindexJobStatus,
)

logger = logging.getLogger(__name__)

# Un item RUNNING dont le lease a expiré est réputé perdu (worker mort). Large
# devant la durée d'un document (~secondes à dizaines de secondes).
ITEM_LEASE_SECONDS = int(getattr(settings, "REINDEX_ITEM_LEASE_SECONDS", 900))

# Garde EAGER (dev sans broker) : au-delà, on refuse plutôt que de bloquer la
# requête HTTP ~30 min. En mode worker (broker réel), aucune limite.
EAGER_MAX_SYNC_DOCUMENTS = int(getattr(settings, "REINDEX_EAGER_MAX_SYNC_DOCUMENTS", 25))


class ReindexJobError(Exception):
    def __init__(self, code: str, http_status: int = 409):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass
class CreateOutcome:
    job: RagSpaceReindexJob
    created: bool
    already_running: bool


# --------------------------------------------------------------------------- #
# Sélection des candidats
# --------------------------------------------------------------------------- #
def _active_job(rag_space_id) -> RagSpaceReindexJob | None:
    """Job actif de l'espace, **tous kinds confondus**.

    L'exclusivité porte sur le RagSpace, pas sur la portée du job : une reprise
    ciblée et une réindexation globale se disputeraient les mêmes documents et les
    mêmes workers. Une seule campagne à la fois, quelle que soit sa nature.
    """
    return (
        RagSpaceReindexJob.objects
        .filter(rag_space_id=rag_space_id, status__in=list(JOB_ACTIVE_STATUSES))
        .order_by("-created_at")
        .first()
    )


def candidate_documents(rag_space_id) -> list[Document]:
    """Documents réindexables : pas déjà en cours, avec fichier source présent."""
    qs = Document.objects.filter(rag_space_id=rag_space_id).exclude(
        status__in=[DocumentStatus.PENDING, DocumentStatus.INGESTING]
    ).only("id", "storage_file")
    out = []
    for d in qs:
        sf = getattr(d, "storage_file", None)
        if sf and getattr(sf, "name", ""):
            out.append(d)
    return out


def _is_eager() -> bool:
    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


# --------------------------------------------------------------------------- #
# Reprise ciblée des documents en ERREUR
# --------------------------------------------------------------------------- #
#: Garde-fou de taille d'une campagne de reprise. Au-delà, l'utilisateur restreint
#: par la recherche ou procède en plusieurs fois : une campagne d'un seul tenant
#: dont personne ne peut suivre la fin n'aide personne.
MAX_RETRY_SELECTION = int(getattr(settings, "REINDEX_MAX_RETRY_SELECTION", 2000))


def _documents_with_active_item():
    """Sous-requête : documents déjà pris en charge par un item non terminé.

    Deux items concurrents sur le même document sont exactement le scénario où la
    tâche la plus ancienne peut écraser le résultat de la plus récente. On ne les
    crée donc pas.
    """
    return (
        RagSpaceReindexJobItem.objects
        .filter(
            status__in=list(ITEM_UNFINISHED_STATUSES),
            job__status__in=list(JOB_ACTIVE_STATUSES),
        )
        .values("document_id")
    )


def eligible_failed_documents(*, rag_space_id, extra_filter: Q | None = None,
                              document_ids=None, excluded_document_ids=None):
    """Queryset des documents RÉELLEMENT reprenables d'un espace.

    Éligibilité, entièrement en SQL (aucune requête par document) :

    - appartient au RagSpace demandé — c'est ce qui rend un identifiant venu du
      client inoffensif : hors périmètre, il ne matche simplement pas ;
    - statut ``FAILED`` : la reprise ne touche pas un document sain, et un document
      ``PENDING``/``INGESTING`` est de ce fait exclu par construction ;
    - possède un fichier source (sans lui, il n'y a rien à ré-extraire) ;
    - n'est pas déjà porté par un item actif d'un job actif.

    ``document_ids`` restreint à une sélection explicite ; ``excluded_document_ids``
    retire des lignes d'une sélection par filtre. Les deux sont des *restrictions* :
    aucun ne peut élargir le périmètre.
    """
    qs = Document.objects.filter(
        rag_space_id=rag_space_id, status=DocumentStatus.FAILED
    ).exclude(storage_file="").exclude(storage_file__isnull=True)

    if extra_filter is not None:
        qs = qs.filter(extra_filter)
    if document_ids is not None:
        qs = qs.filter(id__in=list(document_ids))
    if excluded_document_ids:
        qs = qs.exclude(id__in=list(excluded_document_ids))

    return qs.exclude(id__in=_documents_with_active_item())


#: Champs strictement nécessaires à une décision de stratégie. Rien d'autre n'est
#: chargé : une campagne de 2 000 documents ne doit pas ramener leur contenu.
_DECISION_FIELDS = ("id", "filename", "content_type", "error_message", "meta")


def plan_for_documents(documents, requested_strategy: str) -> "RetryPlan":
    """Résout la stratégie de chaque document d'un ensemble DÉJÀ constitué.

    Pure vis-à-vis de la base : elle ne fait qu'appliquer la table de décision. La
    séparer de la requête d'éligibilité permet de la réutiliser sur un ensemble
    construit autrement (relance des échecs d'un job), sans imposer les critères
    d'une campagne « documents en erreur ».
    """
    from .retry_strategy import resolve_retry_strategy

    decisions = [(d.id, resolve_retry_strategy(d, requested_strategy)) for d in documents]
    return RetryPlan(requested_strategy=requested_strategy, decisions=decisions)


def plan_retry(*, rag_space_id, requested_strategy: str, extra_filter: Q | None = None,
               document_ids=None, excluded_document_ids=None) -> "RetryPlan":
    """Résout la stratégie de CHAQUE document éligible, **sans rien créer**.

    Fonction unique partagée par la prévisualisation et la création : deux chemins
    distincts finiraient par annoncer une répartition et en exécuter une autre.
    Ne lit aucun fichier, ne lance aucune tâche — seulement des champs déjà en base.
    """
    docs = eligible_failed_documents(
        rag_space_id=rag_space_id, extra_filter=extra_filter,
        document_ids=document_ids, excluded_document_ids=excluded_document_ids,
    ).only(*_DECISION_FIELDS)
    return plan_for_documents(list(docs), requested_strategy)


@dataclass
class RetryPlan:
    """Ce qui sera fait, document par document, avant que quoi que ce soit ne le soit."""

    requested_strategy: str
    decisions: list  # list[tuple[UUID, RetryDecision]]

    @property
    def actionable(self) -> list:
        return [(doc_id, d) for doc_id, d in self.decisions if d.is_actionable]

    @property
    def skipped(self) -> list:
        return [(doc_id, d) for doc_id, d in self.decisions if not d.is_actionable]

    def strategy_breakdown(self) -> dict[str, int]:
        """Nombre de documents par stratégie effective (items traitables seulement)."""
        out: dict[str, int] = {}
        for _, d in self.actionable:
            out[d.effective] = out.get(d.effective, 0) + 1
        return out

    def skip_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, d in self.skipped:
            out[d.skip_reason] = out.get(d.skip_reason, 0) + 1
        return out

    def as_preview(self) -> dict:
        return {
            "requested_strategy": self.requested_strategy,
            "total_documents": len(self.decisions),
            "actionable_documents": len(self.actionable),
            "skipped_documents": len(self.skipped),
            "strategy_breakdown": self.strategy_breakdown(),
            "skip_reasons": self.skip_reasons(),
        }


def create_failed_documents_retry_job(
    *, rag_space, user, selection_mode: str, extra_filter: Q | None = None,
    document_ids=None, excluded_document_ids=None, requested_count: int | None = None,
    selection_meta: dict | None = None, requested_strategy: str = "standard",
) -> CreateOutcome:
    """Crée un job de **reprise des documents en erreur**, snapshot figé au lancement.

    Le snapshot est matérialisé en items dans la MÊME transaction que le job : le
    filtre ayant servi à le construire n'est plus jamais réévalué. Sans cela le
    total bougerait pendant le traitement, de nouveaux documents entreraient dans un
    job déjà commencé, et la progression cesserait d'être déterministe.

    Même exclusivité que la réindexation globale (contrainte partielle « un job actif
    par espace ») : un job déjà actif est RETOURNÉ, jamais doublé.
    """
    existing = _active_job(rag_space.id)
    if existing is not None:
        return CreateOutcome(job=existing, created=False, already_running=True)

    plan = plan_retry(
        rag_space_id=rag_space.id,
        requested_strategy=requested_strategy,
        extra_filter=extra_filter,
        document_ids=document_ids,
        excluded_document_ids=excluded_document_ids,
    )
    if not plan.decisions:
        raise ReindexJobError("no_eligible_documents", http_status=409)
    if not plan.actionable:
        # Tous les documents retenus sont des échecs définitifs ou incompatibles :
        # créer un job qui ne traiterait rien n'apporterait qu'une fausse impression
        # d'action. On le dit, avec le détail des raisons.
        raise ReindexJobError("no_retryable_documents", http_status=409)
    if len(plan.decisions) > MAX_RETRY_SELECTION:
        raise ReindexJobError("selection_too_large", http_status=409)
    if _is_eager() and len(plan.actionable) > EAGER_MAX_SYNC_DOCUMENTS:
        from apps.rag_core.models.reindex_job import ERROR_EAGER_TOO_LARGE
        raise ReindexJobError(ERROR_EAGER_TOO_LARGE, http_status=503)

    doc_ids = [doc_id for doc_id, _ in plan.decisions]
    meta = dict(selection_meta or {})
    meta.update({
        "mode": selection_mode,
        # `requested` = ce que le client a demandé, `eligible` = ce qui a été retenu.
        # L'écart est la réponse honnête à « pourquoi 184 et pas 190 ? ».
        "requested_count": int(requested_count if requested_count is not None else len(doc_ids)),
        "eligible_count": len(doc_ids),
        "strategy_breakdown": plan.strategy_breakdown(),
        "skip_reasons": plan.skip_reasons(),
    })

    try:
        with transaction.atomic():
            job = RagSpaceReindexJob.objects.create(
                rag_space=rag_space,
                requested_by=user if getattr(user, "pk", None) else None,
                status=ReindexJobStatus.PENDING,
                kind=ReindexJobKind.FAILED_DOCUMENTS_RETRY,
                selection_meta=meta,
                requested_strategy=plan.requested_strategy,
                total_count=len(doc_ids),
                pending_count=len(plan.actionable),
                skipped_count=len(plan.skipped),
            )
            # Les documents non traitables entrent QUAND MÊME dans le snapshot, à
            # l'état `skipped` : le job rend ainsi compte de tout ce qui était
            # sélectionné, y compris de ce qu'il a décidé de ne pas tenter. Les
            # taire donnerait un total qui ne correspondrait à rien de demandé.
            RagSpaceReindexJobItem.objects.bulk_create([
                RagSpaceReindexJobItem(
                    job=job,
                    document_id=doc_id,
                    status=(
                        ReindexItemStatus.PENDING if decision.is_actionable
                        else ReindexItemStatus.SKIPPED
                    ),
                    effective_strategy=decision.effective,
                    strategy_meta=decision.as_meta(),
                    skip_reason=decision.skip_reason,
                    finished_at=None if decision.is_actionable else timezone.now(),
                )
                for doc_id, decision in plan.decisions
            ], batch_size=500)
            job_id = str(job.id)
            transaction.on_commit(lambda: _enqueue_orchestrator(job_id))
    except IntegrityError:
        existing = _active_job(rag_space.id)
        if existing is not None:
            return CreateOutcome(job=existing, created=False, already_running=True)
        raise
    return CreateOutcome(job=job, created=True, already_running=False)


# --------------------------------------------------------------------------- #
# Création
# --------------------------------------------------------------------------- #
def create_reindex_job(*, rag_space, user) -> CreateOutcome:
    """Crée un job durable + un item par document, puis enqueue APRÈS commit.

    Exclusivité garantie par la contrainte partielle : si un job actif existe déjà
    pour l'espace, on le retourne (``already_running=True``) plutôt que d'en créer
    un second. Garde EAGER : au-delà d'un seuil sans broker, on refuse (503).
    """
    # Job actif déjà présent ? (chemin rapide, la contrainte reste le garde-fou dur).
    existing = _active_job(rag_space.id)
    if existing is not None:
        return CreateOutcome(job=existing, created=False, already_running=True)

    docs = candidate_documents(rag_space.id)
    if _is_eager() and len(docs) > EAGER_MAX_SYNC_DOCUMENTS:
        from apps.rag_core.models.reindex_job import ERROR_EAGER_TOO_LARGE
        raise ReindexJobError(ERROR_EAGER_TOO_LARGE, http_status=503)

    try:
        with transaction.atomic():
            job = RagSpaceReindexJob.objects.create(
                rag_space=rag_space,
                requested_by=user if getattr(user, "pk", None) else None,
                status=ReindexJobStatus.PENDING,
                total_count=len(docs),
                pending_count=len(docs),
            )
            RagSpaceReindexJobItem.objects.bulk_create([
                RagSpaceReindexJobItem(job=job, document=d, status=ReindexItemStatus.PENDING)
                for d in docs
            ], batch_size=500)
            job_id = str(job.id)
            transaction.on_commit(lambda: _enqueue_orchestrator(job_id))
    except IntegrityError:
        # Course perdue sur la contrainte partielle : un autre job actif a gagné.
        existing = _active_job(rag_space.id)
        if existing is not None:
            return CreateOutcome(job=existing, created=False, already_running=True)
        raise
    return CreateOutcome(job=job, created=True, already_running=False)


def _enqueue_orchestrator(job_id: str) -> None:
    """Enqueue l'orchestrateur ; en cas d'échec broker, marque le job FAILED (jamais
    de faux succès)."""
    from apps.rag_core.models.reindex_job import ERROR_BROKER_UNAVAILABLE
    from apps.rag_core.tasks import run_reindex_job_task
    try:
        run_reindex_job_task.delay(job_id=job_id)
    except Exception:  # noqa: BLE001 - broker injoignable
        logger.exception("reindex: enqueue orchestrator failed job=%s", job_id)
        _fail_job(job_id, ERROR_BROKER_UNAVAILABLE)


def _fail_job(job_id: str, code: str) -> None:
    with transaction.atomic():
        job = RagSpaceReindexJob.objects.select_for_update().filter(id=job_id).first()
        if job is None or job.is_terminal:
            return
        # Depuis PENDING/QUEUING/RUNNING → FAILED est autorisé.
        try:
            job.transition_to(ReindexJobStatus.FAILED)
        except Exception:  # noqa: BLE001
            job.status = ReindexJobStatus.FAILED
        job.error_code = code
        job.error_message = job.safe_error_message(code)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_code", "error_message", "finished_at", "updated_at"])


# --------------------------------------------------------------------------- #
# Compteurs (agrégation — jamais d'incrément naïf)
# --------------------------------------------------------------------------- #
def recompute_counts(job_id: str) -> dict:
    agg = RagSpaceReindexJobItem.objects.filter(job_id=job_id).aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=ReindexItemStatus.PENDING)),
        queued=Count("id", filter=Q(status=ReindexItemStatus.QUEUED)),
        running=Count("id", filter=Q(status=ReindexItemStatus.RUNNING)),
        succeeded=Count("id", filter=Q(status=ReindexItemStatus.SUCCEEDED)),
        failed=Count("id", filter=Q(status=ReindexItemStatus.FAILED)),
        cancelled=Count("id", filter=Q(status=ReindexItemStatus.CANCELLED)),
        interrupted=Count("id", filter=Q(status=ReindexItemStatus.INTERRUPTED)),
        skipped=Count("id", filter=Q(status=ReindexItemStatus.SKIPPED)),
    )
    return {k: (v or 0) for k, v in agg.items()}


def refresh_and_maybe_finalize(job_id: str) -> None:
    """Recompte les compteurs par agrégation et finalise le job si plus aucun item
    n'est en cours. Race-safe : select_for_update sur le job."""
    with transaction.atomic():
        job = RagSpaceReindexJob.objects.select_for_update().filter(id=job_id).first()
        if job is None:
            return
        c = recompute_counts(job_id)
        job.total_count = c["total"]
        job.pending_count = c["pending"]
        job.queued_count = c["queued"]
        job.running_count = c["running"]
        job.succeeded_count = c["succeeded"]
        job.failed_count = c["failed"]
        job.cancelled_count = c["cancelled"]
        job.interrupted_count = c["interrupted"]
        job.skipped_count = c["skipped"]

        unfinished = c["pending"] + c["queued"] + c["running"]
        fields = [
            "total_count", "pending_count", "queued_count", "running_count",
            "succeeded_count", "failed_count", "cancelled_count", "interrupted_count",
            "skipped_count", "updated_at",
        ]
        if unfinished == 0 and job.status == ReindexJobStatus.RUNNING:
            if c["interrupted"] > 0:
                job.transition_to(ReindexJobStatus.INTERRUPTED)
            elif c["failed"] > 0:
                job.transition_to(ReindexJobStatus.COMPLETED_WITH_ERRORS)
            elif c["cancelled"] > 0 and c["succeeded"] == 0:
                job.transition_to(ReindexJobStatus.CANCELLED)
            else:
                job.transition_to(ReindexJobStatus.COMPLETED)
            job.finished_at = timezone.now()
            fields += ["status", "finished_at"]
        job.save(update_fields=fields)


# --------------------------------------------------------------------------- #
# Relance des échecs
# --------------------------------------------------------------------------- #
def retry_failed(*, job: RagSpaceReindexJob, user, requested_strategy: str | None = None) -> CreateOutcome:
    """Crée un NOUVEAU job lié au précédent, portant les items FAILED/INTERRUPTED.

    N'écrase jamais l'historique : le job d'origine reste consultable ; le nouveau
    référence les mêmes documents dans un job neuf (``parent_job``).

    ``requested_strategy`` : ``None`` **conserve** la stratégie du parent (rejouer à
    l'identique) ; ``"auto"`` la fait recalculer sur les codes d'erreur À JOUR — ceux
    que la tentative précédente vient d'écrire. Les items ``skipped`` ne sont jamais
    repris : ils n'ont pas échoué, ils ont été écartés en connaissance de cause.
    """
    if not job.is_terminal:
        raise ReindexJobError("job_not_terminal", http_status=409)

    active = _active_job(job.rag_space_id)
    if active is not None:
        return CreateOutcome(job=active, created=False, already_running=True)

    failed_doc_ids = list(
        RagSpaceReindexJobItem.objects
        .filter(job=job, status__in=[ReindexItemStatus.FAILED, ReindexItemStatus.INTERRUPTED])
        .values_list("document_id", flat=True)
    )
    if not failed_doc_ids:
        raise ReindexJobError("no_failed_items", http_status=409)

    strategy = (requested_strategy or job.requested_strategy or "standard").strip().lower()
    # Périmètre VOLONTAIREMENT différent d'une campagne « documents en erreur » : on
    # reprend les items qui ont échoué, quel que soit le statut actuel du document.
    # Un item peut échouer sans que le document ne soit FAILED (enqueue perdu,
    # worker interrompu) ; l'exclure ici rendrait ces échecs-là irrattrapables.
    plan = plan_for_documents(
        list(
            Document.objects.filter(
                id__in=failed_doc_ids, rag_space_id=job.rag_space_id
            ).only(*_DECISION_FIELDS)
        ),
        strategy,
    )
    if not plan.actionable:
        # Après une tentative, tout ce qui reste peut être devenu non reprenable
        # (fichier absent, échec définitif désormais connu). Le dire plutôt que de
        # créer un job qui n'aurait rien à faire.
        raise ReindexJobError("no_retryable_documents", http_status=409)

    with transaction.atomic():
        new_job = RagSpaceReindexJob.objects.create(
            rag_space_id=job.rag_space_id,
            requested_by=user if getattr(user, "pk", None) else None,
            parent_job=job,
            status=ReindexJobStatus.PENDING,
            # La relance HÉRITE de la portée du parent : relancer les échecs d'une
            # reprise ciblée reste une reprise ciblée, pas une réindexation globale.
            kind=job.kind,
            requested_strategy=strategy,
            selection_meta={
                **(job.selection_meta or {}),
                "mode": "retry_failed",
                "parent_job_id": str(job.id),
                "requested_count": len(plan.decisions),
                "eligible_count": len(plan.decisions),
                "strategy_breakdown": plan.strategy_breakdown(),
                "skip_reasons": plan.skip_reasons(),
            },
            total_count=len(plan.decisions),
            pending_count=len(plan.actionable),
            skipped_count=len(plan.skipped),
        )
        RagSpaceReindexJobItem.objects.bulk_create([
            RagSpaceReindexJobItem(
                job=new_job,
                document_id=doc_id,
                status=(
                    ReindexItemStatus.PENDING if decision.is_actionable
                    else ReindexItemStatus.SKIPPED
                ),
                effective_strategy=decision.effective,
                strategy_meta=decision.as_meta(),
                skip_reason=decision.skip_reason,
                finished_at=None if decision.is_actionable else timezone.now(),
            )
            for doc_id, decision in plan.decisions
        ], batch_size=500)
        new_id = str(new_job.id)
        transaction.on_commit(lambda: _enqueue_orchestrator(new_id))
    return CreateOutcome(job=new_job, created=True, already_running=False)


# --------------------------------------------------------------------------- #
# Récupération des items « stale »
# --------------------------------------------------------------------------- #
def _published_index_exists(document_id) -> bool:
    """Le document a-t-il un index publié ? (INGEST-1C-R2 : décide READY ou FAILED)."""
    from apps.rag_core.models import Chunk

    return Chunk.objects.filter(document_id=document_id).exists()


def _stale_ingesting_documents(now) -> list:
    """Documents INGESTING dont la tentative est réputée MORTE.

    INVARIANT CRITIQUE : le seuil (``ITEM_LEASE_SECONDS``) DOIT dépasser la limite
    dure de toute exécution d'ingestion sans heartbeat (limite Celery, limite des
    items, timeout d'extraction, timeout HTTP synchrone). Une tentative plus jeune
    que le seuil peut être vivante : elle n'est jamais touchée. Verrouillé par
    ``test_reaper_active_ingestion_race.TestSeuil``.

    Prédicat, entièrement en base :
      status = INGESTING
      ET aucun item RUNNING au lease frais sur ce document (le lease prime : un item
         vivant protège son document quel que soit l'âge)
      ET COALESCE(ingest_started_at, created_at) < now - seuil
         (``created_at`` ne sert qu'aux lignes antérieures au champ).

    Rend ``[(id, ingest_started_at, début_effectif)]`` : ``ingest_started_at`` tel que
    lu sert de comparaison-échange ; ``début_effectif`` (repli compris) sert au journal.
    """
    from django.db.models import Exists, OuterRef
    from django.db.models.functions import Coalesce

    limite = now - timedelta(seconds=ITEM_LEASE_SECONDS)
    item_vivant = RagSpaceReindexJobItem.objects.filter(
        document_id=OuterRef("pk"),
        status=ReindexItemStatus.RUNNING,
        lease_expires_at__gte=now,
    )
    return list(
        Document.objects
        .filter(status=DocumentStatus.INGESTING)
        .annotate(_attempt_started=Coalesce("ingest_started_at", "created_at"))
        .filter(_attempt_started__lt=limite)
        .exclude(Exists(item_vivant))
        .values_list("id", "ingest_started_at", "_attempt_started")
    )


def recover_stale_items(*, dry_run: bool = True, now=None) -> dict:
    """Détecte les items RUNNING dont le lease a expiré et les documents INGESTING dont
    la tentative est réputée morte, et (si ``dry_run=False``) les marque INTERRUPTED /
    READY-ou-FAILED.

    Ne supprime AUCUN chunk : les anciens chunks valides sont conservés. Une tentative
    plus jeune que le seuil n'est jamais touchée (cf. ``_stale_ingesting_documents``).
    """
    now = now or timezone.now()

    stale_items = list(
        RagSpaceReindexJobItem.objects
        .filter(status=ReindexItemStatus.RUNNING)
        .filter(Q(lease_expires_at__lt=now) | Q(lease_expires_at__isnull=True))
        .select_related("job")
    )
    stale_docs = _stale_ingesting_documents(now)

    result = {
        "stale_items": [str(i.id) for i in stale_items],
        "stale_ingesting_documents": [str(doc_id) for doc_id, _, _ in stale_docs],
        "recovered_ingesting_documents": [],
        "applied": not dry_run,
    }
    if dry_run:
        return result

    affected_jobs = set()
    for item in stale_items:
        with transaction.atomic():
            it = RagSpaceReindexJobItem.objects.select_for_update().get(id=item.id)
            if it.status != ReindexItemStatus.RUNNING:
                continue
            it.status = ReindexItemStatus.INTERRUPTED
            it.error_code = ERROR_INTERRUPTED
            it.error_message = "Traitement interrompu (worker perdu)."
            it.finished_at = now
            it.save(update_fields=["status", "error_code", "error_message", "finished_at", "updated_at"])
            affected_jobs.add(str(it.job_id))
    for doc_id, started_at, attempt_started in stale_docs:
        # Sans supprimer les anciens chunks : on ne fait que sortir le document de
        # l'état intermédiaire trompeur. INGEST-1C-R2 : le statut exprime la
        # disponibilité du dernier index publié, pas le sort de la tentative
        # interrompue (celle-ci reste portée par l'item INTERRUPTED et
        # `error_message`). Des fragments publiés existent => READY, l'ancien index
        # reste actif ; aucun (première ingestion interrompue) => FAILED.
        has_published_index = _published_index_exists(doc_id)
        final = DocumentStatus.READY if has_published_index else DocumentStatus.FAILED
        # COMPARAISON-ÉCHANGE : n'écrit que si la ligne porte encore LA tentative
        # sélectionnée. Une tentative redémarrée entre-temps (nouvel horodatage) ou une
        # publication concurrente (statut changé) laisse 0 ligne : rien n'est retenté.
        # NULL (ligne antérieure au champ) se compare par `isnull`, jamais par égalité.
        cas = Document.objects.filter(id=doc_id, status=DocumentStatus.INGESTING)
        cas = (cas.filter(ingest_started_at__isnull=True) if started_at is None
               else cas.filter(ingest_started_at=started_at))
        if not cas.update(status=final, error_message=ERROR_INTERRUPTED):
            continue
        result["recovered_ingesting_documents"].append(str(doc_id))
        logger.warning(
            "stale_ingestion_recovered document_id=%s attempt_age_s=%d ingest_started_at=%s "
            "status_written=%s published_index=%s",
            doc_id, int((now - attempt_started).total_seconds()),
            started_at.isoformat() if started_at else "null", final, has_published_index,
        )
    for job_id in affected_jobs:
        refresh_and_maybe_finalize(job_id)
    return result


# --------------------------------------------------------------------------- #
# Sérialisation API (aucun contenu sensible)
# --------------------------------------------------------------------------- #
def strategy_breakdown(job_id) -> dict[str, dict[str, int]]:
    """Avancement PAR STRATÉGIE effective, en une seule requête.

    Sans cette ventilation, un job « auto » n'affiche qu'un total : impossible de
    voir que l'OCR forcé rattrape ses documents pendant que le délai étendu échoue
    encore, alors que c'est exactement ce qu'on veut savoir d'une reprise adaptative.
    """
    rows = (
        RagSpaceReindexJobItem.objects
        .filter(job_id=job_id)
        .exclude(status=ReindexItemStatus.SKIPPED)
        .values("effective_strategy")
        .annotate(
            total=Count("id"),
            succeeded=Count("id", filter=Q(status=ReindexItemStatus.SUCCEEDED)),
            failed=Count("id", filter=Q(status__in=[
                ReindexItemStatus.FAILED, ReindexItemStatus.INTERRUPTED,
            ])),
        )
    )
    return {
        (r["effective_strategy"] or "standard"): {
            "total": r["total"], "succeeded": r["succeeded"], "failed": r["failed"],
        }
        for r in rows
    }


def skip_reason_breakdown(job_id) -> dict[str, int]:
    """Raisons STRUCTURÉES des items sautés — jamais un compteur global opaque."""
    rows = (
        RagSpaceReindexJobItem.objects
        .filter(job_id=job_id, status=ReindexItemStatus.SKIPPED)
        .values("skip_reason")
        .annotate(count=Count("id"))
    )
    return {(r["skip_reason"] or "unknown"): r["count"] for r in rows}


def serialize_job(job: RagSpaceReindexJob, *, with_errors: bool = False) -> dict:
    payload = {
        "job_id": str(job.id),
        "rag_space_id": str(job.rag_space_id),
        "status": job.status,
        "kind": job.kind,
        # Métadonnées de sélection : compteurs et mode uniquement. Aucun identifiant
        # de document, aucun terme de recherche n'y est renvoyé au-delà de ce que le
        # demandeur a lui-même envoyé.
        "selection": dict(job.selection_meta or {}),
        "total": job.total_count,
        "pending": job.pending_count,
        "queued": job.queued_count,
        "running": job.running_count,
        "succeeded": job.succeeded_count,
        "failed": job.failed_count,
        "cancelled": job.cancelled_count,
        "interrupted": job.interrupted_count,
        "skipped": job.skipped_count,
        "progress_percent": job.progress_percent,
        "requested_strategy": job.requested_strategy,
        "strategy_breakdown": strategy_breakdown(job.id),
        "skip_reasons": skip_reason_breakdown(job.id),
        "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
        "error_code": job.error_code or None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
    if with_errors:
        payload["failed_items"] = [
            {
                "document_id": str(i.document_id),
                "filename": (i.document.filename if i.document_id else "")[:255],
                "status": i.status,
                "error_code": i.error_code or None,
                "attempt_count": i.attempt_count,
            }
            for i in RagSpaceReindexJobItem.objects
            .filter(job=job, status__in=[ReindexItemStatus.FAILED, ReindexItemStatus.INTERRUPTED])
            .select_related("document")[:200]
        ]
    return payload

"""Préflight de VIDANGE ASR avant/après un changement d'acheminement Gateway.
LECTURE SEULE.

Pourquoi : un job ASR est un état CÔTÉ Gateway, désigné par un identifiant que RAGatm
persiste dans ``AudioTranscript.parts[].job_id``. AI Gateway Pool retrouve le nœud d'un
job par une affinité (Redis du pool) écrite À SA CRÉATION via le pool. Un job créé en
direct est donc inconnu du pool (``404 JOB_AFFINITY_UNKNOWN``). On ne bascule les URL
qu'une fois aucun job en vol ni à venir.

Source de vérité : la base (le modèle ``AudioTranscript`` et ses parties), jamais la file
Celery — une étape différée (``countdown``) est tenue par un worker, invisible dans la file,
mais sa ligne reste ``preparing``/``transcribing`` tant que la chaîne vit.

États d'une partie (``orchestrator``) : ``pending`` → ``submitting`` → ``submitted`` → ``done``.
Il n'existe AUCUN état « en annulation » : une annulation est synchrone (DELETE puis ligne
``failed``/``cancelled``). Un résultat Gateway n'est jamais « terminé mais non persisté »
hors d'une partie ``submitted`` : la partie passe à ``done`` dans la même écriture que son
résultat.

Verdict :
- ``PASS``    : rien en vol, rien à venir, rien d'inexpliqué ;
- ``BLOCKED`` : des jobs en vol ou à venir — attendre (jamais annuler pour passer) ;
- ``UNKNOWN`` : un état non reconnu ou une lecture impossible — jamais un faux GO.

Ce module n'écrit rien, n'annule rien, ne supprime rien, n'appelle pas la Gateway.
"""
from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_PART_STATUSES = frozenset({"pending", "submitting", "submitted", "done"})
ACTIVE_TRANSCRIPT_STATUSES = ("preparing", "transcribing")

PASS = "PASS"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"


@dataclass
class DrainReport:
    active_jobs: int = 0            # parties `submitted` : job créé côté Gateway, relu par RAGatm
    submitting_jobs: int = 0        # parties `submitting` : dépôt en cours, issue incertaine
    pending_jobs: int = 0           # parties `pending` d'une tentative active + tentatives `preparing`
    queued_documents: int = 0       # documents audio PENDING/INGESTING sans tentative active
    unknown_jobs: int = 0           # états non reconnus (bloquent le GO)
    ready_awaiting_indexing: int = 0  # info : transcription prête, document pas encore READY
    active_transcripts: list = field(default_factory=list)
    unknown_reasons: list = field(default_factory=list)
    error: str = ""
    read_only_enforced: bool = False  # transaction READ ONLY posée (PostgreSQL, hors bloc englobant)

    @property
    def status(self) -> str:
        if self.error or self.unknown_jobs:
            return UNKNOWN
        if self.active_jobs or self.submitting_jobs or self.pending_jobs or self.queued_documents:
            return BLOCKED
        return PASS

    def as_lines(self) -> list[str]:
        return [
            f"ASR_ACTIVE_JOBS={self.active_jobs}",
            f"ASR_SUBMITTING_JOBS={self.submitting_jobs}",
            f"ASR_PENDING_JOBS={self.pending_jobs + self.queued_documents}",
            f"ASR_PENDING_PARTS={self.pending_jobs}",
            f"ASR_QUEUED_DOCUMENTS={self.queued_documents}",
            f"ASR_UNKNOWN_JOBS={self.unknown_jobs}",
            f"ASR_READY_AWAITING_INDEXING={self.ready_awaiting_indexing}",
            f"ASR_DRAIN_STATUS={self.status}",
        ]

    def as_dict(self) -> dict:
        return {
            "ASR_ACTIVE_JOBS": self.active_jobs,
            "ASR_SUBMITTING_JOBS": self.submitting_jobs,
            "ASR_PENDING_JOBS": self.pending_jobs + self.queued_documents,
            "ASR_PENDING_PARTS": self.pending_jobs,
            "ASR_QUEUED_DOCUMENTS": self.queued_documents,
            "ASR_UNKNOWN_JOBS": self.unknown_jobs,
            "ASR_READY_AWAITING_INDEXING": self.ready_awaiting_indexing,
            "ASR_DRAIN_STATUS": self.status,
            "active_transcripts": list(self.active_transcripts),
            "unknown_reasons": list(self.unknown_reasons),
            "error": self.error,
            "read_only_enforced": self.read_only_enforced,
        }


def classify_transcript(status: str, parts, report: DrainReport, *, transcript_id: str) -> None:
    """Ajoute UNE tentative active au rapport (fonction pure, testée sans base)."""
    if status == "preparing":
        # Plan des parties pas encore établi : au moins un dépôt à venir.
        report.pending_jobs += 1
        report.active_transcripts.append({"id": transcript_id, "status": status})
        return
    if not isinstance(parts, list):
        report.unknown_jobs += 1
        report.unknown_reasons.append(f"{transcript_id}:parts_not_a_list")
        return
    counts = {"pending": 0, "submitting": 0, "submitted": 0, "done": 0}
    for idx, part in enumerate(parts):
        st = part.get("status") if isinstance(part, dict) else None
        if st not in KNOWN_PART_STATUSES:
            report.unknown_jobs += 1
            report.unknown_reasons.append(f"{transcript_id}:part{idx}:status={st!r}")
            continue
        if st == "submitted" and not part.get("job_id"):
            report.unknown_jobs += 1
            report.unknown_reasons.append(f"{transcript_id}:part{idx}:submitted_without_job_id")
            continue
        counts[st] += 1
    report.active_jobs += counts["submitted"]
    report.submitting_jobs += counts["submitting"]
    report.pending_jobs += counts["pending"]
    if not any(counts[k] for k in ("pending", "submitting", "submitted")) and counts["done"]:
        # Toutes les parties faites mais tentative encore `transcribing` : l'assemblage est à
        # venir (aucun appel ASR), MAIS la chaîne vit encore — on attend sa fin.
        report.pending_jobs += 1
    report.active_transcripts.append({"id": transcript_id, "status": status, **counts})


def build_report() -> DrainReport:
    """Lit la base en transaction READ ONLY (PostgreSQL) et rend le rapport. Ne lève pas."""
    from django.db import connection, transaction

    report = DrainReport()
    try:
        from apps.rag_core.models import (
            AudioTranscript,
            AudioTranscriptStatus,
            Document,
            DocumentStatus,
        )

        # READ ONLY n'est posable qu'en tête de transaction : sous un bloc atomique déjà
        # ouvert (appel depuis du code transactionnel, tests), on lit sans le poser.
        outer = connection.in_atomic_block
        with transaction.atomic():
            if connection.vendor == "postgresql" and not outer:
                with connection.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                report.read_only_enforced = True
            known = {c.value for c in AudioTranscriptStatus}
            for st in AudioTranscript.objects.values_list("status", flat=True).distinct():
                if st not in known:
                    report.unknown_jobs += 1
                    report.unknown_reasons.append(f"transcript_status={st!r}")
            active_doc_ids = set()
            for tid, doc_id, st, parts in AudioTranscript.objects.filter(
                status__in=list(ACTIVE_TRANSCRIPT_STATUSES),
            ).values_list("id", "document_id", "status", "parts"):
                active_doc_ids.add(doc_id)
                classify_transcript(st, parts, report, transcript_id=str(tid))
            in_flight_docs = Document.objects.filter(
                meta__has_key="audio", status__in=[DocumentStatus.PENDING, DocumentStatus.INGESTING],
            )
            ready_doc_ids = set(AudioTranscript.objects.filter(
                status=AudioTranscriptStatus.READY,
                document_id__in=in_flight_docs.values("id"),
            ).values_list("document_id", flat=True))
            for doc_id in in_flight_docs.values_list("id", flat=True):
                if doc_id in active_doc_ids:
                    continue
                if doc_id in ready_doc_ids:
                    report.ready_awaiting_indexing += 1
                else:
                    report.queued_documents += 1
    except Exception as exc:  # noqa: BLE001 - une lecture impossible n'est jamais un GO
        report.error = type(exc).__name__
    return report

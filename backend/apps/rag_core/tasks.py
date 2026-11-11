# backend/apps/rag_core/tasks.py
from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import transaction

from apps.rag_core.services.documents.source_availability import (
    NO_STORAGE_FILE,
    SOURCE_FILE_MISSING,
)

# Source UNIQUE de la classification "transitoire" + exception dédiée (cf. ticket 2).
from apps.rag_core.services.ingest.types import (
    RetryableIngestError,
)
from apps.rag_core.services.ingest.types import (
    is_retryable_error as _is_retryable_error,
)

logger = logging.getLogger(__name__)


def _backoff_s(retry_idx: int) -> int:
    """
    Exponential backoff: 2^n seconds, plafonné.
    retry_idx commence à 1.
    """
    return int(min(60, max(2, math.pow(2, retry_idx))))


def _has_storage_file(doc) -> bool:
    """
    True if Document has a usable storage_file (FileField.name is set).
    """
    try:
        sf = getattr(doc, "storage_file", None)
        return bool(sf and getattr(sf, "name", ""))
    except Exception:
        return False


def _is_source_unavailable_error(e: Exception) -> bool:
    """Source indisponible : erreur PERMANENTE, jamais un retry.

    Couvre les deux formes : aucune source conservée
    (`no_storage_file_for_reindex`) et source déclarée mais introuvable
    (`source_file_missing`). Toutes deux sont définitives — réessayer à
    l'identique ne fera pas réapparaître un fichier. Les distinguer reste utile à
    l'opérateur : le second cas appelle un nouveau téléversement.
    """
    msg = (str(e) or "").lower()
    return NO_STORAGE_FILE in msg or SOURCE_FILE_MISSING in msg


# ---------------------------------------------------------------------
# Document date extraction in async workers (best-effort)
# ---------------------------------------------------------------------


def _model_has_field(model: Any, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _update_document_date_best_effort(*, DocumentModel, doc_id, doc_obj, doc_date: date | None) -> None:
    """
    Best-effort: supports schema variants:
    - Document.document_date (preferred)
    - Document.meta JSONField
    - Document.metadata JSONField
    Never raises.
    """
    if not doc_date:
        return

    try:
        if _model_has_field(DocumentModel, "document_date"):
            DocumentModel.objects.filter(id=doc_id).update(document_date=doc_date)
            return

        iso = doc_date.isoformat()
        for json_field in ("meta", "metadata"):
            if _model_has_field(DocumentModel, json_field):
                current = getattr(doc_obj, json_field, None)
                if not isinstance(current, dict):
                    current = {}
                current.setdefault("document_date", iso)
                DocumentModel.objects.filter(id=doc_id).update(**{json_field: current})
                return
    except Exception:
        return


def _best_effort_extract_and_store_doc_date(doc) -> None:
    """
    Extract document date from doc.storage_file (TXT/PDF) and persist to Document.
    - never raises
    - designed to be safe inside celery tasks
    """
    if not _has_storage_file(doc):
        return

    # Lazy imports (avoid Django init issues / heavy libs)
    try:
        from apps.rag_core.models.document import Document as DocumentModel
    except Exception:
        return

    # If already present, do nothing (avoid overwriting)
    try:
        if _model_has_field(DocumentModel, "document_date"):
            # If field exists and already set -> skip
            current = DocumentModel.objects.filter(id=doc.id).values_list("document_date", flat=True).first()
            if current:
                return
        else:
            for json_field in ("meta", "metadata"):
                if _model_has_field(DocumentModel, json_field):
                    current = getattr(doc, json_field, None)
                    if isinstance(current, dict) and current.get("document_date"):
                        return
    except Exception:
        # if detection fails, continue (best-effort)
        pass

    # Read bytes (best-effort)
    try:
        sf = doc.storage_file
        # Ensure file is open
        try:
            sf.open("rb")
        except Exception:
            pass
        file_bytes = sf.read()  # type: ignore[call-arg]
    except Exception:
        return

    # Decide type
    try:
        name = (getattr(doc, "filename", "") or getattr(doc.storage_file, "name", "") or "").lower()
        ctype = (getattr(doc, "content_type", "") or "").lower()
        is_pdf = name.endswith(".pdf") or ctype == "application/pdf"
        is_txt = name.endswith(".txt") or ctype.startswith("text/")
    except Exception:
        is_pdf, is_txt = False, False

    # Lazy import date extractors
    try:
        from apps.rag_core.services.ingest.extractors import (
            extract_text_from_txt_bytes,
            extract_text_pages_from_pdf_bytes,
        )
    except Exception:
        return

    try:
        from apps.rag_core.services.ingest.utils import (
            extract_document_date_from_pages,
            extract_document_date_from_text,
        )
    except Exception:
        extract_document_date_from_pages = None  # type: ignore
        extract_document_date_from_text = None  # type: ignore

    doc_date: date | None = None

    try:
        if is_pdf and extract_document_date_from_pages is not None:
            # pages: list[(page_no, text)]
            pages = extract_text_pages_from_pdf_bytes(file_bytes)
            doc_date = extract_document_date_from_pages(list(pages))
        elif is_txt and extract_document_date_from_text is not None:
            txt = extract_text_from_txt_bytes(file_bytes)
            doc_date = extract_document_date_from_text(txt)
        else:
            # fallback: try as text anyway (harmless)
            if extract_document_date_from_text is not None:
                txt = extract_text_from_txt_bytes(file_bytes)
                doc_date = extract_document_date_from_text(txt)
    except Exception:
        doc_date = None

    if not doc_date:
        return

    _update_document_date_best_effort(
        DocumentModel=DocumentModel,
        doc_id=doc.id,
        doc_obj=doc,
        doc_date=doc_date,
    )


def _emit_terminal_ingestion_failure(self, *, doc, request_id, error_code: str) -> None:
    """Émet l'UNIQUE événement terminal `action.failed` d'une tentative d'ingestion
    dont l'échec est définitif (INGEST-1D-R2).

    Ne décide de rien : ni retry, ni statut du document, ni classification de
    l'erreur. `metadata.doc_status` porte la DISPONIBILITÉ du document telle que
    réglée par l'appelant (READY si l'ancien index publié reste actif, FAILED sinon),
    distincte du résultat de la tentative. Corrélation inchangée : `request_id` et
    `task_id` sont ceux de la chaîne (Celery conserve le task_id entre retries).
    Jamais de contenu documentaire dans l'événement.
    """
    from apps.audit.action_audit import emit_action_event
    from apps.rag_core.models.document import Document

    doc_status = Document.objects.filter(id=doc.id).values_list("status", flat=True).first() or ""
    emit_action_event(
        event_name="action.failed", action_name="document.ingestion", status="failed",
        rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
        resource_id=doc.id, request_id=request_id or "",
        task_id=str(getattr(getattr(self, "request", None), "id", "") or ""),
        error_code=(error_code or "ingest_error")[:100],
        metadata={"doc_status": doc_status},
    )


def _handle_ingest_failure(self, *, doc, exc, msg, request_id, log_prefix, generic=False, force_retryable=False) -> dict:
    """Retry sur erreur transitoire (backoff), sinon FAILED définitif.

    Mutualisé entre ``ingest_document_task`` et ``reindex_document_task`` pour
    garantir un comportement strictement identique. ``self.retry()`` lève une
    exception ``Retry`` que Celery intercepte : c'est volontaire.

    ``force_retryable`` : True quand l'appelant a déjà identifié l'erreur comme
    transitoire (ex. ``RetryableIngestError`` remontée par la couche d'indexation).
    """
    from apps.rag_core.models.document import Document, DocumentStatus

    if (force_retryable or _is_retryable_error(exc)) and self.request.retries < self.max_retries:
        retry_idx = int(self.request.retries) + 1
        countdown = _backoff_s(retry_idx)
        logger.warning(
            "%s: retryable error; retrying document_id=%s retry=%s/%s in %ss request_id=%s err=%s",
            log_prefix, doc.id, retry_idx, self.max_retries, countdown, request_id, (msg or "")[:200],
        )
        # PENDING pendant l'attente du retry (visible côté UI / polling).
        try:
            Document.objects.filter(id=doc.id).update(status=DocumentStatus.PENDING, error_message=(msg or "")[:1000])
        except Exception:
            logger.warning("%s: unable to set status=PENDING before retry document_id=%s request_id=%s", log_prefix, doc.id, request_id)
        raise self.retry(exc=exc, countdown=countdown)

    # Erreur finale : statut TERMINAL persistant (jamais bloqué en INGESTING).
    # INGEST-1C-R2 : le statut du document exprime la disponibilité de son dernier
    # index publié, pas le résultat de cette tentative. Un index publié existe
    # (réindexation d'un document déjà servi, retries épuisés compris) => READY, les
    # anciens fragments restent l'index actif ; sinon (première ingestion) => FAILED.
    # La tentative, elle, reste un échec (retour ok=False, error_message).
    # INGEST-1F1-R1 : décision sur l'état COURANT de l'index, sous verrou document
    # (une tentative concurrente peut avoir publié pendant la préparation).
    from apps.rag_core.services.ingest.indexing import settle_terminal_failure

    final_status = DocumentStatus.FAILED
    try:
        settled = settle_terminal_failure(doc.id, message=(msg or "")[:1000])
        if settled is not None:
            final_status = settled
    except Exception:
        # F5-A : ne pas masquer un échec de transition (sinon doc bloqué INGESTING).
        logger.exception(
            "%s: unable to mark document %s (final) document_id=%s request_id=%s",
            log_prefix, final_status, doc.id, request_id,
        )
    logger.exception("%s: ingest error (final) document_id=%s request_id=%s", log_prefix, doc.id, request_id)
    error = "unexpected_error" if generic else (msg or "ingest_error")
    # INGEST-1D-R2 : ici, et seulement ici, la tentative est TERMINALE (aucun
    # `self.retry` n'a été levé au-dessus) => exactement un événement d'échec.
    # `error_code` = préfixe stable du message (`transient_ingest_error`,
    # `rag_space_deleted`, `unexpected_error`…), jamais le détail technique.
    _emit_terminal_ingestion_failure(
        self, doc=doc, request_id=request_id, error_code=error.split(":", 1)[0].strip(),
    )
    return {
        "ok": False,
        "document_id": str(doc.id),
        "error": error,
        "request_id": request_id,
    }


def _run_ingestion(
    self, *, document_id: str, request_id: str | None, log_prefix: str,
    ocr_enabled: bool | None = None, access_context: dict | None = None,
    extraction_timeout_seconds: int | None = None, drop_visual_evidence: bool = False,
    enqueue_token: str | None = None, visual_primary: bool = False,
) -> dict:
    """Cœur commun d'ingestion/réindexation asynchrone.

    - garde permanente : pas de ``storage_file`` => FAILED, aucun retry ;
    - PENDING -> INGESTING ;
    - extraction + chunking + embeddings via ``reindex_existing_document`` ;
    - retry sur erreurs transitoires (provider embeddings, réseau, 429, 5xx…),
      ``max_retries=3``, backoff exponentiel ;
    - erreurs métier définitives (fichier invalide/vide/format/no_storage_file)
      => FAILED sans retry.

    ``ocr_enabled`` (P1 bis) : ``True`` force l'OCR local pour cette exécution
    uniquement (réindexation OCR à la demande).
    """
    from apps.audit.action_audit import emit_action_event
    from apps.rag_core.models.document import Document, DocumentStatus
    from apps.rag_core.services.ingest.types import DUPLICATE_CONTENT, DuplicateContentError
    from apps.rag_core.services.ingest_document import IngestError, reindex_existing_document
    from apps.rag_core.services.ingest.audio.orchestrator import AudioTranscriptionDeferred

    _task_id = str(getattr(getattr(self, "request", None), "id", "") or "")

    doc = Document.objects.filter(id=document_id).first()
    if not doc:
        logger.warning("%s: document not found document_id=%s request_id=%s", log_prefix, document_id, request_id)
        emit_action_event(
            event_name="action.failed", action_name="document.ingestion", status="failed",
            resource_type="document", resource_id=document_id, request_id=request_id or "",
            task_id=_task_id, error_code="not_found",
        )
        return {"ok": False, "document_id": document_id, "error": "not_found", "request_id": request_id}

    # Garde des messages PÉRIMÉS (VISUAL-ENRICHMENT-DEFERRED) : un message remplacé par un
    # enfilement plus récent, ou un doublon historique d'un document déjà réglé, n'est
    # pas retraité. Relances et redélivrances passent (cf. services/ingest/enqueue_token.py).
    from apps.rag_core.services.ingest import enqueue_token as _enq

    _request = getattr(self, "request", None)
    _delivery = (getattr(_request, "delivery_info", None) or {}) if _request is not None else {}
    _admission = _enq.admit(
        doc=doc, token=enqueue_token,
        retries=int(getattr(_request, "retries", 0) or 0),
        redelivered=bool(_delivery.get("redelivered")),
        from_broker=bool(_delivery.get("routing_key")) and not bool(getattr(_request, "is_eager", False)),
    )
    if not _admission.proceed:
        _enq.log_skip(document_id=doc.id, task_id=_task_id, reason=_admission.reason, log_prefix=log_prefix)
        return {"ok": True, "document_id": str(doc.id), "skipped": _enq.SKIP_EVENT,
                "reason": _admission.reason, "request_id": request_id}

    # Revalidation À L'EXÉCUTION (Lot 3) : ingestion = surface SENSIBLE (écrit des
    # chunks/embeddings). `require_context=True` ⇒ sous enforcement, un acteur non
    # identifiable est REFUSÉ (jamais de bypass silencieux). rag_space rechargé
    # depuis le document (autoritatif) ; on ne fait confiance qu'aux identifiants.
    from apps.workspaces.services import task_guard

    _ctx = access_context or {}
    _proceed, _reason = task_guard.enforce_or_log(
        user_id=_ctx.get("user_id"), rag_space_id=doc.rag_space_id,
        required_permission="write", job_label=log_prefix, require_context=True,
        request_id=request_id,
    )
    if not _proceed:
        try:
            Document.objects.filter(id=doc.id).update(
                status=DocumentStatus.FAILED, error_message=f"owner_scope: {_reason}"[:1000]
            )
        except Exception:  # noqa: BLE001
            logger.exception("%s: échec marquage FAILED (owner_scope) document_id=%s", log_prefix, doc.id)
        emit_action_event(
            event_name="action.denied", action_name="document.ingestion", status="denied",
            resource_type="document", resource_id=str(doc.id), request_id=request_id or "",
            task_id=_task_id, error_code=_reason,
        )
        logger.warning("%s: ingestion annulée (owner_scope=%s) document_id=%s", log_prefix, _reason, doc.id)
        return {"ok": False, "document_id": str(doc.id), "error": _reason, "request_id": request_id}

    # Garde permanente : no storage_file => erreur permanente, pas de stacktrace.
    # Le cas VOISIN — source déclarée mais absente du stockage — n'est PAS testé
    # ici : il est détecté par `reindex_existing_document`, au plus près de la
    # lecture du fichier, et remonte en `IngestError(source_file_missing)` que le
    # bloc `except IngestError` ci-dessous traite comme définitif. Un second
    # contrôle ici ferait un accès disque de plus par tâche sans rien apporter.
    if not _has_storage_file(doc):
        try:
            Document.objects.filter(id=doc.id).update(
                status=DocumentStatus.FAILED,
                error_message=NO_STORAGE_FILE,
            )
        except Exception:
            # F5-A : échec de transition -> FAILED journalisé (sinon statut incohérent).
            logger.exception("%s: unable to mark document FAILED (no_storage) document_id=%s request_id=%s", log_prefix, doc.id, request_id)
        logger.warning("%s: cannot run (no storage_file) document_id=%s request_id=%s", log_prefix, doc.id, request_id)
        emit_action_event(
            event_name="action.failed", action_name="document.ingestion", status="failed",
            rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
            resource_id=doc.id, request_id=request_id or "", task_id=_task_id,
            error_code="no_storage_file",
        )
        return {"ok": False, "document_id": str(doc.id), "error": NO_STORAGE_FILE, "request_id": request_id}

    # Date document depuis le contenu (best-effort, ne bloque jamais).
    try:
        _best_effort_extract_and_store_doc_date(doc)
    except Exception:
        pass

    # PENDING -> INGESTING. Chaque exécution de la tâche (relance comprise : elle est
    # repassée par PENDING) OUVRE une tentative : `ingest_started_at` est renouvelé
    # dans le même UPDATE (cf. services/ingest/attempts.py).
    try:
        from apps.rag_core.services.ingest.attempts import ingesting_update_fields

        # Point de sauvegarde : si la transition heurte l'index unique de contenu
        # (collision de doublon), la connexion reste utilisable ; la collision est
        # requalifiée plus loin par `reindex_existing_document` (duplicate_content).
        with transaction.atomic():
            Document.objects.filter(id=doc.id).update(
                **ingesting_update_fields(new_attempt=True), error_message="",
            )
        # Observabilité (lot STALE-ERROR-METADATA) : la tentative est ouverte ET l'erreur
        # active de la tentative précédente neutralisée dans le même UPDATE. Aucun contenu.
        logger.info(
            "%s: attempt opened, previous active error cleared document_id=%s request_id=%s",
            log_prefix, doc.id, request_id,
        )
    except Exception:
        logger.warning("%s: unable to set status=INGESTING document_id=%s request_id=%s", log_prefix, doc.id, request_id)

    emit_action_event(
        event_name="action.started", action_name="document.ingestion", status="started",
        rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
        resource_id=doc.id, request_id=request_id or "", task_id=_task_id,
    )

    try:
        # allow_retry=True : laisse remonter RetryableIngestError sur erreur
        # transitoire d'embeddings/indexing (sinon dégradée en FAILED côté sync).
        # DERNIÈRE tentative = aucune relance Celery ne suivra. Seul
        # un enrichissement visuel relançable peut alors se dégrader en avertissement.
        # Transmis SEULEMENT hors valeur par défaut : l'appel nominal reste identique.
        visual_kwargs = {}
        if (int(getattr(getattr(self, "request", None), "retries", 0) or 0)
                >= int(getattr(self, "max_retries", 0) or 0)):
            visual_kwargs["last_attempt"] = True
        if drop_visual_evidence:
            visual_kwargs["drop_visual_evidence"] = True
        if visual_primary:
            visual_kwargs["visual_primary"] = True
        # Contexte d'accès transmis aux suites DIFFÉRÉES (enrichissement visuel, reprise
        # primaire) : elles revalident les droits comme toute tâche d'ingestion.
        if access_context:
            visual_kwargs["access_context"] = access_context
        res = reindex_existing_document(
            document_id=str(doc.id), allow_retry=True, ocr_enabled=ocr_enabled,
            extraction_timeout_seconds=extraction_timeout_seconds, **visual_kwargs,
        )
        # INGEST-1D : `reindex_existing_document` RENVOIE un IngestResult FAILED sans
        # lever pour un échec métier (extraction, résultat vide, embeddings en sync…).
        # Le résultat de la TENTATIVE est `res.status`, jamais `Document.status` :
        # depuis INGEST-1C-R2 un document dont l'ancien index est conservé reste READY
        # alors même que cette tentative a échoué.
        if res.status == DocumentStatus.FAILED:
            doc_status = (
                Document.objects.filter(id=doc.id).values_list("status", flat=True).first() or ""
            )
            error_message = (
                Document.objects.filter(id=doc.id).values_list("error_message", flat=True).first() or ""
            )
            logger.warning(
                "%s: attempt failed document_id=%s doc_status=%s request_id=%s",
                log_prefix, res.document_id, doc_status, request_id,
            )
            emit_action_event(
                event_name="action.failed", action_name="document.ingestion", status="failed",
                rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
                resource_id=doc.id, request_id=request_id or "", task_id=_task_id,
                error_code="ingest_failed",
                # `doc_status` = disponibilité du document (READY si l'ancien index
                # est resté actif), distinct du résultat de la tentative.
                metadata={"chunk_count": res.chunks_created, "doc_status": doc_status},
            )
            return {
                "ok": False,
                "document_id": res.document_id,
                "status": res.status,          # résultat de la TENTATIVE (IngestResult)
                "doc_status": doc_status,      # disponibilité du DOCUMENT
                "chunks_created": res.chunks_created,
                "error": error_message[:1000] or "ingest_failed",
                "request_id": request_id,
            }

        logger.info(
            "%s: done document_id=%s status=%s chunks=%s request_id=%s",
            log_prefix, res.document_id, res.status, res.chunks_created, request_id,
        )
        emit_action_event(
            event_name="action.completed", action_name="document.ingestion", status="completed",
            rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
            resource_id=doc.id, request_id=request_id or "", task_id=_task_id,
            metadata={"chunk_count": res.chunks_created, "doc_status": res.status},
        )
        return {
            "ok": True,
            "document_id": res.document_id,
            "status": res.status,
            "chunks_created": res.chunks_created,
            "request_id": request_id,
        }

    except AudioTranscriptionDeferred as e:
        # l'audio attend sa transcription. Le document est rendu à
        # PENDING et la chaîne ASR (file `ingest_audio`) le ré-enfilera une fois la
        # transcription persistée. Ni échec, ni ingestion terminée : un événement « queued ».
        logger.info("%s: audio transcription deferred document_id=%s transcript_id=%s request_id=%s",
                    log_prefix, doc.id, e.transcript_id, request_id)
        emit_action_event(
            event_name="action.queued", action_name="document.ingestion", status="queued",
            rag_space_id=getattr(doc, "rag_space_id", None), resource_type="document",
            resource_id=doc.id, request_id=request_id or "", task_id=_task_id,
            metadata={"operation_mode": "audio_transcription"},
        )
        return {"ok": True, "document_id": str(doc.id), "status": "PENDING",
                "deferred": "audio_transcription", "request_id": request_id}
    except RetryableIngestError as e:
        # Erreur transitoire identifiée par la couche d'indexation (provider
        # embeddings indisponible, 429, 5xx, timeout…) => retry Celery.
        # (Doit précéder `except IngestError` : RetryableIngestError en hérite.)
        return _handle_ingest_failure(
            self, doc=doc, exc=e, msg=(str(e) or "transient_ingest_error"),
            request_id=request_id, log_prefix=log_prefix, force_retryable=True,
        )

    except DuplicateContentError as e:
        # Collision de contenu : réglée FAILED sous verrou par `reindex_existing_document`
        # (statut inchangé, 0 fragment, `meta.duplicate_of`). Définitive pour cette
        # ligne : aucun retry, un seul événement terminal, code métier explicite.
        logger.warning(
            "%s: duplicate_content document_id=%s duplicate_of=%s request_id=%s",
            log_prefix, doc.id, e.duplicate_of, request_id,
        )
        _emit_terminal_ingestion_failure(self, doc=doc, request_id=request_id, error_code=DUPLICATE_CONTENT)
        return {
            "ok": False, "document_id": str(doc.id), "error": DUPLICATE_CONTENT,
            "duplicate_of": e.duplicate_of, "request_id": request_id,
        }

    except IngestError as e:
        msg = str(e) or "ingest_error"
        # Erreur métier permanente (fichier manquant) => FAILED, pas de retry.
        if _is_source_unavailable_error(e):
            try:
                Document.objects.filter(id=doc.id).update(status=DocumentStatus.FAILED, error_message=msg[:1000])
            except Exception:
                # F5-A : échec de transition -> FAILED journalisé (sinon statut incohérent).
                logger.exception("%s: unable to mark document FAILED (permanent) document_id=%s request_id=%s", log_prefix, doc.id, request_id)
            logger.warning("%s: permanent error (%s) document_id=%s request_id=%s", log_prefix, msg, doc.id, request_id)
            # INGEST-1D-R2 : sortie terminale sans retry => un événement d'échec.
            # (La politique FAILED/source elle-même relève d'INGEST-1E.)
            _emit_terminal_ingestion_failure(self, doc=doc, request_id=request_id, error_code=msg)
            return {"ok": False, "document_id": str(doc.id), "error": msg, "request_id": request_id}
        # Les autres IngestError (format/vide/corrompu…) ne matchent pas les
        # marqueurs transitoires => FAILED définitif via _handle_ingest_failure.
        return _handle_ingest_failure(self, doc=doc, exc=e, msg=msg, request_id=request_id, log_prefix=log_prefix)

    except Exception as e:  # noqa: BLE001 — inclut SoftTimeLimitExceeded
        return _handle_ingest_failure(
            self, doc=doc, exc=e, msg=(str(e) or "unexpected_error"),
            request_id=request_id, log_prefix=log_prefix, generic=True,
        )


@shared_task(bind=True, max_retries=3, autoretry_for=(), retry_backoff=False)
def ingest_document_task(
    self, *, document_id: str, request_id: str | None = None, ocr: bool | None = None,
    access_context: dict | None = None, extraction_timeout_seconds: int | None = None,
    drop_visual_evidence: bool = False, enqueue_token: str | None = None,
    visual_primary: bool = False,
) -> dict:
    """
    Ingestion asynchrone d'un Document.
    Requiert ``storage_file`` ; retry sur erreurs transitoires, FAILED définitif
    sur erreurs métier ou après épuisement des retries.

    ``ocr`` (P1 bis) : ``True`` force l'OCR local pour cette tâche (réindexation
    OCR à la demande). Compatible ascendant (défaut ``None`` = config globale).

    ``drop_visual_evidence`` : opt-in explicite pour retirer une
    évidence visuelle publiée alors que l'analyse visuelle est éteinte.
    """
    return _run_ingestion(
        self, document_id=document_id, request_id=request_id,
        log_prefix="ingest_document_task", ocr_enabled=ocr, access_context=access_context,
        extraction_timeout_seconds=extraction_timeout_seconds,
        drop_visual_evidence=drop_visual_evidence,
        enqueue_token=enqueue_token, visual_primary=visual_primary,
    )


@shared_task(bind=True, max_retries=3, autoretry_for=(), retry_backoff=False)
def reindex_document_task(
    self, *, document_id: str, request_id: str | None = None, ocr: bool | None = None,
    access_context: dict | None = None, extraction_timeout_seconds: int | None = None,
    drop_visual_evidence: bool = False, enqueue_token: str | None = None,
    visual_primary: bool = False,
) -> dict:
    """
    Réindexation asynchrone d'un Document existant.
    Comportement strictement identique à ``ingest_document_task`` (logique
    mutualisée dans ``_run_ingestion``).
    """
    return _run_ingestion(
        self, document_id=document_id, request_id=request_id,
        log_prefix="reindex_document_task", ocr_enabled=ocr, access_context=access_context,
        extraction_timeout_seconds=extraction_timeout_seconds,
        drop_visual_evidence=drop_visual_evidence,
        enqueue_token=enqueue_token, visual_primary=visual_primary,
    )


#: Continuations d'un enrichissement dont le budget de temps est épuisé. Chacune reprend
#: les visuels non traités ; au-delà, les visuels restants sont déclarés en échec.
VISUAL_ENRICHMENT_MAX_CONTINUATIONS = 8


@shared_task(bind=True, max_retries=0)
def visual_enrichment_task(
    self, *, document_id: str, generation: str, request_id: str | None = None,
    access_context: dict | None = None, continuation: int = 0,
) -> dict:
    """Enrichissement visuel DIFFÉRÉ d'un document déjà READY (VISUAL-ENRICHMENT-DEFERRED).

    Aucune relance Celery du document : un visuel en défaillance relançable est rejoué
    SEUL dans la tâche ; si le budget de temps est épuisé, une CONTINUATION (même
    génération) reprend les visuels non traités — ceux déjà publiés sont sautés.
    Document supprimé, remplacé ou réindexé entre-temps : no-op sûr (génération).
    """
    from apps.rag_core.models.document import Document
    from apps.rag_core.services.ingest.queues import INGEST_VISUAL_QUEUE
    from apps.rag_core.services.ingest.visual import deferred as D
    from apps.rag_core.services.ingest.visual.enrichment import (
        OUTCOME_CONTINUE,
        run_enrichment,
    )
    from apps.workspaces.services import task_guard

    doc = Document.objects.filter(id=document_id).only("id", "rag_space_id").first()
    if doc is None:
        logger.info("visual_enrichment_task: document gone document_id=%s (no-op)", document_id)
        return {"ok": True, "document_id": document_id, "skipped": "not_found"}
    ctx = access_context or {}
    proceed, reason = task_guard.enforce_or_log(
        user_id=ctx.get("user_id"), rag_space_id=doc.rag_space_id, required_permission="write",
        job_label="visual_enrichment_task", require_context=True, request_id=request_id,
    )
    if not proceed:
        D.update_state(doc.id, generation=generation,
                       patch={"status": D.STATUS_FAILED, "error_code": f"owner_scope:{reason}"})
        return {"ok": False, "document_id": document_id, "error": reason}

    try:
        outcome = run_enrichment(document_id=document_id, generation=generation)
    except Exception as exc:  # noqa: BLE001 - jamais d'état « en cours » éternel
        logger.exception("visual_enrichment_task: unexpected error document_id=%s", document_id)
        D.update_state(doc.id, generation=generation,
                       patch={"status": D.STATUS_FAILED, "error_code": "unexpected_error"})
        return {"ok": False, "document_id": document_id, "error": exc.__class__.__name__}
    logger.info(
        "visual_enrichment_task: document_id=%s outcome=%s reason=%s published=%d failed=%d "
        "skipped_published=%d vision_calls=%d asset_retries=%d continuation=%d",
        document_id, outcome.action, outcome.reason, outcome.published, outcome.failed,
        outcome.skipped_published, outcome.vision_calls, outcome.asset_retries, continuation,
    )
    if outcome.action == OUTCOME_CONTINUE:
        if continuation < VISUAL_ENRICHMENT_MAX_CONTINUATIONS:
            visual_enrichment_task.apply_async(
                kwargs={"document_id": document_id, "generation": generation, "request_id": request_id,
                        "access_context": access_context, "continuation": continuation + 1},
                queue=INGEST_VISUAL_QUEUE, countdown=2,
            )
        else:
            D.update_state(doc.id, generation=generation,
                           patch={"status": D.STATUS_PARTIAL, "error_code": "continuation_budget_exhausted"})
    if outcome.action != OUTCOME_CONTINUE or continuation >= VISUAL_ENRICHMENT_MAX_CONTINUATIONS:
        # Extraction structurée automatique (chart/table) : APRÈS le describe de la
        # génération, dont elle lit l'évidence. Portes revérifiées ; jamais d'exception.
        from apps.rag_core.services.ingest.visual import structured_auto

        structured_auto.enqueue_after_enrichment(document_id=document_id, generation=generation,
                                                 request_id=request_id, access_context=access_context)
    return {"ok": True, "document_id": document_id, "outcome": outcome.action, "reason": outcome.reason,
            "published": outcome.published, "failed": outcome.failed, "vision_calls": outcome.vision_calls,
            "asset_retries": outcome.asset_retries}


@shared_task(bind=True, max_retries=0)
def structured_visual_task(
    self, *, document_id: str, generation: str, attempt: int = 1, request_id: str | None = None,
    access_context: dict | None = None,
) -> dict:
    """Extraction structurée AUTOMATIQUE chart/table d'une génération (STRUCTURED-VISUAL-2).

    Aucune relance Celery : sur GPU occupé, l'état est persisté et la tâche se
    reprogramme elle-même (``apply_async(countdown)``, bornée). Génération remplacée,
    document supprimé, porte refermée : no-op sûr. Le document n'est jamais modifié."""
    from apps.rag_core.models.document import Document
    from apps.rag_core.services.ingest.visual import structured_auto
    from apps.workspaces.services import task_guard

    doc = Document.objects.filter(id=document_id).only("id", "rag_space_id").first()
    if doc is None:
        return {"ok": True, "document_id": document_id, "skipped": "not_found"}
    ctx = access_context or {}
    proceed, reason = task_guard.enforce_or_log(
        user_id=ctx.get("user_id"), rag_space_id=doc.rag_space_id, required_permission="write",
        job_label="structured_visual_task", require_context=True, request_id=request_id,
    )
    if not proceed:
        return {"ok": False, "document_id": document_id, "error": reason}
    try:
        summary = structured_auto.run_auto(document_id=document_id, generation=generation, attempt=attempt,
                                           request_id=request_id, access_context=access_context)
    except Exception as exc:  # noqa: BLE001 - jamais d'état « en cours » éternel
        logger.exception("structured_visual_task: unexpected error document_id=%s", document_id)
        structured_auto._save_auto(document_id, generation, {"status": structured_auto.STATUS_FAILED,
                                                             "error_code": "unexpected_error"})
        return {"ok": False, "document_id": document_id, "error": exc.__class__.__name__}
    logger.info("structured_visual_task: document_id=%s attempt=%d status=%s skipped=%s", document_id,
                attempt, summary.get("status"), summary.get("skipped"))
    return {"ok": True, "document_id": document_id, "attempt": attempt,
            "status": summary.get("status"), "skipped": summary.get("skipped")}


# --------------------------------------------------------------------------- #
# Vision V2 orchestrée — ÉTEINTE par défaut
# --------------------------------------------------------------------------- #
# Aucune relance Celery (``max_retries=0``, jamais ``self.retry()``) : les reports sont
# des transitions persistées + ``apply_async(countdown)`` (cf. ``raster/orchestration``).
# Portes revérifiées à CHAQUE activation : refermer le drapeau arrête le rail.


@shared_task(bind=True, max_retries=0)
def visual_v2_prepare_version(self, *, run_id: str | None = None, version_id: str | None = None,
                              fingerprint: str | None = None, generation: int | None = None,
                              page_cursor: int = 0) -> dict:
    """Prépare UNE page à visuels d'une préparation DURABLE (``VisualPrepareRun``), puis se
    continue sur la page suivante. N'appelle jamais Vision.

    Seul un ``run_id`` DÉJÀ persisté est accepté (CV-3E4C) : un message sans préparation
    durable (ancien format ``version_id``/``fingerprint``, antérieur à CV-3E4A) est refusé
    sans rien créer — aucune préparation automatique n'existe hors de la transaction READY
    (``orchestration.persist_auto_prepare``). Les anciens paramètres restent dans la
    signature pour qu'un tel message soit consommé proprement, jamais en erreur de worker.
    """
    from apps.rag_core.services.raster import orchestration as O

    if run_id is None:
        logger.warning("visual_v2_prepare_version: message without durable run refused version_id=%s",
                       version_id)
        return {"ok": False, "error": "missing_run"}
    try:
        res = O.prepare_version_step(run_id)
    except Exception as exc:  # noqa: BLE001 - jamais d'effet sur le document texte
        logger.exception("visual_v2_prepare_version: unexpected error run_id=%s", run_id)
        return {"ok": False, "run_id": run_id, "error": exc.__class__.__name__}
    return {"ok": True, "run_id": run_id, "outcome": res.outcome, "page_number": res.page_number,
            "crops": res.crops, "jobs_created": res.jobs_created, "next_cursor": res.next_cursor}


@shared_task(bind=True, max_retries=0)
def visual_v2_sweep_task(self) -> dict:
    """Balayeur Vision V2 (CV-3E4A) — file ``maintenance``, ordonnancé par Beat. Une seule
    passe à la fois (``periodic_run``) ; fail-closed (drapeau éteint / production : aucune
    requête). Ne rouvre jamais un état final."""
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run
    from apps.rag_core.services.raster import orchestration as O

    try:
        with periodic_run("visual_v2_sweep") as run:
            res = O.sweep()
            run["sent"] = int(res.get("sent", 0))
    except LockNotAcquired:
        return {"ok": True, "skipped": True, "reason": "lock_held"}
    return {"ok": True, **res}


@shared_task(bind=True, max_retries=0)
def analyze_crop_job(self, *, job_id: str) -> dict:
    """UNE activation = UN crop (équité : la tâche rend la main à la file entre deux crops)."""
    from apps.rag_core.services.raster import orchestration as O

    return {"job_id": job_id, "outcome": O.run_crop_job(job_id)}


@shared_task(bind=True, max_retries=3)
def generate_document_summary_task(self, *, summary_id: str, user_id=None) -> dict:
    """Génère un résumé de document long (map-reduce) en arrière-plan.

    La ligne ``DocumentSummary`` (statut PENDING) est passée à READY avec le
    contenu, ou FAILED avec ``error_message`` en cas d'échec. Ne laisse jamais un
    blocage silencieux en PENDING : un FAILED explicite est préférable.

    Résilience (Priorité C) : une erreur provider transitoire (LLM indisponible,
    429/5xx/timeout → ``SummaryProviderError``) déclenche un retry avec backoff
    exponentiel (jusqu'à 3 fois), comme l'ingestion. Les erreurs métier permanentes
    (document non prêt, sans contenu) échouent immédiatement, sans retry inutile.
    """
    from celery.exceptions import MaxRetriesExceededError

    from apps.rag_core.models.summary import DocumentSummary
    from apps.rag_core.services.summary_service import (
        SummaryError,
        SummaryProviderError,
        run_summary_generation_for_row,
    )

    row = (
        DocumentSummary.objects.select_related("document", "rag_space")
        .filter(id=summary_id)
        .first()
    )
    if row is None:
        logger.warning("generate_document_summary_task: summary introuvable (id=%s)", summary_id)
        return {"ok": False, "summary_id": str(summary_id), "error": "not_found"}

    def _mark_failed(message: str, code: str) -> dict:
        DocumentSummary.objects.filter(id=summary_id).update(
            status="FAILED", error_message=(message or code)[:1000]
        )
        # Les messages de chat en attente de CE résumé doivent basculer en échec
        # explicite. Sinon le fil garde un « génération en cours » perpétuel, que
        # ni un refresh ni une réouverture ne résoudraient.
        from apps.rag_core.services.summary_service import _settle_chat_messages

        _settle_chat_messages(summary_id, ready=False, error=code)
        return {"ok": False, "summary_id": str(summary_id), "error": code}

    # Revalidation À L'EXÉCUTION (Lot 3) : no-op si flags OFF ; annule (sans retry
    # ni résultat partiel) si l'accès a changé depuis l'enqueue (révocation,
    # transfert de périmètre, workspace désactivé, espace supprimé…).
    from apps.workspaces.services import task_guard

    _proceed, _reason = task_guard.enforce_or_log(
        user_id=user_id, rag_space_id=row.rag_space_id, required_permission="read",
        job_label="document_summary",
    )
    if not _proceed:
        logger.warning("summary annulé par revalidation owner_scope (id=%s): %s", summary_id, _reason)
        return _mark_failed(f"owner_scope: {_reason}", _reason)

    try:
        run_summary_generation_for_row(row, user_id=user_id)
        return {"ok": True, "summary_id": str(summary_id)}
    except SummaryProviderError as e:
        # Transitoire : on retente avec backoff exponentiel (2s, 4s, 8s… max 60s).
        try:
            backoff_s = min(60, 2 ** (self.request.retries + 1))
            logger.warning(
                "summary provider error (id=%s), retry %d/3 in %ss: %s",
                summary_id, self.request.retries + 1, backoff_s, e,
            )
            raise self.retry(countdown=backoff_s, exc=e)
        except MaxRetriesExceededError:
            logger.error("summary provider error exhausted retries (id=%s): %s", summary_id, e)
            return _mark_failed(str(e), e.code)
    except SummaryError as e:
        # Permanent (document non prêt / sans contenu) : pas de retry.
        return _mark_failed(str(e), e.code)
    except Exception as e:  # noqa: BLE001
        logger.exception("generate_document_summary_task failed (id=%s): %s", summary_id, e)
        return _mark_failed(str(e) or "unexpected_error", "summary_failed")


@shared_task(bind=True, max_retries=0)
def reap_stale_summaries_task(self, *, timeout_s: int | None = None) -> dict:
    """Reap périodique des résumés bloqués en PENDING (tâche perdue → FAILED explicite).

    Planifié via `settings.CELERY_BEAT_SCHEDULE` (Lot 4-B). Idempotent et sans
    effet si aucun résumé n'est bloqué. Exécuté sous verrou distribué.
    """
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run
    from apps.rag_core.services.summary_service import reap_stale_summaries

    try:
        with periodic_run("reap_stale_summaries") as run:
            reaped = reap_stale_summaries(timeout_s=timeout_s)
            run["reaped"] = reaped
    except LockNotAcquired:
        return {"ok": True, "skipped": True, "reason": "lock_held"}

    return {"ok": True, "reaped": reaped}


# ===========================================================================
# Analyses globales de corpus (L2) — queue DÉDIÉE `analytics`.
#
# Ces tâches ne doivent JAMAIS tomber sur la queue par défaut : une analyse
# longue y affamerait l'ingestion et le chat sur les mêmes slots, et serait tuée
# par la limite globale de 540 s. Le routage est déclaré à trois endroits
# (défense en profondeur) : CELERY_TASK_ROUTES, `queue=` sur la signature de la
# tâche, et `queue="analytics"` explicite à l'apply_async.
# ===========================================================================
@shared_task(
    bind=True,
    name="apps.rag_core.tasks.run_corpus_analysis_job",
    queue="analytics",
    max_retries=0,           # aucun retry aveugle : cf. politique ci-dessous
    autoretry_for=(),
    acks_late=True,
    soft_time_limit=int(getattr(settings, "CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT", 3300)),
    time_limit=int(getattr(settings, "CORPUS_ANALYTICS_TASK_TIME_LIMIT", 3600)),
)
def run_corpus_analysis_job(self, job_id: str) -> dict:
    """Exécute un CorpusAnalysisJob.

    Politique de retry : AUCUN retry automatique. `execute_job` ne lève jamais —
    toute issue (annulation, type inconnu, erreur de handler, résultat invalide,
    dépassement de durée) devient un statut terminal persistant. Rejouer
    aveuglément une erreur de validation ou une annulation serait au mieux inutile,
    au pire coûteux. Un rejeu de la même tâche est de toute façon absorbé par la
    garde de double exécution (job terminal / déjà running).
    """
    from apps.rag_core.analysis.runner import execute_job
    from apps.rag_core.models import AnalysisStatus, CorpusAnalysisJob
    from apps.workspaces.services import task_guard

    _job = CorpusAnalysisJob.objects.filter(id=job_id).first()
    if _job is not None:
        _proceed, _reason = task_guard.enforce_or_log(
            user_id=_job.requested_by_id, rag_space_id=_job.rag_space_id,
            required_permission="read", job_label="corpus_analysis",
        )
        if not _proceed:
            CorpusAnalysisJob.objects.filter(id=job_id).exclude(
                status__in=[AnalysisStatus.FAILED, AnalysisStatus.CANCELLED]
            ).update(status=AnalysisStatus.CANCELLED)
            logger.warning("analysis annulé par revalidation owner_scope (id=%s): %s", job_id, _reason)
            return {"ok": False, "job_id": str(job_id), "reason": _reason}

    return execute_job(job_id)


@shared_task(
    bind=True,
    name="apps.rag_core.tasks.purge_expired_analysis_jobs_task",
    queue="analytics",
    max_retries=0,
)
def purge_expired_analysis_jobs_task(self) -> dict:
    """Purge des jobs TERMINAUX expirés. Idempotente, sans effet si rien n'a expiré."""
    from apps.rag_core.analysis.purge import purge_expired_analysis_jobs

    return {"ok": True, "purged": purge_expired_analysis_jobs()}


# ===========================================================================
# Réindexation durable d'un RagSpace (queue DÉDIÉE `reindex`).
#
# Architecture : 1 tâche orchestratrice légère (fan-out) + 1 tâche PAR document.
# La réponse HTTP 202 est déjà partie ; ces tâches ne bloquent aucune requête.
# Chaque item est idempotent (transition verrouillée) et atomique par document
# (l'ancien index n'est remplacé qu'en cas de succès — cf. index_extraction_result).
# ===========================================================================
@shared_task(
    bind=True,
    name="apps.rag_core.tasks.run_reindex_job_task",
    queue="reindex",
    max_retries=0,
    acks_late=True,
)
def run_reindex_job_task(self, *, job_id: str) -> dict:
    """Orchestrateur : passe le job en RUNNING et met un item par document en file.

    Ne fait AUCUNE extraction : il ne fait que semer les tâches document. En mode
    EAGER (tests), `delay()` s'exécute inline, donc la finalisation intervient
    naturellement après le dernier item.
    """
    from django.db import transaction
    from django.utils import timezone as _tz

    from apps.rag_core.models.reindex_job import (
        RagSpaceReindexJob,
        RagSpaceReindexJobItem,
        ReindexItemStatus,
        ReindexJobStatus,
    )
    from apps.rag_core.services.ingest.reindex_jobs import refresh_and_maybe_finalize

    with transaction.atomic():
        job = RagSpaceReindexJob.objects.select_for_update().filter(id=job_id).first()
        if job is None or job.is_terminal:
            return {"ok": False, "job_id": job_id, "reason": "absent_or_terminal"}

        # Revalidation À L'EXÉCUTION (Lot 3) : annule le job (sans retry) si l'accès
        # de l'initiateur a changé depuis l'enqueue. No-op si flags OFF.
        from apps.workspaces.services import task_guard as _tg

        _proceed, _reason = _tg.enforce_or_log(
            user_id=job.requested_by_id, rag_space_id=job.rag_space_id,
            required_permission="manage", job_label="ragspace_reindex",
        )
        if not _proceed:
            job.transition_to(ReindexJobStatus.CANCELLED)
            job.save(update_fields=["status", "updated_at"])
            return {"ok": False, "job_id": job_id, "reason": _reason}

        if job.status == ReindexJobStatus.PENDING:
            job.transition_to(ReindexJobStatus.QUEUING)
        if job.status == ReindexJobStatus.QUEUING:
            job.transition_to(ReindexJobStatus.RUNNING)
        if job.started_at is None:
            job.started_at = _tz.now()
        job.save(update_fields=["status", "started_at", "updated_at"])

    from apps.rag_core.services.ingest.queues import ingest_queue_for

    items = list(
        RagSpaceReindexJobItem.objects
        .filter(job_id=job_id, status=ReindexItemStatus.PENDING)
        .values_list("id", "document__filename")
    )
    item_ids = [i for i, _ in items]
    queued = 0
    for item_id, filename in items:
        # QUEUED avant l'enqueue : si l'enqueue échoue, l'item reste visible.
        RagSpaceReindexJobItem.objects.filter(
            id=item_id, status=ReindexItemStatus.PENDING
        ).update(status=ReindexItemStatus.QUEUED)
        try:
            # File DÉDIÉE par document : une maquette IFC ne doit
            # jamais occuper le worker `reindex` — même isolation qu'à l'upload.
            queue = ingest_queue_for(filename=filename)
            if queue:
                reindex_job_item_task.apply_async(kwargs={"item_id": str(item_id)}, queue=queue)
            else:
                reindex_job_item_task.delay(item_id=str(item_id))
            queued += 1
        except Exception:  # noqa: BLE001 - broker perdu en cours de route
            RagSpaceReindexJobItem.objects.filter(id=item_id).update(
                status=ReindexItemStatus.FAILED,
                error_code="enqueue_failed",
                error_message="La mise en file de ce document a échoué.",
            )
            logger.exception("reindex: enqueue item failed item=%s", item_id)

    # Cas d'un job sans item (corpus vide) : finaliser immédiatement.
    if not item_ids:
        refresh_and_maybe_finalize(job_id)
    return {"ok": True, "job_id": job_id, "queued": queued, "total": len(item_ids)}


@shared_task(
    bind=True,
    name="apps.rag_core.tasks.reindex_job_item_task",
    queue="reindex",
    max_retries=0,
    acks_late=True,
    soft_time_limit=int(getattr(settings, "REINDEX_ITEM_SOFT_TIME_LIMIT", 600)),
    time_limit=int(getattr(settings, "REINDEX_ITEM_TIME_LIMIT", 660)),
)
def reindex_job_item_task(self, *, item_id: str) -> dict:
    """Réindexe UN document. Idempotent, atomique par document, échec isolé.

    - transition verrouillée PENDING/QUEUED → RUNNING (une double livraison est
      absorbée : un item déjà terminal ou RUNNING frais est ignoré) ;
    - réutilise `reindex_existing_document` (remplacement atomique des chunks :
      les anciens ne sont supprimés qu'en cas d'extraction réussie) ;
    - jamais de retry aveugle : une erreur devient un item FAILED persistant.
    """
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone as _tz

    from apps.rag_core.models.reindex_job import (
        ERROR_EXTRACTION_FAILED,
        ERROR_NO_STORAGE_FILE,
        ERROR_SOURCE_FILE_MISSING,
        ITEM_UNFINISHED_STATUSES,
        SAFE_ERROR_MESSAGES,
        RagSpaceReindexJob,
        RagSpaceReindexJobItem,
        ReindexItemStatus,
    )
    from apps.rag_core.services.ingest.reindex_jobs import (
        ITEM_LEASE_SECONDS,
        refresh_and_maybe_finalize,
    )
    from apps.rag_core.services.ingest.types import IngestError
    from apps.rag_core.services.ingest_document import reindex_existing_document
    from apps.rag_core.services.ingest.audio.orchestrator import AudioTranscriptionDeferred

    now = _tz.now()
    # Prise de lease atomique : une seule livraison passe RUNNING.
    with transaction.atomic():
        item = RagSpaceReindexJobItem.objects.select_for_update().filter(id=item_id).first()
        if item is None:
            return {"ok": False, "item_id": item_id, "reason": "absent"}
        if item.status not in ITEM_UNFINISHED_STATUSES:
            return {"ok": False, "item_id": item_id, "reason": f"not_runnable:{item.status}"}
        if item.status == ReindexItemStatus.RUNNING and item.lease_expires_at and item.lease_expires_at > now:
            # Déjà pris par un worker vivant : double livraison, on n'y touche pas.
            return {"ok": False, "item_id": item_id, "reason": "lease_held"}
        if item.status == ReindexItemStatus.SKIPPED:
            # Décidé au snapshot : rien à tenter. Absorbé silencieusement plutôt
            # que traité comme une anomalie.
            return {"ok": True, "item_id": item_id, "reason": "skipped"}
        item.status = ReindexItemStatus.RUNNING
        item.attempt_count = (item.attempt_count or 0) + 1
        item.started_at = item.started_at or now
        item.heartbeat_at = now
        item.lease_expires_at = now + timedelta(seconds=ITEM_LEASE_SECONDS)
        item.celery_task_id = getattr(self.request, "id", "") or ""
        item.error_code = ""
        item.error_message = ""
        item.save(update_fields=[
            "status", "attempt_count", "started_at", "heartbeat_at",
            "lease_expires_at", "celery_task_id", "error_code", "error_message", "updated_at",
        ])
        job_id = str(item.job_id)
        document_id = str(item.document_id)
        # Acteur qui a DEMANDÉ le job : l'enrichissement visuel différé qu'une
        # réindexation déclenche revalide ses droits comme toute tâche d'ingestion
        # (VISUAL-ENRICHMENT-DEFERRED) — sans lui, il serait refusé sous enforcement.
        requested_by_id = RagSpaceReindexJob.objects.filter(id=item.job_id).values_list(
            "requested_by_id", flat=True).first()
        # Stratégie FIGÉE au snapshot : on la relit sur l'item, jamais on ne la
        # recalcule ici. Un job en vol ne peut donc pas changer de méthode parce
        # que le code d'erreur du document aurait bougé entretemps.
        effective_strategy = item.effective_strategy or "standard"

    from apps.rag_core.models.document import DocumentStatus

    from apps.rag_core.services.ingest.retry_strategy import ReindexOptions

    options = ReindexOptions.for_strategy(effective_strategy)
    ok = True
    err_code = ""
    err_msg = ""
    try:
        res = reindex_existing_document(
            document_id=document_id,
            allow_retry=False,
            # `force_ocr=False` doit rester `None` (= configuration globale) et non
            # `False`, qui DÉSACTIVERAIT l'OCR d'un environnement où il est activé.
            ocr_enabled=True if options.force_ocr else None,
            extraction_timeout_seconds=options.extraction_timeout_seconds,
            **({"access_context": {"user_id": requested_by_id, "request_id": f"reindex-job:{job_id}"}}
               if requested_by_id else {}),
        )
        logger.info(
            "reindex.item.completed job=%s item=%s document=%s strategy=%s status=%s",
            job_id, item_id, document_id, effective_strategy, getattr(res, "status", None),
        )
        # `extract_and_index_bytes` NE LÈVE PAS pour un échec de contenu (classeur
        # trop volumineux, PDF illisible, timeout d'extraction…) : il RENVOIE un
        # IngestResult FAILED. Un item ne doit donc pas se dire « réussi » quand le
        # document a échoué — sinon le job affiche « 0 échec » en masquant des
        # documents FAILED et `retry-failed` ne les voit pas.
        if getattr(res, "status", None) == DocumentStatus.FAILED:
            ok = False
            err_code = ERROR_EXTRACTION_FAILED
            err_msg = "La ré-extraction de ce document a échoué."
    except AudioTranscriptionDeferred:
        # audio sans transcription réutilisable → chaîne ASR planifiée
        # (le document est PENDING et sera indexé à son terme). Rien n'a échoué.
        logger.info("reindex.item.audio_deferred job=%s item=%s document=%s", job_id, item_id, document_id)
    except IngestError as e:
        ok = False
        # Une source indisponible n'est pas un échec d'extraction : la distinguer
        # est ce qui permet à l'opérateur de savoir qu'il doit fournir le fichier
        # à nouveau, au lieu de relancer une reprise qui échouera pareil.
        _codes = {
            NO_STORAGE_FILE: ERROR_NO_STORAGE_FILE,
            SOURCE_FILE_MISSING: ERROR_SOURCE_FILE_MISSING,
        }
        err_code = _codes.get(str(e), ERROR_EXTRACTION_FAILED)
        err_msg = SAFE_ERROR_MESSAGES.get(err_code, "La ré-extraction de ce document a échoué.")
    except Exception as e:  # noqa: BLE001
        ok = False
        err_code = ERROR_EXTRACTION_FAILED
        err_msg = "La ré-extraction de ce document a échoué."
        logger.exception("reindex_job_item_task failed item=%s doc=%s: %s", item_id, document_id, e)

    with transaction.atomic():
        item = RagSpaceReindexJobItem.objects.select_for_update().filter(id=item_id).first()
        if item is not None and item.status == ReindexItemStatus.RUNNING:
            item.status = ReindexItemStatus.SUCCEEDED if ok else ReindexItemStatus.FAILED
            item.finished_at = _tz.now()
            item.lease_expires_at = None
            item.error_code = err_code
            item.error_message = err_msg
            item.save(update_fields=[
                "status", "finished_at", "lease_expires_at",
                "error_code", "error_message", "updated_at",
            ])

    refresh_and_maybe_finalize(job_id)
    return {"ok": ok, "item_id": item_id, "document_id": document_id}


@shared_task(
    bind=True,
    name="apps.rag_core.tasks.reap_stale_reindex_items_task",
    queue="reindex",
    max_retries=0,
)
def reap_stale_reindex_items_task(self) -> dict:
    """Reap périodique : items RUNNING au lease expiré → INTERRUPTED ; documents
    INGESTING dont la tentative dépasse le seuil (sans item vivant) → READY si un index
    publié existe, FAILED sinon. N'efface aucun chunk. Idempotent.

    Lot 4-C : exécution sous verrou distribué. Sans lui, un reap lent redéclenché
    par Beat pouvait traiter les mêmes items en parallèle — inoffensif mais
    inutile, et générateur de conflits de verrous en base.
    """
    from apps.rag_core.services.ingest.reindex_jobs import recover_stale_items
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run

    try:
        with periodic_run("reap_stale_reindex_items") as run:
            res = recover_stale_items(dry_run=False)
            run["items"] = len(res.get("stale_items", []))
            # Documents RÉELLEMENT récupérés (la comparaison-échange peut en écarter).
            run["documents"] = len(res.get("recovered_ingesting_documents", []))
    except LockNotAcquired:
        return {"ok": True, "skipped": True, "reason": "lock_held"}

    return {
        "ok": True,
        "recovered_items": len(res.get("stale_items", [])),
        "recovered_documents": len(res.get("recovered_ingesting_documents", [])),
    }


# ===========================================================================
# Battement de cœur de Celery Beat (Lot 4-G)
# ===========================================================================

@shared_task(
    name="apps.rag_core.tasks.celery_beat_heartbeat_task",
    # Volontairement SANS verrou : c'est la tâche la plus légère du système, et
    # un verrou ferait échouer le heartbeat pendant qu'une autre instance écrit —
    # exactement le contraire du signal recherché.
    soft_time_limit=30,
    time_limit=60,
    max_retries=0,
)
def celery_beat_heartbeat_task() -> dict:
    """Écrit un battement daté prouvant que Beat ordonnance ET qu'un worker exécute.

    C'est la seule sonde qui traverse toute la chaîne scheduler → broker → queue
    → worker. Un `pgrep celery` répondrait « vivant » alors que le scheduler ne
    planifie plus rien.
    """
    from apps.rag_core.services.beat_heartbeat import write_heartbeat

    payload = write_heartbeat()
    logger.info(
        "beat.heartbeat instance=%s environment=%s",
        payload.get("instance_id"), payload.get("environment") or "-",
    )
    return payload


# ===========================================================================
# Cohérence du stockage documentaire (P0-2) — READ-ONLY
# ===========================================================================
# Noms de compteurs STABLES (cf. convention tabular/observability.py).
MEDIA_AUDIT_LAST_SUCCESS = "ragatm_media_storage_audit_last_success_timestamp"
MEDIA_AUDIT_MISSING_FILES = "ragatm_media_storage_audit_missing_files"
MEDIA_AUDIT_ORPHAN_FILES = "ragatm_media_storage_audit_orphan_files"
MEDIA_AUDIT_FAILURES = "ragatm_media_storage_audit_failures_total"


@shared_task(
    name="apps.rag_core.tasks.audit_media_storage_task",
    soft_time_limit=1740,   # 29 min — un volume documentaire se parcourt lentement
    time_limit=1800,
    # AUCUN retry : rejouer immédiatement un parcours complet du volume coûte cher
    # et n'apprend rien de plus. Le cycle suivant s'en charge.
    max_retries=0,
)
def audit_media_storage_task() -> dict:
    """Contrôle périodique base ↔ fichiers. **Ne supprime jamais rien.**

    Cette tâche est la contrepartie opérationnelle de l'ordre « fichiers d'abord »
    de la purge d'un RagSpace : ce compromis n'est défendable que parce que
    l'anomalie résultante est DÉTECTABLE. Tant que la détection ne tournait qu'à
    la main, la compensation n'existait que sur le papier.
    """
    import time as _time

    from django.conf import settings as _settings

    from apps.rag_core.services import media_storage_audit
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run
    from apps.rag_core.tabular import observability as obs

    if not bool(getattr(_settings, "RAG_MEDIA_STORAGE_AUDIT_ENABLED", False)):
        # Désactivé PAR DÉFAUT : l'activation est un geste d'exploitation
        # explicite, environnement par environnement.
        logger.info("media_storage_audit disabled RAG_MEDIA_STORAGE_AUDIT_ENABLED=0")
        return {"skipped": True, "reason": "RAG_MEDIA_STORAGE_AUDIT_ENABLED=0"}

    try:
        with periodic_run("audit_media_storage") as run:
            # `collect()` SANS exemples : aucun nom de fichier ne doit atteindre
            # les journaux centraux (ils sont réservés à la commande opérateur).
            report = media_storage_audit.collect()
            run["status"] = report["status"]
            run["missing_file"] = report["missing_file"]
            run["orphan_file"] = report["orphan_file"]
            run["referenced_total"] = report["referenced_total"]
    except LockNotAcquired:
        return {"skipped": True, "reason": "lock_held"}
    except Exception:
        obs.incr(MEDIA_AUDIT_FAILURES)
        raise

    obs.set_gauge(MEDIA_AUDIT_LAST_SUCCESS, _time.time())
    obs.set_gauge(MEDIA_AUDIT_MISSING_FILES, report["missing_file"])
    obs.set_gauge(MEDIA_AUDIT_ORPHAN_FILES, report["orphan_file"])

    summary = media_storage_audit.public_summary(report)
    if not media_storage_audit.is_clean(report):
        # ERROR et non WARNING : une référence sans fichier signifie un document
        # devenu inconsultable, un orphelin signifie une purge incomplète. Les
        # deux méritent qu'on regarde, aucun ne se répare tout seul.
        logger.error(
            "media_storage_audit anomaly status=%s missing_file=%s orphan_file=%s "
            "referenced_total=%s files_on_disk=%s",
            summary["status"], summary["missing_file"], summary["orphan_file"],
            summary["referenced_total"], summary["files_on_disk"],
        )
    else:
        logger.info(
            "media_storage_audit status=ok referenced_total=%s files_on_disk=%s",
            summary["referenced_total"], summary["files_on_disk"],
        )
    return summary


# ===========================================================================
# Supervision des purges de RagSpace en échec (P0-3) — READ-ONLY
# ===========================================================================
DELETE_MONITOR_LAST_SUCCESS = "ragatm_ragspace_delete_monitor_last_success_timestamp"
DELETE_MONITOR_FAILURES_SEEN = "ragatm_ragspace_delete_failures_seen_total"
DELETE_MONITOR_ERRORS = "ragatm_ragspace_delete_monitor_errors_total"


@shared_task(
    name="apps.rag_core.tasks.monitor_ragspace_deletion_failures_task",
    soft_time_limit=120,
    time_limit=180,
    max_retries=0,
)
def monitor_ragspace_deletion_failures_task() -> dict:
    """Remonte les `ragspace.delete` en échec depuis le dernier passage.

    Un échec de purge laissait une trace d'audit que personne ne lisait : la
    seule façon de le découvrir était une requête SQL manuelle. Cette tâche ne
    modifie AUCUN événement — elle lit, journalise et compte.
    """
    import time as _time

    from django.conf import settings as _settings

    from apps.rag_core.services import rag_space_deletion_monitoring as monitoring
    from apps.rag_core.services.periodic import LockNotAcquired, periodic_run
    from apps.rag_core.tabular import observability as obs

    if not bool(getattr(_settings, "RAG_RAGSPACE_DELETE_MONITORING_ENABLED", False)):
        logger.info(
            "ragspace_delete_monitor disabled RAG_RAGSPACE_DELETE_MONITORING_ENABLED=0"
        )
        return {"skipped": True, "reason": "RAG_RAGSPACE_DELETE_MONITORING_ENABLED=0"}

    window = int(getattr(_settings, "RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS", 86400))

    try:
        with periodic_run("monitor_ragspace_deletions") as run:
            cursor = monitoring.read_cursor()
            report = monitoring.scan(window_seconds=window, since=cursor)
            monitoring.log_report(report)
            # Curseur écrit APRÈS la journalisation : si le processus meurt entre
            # les deux, l'alerte repart au tour suivant plutôt que d'être perdue.
            run["cursor_persisted"] = int(monitoring.write_cursor(
                _parse_cursor(report.get("cursor"))
            ))
            run["failures_total"] = report["failures_total"]
            run["status"] = report["status"]
    except LockNotAcquired:
        return {"skipped": True, "reason": "lock_held"}
    except Exception:
        obs.incr(DELETE_MONITOR_ERRORS)
        raise

    obs.set_gauge(DELETE_MONITOR_LAST_SUCCESS, _time.time())
    if report["failures_total"]:
        obs.incr(DELETE_MONITOR_FAILURES_SEEN, report["failures_total"])

    # Rapport SANS le détail des événements : les identifiants sont déjà partis
    # en journal, une par ligne. Ici, seul le résumé chiffré.
    return {
        "status": report["status"],
        "window_seconds": report["window_seconds"],
        "failures_total": report["failures_total"],
        "reported": report["reported"],
        "truncated": report["truncated"],
    }


def _parse_cursor(value):
    """ISO 8601 -> datetime (ou None). Isolé pour rester testable sans Redis."""
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None



# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=0,
             soft_time_limit=1500, time_limit=1620)
def audio_transcription_step(self, transcript_id: str, token: str) -> dict:
    """UNE étape de la chaîne de transcription (file `ingest_audio`, concurrence 1).

    Courte par construction (réencodage d'une partie, dépôt, OU une relecture) : la
    chaîne se re-planifie elle-même. L'état vit en base : une étape rejouée (message
    redélivré, chien de garde) relit l'état et ne refait rien de terminé.
    """
    from apps.rag_core.services.ingest.audio import orchestrator

    outcome = orchestrator.run_step(transcript_id, token)
    return {"ok": True, "transcript_id": transcript_id, "outcome": outcome}


@shared_task
def audio_transcription_watchdog_task() -> dict:
    """Relance les chaînes audio actives restées sans activité (worker redémarré)."""
    from apps.rag_core.services.ingest.audio import orchestrator

    return {"relaunched": orchestrator.watchdog()}

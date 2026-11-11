# backend/api/v1/documents/views_write.py
from __future__ import annotations

import mimetypes

from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .views_common import (
    DocumentStatus,
    RagSpace,
    _boolish,
    _ensure_storage_file_or_fail,
    _serialize_document,
    _sha256_bytes,
    _sha256_text,
    _should_async,
    count_chunks_for_document,
    enqueue_async_or_503,
    is_celery_eager_mode,
    logger,
    reindex_existing_document,
    require_rag_space_access,
    uuid,
)

from apps.rag_core.repositories.document_repository import (
    CreateOrGetResult,
    create_or_get_by_sha,
    find_existing_by_sha,
)
from apps.workspaces.services import quota as quota_service
from apps.rag_core.services.ingest.limits import (
    UploadLimitExceeded,
    check_file_size,
    check_pdf_pages,
)
from apps.rag_core.services.ingest.extraction import (
    ExtractionContext,
    RejectedFile,
    UnsupportedFormat,
    validate_and_classify,
)
from apps.rag_core.services.ingest.extraction.validation import check_email_admission
from apps.rag_core.models.document import Document, DocumentStatus
from apps.rag_core.models.reindex_job import SAFE_ERROR_MESSAGES
from apps.rag_core.services.documents.source_availability import (
    NO_STORAGE_FILE,
    SOURCE_FILE_MISSING,
)
from apps.rag_core.services.ingest.retry_strategy import REPLACE_SOURCE_RECOMMENDED
from apps.rag_core.services.ingest.import_paths import (
    UnsafeImportPath,
    normalize_import_metadata,
)


# Résolu au moment de L'APPEL, et non lié à l'import.
#
# `apps.actions.wiring` importe ce module paresseusement. Quand cet import se
# produit alors qu'un test a substitué `views_common.load_accessible_document`, un
# `from … import` figerait ici le double pour toute la durée du processus — et une
# vue ultérieure recevrait ce double au lieu de la vraie garde d'accès. Le passage
# par le module supprime cette dépendance à l'ordre d'import.
def load_accessible_document(*args, **kwargs):
    from . import views_common

    return views_common.load_accessible_document(*args, **kwargs)


from apps.rag_core.services.ingest.audio.orchestrator import AudioTranscriptionDeferred  # noqa: E402


def _coerce_uuid(v):
    """
    Django url converter <uuid:...> already passes a uuid.UUID.
    But some older routes could still pass str. Handle both.
    """
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except Exception:
        return None


class DocumentDeleteView(APIView):
    """
    DELETE /api/v1/documents/<document_id>/delete/

    Suppression DÉFINITIVE (hard delete) : document + chunks + résumés + fichier
    source. Réservée aux utilisateurs avec accès en écriture sur le RagSpace
    (viewer => 403 ; aucun accès => 404 anti-énumération). Refusée (409) pendant
    l'ingestion (PENDING/INGESTING) : pas d'annulation sûre des tâches Celery.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, document_id):
        from django.core.exceptions import PermissionDenied
        from rest_framework.exceptions import NotFound

        from apps.audit.action_audit import emit_action_event
        from apps.rag_core.models.document import DocumentStatus
        from apps.rag_core.services.documents.delete_document import delete_document

        rid = request.headers.get("x-request-id") or ""
        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            doc = load_accessible_document(request.user, doc_uuid, write=True, request=request)
        except (PermissionDenied, NotFound) as e:
            emit_action_event(
                event_name="action.denied", action_name="document.delete", status="denied",
                actor=request.user, resource_type="document", resource_id=doc_uuid,
                request_id=rid, error_code=e.__class__.__name__.lower(),
            )
            raise

        ws_id = str(getattr(doc.rag_space, "workspace_id", "") or "")
        emit_action_event(
            event_name="action.started", action_name="document.delete", status="started",
            actor=request.user, workspace_id=ws_id, rag_space_id=doc.rag_space_id,
            resource_type="document", resource_id=doc.id, request_id=rid,
        )

        # Suppression refusée pendant le traitement (pas d'annulation Celery sûre).
        # Exception ÉTROITE : un audio PENDING dont la transcription est en
        # préparation/transcription. Cette phase (jusqu'à plusieurs heures) est annulable par
        # construction : chaque étape vérifie que sa ligne existe encore, et les jobs Gateway
        # de CE document sont libérés par `delete_document`. L'indexation finale reste refusée.
        from apps.rag_core.models import AudioTranscript, AudioTranscriptStatus

        audio_cancellable = doc.status == DocumentStatus.PENDING and AudioTranscript.objects.filter(
            document_id=doc.id, status__in=[AudioTranscriptStatus.PREPARING, AudioTranscriptStatus.TRANSCRIBING],
        ).exists()
        if doc.status in (DocumentStatus.PENDING, DocumentStatus.INGESTING) and not audio_cancellable:
            emit_action_event(
                event_name="action.failed", action_name="document.delete", status="failed",
                actor=request.user, workspace_id=ws_id, rag_space_id=doc.rag_space_id,
                resource_type="document", resource_id=doc.id, request_id=rid,
                error_code="document_processing",
            )
            return Response(
                {"detail": "document_processing", "status": doc.status},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            result = delete_document(doc, actor=request.user)
        except Exception:
            emit_action_event(
                event_name="action.failed", action_name="document.delete", status="failed",
                actor=request.user, workspace_id=ws_id, rag_space_id=doc.rag_space_id,
                resource_type="document", resource_id=doc_uuid, request_id=rid,
                error_code="delete_failed",
            )
            raise

        rd = result.as_dict()
        emit_action_event(
            event_name="action.completed", action_name="document.delete", status="completed",
            actor=request.user, workspace_id=ws_id, rag_space_id=doc.rag_space_id,
            resource_type="document", resource_id=doc_uuid, request_id=rid,
            metadata={
                "chunks_deleted": rd.get("chunks_deleted"),
                "storage_deleted": rd.get("file_deleted"),
                # Tracé séparément : « rien à supprimer » n'est pas « supprimé ».
                # Sans cette distinction, l'audit d'une purge sur un espace dégradé
                # affirmerait avoir supprimé des fichiers déjà absents.
                "storage_already_missing": rd.get("file_already_missing"),
                "warnings": rd.get("warnings"),
            },
        )
        return Response({"ok": True, **rd})


def _source_unavailability_code(exc: Exception) -> str:
    """Code d'indisponibilité de la source porté par ``exc``, `""` sinon.

    Les deux codes sont DÉFINITIFS pour ce document : aucune reprise à l'identique
    ne peut aboutir. Les remonter tels quels — plutôt que fondus dans un 500 —
    est ce qui permet à l'interface de proposer « fournir la source à nouveau ».
    """
    msg = (str(exc) or "").strip().lower()
    for code in (SOURCE_FILE_MISSING, NO_STORAGE_FILE):
        if code in msg:
            return code
    return ""


def _emit_reindex(*, request, doc, rid, event, st, decision, **extra):
    """Événement d'audit d'une reprise unitaire, stratégie comprise.

    L'audit doit permettre de répondre plus tard à « pourquoi ce document a-t-il
    été repris avec OCR ? » et « quel délai lui a été accordé ? ». Le code d'erreur
    qui a motivé la décision en fait donc partie — jamais le contenu du document.
    """
    from apps.audit.action_audit import emit_action_event

    metadata = {
        "ocr_enabled": bool(decision.force_ocr),
        "requested_strategy": decision.requested,
        "effective_strategy": decision.effective,
        "retryability": decision.retryability,
        "reason_code": decision.reason_code,
    }
    if decision.timeout_seconds is not None:
        metadata["extended_timeout_seconds"] = decision.timeout_seconds
    if decision.auto_fallback:
        metadata["auto_fallback"] = True
    for key in ("chunk_count", "forced"):
        if key in extra:
            metadata[key] = extra.pop(key)
    emit_action_event(
        event_name=event, action_name="document.reindex", status=st,
        actor=request.user, workspace_id=str(getattr(doc.rag_space, "workspace_id", "") or ""),
        rag_space_id=doc.rag_space_id, resource_type="document", resource_id=doc.id,
        request_id=rid, metadata=metadata, **extra,
    )


REINDEX_ALREADY_IN_PROGRESS = "reindex_already_in_progress"
#: Un contenu déjà présent sur une ligne FAILED a été RÉUTILISÉ comme cible de reprise
#: (même document_id, aucune ligne créée) et ré-enfilé.
FAILED_DOCUMENT_RETRIED = "failed_document_retried"


#: Un doublon de contenu d'un document dont l'ingestion est DÉJÀ planifiée ou en cours.
INGESTION_ALREADY_IN_PROGRESS = "ingestion_already_in_progress"
_ACTIVE_INGESTION_STATUSES = (DocumentStatus.PENDING, DocumentStatus.INGESTING)


def _duplicate_of_active_ingestion(res) -> Response | None:
    """Garde anti-ré-enfilement d'un upload dédupliqué (dette mesurée en staging le
    21/09/2026 : 515 uploads acceptés pour 502 documents, 8 documents en file 2 ou 3 fois,
    chacun traité autant de fois par le worker).

    Avant : un doublon SANS chunk était ré-enfilé — or « 0 chunk » veut aussi dire « pas
    encore traité ». Ici, l'autorité est l'ÉTAT d'ingestion, comme pour la réindexation
    (`reindex_already_in_progress`) : PENDING ou INGESTING = une tâche existe déjà, on
    n'en crée pas une seconde. Hors de ces états (READY sans chunk, par exemple) le
    comportement historique est conservé. Un FAILED n'arrive jamais ici :
    `find_existing_by_sha` l'ignore, et le ré-upload reste une voie de reprise. Un PENDING
    réellement orphelin se reprend par `reindex?force=true`.
    """
    if not res.deduped:
        return None
    doc = res.document
    current = Document.objects.filter(id=doc.id).values_list("status", flat=True).first()
    if current not in _ACTIVE_INGESTION_STATUSES:
        return None
    return Response({
        "ok": True, "queued": True, "deduped": True, "document_id": str(doc.id),
        "status": current, "detail": INGESTION_ALREADY_IN_PROGRESS,
    })


def _parse_force(request):
    """``force`` du corps JSON : absent/``null``/``false`` → False, ``true`` → True.

    Booléen JSON STRICT : ``"true"``, ``1``, ``[]``… sont refusés (``invalid_force``),
    comme ``strategy`` refuse une valeur hors liste — un contournement de garde ne
    doit jamais être accepté par coercition.
    """
    data = request.data if isinstance(request.data, dict) else {}
    value = data.get("force")
    if value is None:
        return False, None
    if isinstance(value, bool):
        return value, None
    return False, Response({"detail": "invalid_force"}, status=status.HTTP_400_BAD_REQUEST)


def _parse_drop_visual_evidence(request):
    """``drop_visual_evidence`` du corps JSON, booléen JSON STRICT.

    Opt-in EXPLICITE pour retirer l'évidence visuelle publiée d'un document quand
    l'analyse visuelle est éteinte. Absent/``null``/``false`` → False. Distinct de
    ``force`` : forcer l'admission n'autorise jamais une perte d'évidence. N'élargit
    aucun droit (mêmes contrôles d'accès que la réindexation).
    """
    data = request.data if isinstance(request.data, dict) else {}
    value = data.get("drop_visual_evidence")
    if value is None:
        return False, None
    if isinstance(value, bool):
        return value, None
    return False, Response({"detail": "invalid_drop_visual_evidence"}, status=status.HTTP_400_BAD_REQUEST)


def _active_reindex_item(doc):
    """Item de job de réindexation RÉELLEMENT actif pour ce document (RUNNING avec
    lease fraîche — la définition d'activité du reaper), ou ``None``. Lecture simple,
    aucun verrou job/item."""
    from apps.rag_core.models.reindex_job import RagSpaceReindexJobItem, ReindexItemStatus
    from django.utils import timezone

    return (
        RagSpaceReindexJobItem.objects
        .filter(document_id=doc.id, status=ReindexItemStatus.RUNNING, lease_expires_at__gte=timezone.now())
        .order_by("-started_at")
        .only("id", "job_id")
        .first()
    )


def _admit_explicit_reindex(*, doc, use_async: bool, force: bool):
    """Admission ATOMIQUE d'une réindexation explicite (REINDEX-GUARD-1).

    Transaction COURTE, verrou de la ligne document seul (jamais l'espace : aucun
    cycle avec la publication espace→document ni le règlement d'échec) :

        SELECT FOR UPDATE → si PENDING/INGESTING et non forcé ⇒ 409 (rien ne démarre)
        sinon transition READY/FAILED → PENDING (async) | INGESTING (sync) → COMMIT

    La transition ferme la course « deux POST simultanés » : la seconde requête
    attend le verrou puis voit l'état posé par la première. Elle n'est qu'un
    LOQUET D'ADMISSION HTTP — ni précondition de publication, ni jeton de
    génération (INGEST-1F). ``force=true`` n'outrepasse que ce loquet : les
    contrôles d'accès, d'éligibilité et de stratégie ont déjà eu lieu.
    Rend ``None`` si admis, sinon la ``Response`` 409 construite depuis la ligne
    verrouillée courante. Le document disparu ⇒ ``NotFound`` (contrat existant).
    """
    from django.db import transaction
    from rest_framework.exceptions import NotFound

    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(id=doc.id).first()
        if locked is None:
            raise NotFound("not_found")
        if locked.status in (DocumentStatus.PENDING, DocumentStatus.INGESTING) and not force:
            body = {
                "ok": False,
                "detail": REINDEX_ALREADY_IN_PROGRESS,
                "error_code": REINDEX_ALREADY_IN_PROGRESS,
                "message": "Une réindexation est déjà en cours ou planifiée pour ce document.",
                "document_id": str(locked.id),
                "status": locked.status,
                "force_allowed": True,
            }
            item = _active_reindex_item(locked)
            if item is not None:
                body["active_job_id"] = str(item.job_id)
                body["active_item_id"] = str(item.id)
            return Response(body, status=status.HTTP_409_CONFLICT)
        if use_async:
            fields = {"status": DocumentStatus.PENDING}
        else:
            # La réindexation SYNCHRONE admise ouvre une tentative (horodatage renouvelé).
            from apps.rag_core.services.ingest.attempts import ingesting_update_fields

            fields = ingesting_update_fields(new_attempt=True)
        Document.objects.filter(id=locked.id).update(**fields, error_message="")
    return None


class DocumentReindexView(APIView):
    """
    POST /api/v1/documents/<document_id>/reindex/?async=true|false&ocr=true|false

    ``ocr=true`` (P1 bis) : force l'OCR LOCAL pour CETTE réindexation uniquement,
    sans activer l'OCR globalement. L'OCR étant coûteux, il **force l'asynchrone**
    (un worker Celery est alors requis ; sinon 503), sauf en mode eager (tests).

    Admission (REINDEX-GUARD-1) : un document déjà ``PENDING`` ou ``INGESTING``
    est refusé **409** ``reindex_already_in_progress`` (même ou autre stratégie),
    sauf corps ``{"force": true}`` — l'utilisateur assume alors une seconde
    opération concurrente (sûre par INGEST-1F, dernier publieur gagne). ``force``
    ne contourne ni l'accès, ni l'éligibilité (``document_not_eligible``), ni la
    stratégie. Les chemins internes (retry worker, items de job, reaper,
    redélivrance) ne passent pas par cette vue et restent libres.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, document_id):
        from django.core.exceptions import PermissionDenied
        from rest_framework.exceptions import NotFound

        from apps.audit.action_audit import emit_action_event

        rid = request.headers.get("x-request-id") or ""
        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            doc = load_accessible_document(request.user, doc_uuid, write=True, request=request)
        except (PermissionDenied, NotFound) as e:
            emit_action_event(
                event_name="action.denied", action_name="document.reindex", status="denied",
                actor=request.user, resource_type="document", resource_id=doc_uuid,
                request_id=rid, error_code=e.__class__.__name__.lower(),
            )
            raise

        from apps.rag_core.services.ingest.retry_strategy import (
            REQUESTABLE_STRATEGIES,
            STRATEGY_FORCE_OCR,
            ReindexOptions,
            resolve_retry_strategy,
        )

        # Contrat unique : `{"strategy": …}` dans le corps. `?ocr=true` reste accepté
        # et TRADUIT en `force_ocr` — **déprécié**, conservé pour ne pas casser les
        # appels existants (menu « Réindexer avec OCR » d'avant ce lot, scripts).
        legacy_ocr = _boolish(request.query_params.get("ocr"))
        raw_strategy = (request.data.get("strategy") if isinstance(request.data, dict) else None)
        if raw_strategy is None:
            requested_strategy = STRATEGY_FORCE_OCR if legacy_ocr else "standard"
        else:
            requested_strategy = str(raw_strategy).strip().lower()
            if requested_strategy not in REQUESTABLE_STRATEGIES:
                return Response(
                    {"detail": "invalid_strategy", "allowed": sorted(REQUESTABLE_STRATEGIES)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        decision = resolve_retry_strategy(doc, requested_strategy)
        if not decision.is_actionable:
            # Refus EXPLICITE plutôt qu'un job qui ne ferait rien : sur une reprise
            # unitaire, l'utilisateur attend un résultat, pas un silence.
            _emit_reindex(
                request=request, doc=doc, rid=rid, event="action.failed", st="failed",
                decision=decision, error_code=decision.skip_reason,
            )
            return Response(
                {
                    "detail": "strategy_not_supported"
                    if decision.skip_reason == "strategy_not_supported" else "document_not_eligible",
                    "retryability": decision.retryability,
                    "error_code": decision.reason_code,
                    "skip_reason": decision.skip_reason,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        options = ReindexOptions.for_strategy(decision.effective)
        ocr_requested = options.force_ocr

        force, invalid = _parse_force(request)
        if invalid is not None:
            return invalid
        drop_visual, invalid = _parse_drop_visual_evidence(request)
        if invalid is not None:
            return invalid
        visual_kwargs = {"drop_visual_evidence": True} if drop_visual else {}

        def _emit(event, st, **extra):
            if force:
                extra.setdefault("forced", True)
            _emit_reindex(
                request=request, doc=doc, rid=rid, event=event, st=st,
                decision=decision, **extra,
            )

        # OCR à la demande => async OBLIGATOIRE (jamais d'OCR dans le thread HTTP).
        # Le délai étendu aussi : par construction, il vise des extractions longues.
        # En eager (CELERY_TASK_ALWAYS_EAGER=1) : pas de broker/worker, on exécute
        # en sync explicite (queued:false) plutôt que de feindre un enqueue.
        # Un format routé vers une file DÉDIÉE (IFC, DWG) ne peut JAMAIS s'exécuter dans
        # le processus web : son parseur/convertisseur n'y est pas déployé et la charge y
        # est interdite. `?async=false` ne peut donc pas le ramener en synchrone
        # (même règle qu'à l'upload, où la décision passe par
        # `resolve_async(format_key=…)`).
        from apps.rag_core.services.ingest.queues import ingest_queue_for

        needs_worker = (
            ocr_requested
            or options.extraction_timeout_seconds is not None
            or ingest_queue_for(filename=getattr(doc, "filename", None)) is not None
        )
        use_async = (_should_async(request) or needs_worker) and not is_celery_eager_mode()

        # Admission ATOMIQUE (REINDEX-GUARD-1) — après accès, éligibilité et
        # stratégie ; AVANT tout événement de travail : une demande refusée n'a rien
        # démarré et ne laisse aucune trace « detected/queued/started ».
        conflict = _admit_explicit_reindex(doc=doc, use_async=use_async, force=force)
        if conflict is not None:
            return conflict

        _emit("action.detected", "detected")

        if use_async:
            failed = enqueue_async_or_503(
                request=request, doc=doc, ocr=ocr_requested or None,
                extraction_timeout_seconds=options.extraction_timeout_seconds,
                **visual_kwargs,
            )
            if failed is not None:
                _emit("action.failed", "failed", error_code="enqueue_unavailable")
                return failed
            _emit("action.queued", "queued")
            return Response({
                "ok": True, "queued": True, "document_id": str(doc.id),
                "ocr": bool(ocr_requested), "strategy": decision.effective,
                "requested_strategy": decision.requested,
            })

        if reindex_existing_document is None:
            _emit("action.failed", "failed", error_code="reindex_not_supported")
            return Response({"detail": "reindex_not_supported"}, status=status.HTTP_400_BAD_REQUEST)

        _emit("action.started", "started")
        try:
            # selon ton service : document_id kw-only ou doc
            try:
                res = reindex_existing_document(
                    document_id=str(doc.id),
                    ocr_enabled=(True if ocr_requested else None),
                    extraction_timeout_seconds=options.extraction_timeout_seconds,
                    **visual_kwargs,
                )  # type: ignore[misc]
            except TypeError:
                res = reindex_existing_document(doc)  # type: ignore[misc]
        except AudioTranscriptionDeferred:
            # la transcription est (re)planifiée ; l'indexation suivra.
            _emit("action.queued", "queued")
            return Response({"ok": True, "queued": True, "document_id": str(doc.id),
                             "deferred": "audio_transcription"})
        except Exception as e:
            # Source indisponible : ce n'est pas une panne du service, c'est un
            # constat définitif sur CE document. Le renvoyer en 500 anonyme
            # laissait l'appelant devant « reindex_failed » sans savoir que le
            # geste utile est de téléverser à nouveau — et laissait le document
            # dans son statut d'avant, sans diagnostic (cf. lot guardrails, §12).
            _source_error = _source_unavailability_code(e)
            if _source_error:
                Document.objects.filter(id=doc.id).update(
                    status=DocumentStatus.FAILED, error_message=_source_error,
                )
                _emit("action.failed", "failed", error_code=_source_error)
                logger.warning("reindex sync: %s document_id=%s", _source_error, doc.id)
                return Response(
                    {
                        "detail": _source_error,
                        "error_code": _source_error,
                        "message": SAFE_ERROR_MESSAGES.get(_source_error, ""),
                        "retryability": REPLACE_SOURCE_RECOMMENDED,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            logger.exception("reindex sync failed: %s", e)
            _emit("action.failed", "failed", error_code="reindex_failed")
            return Response({"detail": "reindex_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        chunks = count_chunks_for_document(document_id=doc.id)
        doc.refresh_from_db()

        # INGEST-1D : le service RENVOIE un IngestResult FAILED sans lever pour un
        # échec métier. Le résultat de la TENTATIVE est `res.status`, pas
        # `document.status` : depuis INGEST-1C-R2 le document reste READY quand son
        # ancien index a été conservé. Même code HTTP (200) et mêmes clés que le
        # succès ; `ok=false` + `error_code`/`message` portent l'échec.
        if getattr(res, "status", None) == DocumentStatus.FAILED:
            _emit("action.failed", "failed", error_code="reindex_attempt_failed", chunk_count=chunks)
            return Response({
                "ok": False, "queued": False, "strategy": decision.effective,
                "requested_strategy": decision.requested,
                "error_code": "reindex_attempt_failed",
                "message": (doc.error_message or "")[:1000],
                "document": _serialize_document(doc, chunks_count=chunks),
            })

        _emit("action.completed", "completed", chunk_count=chunks)
        return Response({
            "ok": True, "queued": False, "strategy": decision.effective,
            "requested_strategy": decision.requested,
            "document": _serialize_document(doc, chunks_count=chunks),
        })


class DocumentIngestView(APIView):
    """
    POST /api/v1/documents/ingest/  (text ingest)
    body: { rag_space_id, filename?, text, async? }
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.rag_core.models.document import Document
        from apps.rag_core.services.ingest_document import ingest_document_from_text

        data = request.data or {}
        rag_space_id = (data.get("rag_space_id") or "").strip()
        text = data.get("text") or data.get("content") or ""
        filename = (data.get("filename") or data.get("name") or "text").strip()

        if not rag_space_id:
            return Response({"detail": "rag_space_id required"}, status=status.HTTP_400_BAD_REQUEST)
        if not text:
            return Response({"detail": "text required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rag_space_uuid = uuid.UUID(rag_space_id)
        except Exception:
            return Response({"detail": "invalid rag_space_id"}, status=status.HTTP_400_BAD_REQUEST)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_uuid, permission="write",
            endpoint="documents.ingest", request=request,
        )
        require_rag_space_access(request.user, rag_space_uuid, write=True)

        space = RagSpace.objects.filter(id=rag_space_uuid).first()
        if not space:
            return Response({"detail": "rag_space_not_found"}, status=status.HTTP_404_NOT_FOUND)

        # --- Quota documentaire (le texte ingéré compte comme 1 document) ---
        # Contenu déjà présent (dédup par sha) => aucune conso, aucun blocage.
        # Pré-vérification NON verrouillée sur ce chemin (l'ingestion texte
        # synchrone crée la ligne dans le pipeline, non enveloppable sous verrou) ;
        # le chemin d'upload de FICHIER utilise la réservation verrouillée.
        size_bytes = len(text.encode("utf-8"))
        sha = _sha256_text(text) if _sha256_text else ""
        is_new_content = not sha or find_existing_by_sha(space=space, sha256=sha) is None
        if is_new_content:
            try:
                quota_service.check_upload_allowed(
                    workspace=space.workspace, added_count=1, added_bytes=size_bytes,
                )
            except quota_service.QuotaExceeded as e:
                return Response(e.as_response(), status=status.HTTP_409_CONFLICT)

        # Décision unifiée (OCR forcé > body `async` > query/header > setting).
        # Neutralisée en eager : pas de broker/worker => sync explicite.
        use_async = _should_async(request, body_value=data.get("async")) and not is_celery_eager_mode()
        request_id = request.headers.get("x-request-id") or None

        if use_async:
            # Async : on pré-crée le document, on persiste l'original (storage_file)
            # pour que la réindexation Celery puisse relire le texte, puis on enfile.
            if _sha256_text is None:
                return Response({"detail": "sha256_helper_missing"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # INGEST-1E3 : réservation de quota ATOMIQUE à la création (même primitive
            # que l'upload fichier). La pré-vérification plus haut n'est qu'un échec
            # rapide ; la décision autoritative est ici, sous verrou.
            from apps.rag_core.services.ingest.pipeline import (
                create_document_with_quota_reservation,
            )

            try:
                res = create_document_with_quota_reservation(
                    space=space, filename=filename, content_type="text/plain",
                    size_bytes=size_bytes, sha256=sha,
                )
            except quota_service.QuotaExceeded as e:
                return Response(e.as_response(), status=status.HTTP_409_CONFLICT)
            doc: Document = res.document

            chunks_count = count_chunks_for_document(document_id=doc.id)
            if res.deduped and chunks_count > 0:
                return Response(
                    {"ok": True, "deduped": True, "document": _serialize_document(doc, chunks_count=chunks_count)}
                )
            in_progress = _duplicate_of_active_ingestion(res)
            if in_progress is not None:
                return in_progress

            try:
                storage_file = getattr(doc, "storage_file", None)
                if storage_file is not None and not getattr(storage_file, "name", ""):
                    doc.storage_file.save(filename or "raw_text.txt", ContentFile(text.encode("utf-8")), save=True)
            except Exception:
                logger.exception("storage_file save failed for async text ingest (doc=%s)", doc.id)

            failed = enqueue_async_or_503(request=request, doc=doc)
            if failed is not None:
                return failed
            body = {"ok": True, "queued": True, "document_id": str(doc.id), "deduped": res.deduped}
            if res.deduped and getattr(doc, "status", "") == DocumentStatus.FAILED:
                body["detail"] = FAILED_DOCUMENT_RETRIED
            return Response(body)

        # ---- SYNC : on délègue entièrement au pipeline (création doc + dédup + chunks) ----
        # async_mode=False est explicite : la vue a déjà tranché en sync, le pipeline
        # ne doit pas re-décider et ré-enfiler (l'OCR resterait prioritaire côté policy,
        # mais OCR=on => use_async=True => on ne passe jamais ici).
        try:
            result = ingest_document_from_text(
                rag_space_id=rag_space_uuid,
                filename=filename,
                text=text,
                content_type="text/plain",
                request_id=request_id,
                async_mode=False,
                actor_user_id=getattr(getattr(request, "user", None), "id", None),
            )
        except quota_service.QuotaExceeded as e:
            # INGEST-1E3 : le pipeline réserve le quota sous verrou à la création ; un
            # refus concurrent (deux requêtes passées par la pré-vérification) arrive
            # ici et garde le contrat HTTP quota existant (409, même payload).
            return Response(e.as_response(), status=status.HTTP_409_CONFLICT)
        except Exception as e:
            logger.exception("ingest from text failed: %s", e)
            return Response({"detail": "ingest_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        doc = Document.objects.filter(id=result.document_id).first()
        chunks_count = count_chunks_for_document(document_id=result.document_id)
        if result.deduped and chunks_count > 0:
            return Response({"ok": True, "deduped": True, "document": _serialize_document(doc, chunks_count=chunks_count)})
        return Response({"ok": True, "queued": False, "document": _serialize_document(doc, chunks_count=chunks_count)})


class DocumentUploadView(APIView):
    """
    POST /api/v1/documents/upload/?rag_space_id=<uuid>&async=true|false
    multipart/form-data with file=<file>
    """

    permission_classes = [IsAuthenticated]
    #: `True` sur `/documents/upload-audio/` : chemin réservé à l'AUDIO (plafond 600 Mo
    #: côté proxy). Tout autre format y est refusé : ce chemin ne doit jamais servir à
    #: contourner la limite générale des autres documents.
    audio_only = False

    def post(self, request):
        # rag_space_id accepté en query param OU dans le corps multipart/form-data
        # (le front l'envoie dans le formulaire ; on reste tolérant aux deux).
        rag_space_id = (
            request.query_params.get("rag_space_id")
            or (request.data.get("rag_space_id") if hasattr(request, "data") else None)
            or ""
        ).strip()
        if not rag_space_id:
            return Response({"detail": "rag_space_id required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rag_space_uuid = uuid.UUID(rag_space_id)
        except Exception:
            return Response({"detail": "invalid rag_space_id"}, status=status.HTTP_400_BAD_REQUEST)

        from django.core.exceptions import PermissionDenied as _PermDenied

        from apps.audit.action_audit import emit_action_event

        _rid = request.headers.get("x-request-id") or ""
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_uuid, permission="write",
            endpoint="documents.upload", request=request,
        )
        try:
            require_rag_space_access(request.user, rag_space_uuid, write=True)
        except _PermDenied:
            emit_action_event(
                event_name="action.denied", action_name="document.upload", status="denied",
                actor=request.user, rag_space_id=rag_space_uuid, resource_type="document",
                request_id=_rid, error_code="permissiondenied",
            )
            raise

        space = RagSpace.objects.filter(id=rag_space_uuid).first()
        if not space:
            return Response({"detail": "rag_space_not_found"}, status=status.HTTP_404_NOT_FOUND)

        upload_file = request.FILES.get("file")
        if not upload_file:
            return Response({"detail": "file required"}, status=status.HTTP_400_BAD_REQUEST)

        _ws_id = str(getattr(space, "workspace_id", "") or "")
        emit_action_event(
            event_name="action.started", action_name="document.upload", status="started",
            actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
            resource_type="document", request_id=_rid,
        )

        # --- Métadonnées d'import de DOSSIER (optionnelles) : validées AVANT tout
        # traitement. Purement métadonnées (Document.meta) : jamais utilisées pour
        # construire un chemin disque. Rejet strict des chemins dangereux. ---
        try:
            source_folder, relative_path = normalize_import_metadata(
                source_folder=request.data.get("source_folder"),
                relative_path=request.data.get("relative_path"),
            )
        except UnsafeImportPath as e:
            return Response({"detail": e.code, "error": e.detail}, status=status.HTTP_400_BAD_REQUEST)

        # --- AUDIO : reconnu sur l'EN-TÊTE (extension ET signature du
        # conteneur), jamais lu en entier. Plafond PROPRE à l'audio ; les autres formats
        # gardent la limite générale.
        from apps.rag_core.services.ingest.audio import formats as audio_formats

        filename = getattr(upload_file, "name", None) or "upload"
        head = upload_file.read(audio_formats.HEAD_BYTES)
        upload_file.seek(0)
        audio_fmt = audio_formats.sniff_audio(head, filename)
        if audio_fmt is None and (self.audio_only or audio_formats.is_audio_filename(filename)):
            # Diagnostic NON sensible (structure de l'en-tête, jamais le nom ni le contenu) :
            # permet d'identifier un conteneur légitime encore inconnu.
            import logging

            logging.getLogger("rag.audio.upload").warning(
                "audio_upload_refused suffix=%s signature=%s",
                audio_formats.audio_suffix(filename) or "none", audio_formats.describe_signature(head),
            )
            return Response(
                {"detail": "unsupported_audio",
                 "error": "Fichier audio non reconnu : formats acceptés MP3, M4A, WAV, FLAC, OGG, WEBM (audio seul)."},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        if audio_fmt is not None:
            from apps.rag_core.services.ingest.extraction import config as _xcfg
            from apps.rag_core.services.ingest.extraction.validation import check_audio_admission

            audio_limit = _xcfg.audio_max_upload_bytes()
            if int(getattr(upload_file, "size", None) or 0) > audio_limit:
                return Response(
                    {"detail": "audio_file_too_large",
                     "error": f"Fichier audio trop volumineux (limite : {audio_limit // 1_000_000} Mo).",
                     "max_bytes": audio_limit},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
            try:
                check_audio_admission(space.id)
            except RejectedFile as e:
                return Response({"detail": e.code, "error": e.detail}, status=status.HTTP_400_BAD_REQUEST)
        else:
            # --- Limite applicative: taille du fichier (AVANT toute lecture/traitement) ---
            try:
                check_file_size(int(getattr(upload_file, "size", None) or 0))
            except UploadLimitExceeded as e:
                return Response(
                    {"detail": e.code, "error": e.detail},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

        if _sha256_bytes is None:
            return Response({"detail": "sha256_helper_missing"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if audio_fmt is not None:
            # Empreinte PAR BLOCS : 600 Mo ne passent jamais en mémoire.
            import hashlib as _hashlib

            _h = _hashlib.sha256()
            _n = 0
            for _block in upload_file.chunks(1 << 20):
                _h.update(_block)
                _n += len(_block)
            upload_file.seek(0)
            if _n == 0:
                return Response({"detail": "empty_file", "error": "Fichier vide."}, status=status.HTTP_400_BAD_REQUEST)
            sha = _h.hexdigest()
            content = b""
            content_type = audio_formats.AUDIO_MIMES[audio_fmt]
            size_bytes = _n
        else:
            content = upload_file.read()
            sha = _sha256_bytes(content)  # type: ignore[misc]
            upload_file.seek(0)
            content_type = getattr(upload_file, "content_type", None) or mimetype_or_octet(filename)
            size_bytes = int(getattr(upload_file, "size", None) or len(content))

        # --- Limite applicative: nombre de pages PDF (AVANT extraction/embeddings) ---
        is_pdf = audio_fmt is None and (filename.lower().endswith(".pdf") or (content_type or "") == "application/pdf")
        if is_pdf:
            try:
                check_pdf_pages(content)
            except UploadLimitExceeded as e:
                return Response(
                    {"detail": e.code, "error": e.detail},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # --- Validation pré-extraction : type RÉEL (allowlist) + refus explicites ---
        # On ne crée pas de Document pour un fichier d'emblée refusé (415/400).
        detected_format = "audio" if audio_fmt is not None else None
        try:
            if audio_fmt is not None:
                raise _AudioValidated()
            _, detected_format = validate_and_classify(
                content,
                ExtractionContext(filename=filename, declared_content_type=content_type),
            )
            # Courriels (.msg, .eml) : admis seulement dans un espace autorisé
            # (DOCUMENT_EMAIL_RAGSPACE_IDS). Sans effet sur les autres formats.
            check_email_admission(detected_format, space.id)
        except _AudioValidated:
            pass  # signature, plafond et admission déjà contrôlés ci-dessus
        except UnsupportedFormat as e:
            return Response(
                {"detail": e.code, "error": e.detail},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        except RejectedFile as e:
            http = (
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                if e.code == "file_too_large"
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": e.code, "error": e.detail}, status=http)

        # --- Quota documentaire du workspace (AVANT création définitive) ---
        # Un doublon de contenu (dédup par sha) n'ajoute AUCUN octet ni fichier :
        # il ne consomme pas de quota. Sinon, la création est faite SOUS VERROU
        # (select_for_update sur la ligne WorkspaceQuota) pour sérialiser les
        # uploads concurrents du même workspace — la ligne créée = la réservation.
        existing = find_existing_by_sha(space=space, sha256=sha)
        if existing is not None:
            res = CreateOrGetResult(document=existing, deduped=True)
        else:
            def _create_doc():
                return create_or_get_by_sha(
                    space=space,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    sha256=sha,
                    status=DocumentStatus.PENDING,
                )

            try:
                res = quota_service.check_and_reserve(
                    workspace=space.workspace,
                    added_count=1,
                    added_bytes=size_bytes,
                    create_fn=_create_doc,
                )
            except quota_service.QuotaExceeded as e:
                emit_action_event(
                    event_name="action.denied", action_name="document.upload", status="denied",
                    actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
                    resource_type="document", request_id=_rid, error_code="workspace_quota_exceeded",
                )
                return Response(e.as_response(), status=status.HTTP_409_CONFLICT)
        doc = res.document

        # Provenance d'import de dossier : persistée en meta.import UNIQUEMENT sur un
        # document neuf (un doublon de contenu conserve sa provenance d'origine —
        # dédup par sha inchangée). L'ingestion async fusionne ensuite meta.extraction
        # sans écraser cette clé (cf. _merge_doc_meta).
        if not res.deduped and audio_fmt is not None:
            try:
                doc.meta = {**(doc.meta or {}), "audio": {"format": audio_fmt, "stage": "queued"}}
                doc.save(update_fields=["meta"])
            except Exception:
                logger.debug("could not persist audio meta (doc=%s)", doc.id)

        if not res.deduped and (source_folder or relative_path):
            try:
                doc.meta = {
                    **(doc.meta or {}),
                    "import": {"source_folder": source_folder, "relative_path": relative_path},
                }
                doc.save(update_fields=["meta"])
            except Exception:
                logger.debug("could not persist import meta (doc=%s)", doc.id)

        request_id = request.headers.get("x-request-id") or ""
        resp = _ensure_storage_file_or_fail(request=request, doc=doc, upload_file=upload_file, request_id=request_id,
                                            stream=audio_fmt is not None)
        if resp is not None:
            emit_action_event(
                event_name="action.failed", action_name="document.upload", status="failed",
                actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
                resource_type="document", resource_id=doc.id, request_id=_rid,
                error_code="storage_failed",
            )
            return resp

        # Fichier + modèle créés et stockés (≠ ingestion terminée : cf. document.ingestion).
        # Contenu identique déjà présent sur une ligne FAILED : la ligne est RÉUTILISÉE
        # comme cible de reprise (même document_id, aucune ligne créée, provenance
        # d'origine conservée) et ré-enfilée ci-dessous. Tracé dans l'audit de l'upload.
        reused_failed = bool(res.deduped and getattr(doc, "status", "") == DocumentStatus.FAILED)
        if reused_failed:
            logger.info(
                "upload_failed_document_reused document_id=%s rag_space_id=%s request_id=%s",
                doc.id, rag_space_uuid, _rid,
            )
        emit_action_event(
            event_name="action.completed", action_name="document.upload", status="completed",
            actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
            resource_type="document", resource_id=doc.id, request_id=_rid,
            metadata={
                "mime_type": content_type, "size_bytes": size_bytes, "deduped": bool(res.deduped),
                **({"reuse": FAILED_DOCUMENT_RETRIED} if reused_failed else {}),
            },
        )

        chunks_count = count_chunks_for_document(document_id=doc.id)
        if res.deduped and chunks_count > 0:
            return Response({"ok": True, "deduped": True, "document": _serialize_document(doc, chunks_count=chunks_count)})

        # En eager (CELERY_TASK_ALWAYS_EAGER=1) : pas de broker/worker, on exécute
        # en sync explicite (queued:false + statut réel) plutôt que feindre un enqueue.
        # Excel force l'async (comme l'OCR) : un classeur ne doit jamais être extrait
        # dans le cycle d'une requête HTTP (E1 : 500 k cellules ≈ 7 s).
        use_async = _should_async(request, format_key=detected_format) and not is_celery_eager_mode()

        if use_async:
            # Garde anti-ré-enfilement : ne concerne que la branche qui ENFILE. Le chemin
            # synchrone (eager, `?async=false`) garde son contrat historique.
            in_progress = _duplicate_of_active_ingestion(res)
            if in_progress is not None:
                return in_progress
            failed = enqueue_async_or_503(request=request, doc=doc)
            if failed is not None:
                emit_action_event(
                    event_name="action.failed", action_name="document.upload", status="failed",
                    actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
                    resource_type="document", resource_id=doc.id, request_id=_rid,
                    error_code="enqueue_unavailable",
                )
                return failed
            emit_action_event(
                event_name="action.queued", action_name="document.upload", status="queued",
                actor=request.user, workspace_id=_ws_id, rag_space_id=rag_space_uuid,
                resource_type="document", resource_id=doc.id, request_id=_rid,
                metadata={"operation_mode": "async_ingestion",
                          **({"reuse": FAILED_DOCUMENT_RETRIED} if reused_failed else {})},
            )
            body = {"ok": True, "queued": True, "document_id": str(doc.id), "deduped": res.deduped}
            if reused_failed:
                body["detail"] = FAILED_DOCUMENT_RETRIED
            return Response(body)

        # SYNC : le storage_file vient d'être persisté ci-dessus, on réindexe le doc existant
        # (réutilise la pipeline d'extraction TXT/PDF + chunking + embeddings).
        if reindex_existing_document is None:
            return Response({"detail": "ingest_not_supported"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        from apps.rag_core.services.ingest.audio.orchestrator import AudioTranscriptionDeferred

        try:
            reindex_existing_document(document_id=str(doc.id))
        except AudioTranscriptionDeferred:
            # Audio : la transcription est planifiée (chaîne ASR asynchrone) ; jamais un échec.
            doc.refresh_from_db()
            return Response({"ok": True, "queued": True, "document_id": str(doc.id), "deduped": res.deduped,
                             "document": _serialize_document(doc, chunks_count=0)})
        except Exception as e:
            logger.exception("ingest from upload failed: %s", e)
            return Response({"detail": "ingest_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        doc.refresh_from_db()
        chunks_count = count_chunks_for_document(document_id=doc.id)
        return Response({"ok": True, "queued": False, "document": _serialize_document(doc, chunks_count=chunks_count)})


class _AudioValidated(Exception):
    """Court-circuit interne : un audio ne passe pas par `validate_and_classify` (qui
    exigerait ses octets complets) — sa validation propre est déjà faite."""


class DocumentAudioUploadView(DocumentUploadView):
    """
    POST /api/v1/documents/upload-audio/?rag_space_id=<uuid>
    multipart/form-data with file=<audio>

    Chemin RÉSERVÉ à l'audio : le proxy frontal lui autorise un corps
    de DOCUMENT_AUDIO_MAX_UPLOAD_BYTES ; tout autre format y est refusé (415).
    """

    audio_only = True


def summary_user_label(doc, scope: str, page) -> str:
    """Libellé de la demande, écrit par le SERVEUR.

    Le client n'a aucune autorité sur ce texte : il est persisté dans le fil et
    doit refléter le document réellement résumé, pas ce que le frontend affiche.
    """
    name = doc.filename or "document"
    if scope == "page" and page is not None:
        return f"Résume la page {page} du document **{name}**"
    return f"Résume le document **{name}**"


def _serialize_summary(doc, res, *, message_id=None, request_id=None) -> dict:
    return {
        "ok": True,
        "document_id": str(doc.id),
        "filename": doc.filename,
        "status": res.status,
        # Présents seulement pour le parcours chat : ils permettent au frontend de
        # réconcilier son message optimiste avec la ligne persistée, par identité
        # et non par position dans le tableau.
        "message_id": str(message_id) if message_id else None,
        "request_id": request_id or None,
        "summary_id": res.summary_id,
        "summary": res.summary,
        "error": res.error,
        "mode": res.mode,
        "scope": res.scope,
        "page": res.page,
        "language": res.language,
        "truncated": res.truncated,
        "provider": res.provider,
        "model": res.model,
        "tokens": res.tokens,
        "prompt_tokens": res.prompt_tokens,
        "completion_tokens": res.completion_tokens,
        "cost_usd": res.cost_usd,
        "cached": res.cached,
        "generated_at": res.generated_at.isoformat() if res.generated_at else None,
        "log_id": res.log_id,
    }


class DocumentSummaryView(APIView):
    """
    POST /api/v1/documents/<document_id>/summary/
        body (optionnel): { mode, scope, page, language, refresh }
        Génère/réutilise le résumé. Documents longs → map-reduce async :
        réponse 202 ``status="pending"`` (poller via GET).
    GET  /api/v1/documents/<document_id>/summary/?mode=&scope=&page=&language=
        Lecture seule de l'état (cache/polling). 200 ``status="none"`` si absent.

    Lecture seule (write=False) : les viewers peuvent résumer ; anti-énumération
    et isolation workspace gérées par ``load_accessible_document``.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, document_id):
        from apps.chat.models import ChatMessage
        from apps.chat.services.summary_messages import (
            SOURCE_TYPE_WORKSPACE,
            SummaryRequestConflict,
            build_summary_meta,
            fail_message,
            get_or_create_summary_exchange,
            reopen_message,
            resolve_thread_for_summary,
            settle_for_result,
        )
        from apps.rag_core.services.summary_service import (
            SummaryError,
            get_cached_summary,
            resolve_summary_language,
            summarize_document,
        )

        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        # write=False : viewers autorisés ; 404 anti-énum si pas d'accès lecture.
        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        data = request.data or {}
        mode = (data.get("mode") or "bullets")
        scope = (data.get("scope") or "document")
        page = data.get("page")
        # `auto` (et non `fr`) quand le client ne précise rien : la langue est alors
        # résolue d'après les préférences du compte. Un `or "fr"` transformait
        # « non précisé » en CHOIX EXPLICITE du français et neutralisait `auto`,
        # y compris pour les clients anciens qui n'envoient aucune langue.
        language = (data.get("language") or "auto")
        refresh = _boolish(data.get("refresh"))
        thread_id = data.get("thread_id") or None
        request_id = (data.get("request_id") or "").strip()

        # --- Parcours chat (optionnel) : persistance du résumé dans le fil -----
        # Les deux champs sont facultatifs pour ne pas casser les appels existants
        # (modale Documents, scripts), mais indissociables : accepter un thread_id
        # sans request_id créerait un nouveau message à chaque retry réseau.
        # Langue CONCRÈTE du résumé, résolue une fois. Les textes déterministes
        # affichés avant/à la place du résumé (« en cours », « échec ») doivent
        # parler la même langue que lui, sinon le fil mélange français et anglais.
        answer_language = resolve_summary_language(language, user=request.user)

        msg = None
        if thread_id:
            if not request_id:
                return Response(
                    {"detail": "request_id_required",
                     "error": "request_id est obligatoire lorsque thread_id est fourni."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            thread = resolve_thread_for_summary(user=request.user, thread_id=thread_id, document=doc)
            if thread is None:
                # Inexistant, étranger ou d'un autre espace : réponse indistinguable.
                return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)

            # Provenance EXPLICITE, même sur le chemin privé : depuis l'arrivée du
            # corpus métier, un `document_id` seul ne suffit plus à désigner une
            # cible. La poser des deux côtés évite que « pas de provenance » ait
            # deux sens (message ancien / message de corpus mal écrit).
            source_ref = {
                "source_type": SOURCE_TYPE_WORKSPACE,
                "document_id": str(doc.id),
                "filename": doc.filename,
                "page_number": page if scope == "page" else None,
            }
            try:
                msg, created = get_or_create_summary_exchange(
                    thread=thread,
                    request_id=request_id,
                    document=doc,
                    user_label=summary_user_label(doc, scope, page),
                    answer_language=answer_language,
                    source=source_ref,
                    meta=build_summary_meta(
                        source=source_ref, scope=scope, mode=mode, language=answer_language
                    ),
                )
            except SummaryRequestConflict:
                return Response(
                    {"detail": "request_id_conflict",
                     "error": "Ce request_id est déjà utilisé pour une autre cible dans ce fil."},
                    status=status.HTTP_409_CONFLICT,
                )
            if not created and msg.status == ChatMessage.STATUS_COMPLETED and not refresh:
                # Rejeu strict : la demande a déjà abouti. On rend l'état persisté
                # tel quel, sans relancer la moindre génération. ``refresh=true``
                # est une demande explicite de régénération : elle passe outre et
                # réécrit le même message.
                cached = get_cached_summary(
                    document=doc, mode=mode, scope=scope, page=page, language=language,
                    user=request.user,
                )
                if cached is not None:
                    return Response(
                        _serialize_summary(doc, cached, message_id=msg.id, request_id=request_id),
                        status=status.HTTP_200_OK,
                    )
            # Relance après échec : on ré-ouvre le message existant au lieu d'en
            # créer un second. Le client peut ainsi garder son request_id d'un
            # essai à l'autre sans jamais produire de résumé en double.
            if msg.status == ChatMessage.STATUS_FAILED:
                reopen_message(msg, answer_language=answer_language)

        try:
            res = summarize_document(
                user=request.user, document=doc, mode=mode, scope=scope, page=page,
                language=language, refresh=refresh,
            )
        except SummaryError as e:
            if msg is not None:
                fail_message(msg, error=e.code, answer_language=answer_language)
            return Response({"detail": e.code, "error": str(e)}, status=e.http_status)
        except Exception as e:
            logger.exception("document summary failed (doc=%s): %s", doc.id, e)
            if msg is not None:
                fail_message(msg, error="summary_failed", answer_language=answer_language)
            return Response({"detail": "summary_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if msg is not None:
            msg = settle_for_result(msg, res)

        http = status.HTTP_202_ACCEPTED if res.status == "pending" else status.HTTP_200_OK
        return Response(
            _serialize_summary(
                doc, res,
                message_id=(msg.id if msg is not None else None),
                request_id=(request_id or None),
            ),
            status=http,
        )

    def get(self, request, document_id):
        from apps.rag_core.services.summary_service import get_cached_summary

        doc_uuid = _coerce_uuid(document_id)
        if not doc_uuid:
            return Response({"detail": "invalid document_id"}, status=status.HTTP_400_BAD_REQUEST)

        doc = load_accessible_document(request.user, doc_uuid, write=False, request=request)

        q = request.query_params
        res = get_cached_summary(
            document=doc,
            mode=q.get("mode") or "bullets",
            scope=q.get("scope") or "document",
            page=q.get("page"),
            # Mêmes défaut et mêmes entrées de résolution que le POST ci-dessus :
            # le polling DOIT viser exactement la variante de cache créée par la
            # création, sinon il ne la trouve jamais.
            language=q.get("language") or "auto",
            user=request.user,
        )
        if res is None:
            return Response({"ok": True, "document_id": str(doc.id), "status": "none"})
        return Response(_serialize_summary(doc, res))


def mimetype_or_octet(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"

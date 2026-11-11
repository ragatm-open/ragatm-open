# backend/apps/rag_core/services/ingest/pipeline.py
from __future__ import annotations

from dataclasses import replace

from datetime import date
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from apps.rag_core.models import RagSpace, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.repositories.document_repository import (
    CreateOrGetResult,
    count_chunks_for_document,
    create_or_get_by_sha,
    find_existing_by_sha,
)

from .extraction import ExtractionContext, validate_and_classify
from .extractors import extract_text_from_txt_bytes, extract_text_pages_from_pdf_bytes
from .types import IngestResult, IngestError
from .utils import resolve_chunk_budget, sha256_bytes, settings_int

# NEW: date extraction from content (implemented in ingest/utils.py)
try:
    from .utils import extract_document_date_from_pages, extract_document_date_from_text  # type: ignore
except Exception:  # pragma: no cover
    extract_document_date_from_pages = None  # type: ignore
    extract_document_date_from_text = None  # type: ignore


def _async_ingest_enabled(explicit=None, format_key=None) -> bool:
    """
    Décision sync/async **centralisée** dans ``ingest.async_policy``.

    ``explicit`` permet à l'appelant (vues) d'imposer le mode déjà décidé côté
    HTTP (``?async=``/header/body), tout en conservant l'invariant "OCR => async".
    ``format_key`` est le format RÉEL rendu par la classification canonique
    (``validate_and_classify``) : c'est lui qui porte les forçages Excel / PPTX
    (INGEST-1E2 — même politique que l'upload HTTP, quel que soit le point d'entrée).
    Sans override, on retombe sur ``RAG_INGEST_ASYNC_DEFAULT``.
    """
    from .async_policy import resolve_async

    return resolve_async(explicit=explicit, format_key=format_key)


def _ensure_storage_file_or_fail(*, doc: Document, filename: str, raw_bytes: bytes, async_mode: bool) -> bool:
    """
    Ensure doc.storage_file contains the original bytes.
    Returns True if the durable source is persisted, False if the attempt must fail.

    FAIL-CLOSED (INGEST-1E1) : la persistance de l'original est un prérequis de
    l'ingestion, pas une option. Si ``save()`` lève, le document est marqué FAILED
    et l'appelant NE DOIT PAS poursuivre (ni extraction, ni embeddings, ni READY) —
    un document READY sans source ne serait jamais réindexable. Le comportement
    est désormais identique en synchrone et en asynchrone ; ``async_mode`` est
    conservé pour la stabilité de la signature interne et ne pèse plus sur la décision.
    """
    if hasattr(doc, "storage_file") and (
        not getattr(doc, "storage_file", None) or not getattr(doc.storage_file, "name", "")
    ):
        try:
            doc.storage_file.save(filename or "document.bin", ContentFile(raw_bytes or b""), save=True)
            return True
        except Exception as e:
            Document.objects.filter(id=doc.id).update(
                status=DocumentStatus.FAILED,
                error_message=f"storage_file_save_failed: {e}"[:1000],
            )
            return False
    return True


def create_document_with_quota_reservation(
    *, space: RagSpace, filename: str, content_type: str, size_bytes: int, sha256: str,
) -> CreateOrGetResult:
    """Dédup, puis réservation de quota ET création du Document dans une même
    transaction COURTE (INGEST-1E3 — « reserve where you create »).

    Même primitive et même ordre que l'upload HTTP (`DocumentUploadView`) :
    `find_existing_by_sha` d'abord (un doublon ne consomme rien), sinon
    `quota.check_and_reserve` verrouille `WorkspaceQuota`, relit l'usage sous verrou et
    exécute `create_or_get_by_sha` — la ligne créée EST la réservation. Le verrou est
    relâché au commit de cette transaction, AVANT tout stockage, extraction ou
    embeddings : cette fonction doit être appelée hors de tout bloc atomique englobant.

    Lève `quota.QuotaExceeded` sans créer de ligne. Deux requêtes portant le même
    contenu : la seconde retombe sur l'index unique partiel dans `create_or_get_by_sha`
    et rend le document existant (`deduped=True`) — une seule ligne, un seul quota.
    """
    from apps.workspaces.services import quota as quota_service

    existing = find_existing_by_sha(space=space, sha256=sha256)
    if existing is not None:
        return CreateOrGetResult(document=existing, deduped=True)

    def _create():
        return create_or_get_by_sha(
            space=space,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            status=DocumentStatus.PENDING,
        )

    return quota_service.check_and_reserve(
        workspace=space.workspace, added_count=1, added_bytes=int(size_bytes or 0), create_fn=_create,
    )


def _enqueue_after_commit(*, document_id: str, request_id: str | None, actor_user_id: int | None = None,
                          filename: str | None = None):
    def _enqueue():
        from apps.rag_core.services.ingest.queues import ingest_queue_for
        from apps.rag_core.tasks import ingest_document_task  # lazy import

        # access_context = identifiants seulement ; la tâche recharge les droits.
        task_kwargs = dict(
            document_id=document_id, request_id=request_id,
            access_context={"user_id": actor_user_id, "request_id": request_id},
        )
        from apps.rag_core.services.ingest.enqueue_token import stamp_enqueue_token

        task_kwargs["enqueue_token"] = stamp_enqueue_token(document_id)
        queue = ingest_queue_for(filename=filename)
        if queue:
            ingest_document_task.apply_async(kwargs=task_kwargs, queue=queue)
        else:
            ingest_document_task.delay(**task_kwargs)

    transaction.on_commit(_enqueue)


def _model_has_field(model: Any, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _update_document_date_best_effort(*, doc: Document, doc_date: date | None) -> None:
    """
    Store document date extracted from content.
    Best-effort: supports different schema variants:
    - Document.document_date (preferred)
    - Document.meta JSONField
    - Document.metadata JSONField
    Never raises.
    """
    if not doc_date:
        return

    iso = doc_date.isoformat()

    try:
        # 1) Dedicated field
        if _model_has_field(Document, "document_date"):
            Document.objects.filter(id=doc.id).update(document_date=doc_date)  # type: ignore[arg-type]
            return

        # 2) meta / metadata JSONField
        for json_field in ("meta", "metadata"):
            if _model_has_field(Document, json_field):
                current = getattr(doc, json_field, None)
                if not isinstance(current, dict):
                    current = {}
                current.setdefault("document_date", iso)
                Document.objects.filter(id=doc.id).update(**{json_field: current})
                return

    except Exception:
        return


def ingest_document_from_text(
    *,
    rag_space_id,
    filename: str,
    text: str,
    content_type: str = "text/plain",
    request_id: str | None = None,
    async_mode: bool | None = None,
    actor_user_id: int | None = None,
) -> IngestResult:
    """
    Sync (default) or async (settings.RAG_INGEST_ASYNC_DEFAULT=True).
    Stores original in Document.storage_file to support reindex.

    ``async_mode`` (optionnel) impose la décision déjà prise côté HTTP ; l'OCR
    force l'async quoi qu'il arrive (cf. ingest.async_policy).
    """
    space = RagSpace.objects.get(id=rag_space_id)

    if text is None or not str(text).strip():
        doc = Document.objects.create(
            rag_space=space,
            filename=filename or "raw_text.txt",
            content_type=content_type or "text/plain",
            size_bytes=0,
            sha256="",
            status=DocumentStatus.FAILED,
            error_message="empty_text",
        )
        # best-effort: store empty original too
        if hasattr(doc, "storage_file") and (
            not getattr(doc, "storage_file", None) or not getattr(doc.storage_file, "name", "")
        ):
            try:
                doc.storage_file.save(doc.filename or "raw_text.txt", ContentFile(b""), save=True)
            except Exception as e:
                Document.objects.filter(id=doc.id).update(error_message=f"empty_text; storage_file_save_failed: {e}")
        return IngestResult(document_id=str(doc.id), status=doc.status, chunks_created=0, deduped=False)

    raw = str(text).encode("utf-8")
    sha = sha256_bytes(raw)
    async_mode = _async_ingest_enabled(explicit=async_mode)

    # ✅ extract date from content early (best-effort)
    doc_date: date | None = None
    try:
        if extract_document_date_from_text is not None:
            doc_date = extract_document_date_from_text(text)
    except Exception:
        doc_date = None

    # Dédup + réservation de quota + création (INGEST-1E3) : transaction courte et
    # autonome, AVANT le bloc atomique de stockage/enqueue ci-dessous, pour que le
    # verrou WorkspaceQuota ne couvre jamais le stockage ni l'indexation.
    created = create_document_with_quota_reservation(
        space=space,
        filename=filename,
        content_type=content_type or "text/plain",
        size_bytes=len(raw),
        sha256=sha,
    )
    doc = created.document
    # Contenu déjà présent sur une ligne FAILED : cette ligne est la cible de REPRISE
    # (même document_id, aucune ligne créée). Un contenu déjà ACTIF est rendu tel quel.
    retried = bool(created.deduped and doc.status == DocumentStatus.FAILED)

    if created.deduped and not retried:
        return IngestResult(
            document_id=str(doc.id),
            status=doc.status,
            chunks_created=count_chunks_for_document(document=doc) if doc.status == DocumentStatus.READY else 0,
            deduped=True,
        )

    with transaction.atomic():
        # Source durable OBLIGATOIRE, en sync comme en async (INGEST-1E1) : sans
        # elle, aucune réindexation future n'est possible. L'échec est relevé ici et
        # traité APRÈS le bloc atomique, pour que la ligne FAILED reste commitée
        # (audit, quota) au lieu d'être annulée par un rollback.
        ok = _ensure_storage_file_or_fail(
            doc=doc,
            filename=filename or "raw_text.txt",
            raw_bytes=raw,
            async_mode=async_mode,
        )
        storage_failed = not ok
        if ok:
            # ✅ persist extracted date as soon as possible
            _update_document_date_best_effort(doc=doc, doc_date=doc_date)

            if async_mode:
                _enqueue_after_commit(document_id=str(doc.id), request_id=request_id, actor_user_id=actor_user_id,
                                      filename=getattr(doc, "filename", None))
                return IngestResult(document_id=str(doc.id), status=DocumentStatus.PENDING, chunks_created=0,
                                    deduped=retried, retried=retried)

    if storage_failed:
        # La tentative s'arrête AVANT toute extraction / embeddings / publication.
        failed = IngestResult(document_id=str(doc.id), status=DocumentStatus.FAILED, chunks_created=0, deduped=False)
        if async_mode:
            return failed
        # Contrat d'erreur du chemin synchrone (INGEST-1B) : l'échec terminal est
        # propagé à l'appelant, jamais rendu comme un succès apparent.
        error_message = (
            Document.objects.filter(id=doc.id).values_list("error_message", flat=True).first()
            or "storage_file_save_failed"
        )
        raise IngestError(error_message)

    # ---- SYNC MODE : indexation par le cœur commun (INGEST-1B) ----
    # Le texte brut est présenté au moteur moderne comme un `ExtractionResult`
    # canonique, puis indexé par `index_extraction_result` — exactement ce que reçoit
    # un fichier après extraction. Il en hérite : lots d'embeddings bornés par
    # `RAG_EMBED_BATCH_SIZE`, identité de pipeline (`pipeline_id`/`pipeline_family`)
    # sur chaque Chunk, refus d'un document dont l'identité change entre deux lots,
    # verrou et revalidation de l'espace avant écriture.
    #
    # Le rail historique de ce chemin (chunker -> `embed_texts` en UN appel ->
    # `Chunk` sans identité) est supprimé : il produisait des documents READY
    # invisibles à la recherche vectorielle gouvernée, et échouait entièrement dès
    # que le nombre de fragments dépassait la borne d'un appel Gateway.
    #
    # Le découpage est inchangé : `index_extraction_result` applique le même
    # `chunk_text_deterministic` au texte tel quel (sans pagination, offset 0), avec le
    # même budget `resolve_chunk_budget`. Le texte n'est ni normalisé ni tronqué ici.
    from .extraction import ExtractionResult
    from .indexing import index_extraction_result

    # Budget de chunk EFFECTIF (lot 3) : réduit pour un espace GOUVERNÉ, dont le
    # modèle refuse les textes au-delà de sa fenêtre. Inchangé sinon.
    chunk_size, overlap = resolve_chunk_budget(space)
    dim = int(getattr(settings, "RAG_EMBEDDING_DIM", 768))

    # Même règle que l'upload : indexer la ligne rendue par la création/dédup.
    doc = Document.objects.filter(id=doc.id).first()
    if not doc:
        raise IngestError("document_create_failed")

    result = ExtractionResult(text=str(text)).finalize(
        fmt="txt", mime=(content_type or "text/plain"), method="text",
    )
    res = index_extraction_result(
        doc=doc, space=space, result=result, filename=filename,
        chunk_size=chunk_size, overlap=overlap, dim=dim,
    )
    if res.status != DocumentStatus.READY:
        # Contrat d'erreur HISTORIQUE de ce chemin, conservé : l'échec est propagé à
        # l'appelant (le document est déjà FAILED, avec son code d'erreur en
        # `meta.extraction`). La façon de surfacer un résultat FAILED sans exception
        # relève du contrat résultat/événement (INGEST-1D), pas de ce lot.
        error_message = (
            Document.objects.filter(id=doc.id).values_list("error_message", flat=True).first()
            or "ingest_failed"
        )
        raise IngestError(error_message)
    return replace(res, deduped=True, retried=True) if retried else res


def ingest_document_from_upload(
    *,
    rag_space_id,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    request_id: str | None = None,
    async_mode: bool | None = None,
    actor_user_id: int | None = None,
) -> IngestResult:
    """
    Ingestion multi-format (TXT/PDF/MD/DOCX/CSV/HTML/XLSX/PPTX/images…).

    Ordre (INGEST-1E1/1E2/1E3) :
      1. classification canonique ``validate_and_classify`` (même primitive et même
         contrat de refus que l'upload HTTP : un fichier refusé lève
         ``RejectedFile``/``UnsupportedFormat`` et ne crée NI Document, NI réservation
         de quota, NI stockage, NI enqueue) ;
      2. décision sync/async ``resolve_async(explicit, format_key)`` — un format forcé
         (Excel, PPTX, OCR) reste asynchrone même avec ``async_mode=False`` ;
      3. dédup + réservation de quota + création (transaction courte) ;
      4. persistance de l'original (fail-closed) ;
      5. enqueue (async) ou extraction/indexation inline (sync).

    Stores original in Document.storage_file.
    De-dup by sha256 within same rag_space (ignore FAILED docs).
    Sync (default) or async (settings.RAG_INGEST_ASYNC_DEFAULT=True).

    ``async_mode`` (optionnel) impose la décision déjà prise côté HTTP ; l'OCR,
    Excel et PPTX forcent l'async quoi qu'il arrive (cf. ingest.async_policy).
    """
    space = RagSpace.objects.get(id=rag_space_id)
    lower = (filename or "").lower()
    file_bytes = file_bytes or b""

    sha = sha256_bytes(file_bytes)

    # Classification canonique AVANT la décision async et AVANT toute création
    # (INGEST-1E2) : la politique de format ne peut s'appliquer que si le pipeline
    # connaît le format RÉEL. Hors de tout verrou (quota) et sans extraction :
    # taille, MIME par contenu, portes de format et refus explicites seulement.
    _, format_key = validate_and_classify(
        file_bytes,
        ExtractionContext(filename=filename or "", declared_content_type=content_type or ""),
    )
    # Courriels : porte d'ESPACE, même règle que l'upload HTTP.
    from .extraction.validation import check_email_admission

    check_email_admission(format_key, space.id)
    async_mode = _async_ingest_enabled(explicit=async_mode, format_key=format_key)
    if async_mode and bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        # Même règle d'ENVIRONNEMENT que la couche HTTP (views_common.is_celery_eager_mode) :
        # en eager il n'existe ni broker ni worker, « enfiler » n'a aucun sens — le
        # document est traité en synchrone explicite plutôt que confié à un
        # ``on_commit`` qui ne représente aucun worker. La décision de FORMAT reste
        # celle de ``resolve_async`` ci-dessus ; ceci n'est pas une seconde politique.
        async_mode = False

    is_txt = lower.endswith(".txt") or (content_type or "").startswith("text/")
    is_pdf = lower.endswith(".pdf") or (content_type or "") == "application/pdf"

    # ✅ extract date from content early (best-effort) BEFORE enqueue.
    # Limité aux formats simples ; ne doit jamais bloquer l'ingestion.
    doc_date: date | None = None
    try:
        if is_txt and extract_document_date_from_text is not None:
            txt = extract_text_from_txt_bytes(file_bytes)
            doc_date = extract_document_date_from_text(txt)
        elif is_pdf and extract_document_date_from_pages is not None:
            pages = extract_text_pages_from_pdf_bytes(file_bytes, enforce_limits=False)
            doc_date = extract_document_date_from_pages(list(pages))
    except Exception:
        doc_date = None

    # Dédup + réservation de quota + création (INGEST-1E3), hors du bloc atomique
    # de stockage/enqueue : même règle que le chemin texte et que l'upload HTTP.
    created = create_document_with_quota_reservation(
        space=space,
        filename=filename,
        content_type=(content_type or "application/octet-stream"),
        size_bytes=len(file_bytes),
        sha256=sha,
    )
    doc = created.document
    # Contenu déjà présent sur une ligne FAILED : cette ligne est la cible de REPRISE
    # (même document_id, aucune ligne créée, provenance d'origine conservée). Un
    # contenu déjà ACTIF (PENDING/INGESTING/READY) est rendu tel quel, comme avant.
    retried = bool(created.deduped and doc.status == DocumentStatus.FAILED)

    with transaction.atomic():
        ok = _ensure_storage_file_or_fail(
            doc=doc,
            filename=filename or "document.bin",
            raw_bytes=file_bytes,
            async_mode=async_mode,
        )
        if not ok:
            return IngestResult(
                document_id=str(doc.id),
                status=DocumentStatus.FAILED,
                chunks_created=0,
                deduped=bool(created.deduped),
                retried=retried,
            )

        # ✅ persist extracted date as early as possible
        _update_document_date_best_effort(doc=doc, doc_date=doc_date)

        if created.deduped and not retried:
            return IngestResult(
                document_id=str(doc.id),
                status=doc.status,
                chunks_created=count_chunks_for_document(document=doc) if doc.status == DocumentStatus.READY else 0,
                deduped=True,
            )

        if async_mode:
            _enqueue_after_commit(document_id=str(doc.id), request_id=request_id, actor_user_id=actor_user_id,
                                      filename=getattr(doc, "filename", None))
            return IngestResult(document_id=str(doc.id), status=DocumentStatus.PENDING, chunks_created=0,
                                deduped=retried, retried=retried)

    # ---- SYNC MODE : extraction multi-format commune (TXT/PDF/MD/DOCX/CSV/HTML/OCR) ----
    from .indexing import extract_and_index_bytes

    # La ligne à indexer est CELLE rendue par la création/dédup (reprise d'un FAILED
    # comprise) — jamais « la plus récente de ce sha », qui pouvait désigner une autre
    # occurrence FAILED historique.
    doc = Document.objects.filter(id=doc.id).first()
    if not doc:
        raise IngestError("document_create_failed")

    res = extract_and_index_bytes(
        doc=doc,
        space=space,
        file_bytes=file_bytes,
        filename=filename or "document.bin",
        content_type=content_type or "",
    )
    # Le résultat d'indexation ne connaît pas la dédup : on y reporte le fait que la
    # ligne était une reprise (même document_id, aucune ligne créée).
    return replace(res, deduped=bool(res.deduped or retried), retried=retried) if retried else res

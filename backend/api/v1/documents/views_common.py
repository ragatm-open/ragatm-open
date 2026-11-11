# backend/api/v1/documents/views_common.py
from __future__ import annotations

import inspect
import logging
import mimetypes
import uuid

from celery.exceptions import Retry
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.http import FileResponse, HttpResponse
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.models.rag_space import RagSpace
from apps.rag_core.repositories.document_repository import (
    count_chunks_for_document as _count_chunks_for_document,
)
# Garde CANONIQUE (owner_scope + ACL, flag-gated). Sous flags OFF elle délègue au
# legacy (comportement identique) ; sous enforcement elle applique le périmètre.
from apps.workspaces.services.access import require_ragspace_access as _raw_require_rag_space_access

logger = logging.getLogger(__name__)

# Optional: reindex service
try:
    from apps.rag_core.services.ingest_document import reindex_existing_document  # type: ignore
except Exception:  # pragma: no cover
    reindex_existing_document = None  # type: ignore

# Hash helpers
try:
    from apps.rag_core.services.ingest_document import _sha256_bytes, _sha256_text  # type: ignore
except Exception:  # pragma: no cover
    _sha256_bytes = None  # type: ignore
    _sha256_text = None  # type: ignore

# Celery tasks (optional)
try:
    from apps.rag_core.tasks import ingest_document_task  # type: ignore
except Exception:  # pragma: no cover
    ingest_document_task = None  # type: ignore


def _boolish(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y", "on")


def _should_async(request, *, body_value=None, format_key=None) -> bool:
    """Décision sync/async pour une requête DRF.

    Délègue à l'unique source de vérité ``ingest.async_policy`` afin d'éviter
    toute divergence avec le pipeline. Priorité : OCR **ou EXCEL** (forcés) >
    override explicite (body_value > ``?async=`` > header ``x-rag-async``) >
    ``RAG_INGEST_ASYNC_DEFAULT``.

    ``format_key`` : format RÉEL détecté par ``validate_and_classify`` quand la vue
    le connaît déjà (upload). Un classeur Excel force l'async, quoi que demande le
    client — comme l'OCR.
    """
    from apps.rag_core.services.ingest.async_policy import should_async_from_request

    return should_async_from_request(request, body_value=body_value, format_key=format_key)


# ---------------------------------------------------------------------
# ✅ COMPAT WRAPPERS
# ---------------------------------------------------------------------
def require_rag_space_access(user, rag_space_id: uuid.UUID, *, write: bool = False):
    """
    Compat wrapper: prod peut avoir une signature différente.
    IMPORTANT: ne jamais appeler en positional (certaines versions sont keyword-only).

    Supporte:
      - require_rag_space_access(*, user, rag_space_id, permission="read"|"write")
      - require_rag_space_access(*, user, rag_space_id, write=<bool>)
      - require_rag_space_access(*, user, rag_space_id, can_write=<bool>)
      - require_rag_space_access(*, user, rag_space_id, write_access=<bool>)
      - require_rag_space_access(user, rag_space_id, <bool>)  # legacy positional (dernier recours)
    """
    fn = _raw_require_rag_space_access

    # 1) Tenter via introspection, mais appeler TOUJOURS en kwargs
    try:
        params = inspect.signature(fn).parameters

        # signature moderne fréquente: permission="read"|"write"
        if "permission" in params:
            return fn(user=user, rag_space_id=rag_space_id, permission=("write" if write else "read"))

        # autres variantes bool
        if "write" in params:
            return fn(user=user, rag_space_id=rag_space_id, write=write)
        if "can_write" in params:
            return fn(user=user, rag_space_id=rag_space_id, can_write=write)
        if "write_access" in params:
            return fn(user=user, rag_space_id=rag_space_id, write_access=write)

        # fallback: kwargs minimaux
        return fn(user=user, rag_space_id=rag_space_id)
    except TypeError:
        # 2) Si signature() échoue / callable exotique: tenter des patterns connus
        for call in (
            lambda: fn(user=user, rag_space_id=rag_space_id, permission=("write" if write else "read")),
            lambda: fn(user=user, rag_space_id=rag_space_id, write=write),
            lambda: fn(user=user, rag_space_id=rag_space_id, can_write=write),
            lambda: fn(user=user, rag_space_id=rag_space_id, write_access=write),
            lambda: fn(user=user, rag_space_id=rag_space_id),
        ):
            try:
                return call()
            except TypeError:
                continue

        # 3) Dernier recours seulement: legacy positional
        try:
            return fn(user, rag_space_id, write)
        except TypeError:
            # Certaines versions refusent tout positional (keyword-only): on relance proprement
            return fn(user=user, rag_space_id=rag_space_id)


def load_accessible_document(user, doc_uuid, *, write: bool = False, request=None):
    """Charge un Document après contrôle d'accès, en réponse **anti-énumération**.

    Sans cette unification, les vues renvoyaient :
      - ``404`` pour un document inexistant,
      - ``403`` pour un document existant mais hors périmètre,
    ce qui transforme l'API en oracle d'existence (énumération de ``document_id``).

    Règle retenue :
      - aucun accès **lecture** → ``404 not_found`` (indiscernable d'un inexistant) ;
      - lecture OK mais **écriture** refusée (ex. viewer) → on laisse remonter
        ``PermissionDenied`` → ``403`` : l'utilisateur voit déjà le document dans
        sa liste, un 403 honnête est préférable et ne fuite aucune information.

    Lève ``rest_framework.exceptions.NotFound`` (404 ``{"detail": "not_found"}``)
    ou ``django.core.exceptions.PermissionDenied`` (403) — toutes deux gérées par
    le handler d'exception DRF par défaut.
    """
    from apps.rag_core.models.document import Document

    doc = Document.objects.filter(id=doc_uuid).first()
    if not doc:
        raise NotFound("not_found")

    # Audit périmètre owner (observation, no-op si flags OFF ; ne lève jamais).
    # Dérivé du RagSpace PARENT du document (jamais du seul document_id) → couvre
    # le cas IDOR (owner A connaît l'UUID d'un doc du périmètre B).
    from apps.workspaces.services import scope_audit
    scope_audit.emit_ragspace_decision(
        user=user, rag_space_id=doc.rag_space_id,
        permission=("write" if write else "read"), endpoint="documents.access", request=request,
    )

    # 1) Lecture obligatoire : sans elle, le document doit rester invisible.
    try:
        require_rag_space_access(user, doc.rag_space_id, write=False)
    except PermissionDenied:
        raise NotFound("not_found")

    # 2) Écriture : l'utilisateur a déjà la lecture → 403 explicite si refus.
    if write:
        require_rag_space_access(user, doc.rag_space_id, write=True)

    return doc


def count_chunks_for_document(*, document_id) -> int:
    """
    Compat wrapper autour du repo (déjà robuste chez toi).
    """
    try:
        return int(_count_chunks_for_document(document_id=document_id) or 0)
    except TypeError:
        return int(_count_chunks_for_document(document_id) or 0)
    except Exception:
        return 0


def is_celery_eager_mode() -> bool:
    """True si Celery exécute les tâches **inline** (``CELERY_TASK_ALWAYS_EAGER=1``).

    En eager il n'y a ni broker ni worker : ``.delay()`` exécute la tâche dans la
    requête. « Enfiler » n'a alors aucun sens — un upload demandé en async est
    traité en **sync explicite** (réponse ``queued: false`` + statut réel), jamais
    enfilé. Évite que ``self.retry`` (timeout provider) ne devienne un faux
    ``503 ingest_enqueue_unavailable``.
    """
    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


def broker_ready() -> bool:
    """Vérification synchrone, best-effort, que le broker Celery est joignable.

    But : ne jamais répondre ``queued: true`` si la tâche ne pourra pas être
    postée. Toujours True en mode EAGER (pas de broker requis). Ne lève jamais.
    """
    if ingest_document_task is None:
        logger.error("ingest enqueue impossible: ingest_document_task indisponible (import KO)")
        return False

    # En EAGER (dev/tests), .delay() exécute la tâche en process : pas de broker.
    if bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
        return True

    try:
        app = ingest_document_task.app
        conn = app.connection_for_write()
        try:
            conn.ensure_connection(max_retries=1, timeout=2)
            return True
        finally:
            try:
                conn.release()
            except Exception:
                pass
    except Exception:
        logger.warning("celery broker injoignable pour l'ingestion (enqueue refusé)", exc_info=True)
        return False


def _mark_document_failed(*, document_id: str, error_message: str) -> None:
    """Persiste un échec sur le Document (jamais de blocage silencieux en PENDING)."""
    try:
        from apps.rag_core.models.document import Document as _Doc

        _Doc.objects.filter(id=document_id).update(
            status=DocumentStatus.FAILED,
            error_message=(error_message or "")[:1000],
        )
    except Exception:
        logger.exception("impossible de marquer le document FAILED (document_id=%s)", document_id)


def _enqueue_ingest(
    *, document_id: str, request_id: str | None = None, rag_space_id: str | None = None,
    ocr: bool | None = None, actor_user_id: int | None = None,
    extraction_timeout_seconds: int | None = None, queue: str | None = None,
    drop_visual_evidence: bool = False,
) -> bool:
    """
    Poste la tâche d'ingestion via Celery. Retourne True ssi la tâche a été
    acceptée (ou exécutée en EAGER). En cas d'échec, marque le Document FAILED
    pour éviter tout blocage silencieux en PENDING. Ne lève jamais.

    ``ocr`` (P1 bis) : transmis à la tâche pour forcer l'OCR local sur CETTE
    réindexation uniquement (OCR à la demande).

    ``extraction_timeout_seconds`` (lot 3) : budget d'extraction de CETTE exécution
    (stratégie « délai étendu »). Valeur issue de la CONFIGURATION, jamais du client.
    """
    if ingest_document_task is None:
        logger.error(
            "enqueue ingest impossible: task indisponible (document_id=%s rag_space_id=%s request_id=%s)",
            document_id, rag_space_id, request_id,
        )
        _mark_document_failed(document_id=document_id, error_message="enqueue_failed: ingest task unavailable")
        return False

    try:
        # Cas nominal (kwargs-only). access_context = IDENTIFIANTS SEULEMENT
        # (acteur + request_id) ; la tâche recharge droits/scope depuis la base.
        task_kwargs = dict(
            document_id=document_id, request_id=request_id, ocr=ocr,
            extraction_timeout_seconds=extraction_timeout_seconds,
            access_context={"user_id": actor_user_id, "request_id": request_id},
        )
        if drop_visual_evidence:
            # opt-in explicite, transmis seulement quand demandé.
            task_kwargs["drop_visual_evidence"] = True
        # Jeton d'enfilement : seule la tâche porteuse du jeton COURANT traitera le
        # document (garde des messages périmés, services/ingest/enqueue_token.py).
        from apps.rag_core.services.ingest.enqueue_token import stamp_enqueue_token

        task_kwargs["enqueue_token"] = stamp_enqueue_token(document_id)
        if queue:
            # File DÉDIÉE par document (IFC) : même tâche, autre file.
            ingest_document_task.apply_async(kwargs=task_kwargs, queue=queue)
        else:
            ingest_document_task.delay(**task_kwargs)
    except Retry:
        # Durcissement : `celery.exceptions.Retry` n'est PAS un échec d'enqueue.
        # Il ne peut survenir qu'en eager (exécution inline) — cas désormais routé
        # vers le chemin sync AVANT d'arriver ici. On relaie sans masquer ni le
        # confondre avec une panne broker (jamais de 503 ingest_enqueue_unavailable
        # ni de queued:true). Filet de sécurité contre une config eager résiduelle.
        raise
    except TypeError:
        # Fallback si une ancienne task acceptait du positional.
        try:
            ingest_document_task.delay(document_id)
        except Retry:
            raise
        except Exception:
            logger.exception(
                "enqueue ingest failed (document_id=%s rag_space_id=%s request_id=%s)",
                document_id, rag_space_id, request_id,
            )
            _mark_document_failed(document_id=document_id, error_message="enqueue_failed: broker rejected task")
            return False
    except Exception:
        logger.exception(
            "enqueue ingest failed (document_id=%s rag_space_id=%s request_id=%s)",
            document_id, rag_space_id, request_id,
        )
        _mark_document_failed(document_id=document_id, error_message="enqueue_failed: broker rejected task")
        return False

    logger.info(
        "ingest enqueued (document_id=%s rag_space_id=%s request_id=%s)",
        document_id, rag_space_id, request_id,
    )
    return True


def _enqueue_unavailable_response(*, doc) -> Response:
    return Response(
        {
            "ok": False,
            "queued": False,
            "detail": "ingest_enqueue_unavailable",
            "document_id": str(doc.id),
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def enqueue_async_or_503(
    *, request, doc, ocr: bool | None = None, extraction_timeout_seconds: int | None = None,
    drop_visual_evidence: bool = False,
):
    """Met en file la tâche d'ingestion de façon **observable**.

    Garantie stricte « aucun faux ``queued: true`` » :

    1. pré-check ``broker_ready`` (court-circuité en EAGER) : échoue tôt/proprement
       si le broker est déjà connu indisponible ;
    2. enqueue **direct** via ``_enqueue_ingest`` dont on capte le booléen AVANT de
       répondre — plus de ``transaction.on_commit`` opaque dont le résultat serait
       ignoré. Si ``.delay()`` lève (course post-pré-check, rejet broker),
       ``_enqueue_ingest`` marque le doc ``FAILED`` et renvoie ``False``.

    Sécurité transactionnelle : le ``Document`` est **déjà committé** à ce stade
    (``create_or_get_by_sha`` et ``storage_file.save`` ont leurs propres
    transactions ; aucun bloc ``atomic()`` n'englobe la vue et ``ATOMIC_REQUESTS``
    est désactivé). Poster directement ne risque donc pas de faire lire un document
    non committé par le worker, tout en rendant l'échec de ``.delay()`` observable.

    Retourne une ``Response`` (503) en cas d'échec, sinon ``None`` : l'appelant
    répond alors lui-même ``queued: true``.
    """
    request_id = request.headers.get("x-request-id") or None
    rag_space_id = str(getattr(doc, "rag_space_id", "") or "")

    # 1. Pré-check broker.
    if not broker_ready():
        _mark_document_failed(
            document_id=str(doc.id),
            error_message="enqueue_failed: ingestion task could not be queued (Celery/broker unavailable)",
        )
        logger.error(
            "ingest enqueue refusé (broker indisponible) document_id=%s rag_space_id=%s request_id=%s",
            doc.id, rag_space_id, request_id,
        )
        return _enqueue_unavailable_response(doc=doc)

    # 2. Enqueue direct + observable (doc déjà committé). _enqueue_ingest marque
    #    le doc FAILED en interne si .delay() échoue.
    from apps.rag_core.services.ingest.queues import ingest_queue_for

    if not _enqueue_ingest(
        document_id=str(doc.id), request_id=request_id, rag_space_id=rag_space_id, ocr=ocr,
        actor_user_id=getattr(getattr(request, "user", None), "id", None),
        extraction_timeout_seconds=extraction_timeout_seconds,
        queue=ingest_queue_for(filename=getattr(doc, "filename", None)),
        **({"drop_visual_evidence": True} if drop_visual_evidence else {}),
    ):
        logger.error(
            "ingest enqueue échoué (.delay) document_id=%s rag_space_id=%s request_id=%s",
            doc.id, rag_space_id, request_id,
        )
        return _enqueue_unavailable_response(doc=doc)

    return None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _safe_str(v) -> str:
    if v is None:
        return ""
    return str(v)


def _guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or fallback


def _derive_extraction_substatus(doc, ext: dict) -> str:
    """Sous-statut lisible dérivé de Document.status + meta.extraction.

    Ne crée AUCUN nouvel état DB : c'est une projection pour l'API/UX. Valeurs :
    success | ready_with_warnings | partial | failed | rejected | ocr_required.
    """
    raw_status = _safe_str(getattr(doc, "status", "")).upper()
    ext_status = _safe_str(ext.get("extraction_status", ""))
    error_code = _safe_str(ext.get("error_code", ""))
    error_message = _safe_str(getattr(doc, "error_message", ""))

    if raw_status == DocumentStatus.FAILED:
        if "pdf_scanned_requires_ocr" in (error_code or error_message) or "ocr_no_text" in (
            error_code or error_message
        ):
            return "ocr_required"
        if ext_status == "rejected" or error_code:
            return "rejected"
        return "failed"

    if raw_status == DocumentStatus.READY:
        if ext.get("warnings") or ext.get("empty_pages"):
            return "ready_with_warnings"
        if ext_status == "partial":
            return "partial"
        return "success"

    # PENDING/INGESTING : pas de sous-statut d'extraction encore stabilisé.
    return ext_status or ""


def _retry_hint(doc) -> tuple[str, str | None]:
    """(retryabilité, stratégie recommandée) d'un document en échec.

    `(""; None)` pour un document qui n'est pas en échec : rien à recommander.
    Ne lève jamais — une erreur de classification ne doit pas faire échouer la
    lecture d'un document.
    """
    if _safe_str(getattr(doc, "status", "")).upper() != DocumentStatus.FAILED:
        return "", None
    try:
        from apps.rag_core.services.ingest.retry_strategy import classify_retryability

        retryability, _code, recommended = classify_retryability(doc)
        return retryability, recommended
    except Exception:  # pragma: no cover - défense en profondeur
        logger.debug("retryability classification failed (doc=%s)", getattr(doc, "id", None))
        return "", None


def _current_error_code(doc, ext: dict) -> str:
    """`error_code` exposé par l'API : celui de la tentative COURANTE d'un document FAILED.

    Pour un document en échec, la même lecture que la décision de reprise
    (`retry_strategy.document_error_code`) : l'écran reçoit un `error_code` cohérent
    avec `retryability`/`recommended_strategy`, jamais le code d'une tentative
    antérieure quand le message courant porte un code structuré connu. Hors du
    vocabulaire gouverné (ou hors FAILED), la valeur brute de `meta.extraction` est
    rendue telle quelle — aucun préfixe de prose n'est promu en code. Ne lève jamais.
    """
    raw = _safe_str(ext.get("error_code", ""))
    if _safe_str(getattr(doc, "status", "")).upper() != DocumentStatus.FAILED:
        return raw
    try:
        from apps.rag_core.services.ingest.retry_strategy import document_error_code, known_error_codes

        code = _safe_str(document_error_code(doc))
        return code if code and code in known_error_codes() else raw
    except Exception:  # pragma: no cover - défense en profondeur
        logger.debug("current error code resolution failed (doc=%s)", getattr(doc, "id", None))
        return raw


def _safe_extraction_view(doc) -> dict | None:
    """Sous-ensemble SÛR de Document.meta["extraction"] destiné à l'API/UX.

    Ne renvoie JAMAIS de texte extrait ni de secret : uniquement des métadonnées
    techniques (méthode, moteur natif, pages, warnings, compteurs)."""
    meta = getattr(doc, "meta", None)
    ext = meta.get("extraction") if isinstance(meta, dict) else None
    if not isinstance(ext, dict):
        # Document en échec SANS métadonnées d'extraction : ce sont les échecs
        # survenus hors de la couche d'extraction (fichier source absent, enqueue
        # perdu) et les documents antérieurs à la persistance systématique du code.
        # Ils méritent quand même une recommandation — sinon l'écran ne saurait rien
        # proposer précisément pour les cas les plus définitifs.
        if _safe_str(getattr(doc, "status", "")).upper() != DocumentStatus.FAILED:
            return None
        retryability, recommended = _retry_hint(doc)
        return {
            "status": _derive_extraction_substatus(doc, {}),
            "extraction_status": "",
            "error_code": "",
            "retryability": retryability,
            "recommended_strategy": recommended,
        }

    empty_pages = ext.get("empty_pages") or []
    # Retryabilité DÉCIDÉE côté backend (lot 3) : le frontend affiche l'action
    # recommandée, il ne la déduit pas. Sans cela, la table de décision existerait
    # en double et les deux finiraient par diverger — l'écran proposerait « OCR »
    # sur un document dont le backend refuserait précisément l'OCR.
    retryability, recommended = _retry_hint(doc)
    return {
        "status": _derive_extraction_substatus(doc, ext),
        "extraction_status": _safe_str(ext.get("extraction_status", "")),
        "error_code": _current_error_code(doc, ext),
        "retryability": retryability,
        "recommended_strategy": recommended,
        "method": _safe_str(ext.get("method", "")),
        "type": _safe_str(ext.get("type", "")),
        "is_image_only_pdf": bool(ext.get("is_image_only_pdf", False)),
        "requires_visual_check": bool(ext.get("requires_visual_check", False)),
        # Métadonnées image (Lot 2) : format/dimensions/mode/orientation. Jamais de
        # contenu OCR ici (le texte vit dans les Chunk).
        "image": ext.get("image") if isinstance(ext.get("image"), dict) else None,
        "page_count": ext.get("page_count", ext.get("pages")),
        "ocr_used": bool(ext.get("ocr_used", False)),
        "native_fallback_used": bool(ext.get("pdf_native_fallback_used", ext.get("native_fallback_used", False))),
        "native_fallback_engine": ext.get("native_fallback_engine") or (
            ext.get("pdf_native_engine") if ext.get("pdf_native_fallback_used") else None
        ),
        "pdf_native_engine": ext.get("pdf_native_engine"),
        "pages": ext.get("pages"),
        "text_pages": ext.get("text_pages") or [],
        "empty_pages": empty_pages,
        # image_only ≈ pages sans texte natif non récupérées par OCR (approximation
        # honnête tant qu'on n'a pas de détection d'image dédiée — cf. P2).
        "image_only_pages": empty_pages if not ext.get("ocr_used") else [],
        "warnings": list(ext.get("warnings") or []),
        "native_text_chars": ext.get("native_text_chars"),
        "ocr_text_chars": ext.get("ocr_text_chars"),
        "extraction_quality_score": ext.get("extraction_quality_score"),
        # Entités métier agrégées (P2) — indices à vérifier sur la page source.
        "detected_massifs": ext.get("detected_massifs") or [],
        "detected_views": ext.get("detected_views") or [],
        "detected_nappes": ext.get("detected_nappes") or [],
        "detected_rebar_mentions": ext.get("detected_rebar_mentions") or [],
        "detected_spacing_mentions": ext.get("detected_spacing_mentions") or [],
        "pages_by_type": ext.get("pages_by_type") or {},
        # Index léger des tableaux structurés (P3) — sans les lignes (jamais de
        # valeurs chiffrées détaillées ici ; elles vivent dans les chunks-tableaux).
        "tables": ext.get("tables") or [],
        # --- Outlook (.msg) -------------------------------------------------
        # Objet, expéditeur et date : ce qui permet à l'utilisateur d'identifier le
        # message dans une liste. Les listes `to`/`cc` sont volontairement ABSENTES :
        # elles sont conservées dans Document.meta (bornées par
        # DOCUMENT_MSG_MAX_RECIPIENTS) mais les diffuser par l'API exposerait des
        # carnets d'adresses entiers sans usage produit — seul le compte est utile.
        "subject": _safe_str(ext.get("subject", "")) or None,
        "sender_name": _safe_str(ext.get("sender_name", "")) or None,
        "sender_address": _safe_str(ext.get("sender_address", "")) or None,
        "sent_at": ext.get("sent_at"),
        "recipient_count": ext.get("recipient_count"),
        "body_kind": _safe_str(ext.get("body_kind", "")) or None,
        "removed_boilerplate_chars": ext.get("removed_boilerplate_chars"),
        # Contrat d'honnêteté sur les pièces jointes : les trois clés voyagent
        # ensemble pour que le front ne puisse pas afficher un compte sans dire
        # qu'aucune n'est interrogeable.
        "attachments_detected": ext.get("attachments_detected"),
        "attachments_ingested": ext.get("attachments_ingested"),
        "attachments_supported_in_this_release": bool(
            ext.get("attachments_supported_in_this_release", False)
        ),
        "attachments": _attachment_rows(ext.get("attachments") or []),
        # état d'ingestion des pièces et format d'origine du courriel.
        "source_format": _safe_str(ext.get("source_format", "")) or None,
        "attachment_ingestion": _safe_str(ext.get("attachment_ingestion", "")) or None,
        "email_status": _email_status(ext),
    }


#: Champs d'une pièce jointe exposés par l'API : statut et provenance, JAMAIS de contenu.
_ATTACHMENT_FIELDS = (
    "index", "path", "depth", "parent_path", "filename", "extension", "mimetype", "size_bytes",
    "content_id", "is_inline", "detected_format", "status", "reason", "fragment_count", "same_as",
)


#: lecture des courriels ingérés AVANT le statut NO_EXTRACTABLE_TEXT : ces
#: raisons signifiaient déjà « lu, aucun texte » mais étaient rangées en FAILED. Normalisation À LA
#: LECTURE (aucune donnée modifiée, aucune réindexation) ; les autres échecs restent FAILED.
#: `ocr_no_text` n'est normalisé que pour une IMAGE ; un ancien « no_content » (lisible ou non,
#: on ne le sait plus) reste FAILED.
_LEGACY_NO_TEXT_REASONS = frozenset({"ocr_no_text", "empty_document"})
_EMAIL_PARTIAL = frozenset({"REJECTED", "FAILED", "SKIPPED_LIMIT", "PARTIALLY_INDEXED"})


def _attachment_rows(rows) -> list:
    out = []
    for row in rows[:200] if isinstance(rows, list) else []:
        if isinstance(row, dict):
            item = {k: row.get(k) for k in _ATTACHMENT_FIELDS if k in row}
            reason = item.get("reason")
            if (item.get("status") == "FAILED" and reason in _LEGACY_NO_TEXT_REASONS
                    and (reason != "ocr_no_text" or row.get("detected_format") == "image")):
                item["status"] = "NO_EXTRACTABLE_TEXT"
            out.append(item)
    return out


def _email_status(ext: dict):
    """`complete` / `partial` recalculé sur les statuts NORMALISÉS : un courriel ingéré avant
    27C n'apparaît plus « partiel » à cause de logos sans texte ou de formats non supportés."""
    raw = _safe_str(ext.get("email_status", "")) or None
    rows = ext.get("attachments")
    if raw is None or not isinstance(rows, list) or ext.get("attachment_ingestion") != "enabled":
        return raw
    return "partial" if any(r.get("status") in _EMAIL_PARTIAL for r in _attachment_rows(rows)) else "complete"


def _serialize_document(doc, *, chunks_count: int | None = None) -> dict:
    """
    Canonique (aligné avec tes champs réels):
    Document fields: id, rag_space, filename, content_type, size_bytes, sha256, storage_file, status, error_message, created_at
    """
    from api.v1.documents.listing import file_type_label
    from api.v1.documents.preview_kind import resolve_preview_kind

    filename = _safe_str(getattr(doc, "filename", ""))
    content_type = _safe_str(getattr(doc, "content_type", ""))
    out = {
        "id": str(doc.id),
        "rag_space_id": str(getattr(doc, "rag_space_id", "")),
        "filename": filename,
        "content_type": content_type,
        # Type court AFFICHÉ et TRIÉ (`PDF`, `DOCX`, …). Décidé côté backend, comme
        # `preview_kind` : c'est la même valeur que celle sur laquelle `?ordering=
        # file_type` trie en base, donc l'ordre affiché ne peut pas contredire la
        # colonne affichée.
        "file_type": file_type_label(content_type, filename),
        # Quel VISUALISEUR employer (`pdf` | `spreadsheet` | `text` | `unsupported`).
        # Décision canonique côté backend : sans elle, le frontend chargeait tout
        # document dans une iframe PDF et un `.xlsx` y produisait une zone blanche.
        "preview_kind": resolve_preview_kind(content_type=content_type, filename=filename),
        "size_bytes": getattr(doc, "size_bytes", None),
        "sha256": _safe_str(getattr(doc, "sha256", "")),
        "status": _safe_str(getattr(doc, "status", "")),
        "error_message": _safe_str(getattr(doc, "error_message", "")),
        "created_at": getattr(doc, "created_at", None),
    }
    extraction = _safe_extraction_view(doc)
    if extraction is not None:
        out["extraction"] = extraction
    # VISUAL-ENRICHMENT-DEFERRED : `status=READY` dit « exploitable par la recherche »,
    # jamais « vision terminée ». L'état de l'enrichissement visuel différé est exposé
    # À PART, et seulement quand il existe (champ facultatif : aucun client ne le requiert).
    enrichment = ((getattr(doc, "meta", None) or {}).get("visual_enrichment") or {})
    if isinstance(enrichment, dict) and enrichment.get("status"):
        out["visual_enrichment_status"] = _safe_str(enrichment.get("status"))
    # état de la transcription d'un audio (étape, parties, durée,
    # langue, code d'échec). Champ facultatif, présent seulement pour un audio ; jamais de
    # texte transcrit ni d'identifiant de job Gateway.
    audio = ((getattr(doc, "meta", None) or {}).get("audio") or {})
    if isinstance(audio, dict) and audio:
        view = {}
        for key in ("format", "stage", "error_code", "language"):
            if isinstance(audio.get(key), str) and audio.get(key):
                view[key] = _safe_str(audio.get(key))[:32]
        for key in ("duration_ms", "parts_total", "parts_done", "segment_count"):
            if isinstance(audio.get(key), int) and not isinstance(audio.get(key), bool):
                view[key] = int(audio[key])
        out["audio"] = view
    if chunks_count is not None:
        out["chunks_count"] = int(chunks_count)
    return out


def _ensure_storage_file_or_fail(*, request, doc, upload_file, request_id: str, stream: bool = False):
    """
    Ensure doc.storage_file is properly saved.
    Important: FieldFile can be falsy if name == "".
    Returns a DRF Response on failure, otherwise None.

    ``stream`` : enregistre le fichier téléversé PAR BLOCS (fichier
    temporaire Django → stockage), sans le recharger en mémoire. Réservé à l'audio
    (jusqu'à 600 Mo) ; les autres formats gardent leur chemin historique.
    """
    storage_file = getattr(doc, "storage_file", None)
    has_file = storage_file is not None and bool(getattr(storage_file, "name", "") or "")
    if has_file:
        return None

    try:
        filename = getattr(upload_file, "name", None) or f"{doc.id}"
        streamed = bool(stream and hasattr(upload_file, "chunks"))
        if streamed:
            upload_file.seek(0)
            doc.storage_file.save(filename, upload_file, save=True)
            content = b""
        else:
            content = upload_file.read() if hasattr(upload_file, "read") else None
        if content is None:
            return Response(
                {"detail": "storage_file_save_failed", "request_id": request_id},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not streamed:
            doc.storage_file.save(filename, ContentFile(content), save=True)
    except Exception as e:
        try:
            from apps.rag_core.models.document import Document as DocModel  # type: ignore

            DocModel.objects.filter(id=doc.id).update(
                status=DocumentStatus.FAILED,
                error_message=f"storage_file_save_failed: {e}",
            )
        except Exception:
            # F5-A : ne pas masquer l'échec de transition -> FAILED (statut incohérent).
            logger.exception("unable to mark document FAILED after storage_file_save_failed document_id=%s", doc.id)
        return Response(
            {"detail": "storage_file_save_failed", "request_id": request_id},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        doc.refresh_from_db(fields=["storage_file"])
    except Exception:
        try:
            doc.refresh_from_db()
        except Exception:
            pass

    storage_file2 = getattr(doc, "storage_file", None)
    has_file2 = storage_file2 is not None and bool(getattr(storage_file2, "name", "") or "")
    if not has_file2:
        try:
            from apps.rag_core.models.document import Document as DocModel  # type: ignore

            DocModel.objects.filter(id=doc.id).update(
                status=DocumentStatus.FAILED,
                error_message="storage_file_empty_after_save",
            )
        except Exception:
            # F5-A : ne pas masquer l'échec de transition -> FAILED (statut incohérent).
            logger.exception("unable to mark document FAILED after storage_file_empty document_id=%s", doc.id)
        return Response(
            {"detail": "storage_file_empty_after_save", "request_id": request_id},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None

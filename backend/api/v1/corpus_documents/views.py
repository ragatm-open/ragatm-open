"""API de résumé des documents de **corpus métier**.

Endpoint DÉDIÉ, distinct de ``/api/v1/documents/<id>/summary/``.

Pourquoi une route séparée
--------------------------
``load_accessible_document`` est la porte d'accès de sept vues privées. La rendre
polymorphe ferait accepter un identifiant de corpus à toutes — y compris à celles
qui suppriment ou réindexent. Le choix de la route appartient au client, à partir
du ``source_type`` que le backend lui a lui-même transmis dans la citation : c'est
un contrat explicite, pas une devinette sur la forme d'un UUID.

Détermination du contexte consommateur
--------------------------------------
Un corpus métier est lu DANS un workspace. Le workspace n'est jamais accepté
depuis le client : il est **dérivé** d'un RagSpace dont l'accès est vérifié par la
garde canonique (``require_ragspace_access``), lui-même issu du fil (``thread_id``,
qui appartient déjà à l'utilisateur) ou d'un ``rag_space_id`` confronté à ce même
contrôle. Quand les deux sont fournis, ils doivent désigner le même espace.
"""
from __future__ import annotations

import logging
import uuid

from django.core.exceptions import PermissionDenied
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.corpus.services import summary_events as events

logger = logging.getLogger(__name__)


def _coerce_uuid(v):
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except Exception:
        return None


def _boolish(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def summary_user_label(document, scope: str, page) -> str:
    """Libellé de la demande, écrit par le SERVEUR.

    Le client n'a aucune autorité sur ce texte : il est persisté dans le fil et
    doit refléter le document réellement résumé. Même formulation que le chemin
    privé — un utilisateur ne doit pas déduire la provenance d'un document de la
    tournure d'une phrase.
    """
    name = document.display_title or "document"
    if scope == "page" and page is not None:
        return f"Résume la page {page} du document **{name}**"
    return f"Résume le document **{name}**"


def _serialize_summary(document, res, *, message_id=None, request_id=None) -> dict:
    """Même enveloppe que le résumé privé, plus la provenance corpus.

    Le frontend consomme un seul type de réponse : ajouter des champs additifs
    plutôt qu'un second contrat évite d'avoir deux chemins de rendu pour un même
    résultat métier.
    """
    return {
        "ok": True,
        "source_type": "domain_corpus",
        "document_id": str(document.id),
        "filename": document.display_title,
        "corpus_id": str(document.corpus_id),
        "corpus_name": document.corpus.name if document.corpus_id else None,
        "status": res.status,
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


def _resolve_consumer_space(request, *, thread_id, rag_space_id):
    """Résout le RagSpace CONSOMMATEUR et son workspace. Ne fait jamais confiance au client.

    Retourne ``(rag_space, thread)``. ``thread`` peut être ``None`` (résumé hors
    fil). Lève ``NotFound("not_found")`` pour toute incohérence — un fil d'un autre
    espace, un espace inaccessible et un espace inexistant sont indiscernables.

    L'ordre compte : le fil est la source la plus forte (il appartient déjà à
    l'utilisateur ET porte un espace). Un ``rag_space_id`` fourni en plus doit
    coïncider, sinon on refuse : accepter le fil d'un workspace tout en exécutant
    sous la politique d'un autre est exactement l'abus que ce contrôle empêche.
    """
    from apps.chat.models import ChatThread
    from apps.workspaces.services.access import require_ragspace_access

    space_from_thread = None
    thread = None
    if thread_id:
        if _coerce_uuid(thread_id) is None:
            raise NotFound("not_found")
        thread = (
            ChatThread.objects.select_related("rag_space", "rag_space__workspace")
            .filter(id=thread_id, user=request.user)
            .first()
        )
        if thread is None:
            raise NotFound("not_found")
        space_from_thread = thread.rag_space

    space_from_body = None
    if rag_space_id:
        space_uuid = _coerce_uuid(rag_space_id)
        if space_uuid is None:
            raise NotFound("not_found")
        try:
            # Garde CANONIQUE : existence, soft-delete, membership, périmètre owner.
            # C'est elle qui transforme un identifiant fourni par le client en fait
            # vérifié — sans elle, `rag_space_id` serait une assertion du client.
            space_from_body = require_ragspace_access(
                user=request.user, rag_space_id=space_uuid, permission="read"
            )
        except (PermissionDenied, NotFound):
            raise NotFound("not_found")

    if space_from_thread is not None and space_from_body is not None:
        if str(space_from_thread.id) != str(space_from_body.id):
            raise NotFound("not_found")

    rag_space = space_from_thread or space_from_body
    if rag_space is None:
        raise NotFound("not_found")

    # Le fil n'est PAS re-contrôlé par `require_ragspace_access` quand il est la
    # seule source : il appartient déjà à l'utilisateur (filtre `user=`) et vit dans
    # un espace où il a nécessairement pu écrire. On applique tout de même la garde
    # pour couvrir une révocation postérieure à la création du fil.
    try:
        rag_space = require_ragspace_access(
            user=request.user, rag_space_id=rag_space.id, permission="read"
        )
    except (PermissionDenied, NotFound):
        raise NotFound("not_found")

    return rag_space, thread


class DomainCorpusDocumentSummaryView(APIView):
    """
    POST /api/v1/domain-corpus-documents/<document_id>/summary/
        body : { mode, scope, page, language, refresh, thread_id, request_id,
                 rag_space_id }
        Génère/réutilise le résumé d'un document de corpus métier. Documents
        longs → map-reduce asynchrone : réponse 202 ``status="pending"``.
    GET  /api/v1/domain-corpus-documents/<document_id>/summary/?mode=&scope=&page=&language=&rag_space_id=
        Lecture seule de l'état (polling). 200 ``status="none"`` si absent.

    Anti-énumération : document inexistant, corpus inactif, corpus non associé au
    workspace, association désactivée et fonctionnalité désactivée produisent tous
    ``404 {"detail": "not_found"}``. La cause réelle part dans un événement
    structuré (``apps.corpus.services.summary_events``), jamais dans la réponse.
    """

    permission_classes = [IsAuthenticated]

    # -- helpers partagés POST/GET -----------------------------------------
    def _load(self, request, document_id, *, thread_id=None, rag_space_id=None):
        """Résout (espace consommateur, fil, document autorisé) ou lève 404."""
        from apps.corpus.services.access import load_accessible_domain_corpus_document
        from apps.corpus.services.summary import is_enabled

        if not is_enabled():
            # Volontairement indiscernable d'un document inexistant : une
            # fonctionnalité désactivée ne doit pas révéler l'existence du corpus.
            events.emit(
                events.EVENT_ACCESS_DENIED,
                reason="feature_disabled",
                source_type="domain_corpus",
                user_id=getattr(request.user, "id", None),
                document_id=document_id,
            )
            raise NotFound("not_found")

        doc_uuid = _coerce_uuid(document_id)
        if doc_uuid is None:
            raise NotFound("not_found")

        rag_space, thread = _resolve_consumer_space(
            request, thread_id=thread_id, rag_space_id=rag_space_id
        )
        workspace = rag_space.workspace
        doc = load_accessible_domain_corpus_document(
            user=request.user, workspace=workspace, document_id=doc_uuid
        )
        return rag_space, workspace, thread, doc

    # -- POST ---------------------------------------------------------------
    def post(self, request, document_id):
        from apps.chat.models import ChatMessage
        from apps.chat.services.summary_messages import (
            SOURCE_TYPE_DOMAIN_CORPUS,
            SummaryRequestConflict,
            build_summary_meta,
            fail_message,
            get_or_create_summary_exchange,
            reopen_message,
            resolve_thread_for_summary,
            settle_for_result,
        )
        from apps.corpus.services.summary import (
            get_cached_corpus_summary,
            summarize_corpus_document,
        )
        from apps.rag_core.services.summary_service import SummaryError, resolve_summary_language

        data = request.data or {}
        mode = data.get("mode") or "bullets"
        scope = data.get("scope") or "document"
        page = data.get("page")
        language = data.get("language") or "auto"
        refresh = _boolish(data.get("refresh"))
        thread_id = data.get("thread_id") or None
        request_id = (data.get("request_id") or "").strip()

        if thread_id and not request_id:
            return Response(
                {"detail": "request_id_required",
                 "error": "request_id est obligatoire lorsque thread_id est fourni."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        rag_space, workspace, _thread, doc = self._load(
            request, document_id,
            thread_id=thread_id, rag_space_id=data.get("rag_space_id"),
        )

        answer_language = resolve_summary_language(language, user=request.user)

        msg = None
        if thread_id:
            # Second contrôle du fil, côté persistance : l'espace CONSOMMATEUR doit
            # être celui du fil. `_resolve_consumer_space` l'a déjà garanti quand le
            # fil est la source ; ce passage couvre le cas où `rag_space_id` a aussi
            # été fourni, et garde la règle exprimée à un seul endroit.
            thread = resolve_thread_for_summary(
                user=request.user, thread_id=thread_id, expected_rag_space_id=rag_space.id
            )
            if thread is None:
                return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)

            source_ref = {
                "source_type": SOURCE_TYPE_DOMAIN_CORPUS,
                "document_id": str(doc.id),
                "corpus_id": str(doc.corpus_id),
                "corpus_name": doc.corpus.name if doc.corpus_id else None,
                "filename": doc.display_title,
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
                # Rejeu strict : la demande a déjà abouti, on rend l'état persisté
                # sans relancer la moindre génération.
                cached = get_cached_corpus_summary(
                    document=doc, rag_space=rag_space, mode=mode, scope=scope, page=page,
                    language=language, user=request.user,
                )
                if cached is not None:
                    return Response(
                        _serialize_summary(doc, cached, message_id=msg.id, request_id=request_id),
                        status=status.HTTP_200_OK,
                    )
            if msg.status == ChatMessage.STATUS_FAILED:
                reopen_message(msg, answer_language=answer_language)

        try:
            res = summarize_corpus_document(
                user=request.user, workspace=workspace, rag_space=rag_space, document=doc,
                mode=mode, scope=scope, page=page, language=language, refresh=refresh,
            )
        except SummaryError as e:
            if msg is not None:
                fail_message(msg, error=e.code, answer_language=answer_language)
            return Response({"detail": e.code, "error": str(e)}, status=e.http_status)
        except Exception as e:  # noqa: BLE001
            # Jamais le contenu ni le prompt : seulement des identifiants.
            logger.exception("corpus summary failed (doc=%s): %s", doc.id, e)
            if msg is not None:
                fail_message(msg, error="summary_failed", answer_language=answer_language)
            return Response({"detail": "summary_failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if msg is not None:
            msg = settle_for_result(msg, res, load_row=_load_corpus_summary_row)

        http = status.HTTP_202_ACCEPTED if res.status == "pending" else status.HTTP_200_OK
        return Response(
            _serialize_summary(
                doc, res,
                message_id=(msg.id if msg is not None else None),
                request_id=(request_id or None),
            ),
            status=http,
        )

    # -- GET (polling) ------------------------------------------------------
    def get(self, request, document_id):
        from apps.corpus.services.summary import get_cached_corpus_summary

        q = request.query_params
        rag_space, _workspace, _thread, doc = self._load(
            request, document_id,
            thread_id=q.get("thread_id") or None, rag_space_id=q.get("rag_space_id"),
        )

        res = get_cached_corpus_summary(
            document=doc,
            rag_space=rag_space,
            mode=q.get("mode") or "bullets",
            scope=q.get("scope") or "document",
            page=q.get("page"),
            # Mêmes défauts que le POST : le polling DOIT viser exactement la
            # variante créée par la demande, sinon il ne la trouve jamais.
            language=q.get("language") or "auto",
            user=request.user,
        )
        if res is None:
            return Response({"ok": True, "document_id": str(doc.id), "status": "none"})
        return Response(_serialize_summary(doc, res))


def _load_corpus_summary_row(summary_id):
    from apps.corpus.models import DomainCorpusDocumentSummary

    return DomainCorpusDocumentSummary.objects.filter(id=summary_id).first()

# backend/api/v1/chat/views.py
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Iterable, Optional

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.actions.matchers import HelpMatcher
from apps.actions.normalize import normalize
from apps.actions.plan import MatchResult
from apps.chat.services import corpus_mode_service as _corpus_mode
from apps.audit.action_audit import emit_action_event
from apps.chat.services import excel_guard as _excel_guard
from apps.chat.services.chat_service import chat_query, chat_query_stream
from apps.help.catalog import render_help_markdown
from apps.llm_router.gateway.http_errors import classify_for_http, find_gateway_error
from apps.rag_core.domain.corpus_mode import PARTIAL_COVERAGE_WARNING
from apps.workspaces.services.access import require_ragspace_access
from .serializers import ChatRequestSerializer, ChatThreadCreateSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Help intent (« /aide », « que puis-je faire ? »)
# ---------------------------------------------------------------------
# Intercepté ICI, en amont du routeur d'actions ET du listing : la réponse est
# déterministe (catalogue statique), donc ni retrieval, ni embeddings, ni LLM,
# ni RagQueryLog, ni tâche de reformulation. L'interception étant dans la vue,
# elle est active quel que soit ``ACTIONS_ROUTER_ENABLED``.
#
# Le matcher est l'implémentation UNIQUE, partagée avec ``ActionRouter`` : aucune
# règle de détection n'est dupliquée (ni ici, ni côté frontend).
_HELP_MATCHER = HelpMatcher()


def _match_help(q: str) -> MatchResult | None:
    """``None`` si ce n'est pas une demande d'aide ; sinon le résultat du matcher.

    On renvoie le ``MatchResult``, pas un booléen : le sujet éventuel y est déjà,
    et la vue n'a donc aucun parsing à refaire. Une seule couche parse.
    """
    text = q or ""
    return _HELP_MATCHER.match(norm=normalize(text), original=text)


def _excel_guard_payload(decision, *, thread_id: str) -> dict:
    """Charge utile du refus analytique. Déterministe, sans LLM, sans retrieval.

    `analytics` est volontairement PAUVRE : l'intention et sa non-disponibilité. Pas
    de plan d'exécution, pas de détail technique — c'est une amorce pour le futur
    moteur P2, pas un contrat analytique.
    """
    answer, suggestions = _excel_guard.build_refusal(decision)
    return {
        "answer": answer,
        "sources": [],
        "thread_id": thread_id,
        "log_id": None,               # aucun retrieval n'a eu lieu : aucun log de requête
        "status": "ok",
        "answer_kind": _excel_guard.ANSWER_KIND,
        "suggested_rephrasings": suggestions,
        "analytics": {"requested_intent": decision.intent, "available": False},
    }


def _audit_excel_guard(*, request, rag_space, decision, thread_id: str, request_id: str) -> None:
    """Trace d'audit LÉGÈRE. Aucune cellule, aucune source, aucun contenu de question."""
    try:
        emit_action_event(
            event_name="action.blocked",
            action_name="excel.analytics_blocked",
            status="blocked",
            actor=getattr(request, "user", None),
            workspace_id=str(getattr(rag_space, "workspace_id", "") or ""),
            rag_space_id=getattr(rag_space, "id", None),
            resource_type="chat_thread",
            resource_id=thread_id,
            request_id=request_id,
            detection_method="deterministic_classifier",
            metadata={
                "intent": decision.intent,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "scope": decision.scope,
            },
        )
    except Exception:  # pragma: no cover - l'audit ne bloque jamais une réponse
        logger.debug("excel guard audit failed", exc_info=True)


def _help_answer(match: MatchResult) -> str:
    """Markdown de l'aide, pour un sujet éventuel. Sujet inconnu -> aide de niveau 1."""
    return render_help_markdown(match.parameters.get("topic"))


# ---------------------------------------------------------------------
# L3 — demandes portant sur TOUT le corpus
# ---------------------------------------------------------------------
def _is_corpus_mode_turn(q: str, choice: str | None = None) -> bool:
    """Ce tour relève-t-il du dialogue « rapide / complet » ? (regex seules, aucune DB)

    Un ``choice`` structuré (bouton) suffit à lui seul : il n'y a alors aucun texte
    à reconnaître, et le libellé affiché ne doit surtout pas être interprété.
    """
    if choice:
        return True
    return (
        _corpus_mode.is_global_corpus_request(q)
        or _corpus_mode.is_quick_confirm(q)
        or _corpus_mode.is_full_analysis_request(q)
    )


def _corpus_replay(*, user, thread_id, request_id: str) -> str | None:
    """Réponse déjà produite pour ce ``(thread, request_id)`` — ou ``None``.

    Idempotence des tours L3 : un renvoi du même ``request_id`` rejoue la réponse
    persistée au lieu de relancer un LLM, un RagQueryLog et un second message.
    Volontairement limité aux tours L3 (cf. ``_is_corpus_mode_turn``) : le
    comportement des questions ordinaires reste strictement inchangé.
    """
    if not thread_id or not request_id:
        return None
    from apps.chat.models import ChatMessage, ChatThread

    thread = ChatThread.objects.filter(id=thread_id, user=user).first()
    if thread is None:
        return None
    existing = (
        ChatMessage.objects.filter(thread=thread, request_id=request_id, role="assistant")
        .order_by("created_at")
        .first()
    )
    return existing.content if existing else None


def _link_analysis_to_thread(analysis_id, thread_id) -> None:
    """Lien DURABLE job -> fil, posé une fois le fil connu.

    C'est ce lien qui permet au frontend de reprendre le suivi d'une analyse
    après un rechargement, sans parser le Markdown ni deviner « le dernier job ».
    """
    from apps.rag_core.models.corpus_analysis_job import CorpusAnalysisJob

    CorpusAnalysisJob.objects.filter(pk=analysis_id, chat_thread__isnull=True).update(
        chat_thread_id=thread_id
    )

# ---------------------------------------------------------------------
# Listing intent (ex: "donne moi la liste des articles sur Nordexia")
# ---------------------------------------------------------------------

# NOTE:
# - keep patterns fairly specific to avoid routing normal questions to listing mode
_LISTING_PATTERNS = [
    r"\bliste\b",
    r"\btous les (articles|documents|fichiers)\b",
    r"\bquels (articles|documents|fichiers)\b",
    r"\b(?:articles|documents|fichiers)\s+(?:sur|concernant|à propos de)\b",
    r"\bqui (?:parlent|mentionn(?:e|ent))\b",
]


def _looks_like_listing_query(q: str) -> bool:
    s = (q or "").strip().lower()
    if not s:
        return False
    return any(re.search(p, s) for p in _LISTING_PATTERNS)


def _extract_term_for_listing(q: str) -> str:
    """
    Heuristique simple :
    - "liste des articles sur nordexia" -> "nordexia"
    - "quels documents parlent de groupe nordexia ?" -> "groupe nordexia"
    """
    s = (q or "").strip()
    m = re.search(r"(?:sur|de|concernant|à propos de)\s+(.+)$", s, re.IGNORECASE)
    if m:
        term = m.group(1).strip().strip("\"'")
        term = re.sub(r"[?.!,;:]+$", "", term).strip()
        if term:
            return term

    parts = re.findall(r"[A-Za-z0-9À-ÿ\-_.]+", s)
    return parts[-1] if parts else s


def _format_listing_answer(term: str, hits) -> str:
    term_disp = term.strip() or "ce terme"
    if not hits:
        return f"Je n'ai trouvé aucun document qui mentionne **{term_disp}** dans ce RagSpace."

    lines = [f"Documents qui mentionnent **{term_disp}** :"]
    for h in hits:
        pages = []
        try:
            pages = list(getattr(h, "pages", None) or [])
        except Exception:
            pages = []
        pages_txt = f" (pages: {', '.join(map(str, pages))})" if pages else ""
        lines.append(
            f"- {getattr(h, 'filename', '')}{pages_txt} — {int(getattr(h, 'hits', 0) or 0)} occurrence(s)"
        )
    return "\n".join(lines)


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _sse(event: str, data) -> bytes:
    payload = data
    if not isinstance(payload, str):
        # default=str makes UUID/datetime/Decimal etc. safe for SSE JSON payloads
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    return (f"event: {event}\n" f"data: {payload}\n\n").encode("utf-8")


def _run_listing_search(*, rag_space, term: str, request_data: dict):
    """
    Centralize listing search logic so ChatView and ChatStreamView stay consistent.
    - supports both function-style and class-style repositories (tolerant imports)
    - never silently falls back to RAG: caller decides error strategy
    """
    # Keep backward compat: these fields are NOT in ChatRequestSerializer,
    # so we read them from raw request.data (not validated_data).
    use_fts = str(request_data.get("fts") or "").strip().lower() in ("1", "true", "t", "yes", "y", "on")
    lang = (request_data.get("lang") or "simple").strip() or "simple"
    limit = _safe_int(request_data.get("limit"), 200)
    limit = max(1, min(limit, 1000))

    # Try function-style repo first (recommended)
    try:
        from apps.rag_core.repositories.document_repository import (
            list_documents_mentioning_term,
            list_documents_mentioning_term_fts,
        )

        if use_fts:
            return list_documents_mentioning_term_fts(
                space_id=rag_space.id,
                term=term,
                limit=limit,
                language=lang,
            )
        return list_documents_mentioning_term(
            space_id=rag_space.id,
            term=term,
            limit=limit,
        )
    except Exception:
        # Tolerant fallback: some implementations use a class repository
        from apps.rag_core.repositories.document_repository import DocumentRepository  # type: ignore

        if hasattr(DocumentRepository, "search_documents_by_term"):
            return DocumentRepository.search_documents_by_term(  # type: ignore[attr-defined]
                space_id=rag_space.id,
                term=term,
                limit=limit,
            )

        raise ImportError(
            "No compatible document listing search function found in document_repository.py "
            "(expected list_documents_mentioning_term* or DocumentRepository.search_documents_by_term)"
        )


def _ensure_thread_and_persist_messages(
    *,
    user,
    rag_space,
    thread_id: Optional[str],
    question: str,
    answer: str,
    request_id: str,
    analysis_id: Optional[str] = None,
):
    """
    Persist messages so the frontend doesn't "rollback" when it refetches /messages/.
    Idempotent on (thread_id, request_id, role).
    """
    from apps.chat.models import ChatThread, ChatMessage

    t = None
    if thread_id:
        t = ChatThread.objects.filter(id=thread_id, user=user).select_related("rag_space").first()

    if t is None:
        # Create a thread if none exists (simple title heuristic)
        title = (question or "").strip()
        title = title[:80] if title else None
        t = ChatThread.objects.create(user=user, rag_space=rag_space, title=title)

    # Avoid duplicates on retries
    if not ChatMessage.objects.filter(thread=t, request_id=request_id, role="user").exists():
        ChatMessage.objects.create(
            thread=t,
            role="user",
            content=question,
            sources=[],
            request_id=request_id,
            rag_query_log_id=None,
        )

    if not ChatMessage.objects.filter(thread=t, request_id=request_id, role="assistant").exists():
        ChatMessage.objects.create(
            thread=t,
            role="assistant",
            content=answer,
            sources=[],
            request_id=request_id,
            rag_query_log_id=None,
            analysis_id=analysis_id,
        )

    return t


class EventStreamRenderer(BaseRenderer):
    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class IgnoreClientContentNegotiation(BaseContentNegotiation):
    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        for r in renderers:
            if getattr(r, "media_type", "") == "text/event-stream":
                return (r, r.media_type)
        return (renderers[0], renderers[0].media_type)


class ChatView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rag_space_id = serializer.validated_data.get("rag_space_id")
        # Audit périmètre (1 seul événement par requête, AVANT tout retrieval).
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="chat.sync", request=request,
        )
        rag_space = require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        question = (serializer.validated_data.get("question") or "").strip()
        request_id = (
            getattr(request, "request_id", None)
            or request.headers.get("x-request-id")
            or str(uuid.uuid4())
        )

        # Aide produit — réponse déterministe, AVANT toute logique de recherche.
        help_match = _match_help(question)
        if help_match is not None:
            answer = _help_answer(help_match)
            t = _ensure_thread_and_persist_messages(
                user=request.user,
                rag_space=rag_space,
                thread_id=serializer.validated_data.get("thread_id") or None,
                question=question,
                answer=answer,
                request_id=request_id,
            )
            return Response(
                {
                    "answer": answer,
                    "sources": [],
                    "thread_id": str(t.id),
                    "log_id": None,
                    "status": "ok",
                    "answer_kind": "help",
                },
                status=status.HTTP_200_OK,
            )

        # ------------------------------------------------------------------
        # L3 — demande portant sur TOUT le corpus : clarification déterministe
        # (réponse rapide vs analyse complète) AVANT tout retrieval.
        #
        # Placé ici, dans la vue, en amont de `is_router_enabled()` : le
        # comportement est donc identique routeur ON et OFF. La détection vient
        # du matcher (autorité unique), jamais de regex recopiées ici.
        # ------------------------------------------------------------------
        thread_id_in = serializer.validated_data.get("thread_id") or None
        corpus_choice = serializer.validated_data.get("corpus_choice") or None
        if corpus_choice:
            # Le SERVEUR écrit le message utilisateur. Le navigateur ne dicte pas
            # l'historique, et aucun identifiant technique n'atterrit dans le fil.
            question = _corpus_mode.CHOICE_LABELS[corpus_choice]

        if _is_corpus_mode_turn(question, corpus_choice):
            request_id = (
                getattr(request, "request_id", None)
                or request.headers.get("x-request-id")
                or str(uuid.uuid4())
            )

            # Idempotence : même (thread, request_id) -> on rejoue, on ne recalcule pas.
            replay = _corpus_replay(
                user=request.user, thread_id=thread_id_in, request_id=request_id
            )
            if replay is not None:
                return Response(
                    {
                        "answer": replay,
                        "sources": [],
                        "thread_id": str(thread_id_in),
                        "log_id": None,
                        "status": "ok",
                        "answer_kind": _corpus_mode.ANSWER_KIND_MODE_CHOICE,
                    },
                    status=status.HTTP_200_OK,
                )

            turn = _corpus_mode.resolve_corpus_mode_turn(
                user=request.user,
                rag_space=rag_space,
                thread_id=thread_id_in,
                question=question,
                choice=corpus_choice,
            )

            # Choix envoyé alors que plus rien n'est en attente : déjà consommé, ou
            # le fil a avancé. On n'exécute RIEN et on le dit — le client se resynchronise.
            if turn and turn["kind"] == _corpus_mode.TURN_CHOICE_EXPIRED:
                return Response(
                    {"detail": "corpus_choice_expired"},
                    status=status.HTTP_409_CONFLICT,
                )

            # Clarification / indisponibilité : purement déterministes. Ni retrieval,
            # ni embeddings, ni LLM, ni Celery, ni RagQueryLog.
            if turn and turn["kind"] in (
                _corpus_mode.TURN_MODE_CHOICE,
                _corpus_mode.TURN_FULL_UNAVAILABLE,
                _corpus_mode.TURN_FULL_LAUNCHED,
            ):
                answer = turn["answer"]
                t = _ensure_thread_and_persist_messages(
                    user=request.user,
                    rag_space=rag_space,
                    thread_id=thread_id_in,
                    question=question,
                    answer=answer,
                    request_id=request_id,
                    analysis_id=turn.get("analysis_id"),
                )
                payload = {
                    "answer": answer,
                    "sources": [],
                    "thread_id": str(t.id),
                    "log_id": None,
                    "status": "ok",
                    "answer_kind": turn["answer_kind"],
                }
                if turn.get("coverage") is not None:
                    payload["coverage"] = turn["coverage"]
                if turn["kind"] == _corpus_mode.TURN_MODE_CHOICE:
                    # Les boutons s'affichent sur CE message, et « Analyse complète »
                    # n'est proposée que si le moteur sait réellement la mener.
                    payload["awaiting_corpus_choice"] = True
                    payload["global_analysis_available"] = bool(
                        turn.get("global_search_available")
                    )
                if turn.get("analysis_id"):
                    # Le frontend suit le job avec cet identifiant. Le lien durable
                    # est aussi persisté côté job (chat_thread), pour la reprise
                    # après rechargement du fil.
                    payload["analysis_id"] = turn["analysis_id"]
                    payload["analysis_status"] = turn.get("analysis_status")
                    _link_analysis_to_thread(turn["analysis_id"], t.id)
                return Response(payload, status=status.HTTP_200_OK)

            # Mode rapide confirmé : on rejoue la QUESTION D'ORIGINE dans le RAG
            # classique (l'utilisateur n'a pas à la retaper), le fil persiste ce
            # qu'il a réellement tapé, et l'avertissement de couverture partielle
            # est imposé APRÈS génération — le LLM ne peut pas le retirer.
            if turn and turn["kind"] == _corpus_mode.TURN_QUICK:
                validated = dict(serializer.validated_data)
                validated.pop("corpus_choice", None)  # inconnu de `chat_query`
                validated["question"] = turn["original_question"]
                result = chat_query(
                    user=request.user,
                    rag_space=rag_space,
                    persisted_question=question,
                    answer_prefix=PARTIAL_COVERAGE_WARNING,
                    **validated,
                )
                if isinstance(result, dict):
                    result["answer_kind"] = _corpus_mode.ANSWER_KIND_QUICK
                    result["coverage"] = _corpus_mode.quick_answer_coverage(
                        rag_space=rag_space, sources=result.get("sources") or []
                    )
                    result.setdefault("log_id", None)
                return Response(result, status=status.HTTP_200_OK)

            # Sinon (ex. « réponse rapide » hors contexte) : question ordinaire.

        # ------------------------------------------------------------------
        # E4 — garde analytique Excel. DÉTERMINISTE, et AVANT tout retrieval, tout
        # embedding et tout appel LLM : le modèle ne voit que le top-k, jamais le
        # classeur entier. Additionner ce qu'il voit produirait un chiffre faux
        # présenté comme sûr.
        #
        # Placée ICI, dans la vue, en amont de `is_router_enabled()` — même motif que
        # /help et que le choix corpus : le comportement est donc IDENTIQUE routeur
        # ON et OFF, sans duplication.
        # ------------------------------------------------------------------
        guard_decision = _excel_guard.guard_for_request(
            question=question,
            thread_id=serializer.validated_data.get("thread_id") or None,
            rag_space=rag_space,
        )
        if guard_decision is not None:
            answer, _ = _excel_guard.build_refusal(guard_decision)
            t = _ensure_thread_and_persist_messages(
                user=request.user,
                rag_space=rag_space,
                thread_id=serializer.validated_data.get("thread_id") or None,
                question=question,
                answer=answer,
                request_id=request_id,
            )
            _audit_excel_guard(
                request=request, rag_space=rag_space, decision=guard_decision,
                thread_id=str(t.id), request_id=request_id,
            )
            return Response(
                _excel_guard_payload(guard_decision, thread_id=str(t.id)),
                status=status.HTTP_200_OK,
            )

        # Lot 3 — routeur d'actions central (flag OFF par défaut : bloc ignoré).
        from apps.actions import wiring as _actions_wiring

        if _actions_wiring.is_router_enabled():
            try:
                result = _actions_wiring.run_chat_sync(
                    request=request,
                    user=request.user,
                    rag_space=rag_space,
                    validated_data=serializer.validated_data,
                )
                if isinstance(result, dict) and "log_id" not in result:
                    result["log_id"] = None
                return Response(result, status=status.HTTP_200_OK)
            except _actions_wiring.ListingRouteError as e:
                # Préserve le contrat d'erreur listing historique (§9).
                request_id = (
                    getattr(request, "request_id", None)
                    or request.headers.get("x-request-id")
                    or str(uuid.uuid4())
                )
                logger.exception(
                    "listing_route_failed request_id=%s user_id=%s rag_space_id=%s err=%s",
                    request_id,
                    getattr(request.user, "id", None),
                    rag_space_id,
                    e,
                )
                return Response(
                    {"detail": "listing_route_failed", "request_id": request_id},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
            except Exception:
                request_id = (
                    getattr(request, "request_id", None)
                    or request.headers.get("x-request-id")
                    or str(uuid.uuid4())
                )
                logger.exception(
                    "chat_router_failed request_id=%s user_id=%s rag_space_id=%s",
                    request_id,
                    getattr(request.user, "id", None),
                    rag_space_id,
                )
                return Response(
                    {"detail": "internal_error", "request_id": request_id},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        # Route listing queries to document search (no RAG top-k chunks)
        if _looks_like_listing_query(question):
            term = _extract_term_for_listing(question)
            try:
                hits = _run_listing_search(rag_space=rag_space, term=term, request_data=request.data)
                answer = _format_listing_answer(term, hits)
                return Response(
                    {"answer": answer, "sources": [], "log_id": None},
                    status=status.HTTP_200_OK,
                )
            except Exception as e:
                request_id = (
                    getattr(request, "request_id", None)
                    or request.headers.get("x-request-id")
                    or str(uuid.uuid4())
                )
                logger.exception(
                    "listing_route_failed request_id=%s user_id=%s rag_space_id=%s term=%s err=%s",
                    request_id,
                    getattr(request.user, "id", None),
                    rag_space_id,
                    term,
                    e,
                )
                return Response(
                    {"detail": "listing_route_failed", "request_id": request_id},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        try:
            result = chat_query(
                user=request.user,
                rag_space=rag_space,
                **serializer.validated_data,
            )

            if isinstance(result, dict) and "log_id" not in result:
                result["log_id"] = None

            return Response(result, status=status.HTTP_200_OK)

        except Exception as exc:
            request_id = (
                getattr(request, "request_id", None)
                or request.headers.get("x-request-id")
                or str(uuid.uuid4())
            )

            # une défaillance de l'AI Gateway (directe ou en cause d'une erreur
            # de retrieval/embeddings) devient une erreur NOMMÉE et réessayable ou non,
            # jamais un 500 anonyme. Aucun repli externe n'existe dans l'édition Open ;
            # aucun détail interne ne sort (le journal conserve la trace complète).
            gw = find_gateway_error(exc)
            projected = classify_for_http(gw) if gw is not None else None
            if projected is not None:
                logger.warning(
                    "chat_gateway_failed request_id=%s user_id=%s rag_space_id=%s error=%s http=%s",
                    request_id,
                    getattr(request.user, "id", None),
                    rag_space_id,
                    type(gw).__name__,
                    projected.status,
                )
                resp = Response(projected.payload(request_id=request_id), status=projected.status)
                for k, v in projected.headers().items():
                    resp[k] = v
                return resp

            logger.exception(
                "chat_failed request_id=%s user_id=%s rag_space_id=%s",
                request_id,
                getattr(request.user, "id", None),
                rag_space_id,
            )

            return Response(
                {"detail": "internal_error", "request_id": request_id},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ChatStreamView(APIView):
    permission_classes = [IsAuthenticated]
    renderer_classes = [EventStreamRenderer]
    content_negotiation_class = IgnoreClientContentNegotiation

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rag_space_id = serializer.validated_data.get("rag_space_id")
        # Audit périmètre AVANT le premier événement streaming (1 seul événement,
        # jamais par token).
        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="chat.stream", request=request,
        )
        rag_space = require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        request_id = (
            getattr(request, "request_id", None)
            or request.headers.get("x-request-id")
            or str(uuid.uuid4())
        )

        question = (serializer.validated_data.get("question") or "").strip()
        thread_id = serializer.validated_data.get("thread_id") or None
        thread_id_str = str(thread_id) if thread_id else None
        corpus_choice = serializer.validated_data.get("corpus_choice") or None
        if corpus_choice:
            # Comme en sync : le serveur écrit le message utilisateur.
            question = _corpus_mode.CHOICE_LABELS[corpus_choice]

        def event_iter() -> Iterable[bytes]:
            last_log_id = None

            # ------------------------------------------------------------------
            # Aide produit — réponse déterministe, AVANT le routeur et le listing.
            # Contrat SSE inchangé : meta -> sources -> token -> done.
            # ------------------------------------------------------------------
            help_match = _match_help(question)
            if help_match is not None:
                answer = _help_answer(help_match)
                t = _ensure_thread_and_persist_messages(
                    user=request.user,
                    rag_space=rag_space,
                    thread_id=thread_id_str,
                    question=question,
                    answer=answer,
                    request_id=request_id,
                )
                t_id_str = str(t.id)

                yield _sse("meta", {
                    "request_id": request_id,
                    "thread_id": t_id_str,
                    "log_id": None,
                    "answer_kind": "help",
                })
                yield _sse("sources", {"sources": []})
                yield _sse("token", {"delta": answer})
                yield _sse("done", {
                    "request_id": request_id,
                    "thread_id": t_id_str,
                    "log_id": None,
                    "status": "ok",
                    "answer_kind": "help",
                })
                return

            # ------------------------------------------------------------------
            # L3 — demande portant sur TOUT le corpus. Clarification déterministe
            # AVANT le routeur et le retrieval. Contrat SSE INCHANGÉ :
            # meta -> sources -> token -> done. Aucun nouvel événement.
            # ------------------------------------------------------------------
            if _is_corpus_mode_turn(question, corpus_choice):
                replay = _corpus_replay(
                    user=request.user, thread_id=thread_id_str, request_id=request_id
                )
                turn = None
                if replay is None:
                    turn = _corpus_mode.resolve_corpus_mode_turn(
                        user=request.user,
                        rag_space=rag_space,
                        thread_id=thread_id_str,
                        question=question,
                        choice=corpus_choice,
                    )

                # Choix périmé : rien n'est exécuté, et le libellé du bouton ne part
                # surtout pas dans le RAG. Le client se resynchronisera.
                if turn and turn["kind"] == _corpus_mode.TURN_CHOICE_EXPIRED:
                    yield _sse("error", {
                        "request_id": request_id,
                        "thread_id": thread_id_str,
                        "code": "corpus_choice_expired",
                    })
                    return

                if replay is not None or (
                    turn
                    and turn["kind"]
                    in (
                        _corpus_mode.TURN_MODE_CHOICE,
                        _corpus_mode.TURN_FULL_UNAVAILABLE,
                        _corpus_mode.TURN_FULL_LAUNCHED,
                    )
                ):
                    answer = replay if replay is not None else turn["answer"]
                    answer_kind = (
                        _corpus_mode.ANSWER_KIND_MODE_CHOICE
                        if replay is not None
                        else turn["answer_kind"]
                    )
                    t = _ensure_thread_and_persist_messages(
                        user=request.user,
                        rag_space=rag_space,
                        thread_id=thread_id_str,
                        question=question,
                        answer=answer,
                        request_id=request_id,
                        # `turn` est None sur un rejeu idempotent : ne pas l'appeler
                        # à l'aveugle (c'était une AttributeError en attente).
                        analysis_id=turn.get("analysis_id") if turn else None,
                    )
                    t_id_str = str(t.id)

                    meta = {
                        "request_id": request_id,
                        "thread_id": t_id_str,
                        "log_id": None,
                        "answer_kind": answer_kind,
                    }
                    if turn and turn["kind"] == _corpus_mode.TURN_MODE_CHOICE:
                        meta["awaiting_corpus_choice"] = True
                        meta["global_analysis_available"] = bool(
                            turn.get("global_search_available")
                        )
                    yield _sse("meta", meta)
                    yield _sse("sources", {"sources": []})
                    yield _sse("token", {"delta": answer})
                    done = dict(meta)
                    done["status"] = "ok"
                    if turn and turn.get("coverage") is not None:
                        done["coverage"] = turn["coverage"]
                    if turn and turn.get("analysis_id"):
                        done["analysis_id"] = turn["analysis_id"]
                        done["analysis_status"] = turn.get("analysis_status")
                        _link_analysis_to_thread(turn["analysis_id"], t.id)
                    yield _sse("done", done)
                    return

                if turn and turn["kind"] == _corpus_mode.TURN_QUICK:
                    # Le RAG classique reprend la question d'origine. L'ordre SSE reste
                    # EXACTEMENT meta -> sources -> token -> done : on ne fait que
                    # relayer le flux historique en enrichissant meta/done. L'avertissement
                    # est appliqué par le backend au texte final (`done.answer`, persisté).
                    for evt in chat_query_stream(
                        user=request.user,
                        rag_space=rag_space,
                        request_id=request_id,
                        persisted_question=question,
                        answer_prefix=PARTIAL_COVERAGE_WARNING,
                        **{
                            **{
                                k: v
                                for k, v in serializer.validated_data.items()
                                if k != "corpus_choice"  # inconnu de `chat_query_stream`
                            },
                            "question": turn["original_question"],
                        },
                    ):
                        data = evt.get("data") or {}
                        if evt.get("event") in ("meta", "done"):
                            data["answer_kind"] = _corpus_mode.ANSWER_KIND_QUICK
                        if evt.get("event") == "done":
                            data["coverage"] = _corpus_mode.quick_answer_coverage(
                                rag_space=rag_space, sources=data.get("sources") or []
                            )
                        yield _sse(evt["event"], data)
                    return

                # Sinon (« réponse rapide » hors contexte) : question ordinaire.

            # ------------------------------------------------------------------
            # E4 — garde analytique Excel. Même décision qu'en synchrone, même
            # message, mêmes suggestions. Contrat SSE INCHANGÉ : meta -> sources ->
            # token -> done. Aucun événement dédié : le client rend cette réponse
            # comme un message assistant ordinaire.
            # ------------------------------------------------------------------
            guard_decision = _excel_guard.guard_for_request(
                question=question, thread_id=thread_id_str, rag_space=rag_space,
            )
            if guard_decision is not None:
                payload = _excel_guard_payload(guard_decision, thread_id="")
                t = _ensure_thread_and_persist_messages(
                    user=request.user,
                    rag_space=rag_space,
                    thread_id=thread_id_str,
                    question=question,
                    answer=payload["answer"],
                    request_id=request_id,
                )
                t_id_str = str(t.id)
                _audit_excel_guard(
                    request=request, rag_space=rag_space, decision=guard_decision,
                    thread_id=t_id_str, request_id=request_id,
                )

                yield _sse("meta", {
                    "request_id": request_id,
                    "thread_id": t_id_str,
                    "log_id": None,
                    "answer_kind": _excel_guard.ANSWER_KIND,
                })
                yield _sse("sources", {"sources": []})
                yield _sse("token", {"delta": payload["answer"]})
                yield _sse("done", {
                    "request_id": request_id,
                    "thread_id": t_id_str,
                    "log_id": None,
                    "status": "ok",
                    "answer_kind": _excel_guard.ANSWER_KIND,
                    "suggested_rephrasings": payload["suggested_rephrasings"],
                    "analytics": payload["analytics"],
                })
                return

            # ------------------------------------------------------------------
            # Lot 3 — routeur d'actions central (flag OFF par défaut : ignoré).
            # ------------------------------------------------------------------
            from apps.actions import wiring as _actions_wiring

            if _actions_wiring.is_router_enabled():
                try:
                    for evt in _actions_wiring.run_chat_stream(
                        request=request,
                        user=request.user,
                        rag_space=rag_space,
                        request_id=request_id,
                        validated_data=serializer.validated_data,
                    ):
                        try:
                            if evt.get("event") in ("meta", "done"):
                                last_log_id = (evt.get("data") or {}).get("log_id") or last_log_id
                        except Exception:
                            pass
                        yield _sse(evt["event"], evt["data"])
                except GeneratorExit:
                    return
                except Exception:
                    logger.exception(
                        "chat_router_stream_failed request_id=%s user_id=%s rag_space_id=%s",
                        request_id,
                        getattr(request.user, "id", None),
                        rag_space_id,
                    )
                    msg = "Une erreur interne est survenue."
                    yield _sse("meta", {"request_id": request_id, "thread_id": thread_id_str, "log_id": last_log_id})
                    yield _sse("sources", {"sources": []})
                    yield _sse("token", {"delta": msg})
                    yield _sse("done", {"request_id": request_id, "thread_id": thread_id_str, "log_id": last_log_id, "status": "error"})
                return

            # ------------------------------------------------------------------
            # Listing intent in streaming mode (single-shot SSE)
            # ------------------------------------------------------------------
            if _looks_like_listing_query(question):
                term = _extract_term_for_listing(question)
                try:
                    hits = _run_listing_search(
                        rag_space=rag_space,
                        term=term,
                        request_data=request.data,
                    )
                    answer = _format_listing_answer(term, hits)

                    # Persist so /messages/ contains the new user+assistant messages
                    t = _ensure_thread_and_persist_messages(
                        user=request.user,
                        rag_space=rag_space,
                        thread_id=thread_id_str,
                        question=question,
                        answer=answer,
                        request_id=request_id,
                    )
                    t_id_str = str(t.id)

                    yield _sse("meta", {"request_id": request_id, "thread_id": t_id_str, "log_id": None})
                    yield _sse("sources", {"sources": []})
                    yield _sse("token", {"delta": answer})
                    yield _sse(
                        "done",
                        {
                            "request_id": request_id,
                            "thread_id": t_id_str,
                            "log_id": None,
                            "status": "ok",
                        },
                    )
                    return

                except Exception as e:
                    logger.exception(
                        "listing_stream_route_failed request_id=%s user_id=%s rag_space_id=%s term=%s err=%s",
                        request_id,
                        getattr(request.user, "id", None),
                        rag_space_id,
                        term,
                        e,
                    )

                    msg = (
                        "Désolé, je n’ai pas réussi à produire la liste des documents demandée. "
                        "Veuillez réessayer dans un instant."
                    )

                    # Try to persist the user message + error response (best effort)
                    try:
                        t = _ensure_thread_and_persist_messages(
                            user=request.user,
                            rag_space=rag_space,
                            thread_id=thread_id_str,
                            question=question,
                            answer=msg,
                            request_id=request_id,
                        )
                        t_id_str = str(t.id)
                    except Exception:
                        t_id_str = thread_id_str

                    yield _sse("meta", {"request_id": request_id, "thread_id": t_id_str, "log_id": None})
                    yield _sse("sources", {"sources": []})
                    yield _sse("token", {"delta": msg})
                    yield _sse(
                        "done",
                        {
                            "request_id": request_id,
                            "thread_id": t_id_str,
                            "log_id": None,
                            "status": "error",
                        },
                    )
                    return

            # ------------------------------------------------------------------
            # Standard RAG streaming
            # ------------------------------------------------------------------
            try:
                for evt in chat_query_stream(
                    user=request.user,
                    rag_space=rag_space,
                    request_id=request_id,
                    **serializer.validated_data,
                ):
                    try:
                        if evt.get("event") in ("meta", "done"):
                            last_log_id = (evt.get("data") or {}).get("log_id") or last_log_id
                    except Exception:
                        pass

                    yield _sse(evt["event"], evt["data"])

            except GeneratorExit:
                return

            except Exception as exc:
                # défaillance de l'AI Gateway => événement `error` NOMMÉ puis `done`
                # (status error), sans texte fabriqué ni détail interne. Le frontend affiche
                # l'erreur comme telle (kind "error"), au lieu d'une phrase générique.
                gw = find_gateway_error(exc)
                projected = classify_for_http(gw) if gw is not None else None
                if projected is not None:
                    logger.warning(
                        "chat_stream_gateway_failed request_id=%s user_id=%s rag_space_id=%s error=%s",
                        request_id,
                        getattr(request.user, "id", None),
                        rag_space_id,
                        type(gw).__name__,
                    )
                    yield _sse("meta", {"request_id": request_id, "thread_id": thread_id_str, "log_id": last_log_id})
                    yield _sse("sources", {"sources": []})
                    yield _sse(
                        "error",
                        {
                            **projected.payload(request_id=request_id),
                            "thread_id": thread_id_str,
                            "retry_after": int(projected.headers().get("Retry-After", 0)) or None,
                        },
                    )
                    yield _sse(
                        "done",
                        {
                            "request_id": request_id,
                            "thread_id": thread_id_str,
                            "log_id": last_log_id,
                            "status": "error",
                        },
                    )
                    return

                logger.exception(
                    "chat_stream_failed request_id=%s user_id=%s rag_space_id=%s",
                    request_id,
                    getattr(request.user, "id", None),
                    rag_space_id,
                )

                msg = "Une erreur interne est survenue."

                yield _sse("meta", {"request_id": request_id, "thread_id": thread_id_str, "log_id": last_log_id})
                yield _sse("sources", {"sources": []})
                yield _sse("token", {"delta": msg})
                yield _sse(
                    "done",
                    {
                        "request_id": request_id,
                        "thread_id": thread_id_str,
                        "log_id": last_log_id,
                        "status": "error",
                    },
                )
                return

        resp = StreamingHttpResponse(event_iter(), content_type="text/event-stream; charset=utf-8")
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        resp["X-Request-ID"] = request_id
        return resp


class ChatThreadsView(APIView):
    """
    GET  /api/v1/chat/threads/?rag_space_id=...
    POST /api/v1/chat/threads/ {rag_space_id, title?}
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rag_space_id = (request.query_params.get("rag_space_id") or "").strip()
        if not rag_space_id:
            return Response({"detail": "rag_space_id_required"}, status=400)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="chat.threads.list", request=request,
        )
        rag_space = require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        from django.db.models import Q

        from apps.chat.models import ChatThread

        qs = ChatThread.objects.filter(user=request.user, rag_space=rag_space)
        # Accès progressif au-delà des 100 plus récents (NAVIGATION-SEARCH-1) : `before`
        # = id d'un fil de CET utilisateur dans CET espace ; la page suivante contient
        # les fils strictement plus anciens dans l'ordre (updated_at DESC, id DESC).
        # Sans `before`, réponse inchangée.
        before = (request.query_params.get("before") or "").strip()
        if before:
            try:
                before_id = uuid.UUID(before)
            except ValueError:
                return Response({"detail": "invalid_parameter"}, status=400)
            anchor = qs.filter(id=before_id).only("id", "updated_at").first()
            if anchor is None:
                return Response({"detail": "not_found"}, status=404)
            qs = qs.filter(Q(updated_at__lt=anchor.updated_at)
                           | Q(updated_at=anchor.updated_at, id__lt=anchor.id))
        qs = (
            qs.order_by("-updated_at", "-id")
            .only("id", "rag_space_id", "title", "created_at", "updated_at")[:100]
        )

        data = [
            {
                "id": t.id,
                "rag_space_id": t.rag_space_id,
                "title": t.title,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
            for t in qs
        ]
        return Response(data, status=200)

    def post(self, request):
        serializer = ChatThreadCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rag_space_id = serializer.validated_data["rag_space_id"]
        title = (serializer.validated_data.get("title") or "").strip() or None

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=rag_space_id, permission="read",
            endpoint="chat.threads.create", request=request,
        )
        rag_space = require_ragspace_access(
            user=request.user,
            rag_space_id=rag_space_id,
            permission="read",
        )

        from apps.chat.models import ChatThread

        t = ChatThread.objects.create(user=request.user, rag_space=rag_space, title=title)
        out = {
            "id": t.id,
            "rag_space_id": t.rag_space_id,
            "title": t.title,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }
        return Response(out, status=201)


class ChatThreadMessagesView(APIView):
    """
    GET /api/v1/chat/threads/<thread_id>/messages/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        from apps.chat.models import ChatThread, ChatMessage

        thread = (
            ChatThread.objects.filter(id=thread_id, user=request.user)
            .select_related("rag_space")
            .first()
        )
        if not thread:
            return Response({"detail": "not_found"}, status=404)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=thread.rag_space_id, permission="read",
            endpoint="chat.thread.messages", request=request,
        )
        require_ragspace_access(
            user=request.user,
            rag_space_id=thread.rag_space_id,
            permission="read",
        )

        msgs = (
            ChatMessage.objects.filter(thread=thread)
            .only(
                "id",
                "thread_id",
                "role",
                "content",
                "sources",
                "request_id",
                "rag_query_log_id",
                "analysis_id",
                "kind",
                "status",
                "document_id",
                "summary_id",
                "created_at",
            )
            # Départage stable : deux messages écrits dans la même transaction
            # (demande + réponse d'un résumé) doivent toujours ressortir dans le
            # même ordre, sans dépendre de l'ordre physique des lignes.
            .order_by("created_at", "id")[:500]
        )

        data = [
            {
                "id": m.id,
                "thread_id": m.thread_id,
                "role": m.role,
                "content": m.content,
                "sources": m.sources,
                "request_id": m.request_id,
                "rag_query_log_id": str(m.rag_query_log_id) if m.rag_query_log_id else None,
                "analysis_id": str(m.analysis_id) if m.analysis_id else None,
                # Chaînes vides pour les messages antérieurs à la migration : le
                # frontend dégrade alors vers le rendu assistant standard.
                "kind": m.kind or "",
                "status": m.status or "",
                "document_id": str(m.document_id) if m.document_id else None,
                "summary_id": str(m.summary_id) if m.summary_id else None,
                "awaiting_corpus_choice": False,
                "global_analysis_available": False,
                "created_at": m.created_at,
            }
            for m in msgs
        ]

        # Lot 3 — un choix de mode encore en attente ? C'est le SERVEUR qui le dit,
        # en recalculant l'état comme le fait le chemin d'écriture. Rien n'est stocké,
        # aucun Markdown n'est analysé. Un choix déjà consommé fait avancer le fil,
        # donc plus rien n'est en attente, donc les boutons ne reviennent pas.
        pending = _corpus_mode.pending_corpus_choice(
            user=request.user, thread_id=thread.id
        )
        if pending and data and data[-1]["role"] == ChatMessage.ROLE_ASSISTANT:
            data[-1]["awaiting_corpus_choice"] = True
            data[-1]["global_analysis_available"] = bool(pending["global_search_available"])

        return Response(data, status=200)


class ChatRephrasingSuggestionsView(APIView):
    """
    GET /api/v1/chat/threads/<thread_id>/rephrasing-suggestions/?request_id=<rid>

    Récupère les suggestions de reformulation générées de façon ASYNCHRONE (F2) après
    un ``no_answer``. Réponse : {status: none|pending|ready|failed, suggestions: []}.
    Mêmes permissions que le thread (anti-énumération : 404 si non possédé). N'expose
    jamais l'erreur interne (error_code borné non renvoyé).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        from apps.chat.models import ChatThread, RephrasingSuggestion

        thread = (
            ChatThread.objects.filter(id=thread_id, user=request.user)
            .select_related("rag_space")
            .first()
        )
        if not thread:
            return Response({"detail": "not_found"}, status=404)  # anti-énumération

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=thread.rag_space_id, permission="read",
            endpoint="chat.thread.suggestions", request=request,
        )
        require_ragspace_access(
            user=request.user,
            rag_space_id=thread.rag_space_id,
            permission="read",
        )

        request_id = (request.query_params.get("request_id") or "").strip()
        qs = RephrasingSuggestion.objects.filter(thread=thread)
        row = (
            qs.filter(request_id=request_id).first()
            if request_id
            else qs.order_by("-created_at").first()
        )
        if row is None:
            return Response({"status": "none", "suggestions": []}, status=200)

        suggestions = list(row.suggestions or []) if row.status == RephrasingSuggestion.STATUS_READY else []
        return Response({"status": row.status, "suggestions": suggestions}, status=200)


class ChatThreadSearchView(APIView):
    """
    GET /api/v1/chat/threads/search/?q=&scope=all|title&rag_space_id=&workspace_id=&limit=&cursor=

    Conversations de l'UTILISATEUR courant, sur ses espaces lisibles des workspaces
    actifs (cf. `apps.chat.services.thread_search`). Lecture seule, aucun total.
    Débit : scope DÉDIÉ `chat_thread_search` qui REMPLACE les classes par défaut —
    la frappe ne consomme jamais le seau `user` de `/auth/me/` (incident 429 →
    faux logout).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "chat_thread_search"

    def get(self, request):
        from apps.chat.services import thread_search as TS

        params = request.query_params
        try:
            limit = int(params.get("limit") or TS.DEFAULT_LIMIT)
            rag_space_id = params.get("rag_space_id") or None
            if rag_space_id is not None:
                rag_space_id = uuid.UUID(rag_space_id)
            workspace_id = params.get("workspace_id") or None
            if workspace_id is not None:
                workspace_id = int(workspace_id)
        except ValueError:
            return Response({"detail": "invalid_parameter"}, status=400)
        if not 1 <= limit <= TS.MAX_LIMIT:
            return Response({"detail": "invalid_parameter"}, status=400)
        try:
            page = TS.search_threads(
                request.user, params.get("q") or "", scope=params.get("scope") or "all",
                rag_space_id=rag_space_id, workspace_id=workspace_id, limit=limit,
                cursor=params.get("cursor") or None,
            )
        except TS.SearchError as exc:
            return Response({"detail": exc.code}, status=400)
        except TS.SearchTimeout:
            return Response({"detail": "search_timeout"}, status=503)
        return Response({"results": page.results, "next": page.next}, status=200)


class ChatThreadDetailView(APIView):
    """
    GET   /api/v1/chat/threads/<thread_id>/   (résolution d'un lien direct)
    PATCH /api/v1/chat/threads/<thread_id>/ { title }
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, thread_id):
        """Contexte d'ouverture d'un fil : espace et workspace, rien de plus. Mêmes gardes,
        même ordre que `ChatThreadMessagesView.get` : un UUID connu ne suffit jamais."""
        from apps.chat.models import ChatThread

        t = (ChatThread.objects.filter(id=thread_id, user=request.user)
             .select_related("rag_space").first())
        if not t:
            return Response({"detail": "not_found"}, status=404)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=t.rag_space_id, permission="read",
            endpoint="chat.thread.detail", request=request,
        )
        require_ragspace_access(
            user=request.user,
            rag_space_id=t.rag_space_id,
            permission="read",
        )
        out = {
            "id": t.id,
            "title": t.title,
            "rag_space_id": t.rag_space_id,
            "workspace_id": t.rag_space.workspace_id,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }
        return Response(out, status=200)

    def patch(self, request, thread_id):
        title = (request.data.get("title") or "").strip() or None

        from apps.chat.models import ChatThread

        t = ChatThread.objects.filter(id=thread_id, user=request.user).first()
        if not t:
            return Response({"detail": "not_found"}, status=404)

        from apps.workspaces.services import scope_audit
        scope_audit.emit_ragspace_decision(
            user=request.user, rag_space_id=str(t.rag_space_id), permission="manage",
            endpoint="chat.thread.patch", request=request,
        )
        require_ragspace_access(
            user=request.user,
            rag_space_id=str(t.rag_space_id),
            permission="read",
        )

        t.title = title
        t.save(update_fields=["title", "updated_at"])

        out = {
            "id": t.id,
            "rag_space_id": t.rag_space_id,
            "title": t.title,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }
        return Response(out, status=200)


# ---------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------

from apps.audit.models import RagQueryLog  # noqa: E402


class ChatFeedbackView(APIView):
    """Clic sur le pouce — création d'un signal DURABLE.

    Trois propriétés non négociables sur ce chemin :

    1. **Rapide** : aucune I/O coûteuse, aucun appel LLM, aucune recherche
       vectorielle. Tout ce qui coûte part dans une tâche Celery enfilée APRÈS
       le commit (``transaction.on_commit``).
    2. **Non destructif** : un second envoi ne réécrit plus silencieusement le
       premier — il répond ``409`` et indique quoi modifier (``PATCH``). Écraser
       sans trace faisait disparaître un signalement au profit d'un autre.
    3. **Compatible** : l'ancien payload (``reason``, doublons ``feedback_*``)
       reste accepté et traduit. Un frontend non redéployé continue de
       fonctionner.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "feedback_create"

    def post(self, request):
        from apps.feedback.serializers import FeedbackAckSerializer, FeedbackCreateSerializer
        from apps.feedback.services.create_feedback import (
            FeedbackAlreadyExists,
            create_feedback,
            normalize_kind,
        )

        serializer = FeedbackCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "log_id and score (1 or -1) required", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data

        log = RagQueryLog.objects.filter(id=data["log_id"], user=request.user).first()
        if log is None:
            # 404 volontairement indistinct de « log inexistant » : révéler
            # l'existence du log d'un autre utilisateur serait une fuite.
            return Response({"detail": "log not found"}, status=status.HTTP_404_NOT_FOUND)

        kind = normalize_kind(kind=data.get("kind"), legacy_reason=data.get("reason"))

        try:
            feedback = create_feedback(
                user=request.user,
                log=log,
                rating=data["score"],
                kind=kind,
                comment=(data.get("comment") or "").strip(),
                expected_document=(data.get("expected_document") or "").strip(),
                expected_term=(data.get("expected_term") or "").strip(),
                message_id=data.get("message_id"),
                thread_id=data.get("thread_id"),
            )
        except FeedbackAlreadyExists as exc:
            return Response(
                {
                    "detail": "feedback already exists for this answer",
                    "code": "feedback_exists",
                    "feedback_id": str(exc.existing.id),
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Double écriture TRANSITOIRE des colonnes historiques : les tableaux de
        # bord et requêtes SQL existants lisent encore RagQueryLog.feedback_*.
        # Pilotée par un drapeau pour pouvoir couper la transition sans code.
        if getattr(settings, "FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG", True):
            RagQueryLog.objects.filter(id=log.id).update(
                feedback_score=feedback.rating,
                feedback_reason=_legacy_reason_for(kind),
                feedback_comment=feedback.comment[:2000],
            )

        payload = FeedbackAckSerializer(feedback).data
        # `ok` conservé : contrat de réponse historique du frontend.
        return Response({"ok": True, **payload}, status=status.HTTP_200_OK)


#: Traduction INVERSE vers les motifs historiques, pour la double écriture.
#: `wrong_language` n'a pas d'équivalent ancien : il devient `other` plutôt
#: qu'une valeur inventée que les requêtes existantes ne sauraient pas lire.
_LEGACY_REASON_BY_KIND = {
    "info_in_docs": "missing_sources",
    "wrong_sources": "irrelevant",
    "wrong_answer": "incorrect",
    "wrong_language": "other",
    "other": "other",
}


def _legacy_reason_for(kind: str) -> str:
    return _LEGACY_REASON_BY_KIND.get(kind or "", "")


class ChatFeedbackDetailView(APIView):
    """Relecture et correction EXPLICITE d'un feedback existant.

    C'est la contrepartie du ``409`` : l'utilisateur qui veut préciser son
    signalement le modifie ici, sciemment, plutôt que d'écraser l'ancien par un
    second POST dont personne ne saurait qu'il a remplacé quelque chose.
    """

    permission_classes = [IsAuthenticated]

    def _own(self, request, feedback_id):
        from apps.feedback.models import AnswerFeedback

        return AnswerFeedback.objects.filter(id=feedback_id, created_by=request.user).first()

    def get(self, request, feedback_id):
        from apps.feedback.serializers import FeedbackAckSerializer

        feedback = self._own(request, feedback_id)
        if feedback is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(FeedbackAckSerializer(feedback).data, status=200)

    def patch(self, request, feedback_id):
        from apps.feedback.serializers import FeedbackAckSerializer, FeedbackUpdateSerializer
        from apps.feedback.services.create_feedback import normalize_kind, update_feedback

        feedback = self._own(request, feedback_id)
        if feedback is None:
            return Response({"detail": "not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = FeedbackUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        kind = None
        if "kind" in data:
            kind = normalize_kind(kind=data.get("kind"), legacy_reason=None)

        update_feedback(
            feedback,
            kind=kind,
            comment=data.get("comment"),
            expected_document=data.get("expected_document"),
            expected_term=data.get("expected_term"),
        )
        return Response(FeedbackAckSerializer(feedback).data, status=200)

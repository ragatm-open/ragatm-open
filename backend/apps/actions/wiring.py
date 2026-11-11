# backend/apps/actions/wiring.py
"""Câblage du routeur central sur les chemins de chat live (lot 3).

Activé par ``settings.ACTIONS_ROUTER_ENABLED`` (OFF par défaut). Principe : la
**détection** est centralisée (``ActionRouter``), l'**exécution** réutilise le code
historique — aucun retrieval/génération/listing n'est réimplémenté.

Table de délégation (flag ON) :
  - search.answer / search.news  -> ``chat_query`` / ``chat_query_stream`` VERBATIM
       (la détection news reste interne à chat_query : mêmes appels LLM/retrieval).
  - search.list_documents        -> helpers de listing historiques (parité par
       transport : sync = pas de thread/log ; SSE = thread + persistance).
  - document.summarize           -> CLARIFICATION (aucun handler exécuté).
       L'exécution backend du résumé est différée à un lot dédié (surface nouvelle,
       tests propres requis ; l'UX live reste couverte par le frontend).

Imports paresseux de ``apps.chat`` / ``api.v1.chat.views`` : évite tout cycle au
chargement (la vue importe ce module) et garde le flag OFF sans effet de bord.
"""
from __future__ import annotations

from typing import Any, Iterable

from django.conf import settings

from .plan import ActionContext
from .resolvers import resolve_summary_target
from .router import ActionRouter

# Routeur déterministe par défaut ; le classifieur LLM (lot 4) n'est injecté que si
# ``ACTIONS_LLM_CLASSIFIER_ENABLED`` (sinon None -> comportement déterministe strict).
_router = ActionRouter()


class ListingRouteError(Exception):
    """Échec du repository de listing — mappé par la vue vers ``listing_route_failed``
    (préserve le contrat d'erreur historique quand le flag est actif)."""


def is_router_enabled() -> bool:
    return bool(getattr(settings, "ACTIONS_ROUTER_ENABLED", False))


def _decide(question: str):
    """Route la question. Une SEULE classification par requête (sync et SSE passent
    tous deux par ici, chacun une fois -> jamais de double classification)."""
    from .classifier import get_classifier

    classifier = get_classifier()  # None si désactivé
    router = _router if classifier is None else ActionRouter(llm_classifier=classifier)
    return router.route(question, ActionContext())


# ---------------------------------------------------------------------------
# Clarification résumé (aucun handler métier exécuté)
# ---------------------------------------------------------------------------
def _clarify_message(plan) -> str:
    ref = (plan.parameters or {}).get("ref_kind")
    if ref == "source":
        return (
            "Je n’ai pas les sources d’une réponse précédente ici. Posez d’abord "
            "votre question, puis utilisez le bouton « Résumer » sur une source."
        )
    if ref == "filename":
        name = (plan.parameters or {}).get("document_reference") or "ce document"
        return (
            f"Pour résumer « {name} », ouvrez le document et utilisez « Résumer », "
            "ou précisez le document exact."
        )
    return (
        "Quel document souhaitez-vous résumer ? Précisez son nom, ou utilisez le "
        "bouton « Résumer » sur une source."
    )


def _summarize_missing(plan) -> list[str]:
    return list(plan.missing_parameters) or ["document_reference"]


def _request_id_for(request) -> str:
    """Corrélation de requête, même convention que les vues de chat."""
    import uuid as _uuid

    return (
        getattr(request, "request_id", None)
        or (request.headers.get("x-request-id") if hasattr(request, "headers") else None)
        or str(_uuid.uuid4())
    )


# ---------------------------------------------------------------------------
# Résumé backend adaptatif — UN SEUL chemin métier (sync + SSE partagent ceci)
# ---------------------------------------------------------------------------
def _execute_summary(*, user, rag_space, thread_id, plan, request_id: str = "") -> dict:
    """Résout la cible puis délègue au service de résumé existant.

    Retourne un dict normalisé : ``kind`` ∈ {summary, pending, clarification, error}.
    Aucune génération réimplémentée : ``summarize_document`` est réutilisé tel quel.
    """
    target = resolve_summary_target(user=user, rag_space=rag_space, thread_id=thread_id, plan=plan)

    if target.status != "resolved":
        return {
            "kind": "clarification",
            "answer": target.message or _clarify_message(plan),
            "candidates": target.candidates or ["document.summarize"],
            "missing": target.missing or _summarize_missing(plan),
        }

    doc = target.document
    from .resolvers import SOURCE_TYPE_DOMAIN_CORPUS

    is_corpus = target.source_type == SOURCE_TYPE_DOMAIN_CORPUS

    # Défense en profondeur : re-valide l'accès au document, par le résolveur de SA
    # famille. Aucun essai croisé — un identifiant de corpus n'est jamais présenté
    # à `load_accessible_document`, qui est la porte des vues privées.
    try:
        if is_corpus:
            from apps.corpus.services.access import load_accessible_domain_corpus_document

            load_accessible_domain_corpus_document(
                user=user, workspace=getattr(rag_space, "workspace", None), document_id=doc.id
            )
        else:
            from api.v1.documents.views_common import load_accessible_document

            load_accessible_document(user, doc.id, write=False)
    except Exception:
        # Ne révèle ni le nom ni l'existence : clarification générique.
        return {
            "kind": "clarification",
            "answer": "Je n’ai pas trouvé de document résumable correspondant dans cet espace.",
            "candidates": ["document.summarize"],
            "missing": ["document_reference"],
        }

    from apps.chat.services.summary_messages import (
        SOURCE_TYPE_DOMAIN_CORPUS as MSG_SOURCE_TYPE_DOMAIN_CORPUS,
        SOURCE_TYPE_WORKSPACE as MSG_SOURCE_TYPE_WORKSPACE,
        SummaryRequestConflict,
        build_summary_meta,
        fail_message,
        get_or_create_summary_exchange,
        resolve_thread_for_summary,
        settle_for_result,
    )
    from apps.rag_core.services.summary_service import (
        SummaryError,
        resolve_summary_language,
        summarize_document,
    )

    # Langue concrète du résumé à venir : les textes déterministes ci-dessous
    # (placeholder, échec, document non prêt) doivent la suivre, sinon un résumé
    # anglais s'accompagne de messages français dans le même fil.
    answer_language = resolve_summary_language("auto", user=user)

    # Même action métier que la vue REST => mêmes invariants : fil validé, message
    # persisté à la date de la demande, idempotence par request_id. Sans cela, un
    # résumé demandé en langage naturel disparaîtrait du fil au rechargement,
    # alors que le même résumé demandé par le bouton y resterait.
    msg = None
    thread = resolve_thread_for_summary(
        user=user, thread_id=thread_id,
        expected_rag_space_id=getattr(rag_space, "id", None),
    )
    if thread is not None and request_id:
        if is_corpus:
            from api.v1.corpus_documents.views import summary_user_label

            source_ref = {
                "source_type": MSG_SOURCE_TYPE_DOMAIN_CORPUS,
                "document_id": str(doc.id),
                "corpus_id": str(doc.corpus_id),
                "filename": doc.display_title,
                "page_number": target.page if target.scope == "page" else None,
            }
        else:
            from api.v1.documents.views_write import summary_user_label

            source_ref = {
                "source_type": MSG_SOURCE_TYPE_WORKSPACE,
                "document_id": str(doc.id),
                "filename": doc.filename,
                "page_number": target.page if target.scope == "page" else None,
            }

        try:
            msg, _created = get_or_create_summary_exchange(
                thread=thread,
                request_id=request_id,
                document=doc,
                user_label=summary_user_label(doc, target.scope, target.page),
                answer_language=answer_language,
                source=source_ref,
                meta=build_summary_meta(
                    source=source_ref, scope=target.scope, mode="bullets",
                    language=answer_language,
                ),
            )
        except SummaryRequestConflict:
            # Même `request_id` déjà employé pour une autre cible : on ne rend
            # surtout pas le résumé de l'autre document sous cette demande.
            return {
                "kind": "error",
                "answer": _summary_generic_failure(answer_language),
                "detail": "request_id_conflict",
            }

    try:
        if is_corpus:
            from apps.corpus.services.summary import summarize_corpus_document

            res = summarize_corpus_document(
                user=user,
                workspace=getattr(rag_space, "workspace", None),
                rag_space=rag_space,
                document=doc,
                scope=target.scope,
                page=target.page,
                allow_async=True,
            )
        else:
            res = summarize_document(
                user=user,
                document=doc,
                scope=target.scope,
                page=target.page,
                allow_async=True,  # adaptatif : sync/cache/tâche selon le document
            )
    except SummaryError as e:
        if msg is not None:
            fail_message(
                msg, error=getattr(e, "code", "summary_error"), answer_language=answer_language
            )
        return {
            "kind": "error",
            "answer": _summary_error_message(e, answer_language),
            "detail": getattr(e, "code", "summary_error"),
        }

    if msg is not None:
        if is_corpus:
            from api.v1.corpus_documents.views import _load_corpus_summary_row

            msg = settle_for_result(msg, res, load_row=_load_corpus_summary_row)
        else:
            msg = settle_for_result(msg, res)

    payload = _serialize_summary_safe(doc, res, is_corpus=is_corpus)
    if msg is not None:
        payload["message_id"] = str(msg.id)
        payload["request_id"] = request_id
    lang = getattr(res, "language", None)
    if res.status == "pending":
        return {
            "kind": "pending",
            "answer": (
                "The summary is being generated. It will appear shortly."
                if lang == "en"
                else "Le résumé est en cours de génération. Il apparaîtra dans un instant."
            ),
            "summary": payload,
            "log_id": res.log_id,
        }
    if res.status == "failed":
        return {
            "kind": "error",
            "answer": (
                "Summary generation failed. Please try again in a moment."
                if lang == "en"
                else "La génération du résumé a échoué. Réessayez dans un instant."
            ),
            "detail": "summary_failed",
        }
    return {
        "kind": "summary",
        "answer": res.summary,
        "summary": payload,
        "log_id": res.log_id,
    }


def _serialize_summary_safe(doc, res, *, is_corpus: bool = False) -> dict:
    """Réutilise le sérialiseur de résumé de la BONNE famille de documents.

    Le sérialiseur privé lit ``doc.filename``, absent d'un ``DomainCorpusDocument``.
    Sans ce branchement, un résumé de corpus serait servi par le repli minimal — et
    le fil perdrait le nom du document et sa provenance.
    """
    try:
        if is_corpus:
            from api.v1.corpus_documents.views import _serialize_summary as _serialize_corpus

            return _serialize_corpus(doc, res)
        from api.v1.documents.views_write import _serialize_summary

        return _serialize_summary(doc, res)
    except Exception:  # pragma: no cover - repli minimal
        return {"document_id": str(doc.id), "status": res.status, "summary_id": res.summary_id}


def _summary_generic_failure(answer_language: str | None = None) -> str:
    """Message d'échec NEUTRE : ne dit ni pourquoi, ni sur quel document."""
    if answer_language == "en":
        return "This summary request could not be processed. Please try again."
    return "Cette demande de résumé n’a pas pu être traitée. Réessayez."


def _summary_error_message(e, answer_language: str | None = None) -> str:
    code = getattr(e, "code", "") or ""
    if answer_language == "en":
        if code == "document_not_ready":
            return "The document is not ready yet (indexing in progress)."
        return "This document cannot be summarised at the moment."
    if code == "document_not_ready":
        return "Le document n’est pas encore prêt (indexation en cours)."
    return "Impossible de résumer ce document pour le moment."


def _summary_sync_dict(*, out: dict, thread_id) -> dict:
    """Adapte le résultat métier vers le contrat de chat SYNC."""
    tid = str(thread_id) if thread_id else None
    kind = out["kind"]
    if kind == "summary":
        return {
            "status": "ok", "answer_kind": "summary", "answer": out["answer"],
            "sources": [], "thread_id": tid, "log_id": out.get("log_id"),
            "summary": out.get("summary"),
        }
    if kind == "pending":
        return {
            "status": "pending", "answer_kind": "summary", "answer": out["answer"],
            "sources": [], "thread_id": tid, "log_id": out.get("log_id"),
            "summary": out.get("summary"),
        }
    if kind == "error":
        return {
            "status": "error", "answer_kind": "summary", "answer": out["answer"],
            "sources": [], "thread_id": tid, "log_id": None, "detail": out.get("detail"),
        }
    # clarification
    return {
        "status": "clarification", "answer_kind": "clarification", "answer": out["answer"],
        "sources": [], "candidates": out.get("candidates", ["document.summarize"]),
        "missing_parameters": out.get("missing", ["document_reference"]),
        "thread_id": tid, "log_id": None,
    }


def _summary_stream_events(*, out: dict, thread_id, request_id):
    """Adapte le résultat métier vers des événements SSE (mêmes que le chat)."""
    tid = str(thread_id) if thread_id else None
    kind = out["kind"]
    answer_kind = "summary" if kind in ("summary", "pending") else (
        "clarification" if kind == "clarification" else "summary"
    )
    status_val = {"summary": "ok", "pending": "pending", "error": "error", "clarification": "clarification"}[kind]

    yield {"event": "meta", "data": {"request_id": request_id, "thread_id": tid, "log_id": out.get("log_id")}}
    yield {"event": "sources", "data": {"sources": []}}
    yield {"event": "token", "data": {"delta": out["answer"]}}
    done = {
        "request_id": request_id, "thread_id": tid, "log_id": out.get("log_id"),
        "status": status_val, "answer_kind": answer_kind,
    }
    if kind == "clarification":
        done["candidates"] = out.get("candidates", ["document.summarize"])
        done["missing_parameters"] = out.get("missing", ["document_reference"])
    if kind in ("summary", "pending"):
        done["summary"] = out.get("summary")
    if kind == "error":
        done["detail"] = out.get("detail")
    yield {"event": "done", "data": done}


# ---------------------------------------------------------------------------
# SYNC
# ---------------------------------------------------------------------------
def run_chat_sync(*, request, user, rag_space, validated_data: dict) -> dict:
    question = (validated_data.get("question") or "").strip()
    plan = _decide(question)

    # Aide produit — normalement déjà interceptée par la vue (donc active même avec
    # le flag OFF). Cette branche garantit qu'un plan ``help.capabilities`` ne puisse
    # JAMAIS retomber dans ``chat_query`` (retrieval + LLM) si le chemin d'appel change.
    if plan.action == "help.capabilities":
        from apps.help.catalog import render_help_markdown

        thread_id = validated_data.get("thread_id")
        return {
            # Même sujet que le chemin principal : le filet ne doit jamais rendre
            # autre chose que ce que la vue aurait rendu.
            "answer": render_help_markdown(plan.parameters.get("topic")),
            "sources": [],
            "status": "ok",
            "answer_kind": "help",
            "thread_id": str(thread_id) if thread_id else None,
            "log_id": None,
        }

    if plan.action == "corpus.global_request":
# Garde-fou : un plan ``corpus.global_request`` ne doit JAMAIS retomber dans
# ``chat_query`` (retrieval + LLM). La vue l'intercepte déjà en amont (donc actif
# flag ON comme OFF) ; cette branche protège contre un changement de chemin d'appel.
        from apps.chat.services import corpus_mode_service as cms
        from apps.rag_core.domain.corpus_mode import render_corpus_mode_choice_markdown
        from apps.rag_core.domain.coverage import coverage_to_dict
        from apps.rag_core.services.coverage_service import compute_rag_space_statistics

        stats = compute_rag_space_statistics(rag_space.id)
        coverage = stats["coverage"]
        thread_id = validated_data.get("thread_id")
        return {
            "answer": render_corpus_mode_choice_markdown(statistics=stats, coverage=coverage),
            "sources": [],
            "status": "ok",
            "answer_kind": cms.ANSWER_KIND_MODE_CHOICE,
            "coverage": coverage_to_dict(coverage),
            "thread_id": str(thread_id) if thread_id else None,
            "log_id": None,
        }

    if plan.action == "search.list_documents":
        from api.v1.chat import views as cv

        term = cv._extract_term_for_listing(question)  # extraction historique (parité)
        try:
            hits = cv._run_listing_search(rag_space=rag_space, term=term, request_data=request.data)
            answer = cv._format_listing_answer(term, hits)
        except Exception as e:  # préserve le contrat d'erreur historique (§9)
            raise ListingRouteError(str(e)) from e
        # Parité EXACTE avec le chemin sync historique : ni thread ni log.
        return {"answer": answer, "sources": [], "log_id": None}

    if plan.action == "clarification":
        thread_id = validated_data.get("thread_id")
        return {
            "answer": plan.clarification_message or _clarify_message(plan),
            "sources": [],
            "status": "clarification",
            "answer_kind": "clarification",
            "candidates": list(plan.candidates) or ["search.answer"],
            "missing_parameters": [],
            "thread_id": str(thread_id) if thread_id else None,
            "log_id": None,
        }

    if plan.action == "document.summarize":
        thread_id = validated_data.get("thread_id")
        out = _execute_summary(
            user=user, rag_space=rag_space, thread_id=thread_id, plan=plan,
            request_id=_request_id_for(request),
        )
        return _summary_sync_dict(out=out, thread_id=thread_id)

    # search.answer (les candidats news sont délégués : chat_query fait SA détection).
    from apps.chat.services.chat_service import chat_query

    return chat_query(user=user, rag_space=rag_space, **validated_data)


# ---------------------------------------------------------------------------
# SSE (yield de dicts {event, data} — même forme que chat_query_stream)
# ---------------------------------------------------------------------------
def run_chat_stream(
    *, request, user, rag_space, request_id: str, validated_data: dict
) -> Iterable[dict[str, Any]]:
    question = (validated_data.get("question") or "").strip()
    thread_id = validated_data.get("thread_id") or None
    thread_id_str = str(thread_id) if thread_id else None

    plan = _decide(question)

    # Aide produit — cf. run_chat_sync : garde-fou anti-retombée dans le RAG.
    if plan.action == "help.capabilities":
        from apps.help.catalog import render_help_markdown

        answer = render_help_markdown(plan.parameters.get("topic"))
        yield {"event": "meta", "data": {
            "request_id": request_id, "thread_id": thread_id_str,
            "log_id": None, "answer_kind": "help",
        }}
        yield {"event": "sources", "data": {"sources": []}}
        yield {"event": "token", "data": {"delta": answer}}
        yield {"event": "done", "data": {
            "request_id": request_id, "thread_id": thread_id_str,
            "log_id": None, "status": "ok", "answer_kind": "help",
        }}
        return

    if plan.action == "corpus.global_request":
# Garde-fou : un plan ``corpus.global_request`` ne doit JAMAIS retomber dans
# ``chat_query`` (retrieval + LLM). La vue l'intercepte déjà en amont (donc actif
# flag ON comme OFF) ; cette branche protège contre un changement de chemin d'appel.
        from apps.chat.services import corpus_mode_service as cms
        from apps.rag_core.domain.corpus_mode import render_corpus_mode_choice_markdown
        from apps.rag_core.services.coverage_service import compute_rag_space_statistics

        stats = compute_rag_space_statistics(rag_space.id)
        answer = render_corpus_mode_choice_markdown(
            statistics=stats, coverage=stats["coverage"]
        )
        meta = {
            "request_id": request_id, "thread_id": thread_id_str,
            "log_id": None, "answer_kind": cms.ANSWER_KIND_MODE_CHOICE,
        }
        yield {"event": "meta", "data": dict(meta)}
        yield {"event": "sources", "data": {"sources": []}}
        yield {"event": "token", "data": {"delta": answer}}
        yield {"event": "done", "data": {**meta, "status": "ok"}}
        return

    if plan.action == "search.list_documents":
        yield from _listing_stream(
            request=request, user=user, rag_space=rag_space,
            question=question, thread_id_str=thread_id_str, request_id=request_id,
        )
        return

    if plan.action == "clarification":
        msg = plan.clarification_message or _clarify_message(plan)
        yield {"event": "meta", "data": {"request_id": request_id, "thread_id": thread_id_str, "log_id": None}}
        yield {"event": "sources", "data": {"sources": []}}
        yield {"event": "token", "data": {"delta": msg}}
        yield {"event": "done", "data": {
            "request_id": request_id, "thread_id": thread_id_str, "log_id": None,
            "status": "clarification", "answer_kind": "clarification",
            "candidates": list(plan.candidates) or ["search.answer"], "missing_parameters": [],
        }}
        return

    if plan.action == "document.summarize":
        out = _execute_summary(
            user=user, rag_space=rag_space, thread_id=thread_id, plan=plan,
            request_id=request_id,
        )
        yield from _summary_stream_events(out=out, thread_id=thread_id, request_id=request_id)
        return

    # search.answer / news -> streaming historique VERBATIM.
    from apps.chat.services.chat_service import chat_query_stream

    yield from chat_query_stream(
        user=user, rag_space=rag_space, request_id=request_id, **validated_data
    )


def _listing_stream(*, request, user, rag_space, question, thread_id_str, request_id):
    """Reproduit fidèlement le listing SSE historique (thread + persistance)."""
    from api.v1.chat import views as cv

    term = cv._extract_term_for_listing(question)
    try:
        hits = cv._run_listing_search(rag_space=rag_space, term=term, request_data=request.data)
        answer = cv._format_listing_answer(term, hits)
        t = cv._ensure_thread_and_persist_messages(
            user=user, rag_space=rag_space, thread_id=thread_id_str,
            question=question, answer=answer, request_id=request_id,
        )
        t_id = str(t.id)
        yield {"event": "meta", "data": {"request_id": request_id, "thread_id": t_id, "log_id": None}}
        yield {"event": "sources", "data": {"sources": []}}
        yield {"event": "token", "data": {"delta": answer}}
        yield {"event": "done", "data": {"request_id": request_id, "thread_id": t_id, "log_id": None, "status": "ok"}}
    except Exception:
        msg = (
            "Désolé, je n’ai pas réussi à produire la liste des documents demandée. "
            "Veuillez réessayer dans un instant."
        )
        try:
            t = cv._ensure_thread_and_persist_messages(
                user=user, rag_space=rag_space, thread_id=thread_id_str,
                question=question, answer=msg, request_id=request_id,
            )
            t_id = str(t.id)
        except Exception:
            t_id = thread_id_str
        yield {"event": "meta", "data": {"request_id": request_id, "thread_id": t_id, "log_id": None}}
        yield {"event": "sources", "data": {"sources": []}}
        yield {"event": "token", "data": {"delta": msg}}
        yield {"event": "done", "data": {"request_id": request_id, "thread_id": t_id, "log_id": None, "status": "error"}}

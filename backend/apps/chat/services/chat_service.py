# backend/apps/chat/services/chat_service.py
from __future__ import annotations

import inspect
import logging
from typing import Any

from django.conf import settings

from apps.llm_router.egress import email_scoped as _email_scoped

logger = logging.getLogger(__name__)


def _supported_kwargs(func, candidate: dict) -> dict:
    """Feature detection SÛRE : ne garde que les kwargs réellement acceptés par ``func``.

    Remplace le ``try/except TypeError`` historique autour de l'appel à
    ``core.retrieve`` (P1-A1). Un ``TypeError`` levé À L'INTÉRIEUR de la fonction n'est
    donc plus jamais confondu avec une incompatibilité de signature : l'introspection
    ne protège QUE l'inspection, jamais l'exécution.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # objets non introspectables
        return {}
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return dict(candidate)
    return {k: v for k, v in candidate.items() if k in params}


def _log_persist_failure(*, thread, role: str, stage: str, request_id) -> None:
    """Journalise (avec stack trace, SANS contenu) un échec de persistance d'un
    message de chat. F5-A : la réponse a déjà été calculée/streamée à l'utilisateur ;
    on ne le bloque pas, mais on trace la perte d'historique de façon corrélable
    (thread/role/étape/request_id — jamais le contenu du message)."""
    logger.exception(
        "chat_message_persist_failed thread_id=%s role=%s stage=%s request_id=%s",
        getattr(thread, "id", None), role, stage, request_id or None,
    )

from apps.llm_router.model_utils import gen_options, think_for_focused_task
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import (
    LLMError,
    _gateway_enabled,
    _gateway_streaming_enabled,
    generate_text,
    generate_text_stream,
)
from apps.llm_router.task_routing import TASK_FACTUAL, resolve_model_for_task
from apps.rag_core.services.answer_contract import build_answer_contract
from apps.rag_core.services.answer_contract import answer_was_truncated
from apps.rag_core.services.pricing import compute_cost_from_usage
from apps.rag_core.services.answer_language import log_language_decision, resolve_target
from apps.accounts.models import get_preferred_answer_language

from .chat_audit import extract_request_id_from_user, maybe_update_log
from .chat_fallback import mode_b_enabled, run_fallback_stream, run_fallback_sync
from .chat_formatting import (
    finalize_answer_with_sources,
    heuristic_answer,
    normalize_llm_overrides,
    retrieved_sources_count,
)
from .chat_sources import sources_for_api
from .rephrasing_async import enqueue_no_answer_suggestions
from .chat_threads import (
    InvalidThreadError,
    append_message,
    get_thread_memory_context,
    maybe_update_thread_title,
    resolve_thread_strict,
    thread_previous_user_language,
)
from .intents.news import (
    build_news_sources_from_retriever,
    llm_route_news_or_none,
    summarize_news_with_llm,
    update_log_sources_best_effort,
)


# ---------------------------------------------------------------------
# Thread memory / context (conversation history)
# - Niveau 2: summary + fenêtre récente (implémenté dans chat_threads.py)
# ---------------------------------------------------------------------


def _conversation_context_payload(*, thread) -> str | None:
    """
    Policy:
    - Only include memory when enabled and when we have a thread.
    - Use hierarchical memory (summary + recent window) from chat_threads.get_thread_memory_context().
    """
    if thread is None:
        return None

    # `RAG_CHAT_MEMORY_ENABLED` était consulté ici en repli. Il n'a jamais été
    # projeté dans les settings : la branche ne s'est jamais exécutée, et le poser
    # dans un `.env` ne coupait rien. Retiré plutôt que déclaré — un alias doit
    # préserver une configuration qui a existé.
    from config.settings.access import get_registered_setting

    if not get_registered_setting("CHAT_MEMORY_ENABLED"):
        return None

    ctx = (get_thread_memory_context(thread=thread) or "").strip()
    return ctx or None


# ---------------------------------------------------------------------
# Existing helpers
# ---------------------------------------------------------------------


def _is_detailed_mode(retrieval: dict) -> bool:
    """
    Minimal coupling with query_rag improvement:
    query_rag may return answer_kind: ok | explain | compare | partial
    """
    return (retrieval or {}).get("answer_kind") in ("explain", "compare", "partial")


def _detail_prefix(answer_kind: str | None, answer_language: str | None = None) -> str:
    """
    Small UX boost: when we're answering with partial sources,
    we warn the user *without* blocking the answer.
    """
    if answer_kind == "partial":
        if answer_language == "en":
            return (
                "⚠️ The available documents cover only part of the subject. "
                "I answer with what your sources contain, and point out what is missing.\n\n"
            )
        return (
            "⚠️ Les documents disponibles ne couvrent qu’une partie du sujet. "
            "Je réponds avec les éléments présents dans vos sources, et j’indique ce qui manque.\n\n"
        )
    return ""


def _numeric_evidence_guard(answer: str, *, retrieval: dict, question: str, answer_language: str | None):
    """Contrôle déterministe des affirmations numériques (NUMERIC-CLAIM-GUARD-2), APRÈS génération
    et AVANT tout affichage (le flux est mis en tampon dans le même périmètre).

    Rend ``(réponse, answer_kind forcé ou None, décision)``. Chaque nombre affirmé est contrôlé
    seul ; une proposition non étayée est retirée entière ; plus rien d'étayé : abstention
    contrôlée. Échec du contrôle dans le périmètre protégé (contexte avec OCR ou lecture
    visuelle) : abstention, jamais la réponse non contrôlée ; hors périmètre : inchangée."""
    from apps.rag_core.services.query import numeric_evidence as NE

    sources = (retrieval or {}).get("sources") or []
    try:
        decision = NE.guard_answer(answer, question=question, sources=sources, language=answer_language)
    except Exception:
        logger.exception("numeric evidence guard failed")
        if NE.context_is_protected(sources):
            return (NE.abstention_answer([], answer_language), NE.ANSWER_KIND_BLOCKED,
                    {"verdict": NE.VERDICT_BLOCK, "error": "guard_failed"})
        return answer, None, None
    if decision.verdict != NE.VERDICT_PASS:
        logger.info("numeric evidence guard verdict=%s counts=%s removed=%s",
                    decision.verdict, decision.counts(), decision.removed)
    forced = NE.ANSWER_KIND_BLOCKED if decision.verdict == NE.VERDICT_BLOCK else None
    return decision.answer, forced, decision.to_dict()


def _buffers_numeric_output(retrieval: dict) -> bool:
    """Flux : la réponse est-elle retenue jusqu'au contrôle ? (même périmètre que le contrôle)"""
    from apps.rag_core.services.query import numeric_evidence as NE

    try:
        return NE.context_is_protected((retrieval or {}).get("sources") or [])
    except Exception:
        logger.exception("numeric evidence perimeter failed")
        return True


def _no_sources_answer(answer_language: str | None = None) -> str:
    """Abstention SANS génération : aucun LLM n'est appelé, donc aucune directive de
    langue ne s'applique. Le texte doit donc porter lui-même sa traduction — sinon une
    réponse anglaise se termine par une phrase française (constaté en qualification
    staging : cible `en` résolue et propagée, message d'abstention rendu en français).
    """
    if answer_language == "en":
        return "Not enough information in your documents."
    return "Pas assez d’informations dans vos documents."


def _abstention_contract(retrieval: dict | None, sources: list[dict] | None) -> dict:
    """Contrat de sources pour une réponse SANS génération (no_answer, garde).

    Les sources retrouvées restent affichées — l'utilisateur a le droit de voir ce qui a été
    cherché — mais aucune n'est présentée comme CITÉE : rien n'a été affirmé à partir d'elles.
    """
    return build_answer_contract(
        "",
        prompt_sources_count=len(sources or []),
        retrieved_sources_count=retrieved_sources_count(retrieval, sources),
        force_abstention=True,
    ).to_dict()


def _run_retrieval(
    *,
    rag_space_id,
    rag_space,
    user,
    question: str,
    request_id,
    conversation_context: str | None,
    entrypoint: str = "chat_sync",
    answer_language: str | None = None,
) -> dict:
    """Récupération RAG — point d'entrée UNIQUE pour le chat (sync + stream).

    Le chat passe TOUJOURS par ``apps.rag_core.core.retrieve``. Il n'existe plus de
    bifurcation : le flag ``RAG_USE_CORE_RETRIEVE`` sélectionnait deux chemins dont
    la mesure a montré qu'ils rendaient le même objet — la façade délègue à
    ``query_rag`` et expose son dict verbatim (``RetrievalResult.raw``). Un
    interrupteur qui ne change rien n'est pas un filet de sécurité : c'est une
    branche à tester deux fois et une décision à re-justifier à chaque incident.
    """
    result = _dispatch_retrieval(
        rag_space_id=rag_space_id,
        rag_space=rag_space,
        user=user,
        question=question,
        request_id=request_id,
        conversation_context=conversation_context,
        entrypoint=entrypoint,
        answer_language=answer_language,
    )
    # Transporte la cible de langue résolue jusqu'aux sous-chemins (moteur tabulaire)
    # sans re-résoudre. Clé INTERNE : jamais renvoyée telle quelle au client.
    if isinstance(result, dict):
        result["answer_language"] = answer_language

    # --- Seconde garde : ANALYTIQUE + CLASSEUR retrouvé -> refus AVANT le LLM ------
    #
    # La garde pré-retrieval exige de SAVOIR que la question porte sur un classeur. Au
    # premier tour d'un fil neuf, elle ne le sait pas : « quel est le total des shares
    # outstanding pour AirWorks ? » ne contient aucun mot de tableur. C'est ici, et ici
    # seulement, qu'on l'apprend — le retrieval vient de le dire.
    #
    # Le point est UNIQUE : `_run_retrieval` est partagé par le sync et le SSE. La
    # décision ne peut donc pas diverger entre les deux contrats.
    try:
        from apps.chat.services import excel_guard as _guard

        decision = _guard.guard_after_retrieval(
            question=question, sources=result.get("sources") or [],
        )
        if decision is not None:
            result["excel_guard"] = decision
            result["answer_kind"] = _guard.ANSWER_KIND
            # Le prompt est neutralisé : plus rien en aval ne peut appeler le modèle.
            result["prompt"] = None
    except Exception as exc:
        # P1-A1 : une garde de CORRECTION (refus d'agrégation tabulaire partielle) ne
        # doit PAS être contournée par une exception générique. Fail-open ici pouvait
        # laisser passer une réponse analytique potentiellement FAUSSE. On échoue donc
        # explicitement (FAIL CLOSED) : on ne poursuit pas comme si la garde avait
        # accepté. La frontière HTTP/SSE la convertit en 500 / done{status:"error"}.
        from apps.rag_core.services.query.errors import RetrievalInvariantError

        logger.exception("post-retrieval excel guard failed (fail-closed)")
        logger.error(
            "retrieval_guard_failed",
            extra={
                "request_id": request_id,
                "leg": "excel_guard",
                "error_kind": type(exc).__name__,
                "policy": "error",
            },
        )
        raise RetrievalInvariantError(
            "post-retrieval excel guard failed", leg="excel_guard"
        ) from exc

    return result


def _dispatch_retrieval(
    *,
    rag_space_id,
    rag_space,
    user,
    question: str,
    request_id,
    conversation_context: str | None,
    entrypoint: str = "chat_sync",
    answer_language: str | None = None,
) -> dict:
    """Appelle la façade Core et rend sa charge utile brute.

    ``core.retrieve`` est le CONTRAT public interne du retrieval : c'est lui que le
    chat consomme, et lui seul. ``query_rag`` en est l'IMPLÉMENTATION, libre
    d'évoluer tant que la façade tient son contrat — d'où le passage par ``.raw``,
    qui rend le dict de l'implémentation verbatim le temps que le DTO couvre tout
    ce dont le chat a besoin.
    """
    from apps.rag_core import core

    # Kwargs de base (toujours acceptés) + kwargs optionnels filtrés par SIGNATURE.
    # P1-A1 : plus de `except TypeError` autour de l'exécution — un TypeError interne
    # de core.retrieve se propage (n'est PAS réessayé silencieusement).
    base = dict(
        rag_space_id=rag_space_id,
        rag_space=rag_space,
        user=user,
        question=question,
        request_id=request_id,
    )
    # `answer_language` (cible résolue fr/en, ou None) est OPTIONNEL : filtré par
    # signature via `_supported_kwargs`, il n'est transmis qu'aux façades qui le
    # supportent. None ⇒ prompts français historiques (aucun changement).
    optional = dict(
        conversation_context=conversation_context,
        entrypoint=entrypoint,
        answer_language=answer_language,
    )

    return core.retrieve(**base, **_supported_kwargs(core.retrieve, optional)).raw


# ---------------------------------------------------------------------
# Chat sync
# ---------------------------------------------------------------------


def _excel_guard_answer(
    retrieval: dict,
    *,
    thread,
    sources: list,
    log_id,
    request_id,
) -> dict | None:
    """Réponse de refus quand la garde post-retrieval s'est armée. Aucun LLM.

    Les SOURCES sont conservées : le retrieval a bien eu lieu, et l'utilisateur a le
    droit de voir ce qui a été retrouvé — c'est même ce qui rend le refus crédible.
    Ce qu'on ne fait pas, c'est **répondre** à partir de ces extraits.

    ⚠️ Différence assumée avec la garde PRÉ-retrieval : ici un ``RagQueryLog`` EXISTE
    déjà, et on le garde. Une recherche a réellement été exécutée ; l'effacer
    falsifierait l'audit. On y consigne seulement pourquoi la génération a été refusée.
    """
    decision = retrieval.get("excel_guard")
    if decision is None:
        return None

    from apps.chat.services import excel_guard as _guard

    # Branchement du moteur tabulaire (lot 1). Inerte tant que
    # `TABULAR_QUERY_ENABLED=0` : `try_tabular_answer` rend `None` et le refus
    # historique s'applique tel quel. Quand le moteur est activé et sait traduire la
    # question, il CALCULE la réponse ; s'il ne sait pas, on retombe sur le refus —
    # jamais sur le RAG vectoriel, qui ne voit que le top-k.
    tabular = _try_tabular(retrieval, thread=thread, question=retrieval.get("question"))
    if tabular is not None:
        answer = tabular["answer"]
        maybe_update_log(log_id, answer=answer, policy="tabular_engine", error_stage="")
        if thread is not None:
            append_message(
                thread=thread, role="assistant", content=answer, sources=sources,
                request_id=request_id, rag_query_log_id=log_id,
            )
        return {
            "answer": answer,
            "sources": sources,
            "thread_id": str(thread.id) if thread else None,
            "status": "ok",
            "log_id": log_id,
            "answer_kind": tabular["answer_kind"],
            "suggested_rephrasings": [],
            # Réponse CALCULÉE (déterministe) : elle ne cite que ce qu'elle référence.
            "sources_contract": build_answer_contract(
                answer,
                prompt_sources_count=len(sources or []),
                retrieved_sources_count=retrieved_sources_count(retrieval, sources),
            ).to_dict(),
            "tabular": tabular.get("tabular", {}),
            "analytics": {"requested_intent": decision.intent, "available": True},
        }

    answer, suggestions = _guard.build_refusal(decision)

    maybe_update_log(
        log_id,
        answer=answer,
        policy=_guard.REASON_POST_RETRIEVAL,
        error_stage="",
    )

    if thread is not None:
        append_message(
            thread=thread,
            role="assistant",
            content=answer,
            sources=sources,
            request_id=request_id,
            rag_query_log_id=log_id,
        )

    return {
        "answer": answer,
        "sources": sources,
        "thread_id": str(thread.id) if thread else None,
        "status": "ok",
        "log_id": log_id,
        "answer_kind": _guard.ANSWER_KIND,
        "suggested_rephrasings": list(suggestions),
        # Refus de garde : les sources restent visibles, aucune n'est présentée comme citée.
        "sources_contract": _abstention_contract(retrieval, sources),
        "analytics": {"requested_intent": decision.intent, "available": False},
    }


def _try_tabular(retrieval: dict, *, thread, question):
    """Passe la main au moteur tabulaire si le flag l'autorise. Jamais bloquant."""
    if not question:
        return None
    rag_space_id = retrieval.get("rag_space_id") or getattr(
        getattr(thread, "rag_space", None), "id", None)
    user = retrieval.get("user") or getattr(thread, "user", None)
    if not rag_space_id or user is None:
        return None
    from apps.chat.services import tabular_routing
    return tabular_routing.try_tabular_answer(
        user=user, rag_space_id=rag_space_id, question=question,
        answer_language=retrieval.get("answer_language"),
    )


def _apply_answer_prefix(prefix: str, answer: str, answer_language: str | None = None) -> str:
    """Préfixe déterministe, imposé par le backend APRÈS génération.

    Le LLM ne peut donc pas le supprimer, même s'il affirme dans son texte avoir
    « analysé tous les documents ». Idempotent : ne s'empile pas.

    Traduit dans la langue de réponse résolue : ce bloc n'est pas généré, il ne subit
    donc aucune directive de langue et resterait français au-dessus d'une réponse
    anglaise.
    """
    from apps.rag_core.domain.corpus_mode import localized_answer_prefix

    p = (localized_answer_prefix(prefix, answer_language) or "").strip()
    if not p:
        return answer
    text = answer or ""
    if text.lstrip().startswith(p.split("\n", 1)[0]):
        return text
    return f"{p}\n\n{text}"


@_email_scoped
def chat_query(
    *,
    rag_space_id,
    question: str,
    thread_id=None,
    user=None,
    rag_space=None,
    persisted_question: str | None = None,
    answer_prefix: str = "",
    answer_language: str = "auto",
) -> dict:
    """RAG classique.

    ``persisted_question`` : texte à persister comme message *user*, quand il diffère
    de la question réellement soumise au retrieval (cas L3 « réponse rapide » : on
    rejoue la question globale d'origine, mais le fil doit montrer ce que
    l'utilisateur a tapé). Par défaut, identique à ``question``.

    ``answer_prefix`` : bloc déterministe préfixé à la réponse — appliqué à la fois
    au texte RENVOYÉ et au texte PERSISTÉ, jamais au prompt (le LLM ne peut pas le
    retirer).
    """
    request_id = extract_request_id_from_user(user) if user is not None else ""
    user_turn = persisted_question if persisted_question is not None else question

    thread = None
    prev_lang = None
    if user is not None and rag_space is not None:
        thread = resolve_thread_strict(user=user, rag_space=rag_space, thread_id=thread_id, question=user_turn)
        # Langue du tour PRÉCÉDENT (continuité) — capturée AVANT d'ajouter le tour courant.
        prev_lang = thread_previous_user_language(thread)
        append_message(thread=thread, role="user", content=user_turn, request_id=request_id)
        maybe_update_thread_title(thread=thread, question=user_turn)

    llm_provider, llm_model = normalize_llm_overrides(rag_space)

    # Build conversation memory context (summary + recent window)
    conversation_context = _conversation_context_payload(thread=thread)

    # Langue de réponse RÉSOLUE (fr/en), ou None si la politique est désactivée
    # (→ prompts français historiques). Basée sur la question RÉELLE de l'utilisateur
    # (`user_turn`), jamais sur un libellé de mode synthétique.
    # Préférence effective : un choix FORCÉ dans la requête (fr/en) l'emporte ; sinon
    # la préférence PERSISTÉE du compte (`preferred_answer_language`). Continuité par
    # `prev_lang` quand la question est indéterminable. `preferred_locale` (interface)
    # n'est PAS utilisé ici : le chat reste indépendant de la langue d'interface.
    _pref = answer_language if answer_language in ("fr", "en") else get_preferred_answer_language(user)
    target_language = resolve_target(pref=_pref, question=user_turn, thread_prev_language=prev_lang)
    log_language_decision(
        surface="chat.sync", pref=_pref, question=user_turn,
        thread_prev_language=prev_lang, resolved=target_language,
        provider=llm_provider, model=llm_model,
    )

    # 1) LLM router for "news"
    plan = llm_route_news_or_none(question=question, llm_provider=llm_provider, llm_model=llm_model)
    if plan and plan.get("intent") == "news":
        term = plan["term"]
        top_n = int(plan.get("top_n_docs") or 5)
        time_window_days = int(plan.get("time_window_days") or 120)

        # Keep audit pipeline intact: create log via retrieval, but do not use its answer
        retrieval = _run_retrieval(
            rag_space_id=rag_space_id,
            rag_space=rag_space,
            user=user,
            question=question,
            request_id=request_id or None,
            conversation_context=conversation_context,
            answer_language=target_language,
        )

        log_id = retrieval.get("log_id")

        facts, retrieval_sources = build_news_sources_from_retriever(
            rag_space_id=rag_space_id,
            term=term,
            top_n_docs=top_n,
            time_window_days=time_window_days,
        )

        sources = sources_for_api(retrieval_sources)

        answer = summarize_news_with_llm(
            question=question,
            term=term,
            facts=facts,
            llm_provider=llm_provider,
            llm_model=llm_model,
            answer_language=target_language,
        )
        answer, sources_contract = finalize_answer_with_sources(
            answer, sources, retrieval=retrieval,
        )
        # Make DB audit log consistent with what we returned
        update_log_sources_best_effort(log_id=log_id, sources=sources)

        maybe_update_log(
            log_id,
            answer=answer,
            error_stage="",
            policy="news",
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_total_tokens=None,
        )

        if thread is not None:
            append_message(
                thread=thread,
                role="assistant",
                content=answer,
                sources=sources,
                request_id=request_id,
                rag_query_log_id=log_id,
            )

        return {
            "answer": answer,
            "sources": sources,
            "thread_id": str(thread.id) if thread else None,
            "status": "ok",
            "log_id": log_id,
            "answer_kind": "news",
            "sources_contract": sources_contract.to_dict(),
        }

    # 2) Default path: existing RAG (with memory)
    retrieval = _run_retrieval(
        rag_space_id=rag_space_id,
        rag_space=rag_space,
        user=user,
        question=question,
        request_id=request_id or None,
        conversation_context=conversation_context,
        answer_language=target_language,
    )

    log_id = retrieval.get("log_id")
    sources = sources_for_api(retrieval.get("sources") or [])
    answer_kind = retrieval.get("answer_kind")  # ok | explain | partial (optional)

    # ✅ Block "Option B" fallback when there are no sources (avoid hallucination)
    if retrieval.get("status") == "no_answer" and mode_b_enabled():
        if not sources:
            answer = _no_sources_answer(target_language)
            maybe_update_log(log_id, answer=answer, policy="no_answer", error_stage="")

            if thread is not None:
                append_message(
                    thread=thread,
                    role="assistant",
                    content=answer,
                    sources=[],
                    request_id=request_id,
                    rag_query_log_id=log_id,
                )

            return {
                "answer": answer,
                "sources": [],
                "thread_id": str(thread.id) if thread else None,
                "status": "no_answer",
                "log_id": log_id,
                "answer_kind": "no_answer",
                # Option A (sync) : réponse immédiate, aucune génération LLM inline.
                # Les suggestions ne sont proposées qu'en streaming (événement tardif).
                "suggested_rephrasings": [],
                "sources_contract": _abstention_contract(retrieval, []),
            }

        try:
            out = run_fallback_sync(
                question=question,
                rag_space=rag_space,
                thread=thread,
                request_id=request_id,
                log_id=log_id,
                answer_language=target_language,
            )
            if isinstance(out, dict) and "log_id" not in out:
                out["log_id"] = log_id
            return out
        except LLMError:
            maybe_update_log(log_id, status="error", error_stage="llm", policy="fallback_general")
            raise
        except Exception:
            maybe_update_log(log_id, status="error", error_stage="llm", policy="fallback_general")
            raise

    guarded = _excel_guard_answer(
        retrieval,
        thread=thread,
        sources=sources,
        log_id=log_id,
        request_id=request_id,
    )
    if guarded is not None:
        return guarded

    if retrieval.get("status") == "no_answer":
        answer = _no_sources_answer(target_language)
        maybe_update_log(log_id, answer=answer, policy="no_answer", error_stage="")

        if thread is not None:
            append_message(
                thread=thread,
                role="assistant",
                content=answer,
                sources=sources,
                request_id=request_id,
                rag_query_log_id=log_id,
            )

        return {
            "answer": answer,
            "sources": sources,
            "thread_id": str(thread.id) if thread else None,
            "status": "no_answer",
            "log_id": log_id,
            "answer_kind": "no_answer",
            # Option A (sync) : réponse immédiate, aucune génération LLM inline.
            "suggested_rephrasings": [],
            "sources_contract": _abstention_contract(retrieval, sources),
        }

    try:
        # Routage OPT-IN par tâche (factuel) : override éventuel du modèle local.
        routed_provider, routed_model = resolve_model_for_task(
            TASK_FACTUAL, provider=llm_provider, model=llm_model
        )
        # Preset RAG factuel (temperature/num_ctx/num_predict) transmis à la Gateway.
        # think=false uniquement pour qwen3:14b (évite les réponses vides).
        # `seed` est OPTIONNEL et vaut None par défaut : sans lui, le dict d'options
        # est identique à ce qu'il était, et la génération reste celle d'avant.
        factual_options = gen_options(
            temperature=settings.RAG_FACTUAL_TEMPERATURE,
            num_ctx=settings.RAG_FACTUAL_NUM_CTX,
            num_predict=settings.RAG_FACTUAL_NUM_PREDICT,
            seed=getattr(settings, "RAG_FACTUAL_SEED", None),
        )
        llm_response = generate_text(
            prompt=retrieval["prompt"],
            provider=routed_provider,
            model=routed_model,
            # Politique d'egress résolue pour CET espace (Lot 1 P0-C) : porte à la
            # fois la décision de confidentialité et le périmètre d'audit.
            context=LlmExecutionContext.for_rag_space(
                rag_space, operation="chat.answer", correlation_id=request_id or ""
            ),
            options=factual_options,
            think=think_for_focused_task(routed_model, task="factual"),
        )
        answer = (llm_response.get("text") or "").strip()

        if not answer or answer.lower().startswith("réponse simulée"):
            fallback = heuristic_answer(question, sources)
            if fallback:
                answer = fallback

        answer, forced_kind, numeric_guard = _numeric_evidence_guard(
            answer, retrieval=retrieval, question=question, answer_language=target_language)
        if forced_kind:
            answer_kind = forced_kind
        elif answer_kind:
            answer = _detail_prefix(answer_kind, target_language) + answer

        answer, sources_contract = finalize_answer_with_sources(
            answer, sources, retrieval=retrieval,
            # Le plafond de génération est le SEUL responsable d'une coupure ici.
            answer_truncated=answer_was_truncated(
                (llm_response or {}).get("usage"), settings.RAG_FACTUAL_NUM_PREDICT,
                # `raw` porte `done_reason` — la raison d'arrêt déclarée par la
                # Gateway. Elle prime sur le comptage de tokens.
                raw=(llm_response or {}).get("raw")),
        )
        # Après génération : le LLM ne peut plus retirer l'avertissement.
        answer = _apply_answer_prefix(answer_prefix, answer, target_language)

        prov_used = llm_response.get("provider") or llm_provider
        model_used = llm_response.get("model") or llm_model
        pt, ct, tt, cost = compute_cost_from_usage(
            provider=prov_used,
            model=model_used,
            usage=llm_response.get("usage"),
            total=llm_response.get("tokens"),
        )
        maybe_update_log(
            log_id,
            answer=answer,
            error_stage="",
            policy=answer_kind or "",
            llm_provider=prov_used,
            llm_model=model_used,
            llm_prompt_tokens=pt,
            llm_completion_tokens=ct,
            llm_total_tokens=tt,
            llm_cost_estimated=cost,
        )

        if thread is not None:
            append_message(
                thread=thread,
                role="assistant",
                content=answer,
                sources=sources,
                request_id=request_id,
                rag_query_log_id=log_id,
            )

        return {
            "answer": answer,
            "sources": sources,
            "thread_id": str(thread.id) if thread else None,
            "status": "ok",
            "log_id": log_id,
            "answer_kind": answer_kind or ("explain" if _is_detailed_mode(retrieval) else "ok"),
            # Contrat EXPLICITE des sources : l'UI n'a plus à déduire « citée » de
            # « retrouvée » (cf. rag_core.services.answer_contract).
            "sources_contract": sources_contract.to_dict(),
            "llm_total_tokens": tt,
            "llm_cost_estimated": cost,
            # TRANSPARENCE (Lot 1 P0-C) : la réponse synchrone ne portait AUCUNE
            # information de provider — l'utilisateur ne pouvait pas savoir que sa
            # question avait été traitée par un fournisseur externe. Ces deux
            # champs sont des métadonnées de routage, jamais des données sensibles.
            "provider_effective": prov_used,
            "fallback_used": bool(llm_response.get("fallback_used")),
            "numeric_evidence_guard": numeric_guard,
        }

    except LLMError:
        maybe_update_log(log_id, status="error", error_stage="llm")
        raise
    except Exception:
        maybe_update_log(log_id, status="error", error_stage="llm")
        raise


# ---------------------------------------------------------------------
# Chat streaming
# ---------------------------------------------------------------------


def chat_query_stream(
    *,
    rag_space_id,
    question: str,
    thread_id=None,
    user=None,
    rag_space=None,
    request_id: str | None = None,
    persisted_question: str | None = None,
    answer_prefix: str = "",
    answer_language: str = "auto",
):
    """Idem ``chat_query``, en SSE. Voir ``chat_query`` pour ``persisted_question``
    et ``answer_prefix`` (contrat SSE inchangé : meta -> sources -> token -> done)."""
    rid = (request_id or "").strip() or (extract_request_id_from_user(user) if user is not None else "")
    user_turn = persisted_question if persisted_question is not None else question

    thread = None
    prev_lang = None
    if user is not None and rag_space is not None:
        try:
            thread = resolve_thread_strict(user=user, rag_space=rag_space, thread_id=thread_id, question=user_turn)
            prev_lang = thread_previous_user_language(thread)
            append_message(thread=thread, role="user", content=user_turn, request_id=rid)
            maybe_update_thread_title(thread=thread, question=user_turn)
        except InvalidThreadError:
            yield {"event": "error", "data": {"detail": "invalid_thread_id", "request_id": rid or None}}
            yield {"event": "done", "data": {"request_id": rid or None}}
            return

    llm_provider, llm_model = normalize_llm_overrides(rag_space)

    # Build conversation memory context (summary + recent window)
    conversation_context = _conversation_context_payload(thread=thread)

    # Langue de réponse RÉSOLUE (fr/en), ou None si la politique est désactivée
    # (→ prompts français historiques). Basée sur la question RÉELLE de l'utilisateur
    # (`user_turn`), jamais sur un libellé de mode synthétique.
    # Préférence effective : un choix FORCÉ dans la requête (fr/en) l'emporte ; sinon
    # la préférence PERSISTÉE du compte (`preferred_answer_language`). Continuité par
    # `prev_lang` quand la question est indéterminable. `preferred_locale` (interface)
    # n'est PAS utilisé ici : le chat reste indépendant de la langue d'interface.
    _pref = answer_language if answer_language in ("fr", "en") else get_preferred_answer_language(user)
    target_language = resolve_target(pref=_pref, question=user_turn, thread_prev_language=prev_lang)
    log_language_decision(
        surface="chat.stream", pref=_pref, question=user_turn,
        thread_prev_language=prev_lang, resolved=target_language,
        provider=llm_provider, model=llm_model,
    )

    # 1) LLM router for "news" (streaming)
    plan = llm_route_news_or_none(question=question, llm_provider=llm_provider, llm_model=llm_model)
    if plan and plan.get("intent") == "news":
        term = plan["term"]
        top_n = int(plan.get("top_n_docs") or 5)
        time_window_days = int(plan.get("time_window_days") or 120)

        retrieval = _run_retrieval(
            rag_space_id=rag_space_id,
            rag_space=rag_space,
            user=user,
            question=question,
            request_id=rid or None,
            conversation_context=conversation_context,
            entrypoint="chat_stream",
            answer_language=target_language,
        )

        log_id = retrieval.get("log_id")

        yield {
            "event": "meta",
            "data": {
                "request_id": rid or None,
                "log_id": log_id,
                "rag_space_id": str(rag_space_id),
                "thread_id": str(thread.id) if thread else None,
                "answer_kind": "news",
            },
        }

        facts, retrieval_sources = build_news_sources_from_retriever(
            rag_space_id=rag_space_id,
            term=term,
            top_n_docs=top_n,
            time_window_days=time_window_days,
        )

        sources = sources_for_api(retrieval_sources)
        yield {"event": "sources", "data": {"sources": sources}}

        answer = summarize_news_with_llm(
            question=question,
            term=term,
            facts=facts,
            llm_provider=llm_provider,
            llm_model=llm_model,
            answer_language=target_language,
        )
        final_answer = finalize_answer_with_sources(answer, sources)

        # Make DB audit log consistent with what we returned
        update_log_sources_best_effort(log_id=log_id, sources=sources)

        maybe_update_log(
            log_id,
            answer=final_answer,
            error_stage="",
            policy="news",
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_total_tokens=None,
        )

        if thread is not None:
            try:
                append_message(
                    thread=thread,
                    role="assistant",
                    content=final_answer,
                    sources=sources,
                    request_id=rid,
                    rag_query_log_id=log_id,
                )
            except Exception:
                _log_persist_failure(thread=thread, role="assistant", stage="news", request_id=rid)

        # Stream as a single delta block (fast + stable formatting)
        yield {"event": "token", "data": {"delta": final_answer}}
        yield {
            "event": "done",
            "data": {
                "answer": final_answer,
                "sources": sources,
                "provider": llm_provider,
                "model": llm_model,
                "tokens": None,
                "usage": None,
                "thread_id": str(thread.id) if thread else None,
                "status": "ok",
                "log_id": log_id,
                "answer_kind": "news",
            },
        }
        return

    # 2) Default streaming path: existing RAG (with memory)
    retrieval = _run_retrieval(
        rag_space_id=rag_space_id,
        rag_space=rag_space,
        user=user,
        question=question,
        request_id=rid or None,
        conversation_context=conversation_context,
        entrypoint="chat_stream",
        answer_language=target_language,
    )

    log_id = retrieval.get("log_id")
    answer_kind = retrieval.get("answer_kind")

    yield {
        "event": "meta",
        "data": {
            "request_id": rid or None,
            "log_id": log_id,
            "rag_space_id": str(rag_space_id),
            "thread_id": str(thread.id) if thread else None,
            "answer_kind": answer_kind or None,
        },
    }

    sources = sources_for_api(retrieval.get("sources") or [])
    yield {"event": "sources", "data": {"sources": sources}}

    # Garde post-retrieval : on sort AVANT le premier `token`. Le contrat d'événements
    # est intact (meta -> sources -> done), et le texte final est identique au sync —
    # les deux chemins lisent la MÊME décision, prise dans `_run_retrieval`.
    guarded = _excel_guard_answer(
        retrieval, thread=thread, sources=sources, log_id=log_id, request_id=rid,
    )
    if guarded is not None:
        yield {"event": "done", "data": guarded}
        return

    # ✅ Block "Option B" fallback when there are no sources (avoid hallucination)
    if retrieval.get("status") == "no_answer" and mode_b_enabled():
        if not sources:
            answer = _no_sources_answer(target_language)
            maybe_update_log(log_id, answer=answer, policy="no_answer", error_stage="")

            if thread is not None:
                try:
                    append_message(
                        thread=thread,
                        role="assistant",
                        content=answer,
                        sources=[],
                        request_id=rid,
                        rag_query_log_id=log_id,
                    )
                except Exception:
                    _log_persist_failure(thread=thread, role="assistant", stage="no_answer_empty", request_id=rid)

            yield {
                "event": "done",
                "data": {
                    "answer": answer,
                    "sources": [],
                    "thread_id": str(thread.id) if thread else None,
                    "status": "no_answer",
                    "log_id": log_id,
                    "answer_kind": "no_answer",
                    # ``done`` n'attend JAMAIS les suggestions : liste vide ici, les
                    # suggestions arrivent (si activées) via un événement tardif.
                    "suggested_rephrasings": [],
                    "sources_contract": _abstention_contract(retrieval, []),
                },
            }
            # F2 : génération HORS worker HTTP. On enfile une tâche Celery et on ferme
            # immédiatement le flux (le frontend récupère les suggestions via l'endpoint
            # /rephrasing-suggestions/). Aucun appel LLM inline.
            enqueue_no_answer_suggestions(thread=thread, request_id=rid)
            return

        try:
            yield from run_fallback_stream(
                question=question,
                rag_space=rag_space,
                thread=thread,
                request_id=rid,
                log_id=log_id,
                answer_language=target_language,
            )
            return
        except LLMError:
            maybe_update_log(log_id, status="error", error_stage="llm", policy="fallback_general")
            yield {"event": "error", "data": {"detail": "llm_error", "request_id": rid or None}}
            yield {"event": "done", "data": {"request_id": rid or None, "log_id": log_id}}
            return

    if retrieval.get("status") == "no_answer":
        answer = _no_sources_answer(target_language)
        maybe_update_log(log_id, answer=answer, policy="no_answer", error_stage="")

        if thread is not None:
            try:
                append_message(
                    thread=thread,
                    role="assistant",
                    content=answer,
                    sources=sources,
                    request_id=rid,
                    rag_query_log_id=log_id,
                )
            except Exception:
                _log_persist_failure(thread=thread, role="assistant", stage="no_answer", request_id=rid)

        yield {
            "event": "done",
            "data": {
                "answer": answer,
                "sources": sources,
                "thread_id": str(thread.id) if thread else None,
                "status": "no_answer",
                "log_id": log_id,
                "answer_kind": "no_answer",
                # ``done`` immédiat ; suggestions via événement tardif si activées.
                "suggested_rephrasings": [],
                "sources_contract": _abstention_contract(retrieval, sources),
            },
        }
        # F2 : génération HORS worker HTTP (tâche Celery), flux fermé immédiatement.
        enqueue_no_answer_suggestions(thread=thread, request_id=rid)
        return

    try:
        answer_parts: list[str] = []
        final_result: dict[str, Any] | None = None
        # NUMERIC-CLAIM-GUARD-2 : contexte avec OCR ou lecture visuelle -> AUCUN fragment n'est
        # émis avant le contrôle des chiffres ; la réponse validée part en un seul bloc.
        buffered = _buffers_numeric_output(retrieval)
        # Rupture de flux APRÈS un premier fragment : la réponse déjà affichée est
        # conservée et marquée partielle (cf. événement ``done``), au lieu d'être
        # perdue au profit d'un ``llm_error`` générique.
        stream_error: str = ""

        if _gateway_enabled():
            # Réglages de génération IDENTIQUES dans les deux régimes (pseudo-stream
            # et streaming natif) : même routage de modèle, mêmes options, même
            # ``think``. Un ``num_ctx`` divergent forcerait Ollama à recharger le
            # runner — plusieurs secondes perdues à chaque bascule de régime.
            routed_provider, routed_model = resolve_model_for_task(
                TASK_FACTUAL, provider=llm_provider, model=llm_model
            )
            factual_options = gen_options(
                temperature=settings.RAG_FACTUAL_TEMPERATURE,
                num_ctx=settings.RAG_FACTUAL_NUM_CTX,
                num_predict=settings.RAG_FACTUAL_NUM_PREDICT,
                seed=getattr(settings, "RAG_FACTUAL_SEED", None),
            )
            factual_think = think_for_focused_task(routed_model, task="factual")

            if _gateway_streaming_enabled():
                # Streaming NATIF de la Gateway (`/v1/generate/stream`) : les
                # fragments partent au fil de l'eau, aucune écriture en base par
                # token — la persistance reste faite une seule fois, après le flux.
                try:
                    for evt in generate_text_stream(
                        prompt=retrieval["prompt"],
                        provider=routed_provider,
                        model=routed_model,
                        context=LlmExecutionContext.for_rag_space(
                            rag_space, operation="chat.answer.stream", correlation_id=request_id or ""
                        ),
                        options=factual_options,
                        think=factual_think,
                    ):
                        if evt.get("type") == "delta":
                            delta = evt.get("delta") or ""
                            if delta:
                                answer_parts.append(delta)
                                if not buffered:
                                    yield {"event": "token", "data": {"delta": delta}}
                        elif evt.get("type") == "result":
                            final_result = evt
                except Exception as exc:  # noqa: BLE001 - régime de rupture explicite
                    if not answer_parts:
                        # Aucun fragment émis : comportement historique inchangé
                        # (``llm_error``), y compris pour les erreurs de repli.
                        raise
                    stream_error = type(exc).__name__
                    logger.warning(
                        "chat_stream_partial termination=stream_error error=%s deltas=%d",
                        stream_error,
                        len(answer_parts),
                    )
            else:
                # Pseudo-stream historique : le vrai streaming est désactivé
                # (AI_GATEWAY_STREAMING_ENABLED=0). On génère en NON-STREAMING —
                # mêmes presets/routage/think/retry que le chemin non-streaming —
                # puis on émet la réponse complète en un seul événement.
                llm_response = generate_text(
                    prompt=retrieval["prompt"],
                    provider=routed_provider,
                    model=routed_model,
                    context=LlmExecutionContext.for_rag_space(
                        rag_space, operation="chat.answer.sse", correlation_id=request_id or ""
                    ),
                    options=factual_options,
                    think=factual_think,
                )
                text = (llm_response.get("text") or "").strip()
                if text:
                    answer_parts.append(text)
                    if not buffered:
                        yield {"event": "token", "data": {"delta": text}}
                final_result = {
                    "provider": llm_response.get("provider"),
                    "model": llm_response.get("model"),
                    "usage": llm_response.get("usage"),
                    "fallback_used": bool(llm_response.get("fallback_used")),
                }
        else:
            # Gateway désactivée (dev/tests dégradés) : vrai streaming token-par-token.
            # CORRECTIF P0-C : ce chemin appelait le provider SANS contexte ni
            # politique. Il applique désormais la même garde que le sync.
            for evt in generate_text_stream(
                prompt=retrieval["prompt"],
                provider=llm_provider,
                model=llm_model,
                context=LlmExecutionContext.for_rag_space(
                    rag_space, operation="chat.answer.stream", correlation_id=request_id or ""
                ),
            ):
                if evt.get("type") == "delta":
                    delta = evt.get("delta") or ""
                    if delta:
                        answer_parts.append(delta)
                        if not buffered:
                            yield {"event": "token", "data": {"delta": delta}}
                elif evt.get("type") == "result":
                    final_result = evt

        answer = "".join(answer_parts).strip()
        if not answer:
            fallback = heuristic_answer(question, sources)
            if fallback:
                answer = fallback

        # Même contrôle que le chemin synchrone ; en mode tampon, rien n'a encore été émis.
        answer, forced_kind, numeric_guard = _numeric_evidence_guard(
            answer, retrieval=retrieval, question=question, answer_language=target_language)
        if forced_kind:
            answer_kind = forced_kind
        elif answer_kind:
            answer = _detail_prefix(answer_kind, target_language) + answer
        if buffered and answer:
            yield {"event": "token", "data": {"delta": answer}}

        provider_used = (final_result or {}).get("provider") or llm_provider
        model_used = (final_result or {}).get("model") or llm_model
        usage = (final_result or {}).get("usage")
        pt, ct, tokens, cost = compute_cost_from_usage(
            provider=provider_used, model=model_used, usage=usage
        )

        final_answer, sources_contract = finalize_answer_with_sources(
            answer, sources, retrieval=retrieval,
            # C'est le chemin de l'interface (`chat_stream`) : c'est ici que la
            # troncature doit être vue, faute de quoi elle reste invisible pour son
            # lecteur — la ligne « Sources: » est ajoutée juste après et donne à une
            # réponse coupée l'apparence d'une réponse finie.
            answer_truncated=answer_was_truncated(
                (final_result or {}).get("usage"), settings.RAG_FACTUAL_NUM_PREDICT,
                # `raw` porte `done_reason` — la raison d'arrêt déclarée par la
                # Gateway. Elle prime sur le comptage de tokens.
                raw=(final_result or {}).get("raw")),
        )
        # Après génération : le LLM ne peut plus retirer l'avertissement.
        final_answer = _apply_answer_prefix(answer_prefix, final_answer, target_language)

        maybe_update_log(
            log_id,
            answer=final_answer,
            # Flux rompu APRÈS un premier fragment : la réponse produite est
            # conservée, mais l'incident reste tracé. Le modèle n'a pas d'état
            # "partiel" ; `status=error` + `error_stage=llm` est la forme honnête,
            # et le texte réellement affiché reste consultable dans `answer`.
            status="error" if stream_error else "ok",
            error_stage="llm" if stream_error else "",
            policy=answer_kind or "",
            llm_provider=provider_used,
            llm_model=model_used,
            llm_prompt_tokens=pt,
            llm_completion_tokens=ct,
            llm_total_tokens=tokens,
            llm_cost_estimated=cost,
        )

        if thread is not None:
            try:
                append_message(
                    thread=thread,
                    role="assistant",
                    content=final_answer,
                    sources=sources,
                    request_id=rid,
                    rag_query_log_id=log_id,
                )
            except Exception:
                _log_persist_failure(thread=thread, role="assistant", stage="standard", request_id=rid)

        yield {
            "event": "done",
            "data": {
                "answer": final_answer,
                "sources": sources,
                "provider": provider_used,
                "model": model_used,
                "tokens": tokens,
                "usage": usage,
                "thread_id": str(thread.id) if thread else None,
                "status": "partial" if stream_error else "ok",
                # Contrat de flux : une réponse tronquée est ANNONCÉE comme
                # telle, jamais présentée comme complète.
                "partial": bool(stream_error),
                "termination": "stream_error" if stream_error else "done",
                "log_id": log_id,
                "answer_kind": answer_kind or ("explain" if _is_detailed_mode(retrieval) else "ok"),
                # Le flux ne peut pas porter le contrat sur l'événement ``sources`` (émis
                # AVANT la génération) : il arrive donc avec ``done``, avec le texte final.
                "sources_contract": sources_contract.to_dict(),
                "provider_effective": provider_used,
                "fallback_used": bool((final_result or {}).get("fallback_used")),
                "numeric_evidence_guard": numeric_guard,
            },
        }
        return

    except LLMError:
        maybe_update_log(log_id, status="error", error_stage="llm")
        yield {
            "event": "error",
            "data": {"detail": "llm_error", "request_id": rid or None, "thread_id": str(thread.id) if thread else None},
        }
        yield {
            "event": "done",
            "data": {"request_id": rid or None, "thread_id": str(thread.id) if thread else None, "log_id": log_id},
        }
        return

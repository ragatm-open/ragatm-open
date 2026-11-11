# backend/apps/chat/services/chat_fallback.py
from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError, generate_text, generate_text_stream

from apps.rag_core.services.pricing import compute_cost_from_usage

from .chat_audit import maybe_update_log
from .chat_formatting import normalize_llm_overrides, strip_model_sources
from .chat_threads import append_message


def mode_b_enabled() -> bool:
    """
    Mode B = fallback LLM (hors-docs) quand le RAG ne trouve pas de sources suffisantes.
    """
    return bool(getattr(settings, "RAG_MODE_B_FALLBACK_ENABLED", False))


# Bandeau UI préfixé à la réponse fallback (persisté). Localisé avec la réponse.
FALLBACK_DISCLAIMER_FR = "⚠️ Réponse hors documents du RagSpace.\n\n"
FALLBACK_DISCLAIMER_EN = "⚠️ Answer outside the RagSpace documents.\n\n"


def fallback_disclaimer(answer_language: str | None = None) -> str:
    return FALLBACK_DISCLAIMER_EN if answer_language == "en" else FALLBACK_DISCLAIMER_FR


def build_general_fallback_prompt(question: str, *, answer_language: str | None = None) -> str:
    # IMPORTANT: on ne met PAS de "Sources:" et on force une réponse honnête.
    if answer_language == "en":
        return (
            "You are an assistant. Answer the question using your general knowledge.\n"
            "Constraint: the following answer is OUTSIDE the RagSpace documents (no citation available).\n"
            "Be brief, factual, and if unsure, clearly state your uncertainty.\n"
            "Respond in English.\n\n"
            f"Question: {question}\n"
            "Answer:"
        )
    return (
        "Tu es un assistant. Réponds à la question en te basant sur tes connaissances générales.\n"
        "Contrainte: la réponse suivante est HORS des documents du RagSpace (aucune citation disponible).\n"
        "Sois bref, factuel, et si tu n'es pas certain, indique clairement ton incertitude.\n"
        "Réponds en français.\n\n"
        f"Question: {question}\n"
        "Réponse:"
    )


def run_fallback_sync(
    *, question: str, rag_space, thread, request_id: str, log_id: str | None,
    answer_language: str | None = None,
) -> dict:
    """
    Sync fallback: returns chat payload identical to previous behavior.
    Raises LLMError as before.
    """
    llm_provider, llm_model = normalize_llm_overrides(rag_space)

    llm_response = generate_text(
        prompt=build_general_fallback_prompt(question, answer_language=answer_language),
        provider=llm_provider,
        model=llm_model,
        context=LlmExecutionContext.for_rag_space(
            rag_space, operation="chat.fallback", correlation_id=request_id or ""
        ),
    )
    answer = (llm_response.get("text") or "").strip()
    answer = strip_model_sources(answer)

    # Disclaimer UI (localisé avec la réponse)
    answer = (fallback_disclaimer(answer_language) + answer).strip()

    prov_used = llm_response.get("provider") or llm_provider
    model_used = llm_response.get("model") or llm_model
    pt, ct, tt, cost = compute_cost_from_usage(
        provider=prov_used, model=model_used,
        usage=llm_response.get("usage"), total=llm_response.get("tokens"),
    )
    maybe_update_log(
        log_id,
        status="ok",
        policy="fallback_general",
        answer=answer,
        error_stage="",
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
            sources=[],  # pas de sources
            request_id=request_id,
            rag_query_log_id=log_id,
        )

    return {
        "answer": answer,
        "sources": [],
        "thread_id": str(thread.id) if thread else None,
        "status": "fallback_general",
        "log_id": log_id,  # ✅ critical for feedback
    }


def run_fallback_stream(
    *,
    question: str,
    rag_space,
    thread,
    request_id: str,
    log_id: str | None,
    answer_language: str | None = None,
):
    """
    Streaming fallback: yields SSE events identical to previous behavior.
    """
    llm_provider, llm_model = normalize_llm_overrides(rag_space)

    # Pas de sources en mode fallback (front can re-render)
    yield {"event": "sources", "data": {"sources": []}}

    answer_parts: list[str] = []
    final_result: dict[str, Any] | None = None

    disclaimer = fallback_disclaimer(answer_language)
    yield {"event": "token", "data": {"delta": disclaimer}}
    answer_parts.append(disclaimer)

    # CORRECTIF P0-C : ce chemin ne transmettait ni contexte ni opt-in.
    for evt in generate_text_stream(
        prompt=build_general_fallback_prompt(question, answer_language=answer_language),
        provider=llm_provider,
        model=llm_model,
        context=LlmExecutionContext.for_rag_space(
            rag_space, operation="chat.fallback.stream", correlation_id=request_id or ""
        ),
    ):
        if evt.get("type") == "delta":
            delta = evt.get("delta") or ""
            if delta:
                answer_parts.append(delta)
                yield {"event": "token", "data": {"delta": delta}}
        elif evt.get("type") == "result":
            final_result = evt

    answer = strip_model_sources("".join(answer_parts).strip())

    provider_used = (final_result or {}).get("provider") or llm_provider
    model_used = (final_result or {}).get("model") or llm_model
    usage = (final_result or {}).get("usage")
    pt, ct, tokens, cost = compute_cost_from_usage(
        provider=provider_used, model=model_used, usage=usage
    )

    maybe_update_log(
        log_id,
        status="ok",
        policy="fallback_general",
        answer=answer,
        error_stage="",
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
                content=answer,
                sources=[],
                request_id=request_id,
                rag_query_log_id=log_id,
            )
        except Exception:
            pass

    yield {
        "event": "done",
        "data": {
            "answer": answer,
            "sources": [],
            "provider": provider_used,
            "model": model_used,
            "tokens": tokens,
            "usage": usage,
            "thread_id": str(thread.id) if thread else None,
            "status": "fallback_general",
            "log_id": log_id,  # ✅ critical for feedback
        },
    }

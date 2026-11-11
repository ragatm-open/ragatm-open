# backend/apps/chat/services/chat_formatting.py
from __future__ import annotations

import re
from typing import Any

from apps.rag_core.services.answer_contract import (
    AnswerSourcesContract,
    finalize_answer,
    strip_sources_line,
)

from .chat_audit import safe_str


def heuristic_answer(question: str, sources: list[dict]) -> str | None:
    q = (question or "").lower()

    if "nom" in q and "startup" in q:
        for s in sources:
            m = re.search(r"Nom de la startup\s*:\s*(.+)", safe_str(s.get("snippet")), re.I)
            if m:
                return f"La startup s’appelle {m.group(1).strip()}."

    return None


def normalize_llm_overrides(rag_space) -> tuple[str | None, str | None]:
    provider = getattr(rag_space, "llm_provider", None)
    model = getattr(rag_space, "llm_model", None)

    if isinstance(provider, str):
        provider = provider.strip().lower() or None
    else:
        provider = None

    if isinstance(model, str):
        model = model.strip() or None
    else:
        model = None

    return provider, model


def strip_model_sources(answer: str) -> str:
    """Alias historique — la logique vit dans ``rag_core.services.answer_contract``."""
    return strip_sources_line(answer)


def retrieved_sources_count(retrieval: dict | None, sources: list[dict] | None) -> int:
    """Nombre de sources RETROUVÉES par le retrieval (≥ nombre transmis au modèle).

    La trace P1-A0 porte déjà la valeur (`final_candidate_count`) : le pool de candidats
    survivants AVANT diversification/budget de prompt. Sans trace exploitable, on retombe sur
    le nombre de sources transmises — jamais sur zéro, qui laisserait croire à un retrieval
    vide alors qu'il a fourni des extraits.
    """
    n_prompt = len(sources or [])
    trace = (retrieval or {}).get("retrieval_trace") or {}
    n_retrieved = trace.get("final_candidate_count")
    if isinstance(n_retrieved, int) and n_retrieved > n_prompt:
        return n_retrieved
    return n_prompt


def finalize_answer_with_sources(
    answer: str,
    sources: list[dict],
    *,
    retrieval: dict | None = None,
    force_abstention: bool = False,
    answer_truncated: bool | None = None,
) -> tuple[str, AnswerSourcesContract]:
    """Texte final + contrat de sources (retrouvées / transmises / réellement citées).

    ⚠️ Changement de contrat volontaire : la ligne « Sources: … » n'énumère plus le top-k
    transmis au modèle mais les seules citations qu'il a émises, et disparaît sous une
    abstention. Voir ``answer_contract.finalize_answer``.
    """
    return finalize_answer(
        answer,
        prompt_sources_count=len(sources or []),
        retrieved_sources_count=retrieved_sources_count(retrieval, sources),
        force_abstention=force_abstention,
        answer_truncated=answer_truncated,
    )


def tokens_from_usage(provider_used: str | None, usage: Any) -> int | None:
    """
    Best-effort tokens estimate (kept identical to previous logic).
    Returns None if unknown.
    """
    tokens = 0
    try:
        if isinstance(usage, dict) and (provider_used or "") == "ollama":
            pe = usage.get("prompt_eval_count")
            ev = usage.get("eval_count")
            if isinstance(pe, int) and isinstance(ev, int):
                tokens = pe + ev
        elif isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            # Réponses de l'AI Gateway : `usage.total_tokens` (contrat gateway-api).
            tokens = usage["total_tokens"]
    except Exception:
        tokens = 0
    return tokens or None

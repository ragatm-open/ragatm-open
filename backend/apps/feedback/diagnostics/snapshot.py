"""Assemblage du snapshot diagnostique.

Trois sources, par ordre de fidélité DÉCROISSANTE :

1. le **tampon** — la seule capture exacte ;
2. le **replay** — un retrieval rejoué, donc soumis à tout ce qui a changé
   depuis (index réindexé, seuils modifiés, modèle d'embedding remplacé) ;
3. ce qui reste du **journal** — les sources retenues, la politique, le modèle.

Le mode retenu est écrit noir sur blanc dans ``capture_mode``. Présenter un
replay comme une capture ferait tirer des conclusions sur un état qui n'est pas
celui de l'incident : c'est l'erreur que ce champ existe pour empêcher.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.feedback.models import FeedbackDiagnosticSnapshot

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1

#: Le prompt n'est capturé que si la configuration l'autorise explicitement.
PROMPT_CAPTURE_FULL = "full"
PROMPT_CAPTURE_HASH_ONLY = "hash_only"


def retention_days() -> int:
    try:
        return max(
            1, int(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_SNAPSHOT_RETENTION_DAYS", 30))
        )
    except (TypeError, ValueError):
        return 30


def max_bytes() -> int:
    try:
        return max(
            4096, int(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_MAX_SNAPSHOT_BYTES", 262144))
        )
    except (TypeError, ValueError):
        return 262144


def prompt_capture_allowed() -> bool:
    return bool(getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT", False))


def _size_of(payload: Any) -> int:
    try:
        return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def compute_contains_production_content(
    *, selected_chunks: dict[str, Any], generation_metadata: dict[str, Any],
    request_payload: dict[str, Any],
) -> bool:
    """Décide si le snapshot porte du contenu de production. CALCULÉ, pas déclaré.

    Est du contenu de production tout ce qui vient des documents ou de la
    conversation : un extrait, un nom de fichier, la question, la réponse, le
    prompt. Les identifiants, scores et rangs n'en sont pas.
    """
    for source in (selected_chunks.get("sources") or []):
        if not isinstance(source, dict):
            continue
        if source.get("snippet") or source.get("filename"):
            return True
    if generation_metadata.get("prompt"):
        return True
    if generation_metadata.get("answer") or request_payload.get("question"):
        return True
    return False


def build_selected_chunks(log) -> dict[str, Any]:
    """Sources RETENUES, copiées telles qu'auditées (contrat v3 inchangé)."""
    sources = list(getattr(log, "sources", None) or []) if log is not None else []
    return {
        "sources_schema_version": int(getattr(log, "sources_schema_version", 3) or 3)
        if log is not None
        else 3,
        "sources": sources,
    }


def build_generation_metadata(log, *, prompt: str | None = None) -> dict[str, Any]:
    """Métadonnées de génération, prompt inclus SEULEMENT si autorisé.

    Quand le prompt est capturé, son SHA-256 est confronté au ``prompt_hash``
    du journal. C'est ce contrôle croisé qui transforme un hash décoratif en
    garantie d'intégrité : il dit si le prompt qu'on relit est bien celui qui a
    été envoyé au modèle.
    """
    meta: dict[str, Any] = {
        "provider": getattr(log, "llm_provider", "") or "",
        "model": getattr(log, "llm_model", "") or "",
        "prompt_hash": getattr(log, "prompt_hash", "") or "",
        "policy": getattr(log, "policy", "") or "",
        "status": getattr(log, "status", "") or "",
        "answer": getattr(log, "answer", "") or "",
        "latency_ms": getattr(log, "latency_ms", None),
        "prompt_tokens": getattr(log, "llm_prompt_tokens", None),
        "completion_tokens": getattr(log, "llm_completion_tokens", None),
        "total_tokens": getattr(log, "llm_total_tokens", None),
        "prompt_capture": PROMPT_CAPTURE_HASH_ONLY,
    }

    if prompt and prompt_capture_allowed():
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        meta["prompt"] = prompt
        meta["prompt_capture"] = PROMPT_CAPTURE_FULL
        meta["prompt_sha256"] = digest
        # Divergence = le prompt relu n'est PAS celui envoyé au modèle. On le
        # dit plutôt que de laisser croire à une reproduction fidèle.
        meta["prompt_hash_verified"] = bool(meta["prompt_hash"]) and digest == meta["prompt_hash"]

    return meta


def build_effective_config(*, rag_space=None, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Configuration EFFECTIVE — que des réglages, jamais de donnée."""
    selection = (trace or {}).get("selection") or {}
    config: dict[str, Any] = {
        "thresholds": selection.get("thresholds") or {},
        "budgets": selection.get("budgets") or {},
        "embedding_model": (trace or {}).get("embedding_model")
        or str(getattr(rag_space, "embedding_model", "") or ""),
        "embedding_provider": str(getattr(rag_space, "embedding_provider", "") or ""),
        "flags": {
            name: getattr(settings, name, None)
            for name in (
                "RAG_HYBRID_ENABLED",
                "RAG_RERANK_ENABLED",
                "RAG_RETRIEVAL_DIVERSITY_ENABLED",
                "RAG_STRUCTURAL_NOISE_PENALTY_ENABLED",
                "RAG_STRONG_SIGNALS_ENABLED",
                "RAG_LOWCONF_ENABLED",
                "RAG_QUERY_REWRITE_ENABLED",
                "RAG_ANSWER_LANGUAGE_POLICY_ENABLED",
            )
        },
    }
    return config


def build_request_payload(
    log, *, language_decision: dict[str, Any] | None = None,
    conversation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Requête d'origine + décisions qui en dépendent (langue, contexte)."""
    return {
        "question": getattr(log, "question", "") or "",
        "request_id": getattr(log, "request_id", "") or "",
        "language": language_decision or {},
        "conversation": conversation or {},
    }


def create_snapshot(
    *,
    feedback,
    capture_mode: str,
    request_payload: dict[str, Any],
    effective_config: dict[str, Any],
    retrieval_trace: dict[str, Any],
    selected_chunks: dict[str, Any],
    generation_metadata: dict[str, Any],
) -> FeedbackDiagnosticSnapshot:
    """Crée le snapshot IMMUABLE, en respectant le plafond de taille.

    La troncature retire d'abord les candidats (l'information la plus
    volumineuse et la plus redondante), puis le prompt. Elle est toujours
    SIGNALÉE : ``truncated=True`` et un motif lisible.
    """
    truncated = bool(retrieval_trace.get("truncated", False))
    reason = "buffer_top_k" if truncated else ""
    budget = max_bytes()

    payload = {
        "request_payload": request_payload,
        "effective_config": effective_config,
        "retrieval_trace": retrieval_trace,
        "selected_chunks": selected_chunks,
        "generation_metadata": generation_metadata,
    }

    while _size_of(payload) > budget and (retrieval_trace.get("candidates") or []):
        retrieval_trace["candidates"] = retrieval_trace["candidates"][:-1]
        payload["retrieval_trace"] = retrieval_trace
        truncated = True
        reason = "snapshot_max_bytes"

    if _size_of(payload) > budget and generation_metadata.get("prompt"):
        generation_metadata = dict(generation_metadata)
        generation_metadata.pop("prompt", None)
        generation_metadata["prompt_capture"] = PROMPT_CAPTURE_HASH_ONLY
        payload["generation_metadata"] = generation_metadata
        truncated = True
        reason = "snapshot_max_bytes_prompt_dropped"

    contains_production = compute_contains_production_content(
        selected_chunks=selected_chunks,
        generation_metadata=generation_metadata,
        request_payload=request_payload,
    )

    snapshot = FeedbackDiagnosticSnapshot(
        feedback=feedback,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        capture_mode=capture_mode,
        request_payload=request_payload,
        effective_config=effective_config,
        retrieval_trace=retrieval_trace,
        selected_chunks=selected_chunks,
        generation_metadata=generation_metadata,
        contains_production_content=contains_production,
        truncated=truncated,
        truncation_reason=reason[:128],
        expires_at=timezone.now() + timedelta(days=retention_days()),
    )
    snapshot.save()

    logger.info(
        "feedback_snapshot_created feedback_id=%s capture_mode=%s candidate_count=%s "
        "snapshot_size=%s truncated=%s contains_production_content=%s",
        feedback.id,
        capture_mode,
        len(retrieval_trace.get("candidates") or []),
        _size_of(payload),
        truncated,
        contains_production,
    )
    return snapshot

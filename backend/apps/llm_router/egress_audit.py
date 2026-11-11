"""Émission des événements d'audit d'egress LLM (Lot 1 — P0-C).

Contrat de confidentialité — ce module ne transmet JAMAIS au stockage :
prompt, chunk, réponse, nom de fichier, secret, clé d'API, ni message d'erreur
brut d'un tiers (un message d'erreur peut contenir un écho du prompt).

Contrat de robustesse — l'audit est **fail-open** : une écriture d'audit qui
échoue ne doit pas casser une génération en cours. L'échec est journalisé au
niveau ``error`` afin de rester détectable. Cohérent avec ``emit_action_event``.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Bornes de sécurité sur les champs texte (évite tout stockage volumineux).
_MAX_LEN = 100


def _clip(value: Any, limit: int = _MAX_LEN) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def record_egress_event(
    *,
    context: Any,
    requested_provider: str = "",
    effective_provider: str = "",
    model: str = "",
    gateway_attempted: bool = False,
    fallback_attempted: bool = False,
    fallback_used: bool = False,
    fallback_reason: str = "",
    status: str = "ok",
    error_type: str = "",
    latency_ms: int | None = None,
    prompt_chars: int | None = None,
    usage: dict[str, Any] | None = None,
    cost_estimated: Any = None,
    embedding: bool = False,
) -> None:
    """Enregistre un événement d'egress. Ne lève jamais.

    ``fallback_reason`` et ``error_type`` doivent être des **noms de classe**
    d'exception, pas des messages : c'est ce qui garantit qu'aucun écho de prompt
    renvoyé par un tiers ne se retrouve en base.

    ``embedding=True`` fait porter la colonne ``external_egress_allowed`` sur
    ``external_embedding_allowed`` plutôt que sur ``external_generation_allowed``.
    Sans cela, un refus d'embedding serait enregistré avec la permission de
    GÉNÉRATION de l'espace — une ligne d'audit qui contredirait la décision qu'elle
    est censée tracer. Le défaut ``False`` laisse tous les appelants de génération
    strictement inchangés.
    """
    try:
        from apps.audit.models import LlmEgressAuditEvent

        fields: dict[str, Any] = {
            "operation": _clip(getattr(context, "operation", ""), 64),
            "workspace_id": _clip(getattr(context, "workspace_id", "") or "", 64),
            "user_id": _clip(getattr(context, "user_id", "") or "", 64),
            "correlation_id": _clip(getattr(context, "correlation_id", "") or "", 64),
            "external_egress_allowed": bool(
                getattr(getattr(context, "policy", None), "external_embedding_allowed", False)
                if embedding
                else getattr(context, "external_egress_allowed", False)
            ),
            "policy_source": _clip(
                getattr(getattr(context, "policy", None), "policy_source", ""), 32
            ),
            "requested_provider": _clip(requested_provider, 32),
            "effective_provider": _clip(effective_provider, 32),
            "model": _clip(model, 128),
            "gateway_attempted": bool(gateway_attempted),
            "fallback_attempted": bool(fallback_attempted),
            "fallback_used": bool(fallback_used),
            "fallback_reason": _clip(fallback_reason),
            "status": _clip(status, 16) or "ok",
            "error_type": _clip(error_type),
            "latency_ms": latency_ms,
            "prompt_chars": prompt_chars,
        }

        rs = getattr(context, "rag_space_id", None)
        if rs:
            fields["rag_space_id"] = rs

        u = usage or {}
        if isinstance(u, dict):
            pt = u.get("input_tokens", u.get("prompt_eval_count"))
            ct = u.get("output_tokens", u.get("eval_count"))
            tt = u.get("total_tokens")
            if isinstance(pt, int):
                fields["prompt_tokens"] = pt
            if isinstance(ct, int):
                fields["completion_tokens"] = ct
            if isinstance(tt, int):
                fields["total_tokens"] = tt
            elif isinstance(pt, int) and isinstance(ct, int):
                fields["total_tokens"] = pt + ct

        if cost_estimated is not None:
            fields["cost_estimated"] = cost_estimated

        LlmEgressAuditEvent.objects.create(**fields)
    except Exception as exc:  # noqa: BLE001 — l'audit ne doit jamais casser la génération
        # `exc_info=True` est PROSCRIT ici : la trace inclurait la chaîne
        # d'exceptions courante, donc potentiellement le message d'erreur d'un
        # tiers — lequel peut contenir un écho du prompt. Seul le type est tracé.
        logger.error("llm_egress_audit_write_failed error_type=%s", type(exc).__name__)


def record_policy_change(
    *,
    actor: Any = None,
    scope_type: str = "ragspace",
    scope_ref: str = "",
    workspace_id: str = "",
    field_name: str,
    old_value: Any,
    new_value: Any,
    reason: str = "",
    request_id: str = "",
) -> None:
    """Trace un changement de politique d'egress. Ne lève jamais.

    À appeler pour TOUT changement autorisant ou retirant la sortie de données
    d'un périmètre — c'est le geste le plus sensible de la plateforme.
    """
    try:
        from apps.audit.models import EgressPolicyChangeLog

        EgressPolicyChangeLog.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            scope_type=_clip(scope_type, 16),
            scope_ref=_clip(scope_ref, 64),
            workspace_id=_clip(workspace_id, 64),
            field_name=_clip(field_name, 64),
            old_value=_clip(old_value, 255),
            new_value=_clip(new_value, 255),
            reason=_clip(reason, 255),
            request_id=_clip(request_id, 64),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "egress_policy_change_audit_write_failed error_type=%s", type(exc).__name__
        )

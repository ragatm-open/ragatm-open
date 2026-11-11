# backend/apps/actions/classifier.py
"""Classification LLM structurée et bornée du routeur d'actions (lot 4).

Le LLM ne fait que PROPOSER une intention (JSON strict validé) ; il n'exécute
jamais rien et ne peut proposer que des actions de LECTURE (allowlist). Local
uniquement : aucun fallback externe, aucun retry, timeout court.

Confidentialité : ni la question, ni le prompt, ni la sortie brute ne sont
journalisés. Seules des métadonnées bornées (modèle, durée, intent, validité)
alimentent l'audit.
"""
from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from rest_framework import serializers

from apps.llm_router.model_utils import gen_options
from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import LLMError, generate_text

logger = logging.getLogger(__name__)

# Liste FERMÉE d'intentions proposables (mutations exclues par construction).
_ALLOWED_INTENTS = ("search.answer", "search.list_documents", "search.news", "document.summarize", "none")
_ALT_INTENTS = ("search.answer", "search.list_documents", "search.news", "document.summarize")

_ALLOWED_KEYS = {"intent", "score", "parameters", "missing_parameters", "alternatives"}


class LlmActionClassificationSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=list(_ALLOWED_INTENTS))
    score = serializers.FloatField(min_value=0.0, max_value=1.0)
    parameters = serializers.DictField(required=False, default=dict)
    missing_parameters = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, default=list
    )
    alternatives = serializers.ListField(
        child=serializers.ChoiceField(choices=list(_ALT_INTENTS)), required=False, default=list
    )


# Invariant de configuration : le classifieur est LOCAL uniquement (jamais d'egress
# externe). Vérifié à chaque construction (fail-closed) + par space_provider épinglé.
CLASSIFIER_POLICY = "local_only"


def _classifier_enabled() -> bool:
    return bool(getattr(settings, "ACTIONS_LLM_CLASSIFIER_ENABLED", False))


def _config_error() -> str | None:
    """Retourne un motif si la config viole ``local_only`` / l'allowlist, sinon None.

    Fail-closed (§8) :
    - provider différent de local  -> refus ;
    - modèle épinglé absent d'une allowlist NON vide -> refus ;
    - modèle épinglé mais allowlist VIDE -> refus (un modèle pinné DOIT être allowlisté).
    (modèle vide = défaut Gateway local, autorisé en dev.)
    """
    prov = (getattr(settings, "ACTIONS_LLM_CLASSIFIER_PROVIDER", "local") or "local").strip().lower()
    if prov not in ("", "local", "default"):
        return "non_local_provider"
    model = (getattr(settings, "ACTIONS_LLM_CLASSIFIER_MODEL", "") or "").strip()
    allow = getattr(settings, "ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS", []) or []
    if model:
        if not allow:
            return "pinned_model_without_allowlist"
        if model not in allow:
            return "model_not_in_allowlist"
    return None


def _resolve_provider_model() -> tuple[str | None, str | None]:
    prov = (getattr(settings, "ACTIONS_LLM_CLASSIFIER_PROVIDER", "local") or "local").strip().lower()
    provider = None if prov in ("", "local", "default") else prov  # None => provider local
    model = (getattr(settings, "ACTIONS_LLM_CLASSIFIER_MODEL", "") or "").strip() or None
    return provider, model


def _build_prompt(text: str) -> str:
    return (
        "Tu es un CLASSIFICATEUR d'intention pour un assistant documentaire.\n"
        "Tu ne dois JAMAIS exécuter d'action, seulement classer la demande.\n"
        "Intentions autorisées (liste FERMÉE) :\n"
        "- search.answer : répondre à une question à partir des documents.\n"
        "- search.list_documents : lister les documents qui mentionnent un terme (paramètre: term).\n"
        "- search.news : actualités récentes sur une entité (paramètre: term).\n"
        "- document.summarize : résumer un document (paramètre: document_reference).\n"
        "- none : aucune des précédentes.\n\n"
        "Règles STRICTES :\n"
        "- N'invente jamais de nom de document ni d'identifiant.\n"
        "- N'inclus AUCUNE action de suppression/upload/réindexation/configuration.\n"
        "- Réponds UNIQUEMENT par un objet JSON, sans texte autour, avec EXACTEMENT ces clés :\n"
        '  {"intent": "...", "score": 0.0-1.0, "parameters": {}, "missing_parameters": [], "alternatives": []}\n\n'
        f"Demande utilisateur : {text}\n"
        "JSON :"
    )


def _strip_code_fence(s: str) -> str:
    """Retire une clôture markdown ``` (```json … ```) si présente. Ne fait RIEN
    d'autre : ce n'est pas une extraction du premier {...} d'un texte arbitraire
    (un bloc fencé est un objet unique non ambigu ; du texte libre reste rejeté)."""
    s = s.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        s = s.rstrip()
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _parse_strict(raw: str) -> dict | None:
    """JSON STRICT : après retrait d'une éventuelle clôture ```…```, l'objet ENTIER
    doit être du JSON valide (pas d'extraction du premier {...} d'un texte plus long,
    qui masquerait du texte ou plusieurs objets)."""
    s = _strip_code_fence((raw or "").strip())
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # Champs inconnus interdits.
    if set(obj.keys()) - _ALLOWED_KEYS:
        return None
    ser = LlmActionClassificationSerializer(data=obj)
    if not ser.is_valid():
        return None
    return dict(ser.validated_data)


def _audit(status: str, *, failure: str = "", intent: str = "", valid: bool | None = None,
           candidate_count: int = 0, model: str | None = None, duration_ms: int | None = None) -> None:
    try:
        from apps.audit.action_audit import emit_action_event

        emit_action_event(
            event_name=f"action.classification_{status}",
            action_name="action.classify",
            status="completed" if status == "completed" else "failed",
            detection_method="llm",
            duration_ms=duration_ms,
            metadata={
                "classifier_model": model or "",
                "candidate_count": candidate_count,
                "result_intent": intent,          # string seule, jamais de paramètres
                "result_valid": valid,
                "failure_category": failure,
            },
        )
    except Exception:  # pragma: no cover - audit best-effort
        pass


def classify_action(text: str, candidate_count: int = 0) -> dict | None:
    """Retourne une classification VALIDÉE (dict) ou ``None`` (timeout/erreur/invalide).

    Ne lève jamais. Local uniquement, sans retry, sans fallback externe.
    """
    if not _classifier_enabled():
        return None
    cfg_err = _config_error()
    if cfg_err:  # fail-closed : configuration non conforme à local_only/allowlist
        logger.error("classifier disabled: %s", cfg_err)
        _audit("failed", failure=cfg_err, model=None, candidate_count=candidate_count)
        return None
    provider, model = _resolve_provider_model()

    timeout = float(getattr(settings, "ACTIONS_LLM_CLASSIFIER_TIMEOUT_SECONDS", 3) or 3)
    t0 = time.perf_counter()
    try:
        out = generate_text(
            prompt=_build_prompt(text),
            provider=provider,
            model=model,
            timeout_s=timeout,
            # LOCAL STRICT, désormais structurel : le contexte système interdit
            # tout egress quel que soit le transport. L'ancien montage (épingler
            # un space_provider local) reposait sur une garde qui se contournait
            # dès que la Gateway était désactivée (constat P0-C #2).
            context=LlmExecutionContext.system_local_only(
                operation="actions.classify"
            ),
            retry_on_empty=False,            # aucun retry
            options=gen_options(temperature=0.0, num_predict=128),
            think=False,
        )
    except LLMError:
        _audit("failed", failure="provider_error", model=model, candidate_count=candidate_count,
               duration_ms=int((time.perf_counter() - t0) * 1000))
        return None
    except Exception:
        _audit("failed", failure="exception", model=model, candidate_count=candidate_count,
               duration_ms=int((time.perf_counter() - t0) * 1000))
        return None

    dur = int((time.perf_counter() - t0) * 1000)
    parsed = _parse_strict((out.get("text") or ""))
    if parsed is None:
        _audit("failed", failure="invalid_json", model=model, candidate_count=candidate_count, duration_ms=dur)
        return None

    _audit("completed", intent=parsed["intent"], valid=True, candidate_count=candidate_count,
           model=model, duration_ms=dur)
    return parsed


def get_classifier():
    """Callable injecté dans ``ActionRouter`` (ou None si désactivé/mal configuré)."""
    if not _classifier_enabled():
        return None
    if _config_error():  # fail-closed : config non conforme -> classifieur OFF (déterministe)
        logger.error("classifier disabled at build: %s", _config_error())
        return None

    def _fn(norm_text: str, allowed_actions: tuple, candidate_count: int = 0) -> dict | None:
        return classify_action(norm_text, candidate_count=candidate_count)

    return _fn

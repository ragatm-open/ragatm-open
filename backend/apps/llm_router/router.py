# backend/apps/llm_router/router.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import logging
import os
import json
import requests
from django.conf import settings

from .config_access import router_setting

if TYPE_CHECKING:  # pragma: no cover - typage seulement
    from .egress import LlmExecutionContext

logger = logging.getLogger(__name__)

# Retry "réponse vide" (chemin Gateway uniquement) : borné à UN SEUL retry, avec
# think=false et un budget de tokens RELEVÉ si l'appelant en demandait peu. Ne s'applique
# JAMAIS aux erreurs (timeout/4xx/5xx/JSON invalide) qui suivent le chemin de fallback.
#
# PLANCHER seulement, plus de plafond. Le plancher est la raison d'être du mécanisme : une
# réponse vide vient typiquement d'un budget trop court pour produire quoi que ce soit après
# le bloc de raisonnement interne (qwen3:14b), et des appelants légitimes demandent 70 ou 128.
#
# Le plafond 2048 a été retiré : il ABAISSAIT un budget que l'appelant avait choisi. Il était
# sans effet à sa création (11/07/2026), où le plus gros preset valait justement 2048 ; il n'a
# commencé à mordre qu'avec un appelant plus large. Il ne protégeait de rien : le retry ne suit
# qu'un HTTP 200, donc les options du premier appel ont DÉJÀ été acceptées par la Gateway, et
# c'est elle qui refuse ce qui dépasse le maximum de son profil — avant même ce retry.
#
# Contrepartie ASSUMÉE : le coût pire cas d'un retry vide n'est plus borné à 2048 tokens mais
# vaut le budget de l'appelant (aujourd'hui au plus 3072, sur un GPU partagé avec la production).
_EMPTY_RETRY_NUM_PREDICT_FLOOR = 1024


def _is_blank_text(v: Any) -> bool:
    return not (isinstance(v, str) and v.strip())


def _empty_retry_num_predict(options: dict[str, Any] | None) -> int:
    """num_predict du retry = max(initial, 1024). Le budget de l'appelant n'est jamais abaissé."""
    base = 0
    np = (options or {}).get("num_predict")
    if isinstance(np, int) and not isinstance(np, bool):
        base = np
    return max(base, _EMPTY_RETRY_NUM_PREDICT_FLOOR)


def _enforce_egress(
    *,
    provider: str,
    context: "LlmExecutionContext | None",
    operation: str,
    embedding: bool = False,
) -> None:
    """Barrière avant tout appel réseau externe — ALIAS de la barrière canonique.

    L'implémentation vit désormais dans ``egress.enforce_external_egress`` pour
    qu'``embeddings`` puisse l'appeler sans importer le routeur de génération (et
    sans que qui que ce soit soit tenté d'en écrire une seconde). Comportement
    strictement inchangé : ce module reste le seul point d'appel externe côté
    génération, et il passe toujours par ici.
    """
    from .egress import enforce_external_egress

    enforce_external_egress(
        provider=provider, context=context, operation=operation, embedding=embedding
    )


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str | None = None
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


class LLMError(RuntimeError):
    pass


def _get_setting(name: str) -> Any:
    """Réglage DÉCLARÉ : settings d'abord, environnement ensuite.

    Le repli sur l'environnement est CONSERVÉ — plusieurs réglages du routeur ne
    sont volontairement pas projetés dans les settings. Ce qui change : le nom
    doit être déclaré au registre, et le défaut vient de là. Auparavant,
    n'importe quel nom écrit dans le code devenait un réglage réel mais invisible
    du registre, de la documentation et de la comparaison de parité.
    """
    return router_setting(name)


def _default_provider() -> str:
    """
    Canonical default chat provider.

    Source of truth = RAG_LLM_PROVIDER (settings/env).
    DEFAULT_LLM_PROVIDER is kept only as a backward-compatible alias.
    Effective default = "ollama" (dev/local), aligned with config.settings.base.
    """
    return (
        (_get_setting("RAG_LLM_PROVIDER") or "").strip().lower()
        or (_get_setting("DEFAULT_LLM_PROVIDER") or "").strip().lower()
        or "ollama"
    )


def _default_model_for_provider(provider: str) -> str:
    """Modèle par défaut du provider de génération DIRECT (édition Open : `ollama` seul).

    RAGatm Open ne connaît aucun fournisseur externe : la génération nominale passe
    par l'AI Gateway (`AI_GATEWAY_MODEL`) ; ce chemin direct ne sert qu'aux
    environnements de développement/test où la Gateway est désactivée.
    """
    p = (provider or "").strip().lower()
    if p == "ollama":
        return (
            (_get_setting("DEFAULT_OLLAMA_MODEL") or "").strip()
            or (_get_setting("OLLAMA_DEFAULT_MODEL") or "").strip()
            or "compact-generation-8"
        )
    raise LLMError(
        f"Provider {p!r} is not available in RAGatm Open: generation is served by the AI Gateway."
    )


def _ollama_generate(*, prompt: str, model: str, timeout_s: float) -> LLMResult:
    base_url = (_get_setting("OLLAMA_BASE_URL") or "").rstrip("/")
    url = f"{base_url}/api/generate"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": int(_get_setting("OLLAMA_NUM_CTX")),
            "temperature": float(_get_setting("OLLAMA_TEMPERATURE")),
            "top_p": float(_get_setting("OLLAMA_TOP_P")),
            "repeat_penalty": float(_get_setting("OLLAMA_REPEAT_PENALTY")),
        },
    }

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    if resp.status_code >= 400:
        # Idem : pas d'écho du corps de réponse dans l'exception.
        raise LLMError(f"Ollama error {resp.status_code}")

    data = resp.json()
    text = ""
    if isinstance(data, dict) and isinstance(data.get("response"), str):
        text = data.get("response") or ""

    usage = None
    if isinstance(data, dict):
        u: dict[str, Any] = {}
        for k in (
            "prompt_eval_count",
            "eval_count",
            "total_duration",
            "load_duration",
            "prompt_eval_duration",
            "eval_duration",
        ):
            if k in data:
                u[k] = data.get(k)
        usage = u or None

    return LLMResult(
        text=text,
        provider="ollama",
        model=model,
        usage=usage,
        raw=data if isinstance(data, dict) else None,
    )


def _compute_total_tokens(provider: str, usage: dict[str, Any] | None) -> int:
    """Total de tokens best-effort (total/input+output génériques ; Ollama eval counts).

    Le COÛT n'est PAS calculé ici : le router reste agnostique de la tarification.
    Le coût est estimé par les consommateurs (chat, résumé, benchmark) via la
    source unique ``apps.rag_core.services.pricing`` à partir de ``usage``.
    """
    if not usage:
        return 0

    p = provider.lower().strip()

    if p == "ollama":
        pe = usage.get("prompt_eval_count")
        ev = usage.get("eval_count")
        if isinstance(pe, int) and isinstance(ev, int):
            return pe + ev
        return 0

    # Forme générique (Gateway et tout fournisseur : total, ou entrée + sortie).
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    it = usage.get("input_tokens", usage.get("prompt_tokens"))
    ot = usage.get("output_tokens", usage.get("completion_tokens"))
    if isinstance(it, int) and isinstance(ot, int):
        return it + ot
    return 0


def _generate_text_direct(
    *,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float | None = None,
    context: "LlmExecutionContext | None" = None,
) -> dict:
    """Chemin DIRECT (Ollama local, développement/tests uniquement). Contrat de retour INCHANGÉ.

    CORRECTIF P0-C (fail-open n°2) : ce chemin ignorait totalement la politique
    d'egress. Conséquence prouvée : avec ``AI_GATEWAY_ENABLED=0`` et
    ``RAG_LLM_PROVIDER`` pointant un fournisseur externe (configuration historique), **toutes** les
    gardes étaient contournées — y compris pour des opérations déclarées
    ``local_only``. Le mode de transport ne décide plus de la confidentialité.

    Tout provider EXTERNE exige désormais un contexte l'autorisant explicitement.
    Un provider interne (ollama) ne requiert pas de contexte : rien ne sort.
    """
    provider = (provider or _default_provider()).strip().lower()
    model = (model or "").strip() or _default_model_for_provider(provider)
    timeout_s = timeout_s or float(_get_setting("LLM_TIMEOUT_S"))

    _enforce_egress(provider=provider, context=context, operation="generate_text_direct")

    if provider == "ollama":
        result = _ollama_generate(prompt=prompt, model=model, timeout_s=timeout_s)
    else:
        # Édition Open : aucun fournisseur externe n'existe (ni code, ni clé, ni URL).
        raise LLMError(
            f"Provider {provider!r} is not available in RAGatm Open: generation is served "
            "by the AI Gateway (or, in development only, by a local Ollama)."
        )

    tokens = _compute_total_tokens(result.provider, result.usage)

    return {
        "text": result.text,
        "tokens": tokens,
        # Coût non calculé au niveau du router (agnostique tarification) : estimé
        # par les consommateurs via apps.rag_core.services.pricing. None = inconnu ici.
        "cost": None,
        "provider": result.provider,
        "model": result.model,
        "usage": result.usage,
        "raw": result.raw,
    }


# PR-G3 : les réglages AI Gateway sont résolus via runtime_config (overlay DB)
# au scope environment/global (le router n'a pas de contexte workspace). Tant que
# CORE_CONFIG_RUNTIME_ENABLED=False (défaut), runtime_config renvoie strictement les
# settings => parité stricte avec PR 5c.
def _gateway_enabled() -> bool:
    from apps.rag_core.core import runtime_config

    return bool(runtime_config.get("AI_GATEWAY_ENABLED", default=False))


def _gateway_streaming_enabled() -> bool:
    """True si le streaming natif de la Gateway est autorisé (défaut False).

    Lu à CHAQUE appel (jamais mémorisé) : c'est le levier de repli d'exploitation,
    il doit prendre effet sans redéploiement du code.
    """
    from apps.rag_core.core import runtime_config

    return bool(runtime_config.get("AI_GATEWAY_STREAMING_ENABLED", default=False))


def _gateway_fallback_enabled() -> bool:
    from apps.rag_core.core import runtime_config

    return bool(runtime_config.get("AI_GATEWAY_FALLBACK_ENABLED", default=True))


def _gateway_fallback_provider() -> str:
    """Provider de repli explicite quand la Gateway échoue.

    Édition Open : la seule valeur admise est "none" — il n'existe aucun fournisseur de
    repli. Toute autre valeur est une configuration invalide (``llm_router.E004``).
    """
    from apps.rag_core.core import runtime_config

    return (
        str(runtime_config.get("AI_GATEWAY_FALLBACK_PROVIDER", default="none") or "none")
        .strip()
        .lower()
    )


def _generate_gateway_fallback(
    *,
    prompt: str,
    provider: str | None,
    model: str | None,
    timeout_s: float | None,
    gateway_error: Exception,
    context: "LlmExecutionContext | None" = None,
) -> dict:
    """Repli CONTRÔLÉ après un échec de l'AI Gateway.

    Contrat policy : le repli n'est JAMAIS Ollama direct. Il vaut soit un provider
    externe **explicitement autorisé pour le périmètre**, soit une erreur contrôlée.

    CORRECTIF P0-C (fail-open n°1). L'implémentation historique était :

        if sp and sp != <externe> and not allow_external_fallback: refuser
        return _generate_text_direct(provider=<externe>, ...)

    Le ``if sp and`` rendait la garde inopérante dès que le contexte d'espace
    était absent ou que ``llm_provider`` était vide — cinq call-sites sur douze
    tombaient dans ce cas. La décision repose désormais sur une politique résolue,
    et l'absence de politique vaut REFUS.

    Deuxième correctif : le repli n'a lieu que si la défaillance est
    **transitoire** (cf. ``gateway.exceptions``). Une erreur 4xx de configuration
    ou de contrat remonte telle quelle, sans egress.
    """
    from django.core.exceptions import ImproperlyConfigured

    from .gateway import GatewayUnavailableError, is_fallback_eligible

    # 1) La défaillance justifie-t-elle seulement d'envisager un repli ?
    if not is_fallback_eligible(gateway_error):
        logger.warning(
            "ai_gateway_error_not_fallback_eligible error=%s status=%s",
            type(gateway_error).__name__,
            getattr(gateway_error, "status_code", None),
        )
        raise gateway_error

    # 2) Mode strict (fallback globalement désactivé) => erreur Gateway typée.
    if not _gateway_fallback_enabled():
        raise gateway_error

    fallback_provider = _gateway_fallback_provider()

    if fallback_provider != "none":
        # Édition Open : aucun fournisseur externe n'existe. Une valeur autre que "none"
        # est une configuration invalide, jamais un appel réseau.
        raise ImproperlyConfigured(
            f"Unsupported AI_GATEWAY_FALLBACK_PROVIDER={fallback_provider!r}: RAGatm Open has "
            "no fallback provider (only 'none' is allowed)."
        )
    logger.warning(
        "ai_gateway_unavailable_no_fallback operation=%s error=%s",
        getattr(context, "operation", "unspecified"),
        type(gateway_error).__name__,
    )
    raise GatewayUnavailableError(
        "AI Gateway unavailable and no fallback provider exists in RAGatm Open"
    ) from gateway_error


def _build_gateway_call(
    *,
    prompt: str,
    provider: str | None,
    model: str | None,
    options: dict[str, Any] | None = None,
    think: bool | None = None,
    timeout_s: float | None = None,
    request_id: str | None = None,
) -> "tuple[Any, Any]":
    """Construit le couple (client, requête) envoyé à l'AI Gateway.

    Point de construction UNIQUE des deux modes (``/v1/generate`` et
    ``/v1/generate/stream``) : c'est ce qui rend la **parité de payload**
    structurelle et non déclarative. Un modèle ou un ``num_ctx`` qui divergerait
    entre les deux modes forcerait Ollama à recharger le runner (coût de plusieurs
    secondes) — ce partage l'interdit par construction.

    Provider/modèle envoyés à la Gateway (chemin nominal) :
      - override EXPLICITE de l'appelant (``provider``/``model``) prioritaire ;
      - sinon ``AI_GATEWAY_PROVIDER`` / ``AI_GATEWAY_MODEL`` (défaut ollama / compact-generation-8),
        DÉCOUPLÉS de ``RAG_LLM_PROVIDER`` (qui ne sert QUE de provider externe de repli).

    ``options`` (temperature/num_ctx/num_predict…) et ``think`` (raisonnement interne
    qwen3) sont OPTIONNELS et transmis tels quels à la Gateway (contrat v2.0.0). None =>
    non envoyés => parité stricte avec le comportement historique (défauts Gateway).
    """
    from apps.rag_core.core import runtime_config

    from .gateway import AIGatewayClient, GatewayGenerationRequest

    provider_resolved = (
        provider
        or str(runtime_config.get("AI_GATEWAY_PROVIDER", default="ollama") or "ollama")
    ).strip().lower()
    model_resolved = (model or "").strip() or str(
        runtime_config.get("AI_GATEWAY_MODEL", default="compact-generation-8")
        or "compact-generation-8"
    ).strip()

    # Client construit à partir des valeurs résolues runtime (overlay si activé,
    # sinon settings) — au lieu de AIGatewayClient.from_settings().
    # ``timeout_s`` explicite (override par appel) prioritaire sur le défaut runtime :
    # permet un budget dédié COURT (ex. suggestions de reformulation) sans changer
    # le timeout global. None => défaut historique (parité stricte).
    effective_timeout = (
        float(timeout_s)
        if timeout_s is not None
        else float(runtime_config.get("AI_GATEWAY_TIMEOUT_SECONDS", default=30))
    )
    client = AIGatewayClient(
        base_url=str(runtime_config.get("AI_GATEWAY_BASE_URL", default="") or ""),
        timeout_s=effective_timeout,
        enabled=bool(runtime_config.get("AI_GATEWAY_ENABLED", default=False)),
    )
    req = GatewayGenerationRequest(
        prompt=prompt,
        provider=provider_resolved,
        model=model_resolved,
        options=dict(options) if options else {},
        think=think,
        # Corrélation (qualification) : relayée en EN-TÊTE `X-Request-ID` par le client,
        # jamais dans le corps (`to_payload()` inchangé).
        request_id=request_id,
    )
    return client, req


def _generate_via_gateway(
    *,
    prompt: str,
    provider: str | None,
    model: str | None,
    options: dict[str, Any] | None = None,
    think: bool | None = None,
    timeout_s: float | None = None,
    request_id: str | None = None,
) -> dict:
    """Génération NON-STREAMING via la Gateway. Contrat ``generate_text`` inchangé."""
    client, req = _build_gateway_call(
        prompt=prompt,
        provider=provider,
        model=model,
        options=options,
        think=think,
        timeout_s=timeout_s,
        request_id=request_id,
    )
    return client.generate(req).to_generate_text_dict()


def resolve_execution_context(
    context: "LlmExecutionContext | None" = None,
    *,
    operation: str = "generate_text",
    space_provider: str | None = None,
    allow_external_fallback: bool = False,
) -> "LlmExecutionContext":
    """Normalise le contexte d'exécution d'un appel LLM.

    Un ``context`` explicite est toujours prioritaire. À défaut, on construit un
    contexte de TRANSITION à partir des anciens paramètres, en appliquant les
    mêmes règles fail-closed (cf. ``egress.policy_from_legacy_flags``) : un
    appelant non migré ne peut pas rouvrir le trou corrigé.
    """
    from .egress import LlmExecutionContext, policy_from_legacy_flags

    if context is not None:
        return context
    return LlmExecutionContext(
        operation=operation,
        policy=policy_from_legacy_flags(
            space_provider=space_provider,
            allow_external_fallback=allow_external_fallback,
        ),
    )


def generate_text(
    *,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float | None = None,
    context: "LlmExecutionContext | None" = None,
    space_provider: str | None = None,
    allow_external_fallback: bool = False,
    options: dict[str, Any] | None = None,
    think: bool | None = None,
    retry_on_empty: bool = True,
    operation: str = "generate_text",
) -> dict:
    """Génère du texte. Contrat de retour inchangé.

    Routage : si ``AI_GATEWAY_ENABLED``, passe par l'AI Gateway ; en
    cas d'échec Gateway TRANSITOIRE, repli CONTRÔLÉ via
    ``AI_GATEWAY_FALLBACK_PROVIDER`` (``none`` en édition Open) — JAMAIS Ollama direct.
    Flag off => chemin direct, qui applique **les mêmes contrôles d'egress**.

    ``context`` (``LlmExecutionContext``) est le paramètre CANONIQUE : il porte le
    périmètre (workspace / RagSpace / utilisateur), la politique de confidentialité
    résolue et l'identifiant de corrélation. Tout appel vers un provider externe
    sans contexte est REFUSÉ (``MissingEgressContext``).

    ``space_provider`` / ``allow_external_fallback`` sont **dépréciés** : conservés
    pour la transition des appelants non encore migrés, ils construisent un
    contexte fail-closed équivalent.

    ``options``/``think`` (optionnels) : réglages de génération transmis à la
    Gateway (temperature/num_ctx/num_predict, raisonnement qwen3). None => non
    envoyés (défauts Gateway). Ils ne s'appliquent qu'au chemin Gateway ; le
    chemin direct/repli les ignore.
    """
    import time

    from .egress_audit import record_egress_event

    ctx = resolve_execution_context(
        context,
        operation=operation,
        space_provider=space_provider,
        allow_external_fallback=allow_external_fallback,
    )
    started = time.monotonic()
    prompt_chars = len(prompt or "")
    requested = (provider or "").strip().lower() or ("gateway" if _gateway_enabled() else _default_provider())

    def _audit(result: dict | None, *, gateway_attempted: bool, fallback_attempted: bool,
               fallback_used: bool, fallback_reason: str = "", status: str = "ok",
               error_type: str = "") -> None:
        record_egress_event(
            context=ctx,
            requested_provider=requested,
            effective_provider=(result or {}).get("provider", "") or "",
            model=(result or {}).get("model", "") or "",
            gateway_attempted=gateway_attempted,
            fallback_attempted=fallback_attempted,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            status=status,
            error_type=error_type,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_chars=prompt_chars,
            usage=(result or {}).get("usage"),
        )

    if _gateway_enabled():
        from .gateway import GatewayError

        def _fallback(e: "GatewayError") -> dict:
            reason = type(e).__name__
            try:
                out = _generate_gateway_fallback(
                    prompt=prompt,
                    provider=provider,
                    model=model,
                    timeout_s=timeout_s,
                    gateway_error=e,
                    context=ctx,
                )
            except Exception as denied:
                _audit(
                    None,
                    gateway_attempted=True,
                    fallback_attempted=True,
                    fallback_used=False,
                    fallback_reason=reason,
                    status="denied"
                    if type(denied).__name__ in (
                        "ExternalEgressDenied",
                        "ExternalProviderDenied",
                        "MissingEgressContext",
                        "GatewayUnavailableError",
                    )
                    else "error",
                    error_type=type(denied).__name__,
                )
                raise
            _audit(
                out,
                gateway_attempted=True,
                fallback_attempted=True,
                fallback_used=True,
                fallback_reason=reason,
            )
            out["fallback_used"] = True
            out["fallback_reason"] = reason
            return out

        try:
            # ``timeout_s`` transmis UNIQUEMENT s'il est fourni : parité stricte du
            # call-site pour tous les appelants historiques (None => inchangé).
            _gw_kwargs = dict(prompt=prompt, provider=provider, model=model, options=options, think=think,
                              request_id=ctx.correlation_id)
            if timeout_s is not None:
                _gw_kwargs["timeout_s"] = timeout_s
            result = _generate_via_gateway(**_gw_kwargs)
        except GatewayError as e:
            return _fallback(e)

        # Chemin nominal : réponse non vide => on retourne tel quel.
        if not _is_blank_text(result.get("text")):
            _audit(result, gateway_attempted=True, fallback_attempted=False, fallback_used=False)
            return result

        # ``retry_on_empty=False`` (appelants qui bornent strictement le temps, ex.
        # suggestions de reformulation) : pas de seconde tentative => on remonte le
        # texte vide tel quel, à charge de l'appelant de gérer un fallback local.
        if not retry_on_empty:
            _audit(result, gateway_attempted=True, fallback_attempted=False, fallback_used=False)
            return result

        # Réponse Gateway VIDE (HTTP 200 mais texte absent/blanc) : UN SEUL retry
        # borné, même modèle/prompt/provider, think=false et budget de tokens relevé.
        # Ne journalise ni prompt ni documents (métadonnées non sensibles seulement).
        logger.warning(
            "gateway_empty_response model=%s attempt=1 num_predict=%s think=%s",
            result.get("model") or model or "(default)",
            (options or {}).get("num_predict"),
            think,
        )
        retry_np = _empty_retry_num_predict(options)
        retry_options = dict(options or {})
        retry_options["num_predict"] = retry_np
        try:
            result = _generate_via_gateway(
                prompt=prompt, provider=provider, model=model,
                options=retry_options, think=False, request_id=ctx.correlation_id,
            )
        except GatewayError as e:
            # Le retry conserve les MÊMES règles de fallback qu'une première tentative.
            return _fallback(e)

        if _is_blank_text(result.get("text")):
            logger.warning(
                "gateway_empty_response model=%s attempt=2 num_predict=%s think=%s",
                result.get("model") or model or "(default)", retry_np, False,
            )
            _audit(
                result, gateway_attempted=True, fallback_attempted=False,
                fallback_used=False, status="error", error_type="EmptyGatewayResponse",
            )
            # Pas de masquage heuristique : erreur typée existante, sans prompt.
            raise LLMError("AI Gateway returned an empty response after one retry")
        _audit(result, gateway_attempted=True, fallback_attempted=False, fallback_used=False)
        return result

    # Gateway désactivée : chemin direct. Il applique LA MÊME politique d'egress
    # (le mode de transport ne décide pas de la confidentialité — cf. fail-open n°2).
    try:
        direct = _generate_text_direct(
            prompt=prompt, provider=provider, model=model, timeout_s=timeout_s, context=ctx
        )
    except Exception as exc:
        _audit(
            None, gateway_attempted=False, fallback_attempted=False, fallback_used=False,
            status="denied"
            if type(exc).__name__ in (
                "ExternalEgressDenied", "ExternalProviderDenied", "MissingEgressContext",
            )
            else "error",
            error_type=type(exc).__name__,
        )
        raise
    _audit(direct, gateway_attempted=False, fallback_attempted=False, fallback_used=False)
    return direct


# -----------------------
# Streaming
# -----------------------

def _stream_via_gateway(
    *,
    prompt: str,
    provider: str | None,
    model: str | None,
    timeout_s: float | None,
    options: dict[str, Any] | None,
    think: bool | None,
    ctx: "LlmExecutionContext",
):
    """Streaming natif via l'AI Gateway (`/v1/generate/stream`).

    Émet le contrat interne de ``generate_text_stream`` (``delta`` puis ``result``)
    et concentre les quatre garanties d'exploitation du mode flux :

    1. **Repli AVANT le premier delta seulement.** Après un premier fragment,
       basculer de modèle recollerait la fin d'une génération B au début d'une
       génération A : la rupture est propagée telle quelle et l'appelant marque la
       réponse partielle.
    2. **Un seul événement d'audit d'egress** par génération, émis dans le
       ``finally`` — jamais un par fragment.
    3. **Une seule ligne de journal** de synthèse, sans prompt ni contenu : seuls
       des compteurs et une cause de terminaison.
    4. **Annulation** : si le consommateur ferme le générateur (déconnexion
       cliente), le ``finally`` s'exécute avec ``termination=client_disconnect`` et
       la fermeture de socket du client Gateway interrompt la génération amont.
    """
    import time

    from .egress_audit import record_egress_event
    from .gateway import GatewayError

    started = time.monotonic()
    first_delta_ms: int | None = None
    delta_count = 0
    streamed_chars = 0
    # Défaut PESSIMISTE : tant que le flux n'a pas été mené à son terme, une sortie
    # de ce générateur est une rupture cliente.
    termination = "client_disconnect"
    final: dict[str, Any] | None = None
    fallback_used = False
    fallback_reason = ""
    error_type = ""

    client, req = _build_gateway_call(
        prompt=prompt,
        provider=provider,
        model=model,
        options=options,
        think=think,
        timeout_s=timeout_s,
        request_id=ctx.correlation_id,
    )

    try:
        try:
            for evt in client.generate_stream(req):
                if evt.get("type") == "delta":
                    delta = evt.get("delta") or ""
                    if not delta:
                        continue
                    delta_count += 1
                    streamed_chars += len(delta)
                    if first_delta_ms is None:
                        first_delta_ms = int((time.monotonic() - started) * 1000)
                    yield evt
                elif evt.get("type") == "result":
                    final = evt
                    termination = "done"
                    yield evt
        except GatewayError as e:
            error_type = type(e).__name__
            if delta_count > 0:
                # Garantie 1 : plus aucun repli une fois le flux entamé.
                termination = "stream_error"
                raise
            # Aucun fragment émis : le repli contrôlé reste licite et indiscernable
            # du chemin non-streaming pour l'appelant (un seul delta porteur).
            fallback_reason = error_type
            out = _generate_gateway_fallback(
                prompt=prompt,
                provider=provider,
                model=model,
                timeout_s=timeout_s,
                gateway_error=e,
                context=ctx,
            )
            fallback_used = True
            termination = "fallback"
            text = out.get("text") or ""
            if text:
                delta_count += 1
                streamed_chars += len(text)
                first_delta_ms = int((time.monotonic() - started) * 1000)
                yield {"type": "delta", "delta": text}
            final = {
                "type": "result",
                "provider": out.get("provider"),
                "model": out.get("model"),
                "usage": out.get("usage"),
                "tokens": out.get("tokens"),
                "fallback_used": True,
                "fallback_reason": fallback_reason,
            }
            yield final
    except GeneratorExit:
        # Déconnexion cliente : on laisse ``termination`` à client_disconnect.
        raise
    except Exception as e:  # noqa: BLE001 - tracé puis propagé
        if not error_type:
            error_type = type(e).__name__
        if termination not in ("done", "fallback"):
            termination = "stream_error" if delta_count > 0 else "error"
        raise
    finally:
        total_ms = int((time.monotonic() - started) * 1000)
        effective_provider = str((final or {}).get("provider") or "") or "ai_gateway"
        effective_model = str((final or {}).get("model") or req.model or "")
        # Garantie 3 : une ligne, des compteurs, aucune donnée d'entrée ni de sortie.
        logger.info(
            "gateway_stream_end gateway_stream=true provider=%s model=%s "
            "delta_count=%d streamed_chars=%d first_delta_ms=%s "
            "total_generation_ms=%d termination=%s fallback_used=%s",
            effective_provider,
            effective_model,
            delta_count,
            streamed_chars,
            first_delta_ms if first_delta_ms is not None else "none",
            total_ms,
            termination,
            fallback_used,
        )
        # Garantie 2 : un seul événement d'egress (``record_egress_event`` ne lève
        # jamais). "cancelled" plutôt que "client_disconnect" : le champ est borné
        # à 16 caractères.
        status = {"done": "ok", "fallback": "ok", "client_disconnect": "cancelled"}.get(
            termination, "error"
        )
        record_egress_event(
            context=ctx,
            requested_provider=(provider or "").strip().lower() or "gateway",
            effective_provider=effective_provider,
            model=effective_model,
            gateway_attempted=True,
            fallback_attempted=fallback_used,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            status=status,
            error_type=error_type,
            latency_ms=total_ms,
            prompt_chars=len(prompt or ""),
            usage=(final or {}).get("usage"),
        )


def generate_text_stream(
    *,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float | None = None,
    context: "LlmExecutionContext | None" = None,
    space_provider: str | None = None,
    allow_external_fallback: bool = False,
    options: dict[str, Any] | None = None,
    think: bool | None = None,
    operation: str = "generate_text_stream",
):
    """Stream text deltas from the selected provider.

    Yields dict events:
      - {"type": "delta", "delta": "..."}
      - {"type": "result", "provider": ..., "model": ..., "usage": ..., "raw": ...}

    Raises LLMError on failure.

    Policy Configuration IT : le streaming ne doit JAMAIS contourner l'AI Gateway.
    Trois régimes, dans cet ordre :

    1. Gateway ACTIVE **et** ``AI_GATEWAY_STREAMING_ENABLED=1`` : streaming natif
       ``/v1/generate/stream`` (cf. :func:`_stream_via_gateway`). Le payload est
       celui de ``/v1/generate``, à l'identique.
    2. Gateway active, streaming désactivé : REFUS explicite (erreur contrôlée)
       plutôt qu'un appel direct qui contournerait le serveur IA. Les
       consommateurs (chat) retombent sur ``generate_text`` non-streaming.
    3. Gateway inactive (dev/tests dégradés) : streaming direct, avec les mêmes
       contrôles d'egress que le chemin synchrone.

    ``options``/``think`` ne s'appliquent qu'au régime 1 : ils garantissent que le
    chat streamé demande EXACTEMENT les mêmes réglages de génération que le chat
    non-streamé (température, num_ctx, num_predict, raisonnement).
    """
    if _gateway_enabled():
        if not _gateway_streaming_enabled():
            raise LLMError(
                "Streaming is not supported when AI Gateway is enabled "
                "(AI_GATEWAY_ENABLED=1) and AI_GATEWAY_STREAMING_ENABLED=0. "
                "Use non-streaming generate_text()."
            )
        ctx_gw = resolve_execution_context(
            context,
            operation=operation,
            space_provider=space_provider,
            allow_external_fallback=allow_external_fallback,
        )
        yield from _stream_via_gateway(
            prompt=prompt,
            provider=provider,
            model=model,
            timeout_s=timeout_s,
            options=options,
            think=think,
            ctx=ctx_gw,
        )
        return

    provider = (provider or _default_provider()).strip().lower()
    model = (model or "").strip() or _default_model_for_provider(provider)
    timeout_s = timeout_s or float(_get_setting("LLM_TIMEOUT_S"))

    # CORRECTIF P0-C : le chemin SSE (Gateway off) appelait le provider sans
    # aucun contrôle de périmètre. Il applique désormais la MÊME politique que
    # le chemin synchrone — un flux n'est pas moins sensible qu'une réponse.
    ctx = resolve_execution_context(
        context,
        operation=operation,
        space_provider=space_provider,
        allow_external_fallback=allow_external_fallback,
    )
    _enforce_egress(provider=provider, context=ctx, operation=operation)

    from .egress_audit import record_egress_event

    record_egress_event(
        context=ctx,
        requested_provider=provider,
        effective_provider=provider,
        model=model,
        gateway_attempted=False,
        prompt_chars=len(prompt or ""),
    )

    if provider == "ollama":
        yield from _ollama_stream(prompt=prompt, model=model, timeout_s=timeout_s)
        return

    raise LLMError(f"Unknown provider: {provider}")


def _ollama_stream(*, prompt: str, model: str, timeout_s: float):
    """
    Stream responses from Ollama /api/generate.

    Ollama streams one JSON object per line when `stream=true`, typically:
      {"response":"...","done":false}
      ...
      {"done":true, "prompt_eval_count":..., "eval_count":..., ...}
    """
    base_url = (_get_setting("OLLAMA_BASE_URL") or "").rstrip("/")
    url = f"{base_url}/api/generate"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,  # <-- FIX: must be True to receive deltas
        "options": {
            "num_ctx": int(_get_setting("OLLAMA_NUM_CTX")),
            "temperature": float(_get_setting("OLLAMA_TEMPERATURE")),
            "top_p": float(_get_setting("OLLAMA_TOP_P")),
            "repeat_penalty": float(_get_setting("OLLAMA_REPEAT_PENALTY")),
        },
    }

    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                # Many servers are fine without this, but it helps intermediaries.
                "Accept": "application/json",
            },
            json=payload,
            stream=True,
            timeout=timeout_s,
        )
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    if resp.status_code >= 400:
        raise LLMError(f"Ollama error {resp.status_code}")

    usage: dict[str, Any] = {}
    last_raw = None
    provider = "ollama"

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "ignore")

        try:
            obj = json.loads(line)
        except Exception:
            continue

        if not isinstance(obj, dict):
            continue

        last_raw = obj

        delta = obj.get("response")
        if isinstance(delta, str) and delta:
            yield {"type": "delta", "delta": delta}

        if obj.get("done") is True:
            for k in (
                "prompt_eval_count",
                "eval_count",
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
            ):
                if k in obj:
                    usage[k] = obj.get(k)
            break

    yield {"type": "result", "provider": provider, "model": model, "usage": usage or None, "raw": last_raw}

"""Politique d'egress externe — résolution et invariants fail-closed (Lot 1 P0-C).

Ces tests portent sur la POLITIQUE seule (aucun réseau, aucun mock HTTP). Ils
verrouillent le comportement qui a produit le NO-GO P0-C : une politique non
résolue, un périmètre absent ou un provider vide ne doivent JAMAIS valoir
autorisation.
"""
from __future__ import annotations

import pytest

from apps.llm_router.egress import (
    EXTERNAL_PROVIDERS,
    ExternalEgressDenied,
    ExternalEgressPolicy,
    ExternalProviderDenied,
    LlmExecutionContext,
    is_external_provider,
    policy_from_legacy_flags,
    resolve_policy_for_rag_space,
)


class _Space:
    """Double minimal de RagSpace (aucun accès base nécessaire)."""

    def __init__(self, *, llm_provider="", allow_external_fallback=False, id="sp-1"):
        self.id = id
        self.llm_provider = llm_provider
        self.allow_external_fallback = allow_external_fallback
        self.workspace_id = "ws-1"


# --------------------------------------------------------------------------- #
# Classification des providers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("provider", ["ollama", "Ollama", " OLLAMA ", "stub", "local", "gateway"])
def test_internal_providers_are_not_external(provider):
    assert is_external_provider(provider) is False


@pytest.mark.parametrize("provider", ["openai", "OpenAI", "azure_openai", "anthropic", "mistral"])
def test_known_external_providers(provider):
    assert is_external_provider(provider) is True


@pytest.mark.parametrize("provider", ["", None, "  ", "openai-compatible", "inconnu", "olama"])
def test_unknown_or_empty_provider_is_treated_as_external(provider):
    """Fail-closed : un provider vide ou mal orthographié n'est jamais « interne ».

    C'est la racine du fail-open n°1 : ``space_provider`` vide court-circuitait la
    garde et autorisait la bascule OpenAI.
    """
    assert is_external_provider(provider) is True


# --------------------------------------------------------------------------- #
# Résolution par RagSpace
# --------------------------------------------------------------------------- #

def test_space_without_provider_and_without_optin_is_denied():
    """LE cas prouvé fuyant : espace sans llm_provider, sans opt-in."""
    policy = resolve_policy_for_rag_space(_Space())
    assert policy.external_generation_allowed is False
    assert policy.allowed_external_providers == ()


def test_ollama_space_without_optin_is_denied():
    policy = resolve_policy_for_rag_space(_Space(llm_provider="ollama"))
    assert policy.external_generation_allowed is False


def test_none_space_is_denied():
    policy = resolve_policy_for_rag_space(None)
    assert policy.external_generation_allowed is False
    assert policy.policy_source == "missing-scope"


def test_space_with_explicit_optin_is_allowed(settings):
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    policy = resolve_policy_for_rag_space(
        _Space(llm_provider="ollama", allow_external_fallback=True)
    )
    assert policy.external_generation_allowed is True
    assert "openai" in policy.allowed_external_providers
    assert policy.policy_source == "ragspace"


def test_openai_space_is_allowed_without_optin(settings):
    """Un espace dont le provider NOMINAL est externe parle déjà à ce tiers."""
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    policy = resolve_policy_for_rag_space(_Space(llm_provider="openai"))
    assert policy.external_generation_allowed is True
    assert policy.policy_source == "ragspace-provider"


# --------------------------------------------------------------------------- #
# Le global est un PLAFOND, jamais un plancher
# --------------------------------------------------------------------------- #

def test_global_kill_switch_overrides_space_optin(settings):
    """Un opt-in d'espace ne peut pas rouvrir ce que le global a fermé."""
    settings.AI_GATEWAY_FALLBACK_ENABLED = False
    policy = resolve_policy_for_rag_space(
        _Space(llm_provider="ollama", allow_external_fallback=True)
    )
    assert policy.external_generation_allowed is False


def test_fallback_provider_none_closes_everything(settings):
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    policy = resolve_policy_for_rag_space(
        _Space(llm_provider="ollama", allow_external_fallback=True)
    )
    assert policy.external_generation_allowed is False


def test_intersect_never_widens():
    permissive = ExternalEgressPolicy(
        external_generation_allowed=True,
        external_embedding_allowed=True,
        allowed_external_providers=tuple(sorted(EXTERNAL_PROVIDERS)),
    )
    strict = ExternalEgressPolicy.denied()
    assert permissive.intersect(strict).external_generation_allowed is False
    assert strict.intersect(permissive).external_generation_allowed is False


# --------------------------------------------------------------------------- #
# Contexte d'exécution
# --------------------------------------------------------------------------- #

def test_system_local_only_always_denies():
    ctx = LlmExecutionContext.system_local_only(operation="t")
    with pytest.raises(ExternalEgressDenied):
        ctx.ensure_external_allowed("openai")


def test_system_external_allowed_requires_a_reason():
    with pytest.raises(ValueError):
        LlmExecutionContext.system_external_allowed(
            operation="t", providers=["openai"], reason="  "
        )


def test_context_denies_provider_outside_allowlist(settings):
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    ctx = LlmExecutionContext.for_rag_space(
        _Space(llm_provider="openai"), operation="t"
    )
    ctx.ensure_external_allowed("openai")  # ne lève pas
    with pytest.raises(ExternalProviderDenied):
        ctx.ensure_external_allowed("anthropic")


def test_correlation_id_is_generated_when_absent():
    ctx = LlmExecutionContext(operation="t")
    assert ctx.correlation_id


def test_audit_fields_carry_no_content():
    ctx = LlmExecutionContext.system_local_only(operation="t")
    fields = ctx.audit_fields()
    joined = " ".join(str(v) for v in fields.values())
    assert "prompt" not in joined.lower()
    assert set(fields) >= {
        "operation", "workspace_id", "rag_space_id", "user_id",
        "correlation_id", "external_egress_allowed", "policy_source",
    }


# --------------------------------------------------------------------------- #
# Shim de transition
# --------------------------------------------------------------------------- #

def test_legacy_flags_empty_provider_is_denied():
    """Le shim de compatibilité ne rouvre PAS le trou corrigé."""
    policy = policy_from_legacy_flags(space_provider=None, allow_external_fallback=False)
    assert policy.external_generation_allowed is False


def test_legacy_flags_optin_is_allowed(settings):
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "openai"
    policy = policy_from_legacy_flags(space_provider="ollama", allow_external_fallback=True)
    assert policy.external_generation_allowed is True

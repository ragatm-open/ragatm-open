"""Durcissement de l'overlay runtime Core.

Le trou reproduit ici était réel : avec ``CORE_CONFIG_RUNTIME_ENABLED=1``, un
override autorisé de ``AI_GATEWAY_ENABLED`` désactivait la Gateway au RUNTIME.
La génération **et** les embeddings basculaient alors sur un appel Ollama DIRECT
— précisément ce que ``llm_router.E001``/``E003`` interdisent — sans qu'aucun
system check ne puisse le voir, puisque les checks lisent les settings et non
l'overlay.

Ces tests prouvent les deux remparts :

1. **écriture** — l'override est refusé, rien n'est persisté ;
2. **résolution** — même une ligne posée en base par une écriture SQL directe
   n'atteint jamais l'exécution : le résolveur retombe sur les settings.

Les deux chemins consommateurs sont couverts séparément, car ils sont distincts :
``llm_router.router`` (génération) et ``llm_router.embeddings`` (indexation, donc
workers Celery).
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.rag_core.core import config_policy, runtime_config
from apps.rag_core.core.config_policy import ConfigPolicyViolation
from apps.rag_core.core.config_write import OverrideValidationError, set_override
from config import registry


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Outils : une ligne d'overlay posée SANS passer par le service d'écriture,
# c'est-à-dire exactement ce qu'un `INSERT` manuel ou une ligne antérieure au
# durcissement produirait.
# ---------------------------------------------------------------------------
def _raw_override(scope_type: str, scope_ref: str, key: str, value: str):
    from apps.rag_core.models import CoreConfigOverride

    return CoreConfigOverride.objects.create(
        scope_type=scope_type, scope_ref=scope_ref, key=key, value=value
    )


@pytest.fixture(autouse=True)
def _clear_runtime_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# ===========================================================================
# 1. Le registre ne déclare plus les clés d'infrastructure comme éditables
# ===========================================================================
@pytest.mark.parametrize(
    "key",
    [
        "AI_GATEWAY_ENABLED",
        "AI_GATEWAY_BASE_URL",
        "AI_GATEWAY_PROVIDER",
        "AI_GATEWAY_MODEL",
        "AI_GATEWAY_FALLBACK_PROVIDER",
        "RAG_EMBEDDING_DIM",
    ],
)
def test_infrastructure_keys_are_not_editable_in_the_registry(key):
    definition = registry.require(key)
    assert definition.runtime_editable is False, key
    assert definition.max_scope is None, key
    assert config_policy.is_overridable(key) is False, key


def test_the_remaining_editable_set_is_explicit():
    """Verrou : élargir le périmètre éditable doit être un geste conscient."""
    assert {d.name for d in registry.runtime_editable()} == {
        "CORE_CONFIG_SELFTEST_SCOPE",
        "AI_GATEWAY_TIMEOUT_SECONDS",
        "AI_GATEWAY_FALLBACK_ENABLED",
        "WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT",
        "WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES",
    }


def test_infrastructure_keys_remain_visible_in_the_admin_console():
    """Non éditable n'est pas invisible : l'exploitant doit voir l'état réel."""
    from apps.rag_core.core.config_resolver import EXPOSED_KEYS

    assert "AI_GATEWAY_BASE_URL" in EXPOSED_KEYS
    assert "AI_GATEWAY_ENABLED" in EXPOSED_KEYS


# ===========================================================================
# 2. Rempart n°1 — l'écriture refuse
# ===========================================================================
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AI_GATEWAY_ENABLED", "false"),
        ("AI_GATEWAY_ENABLED", "true"),
        ("AI_GATEWAY_BASE_URL", "http://198.51.100.9:9000"),
        ("RAG_EMBEDDING_DIM", "1536"),
    ],
)
def test_writing_an_infrastructure_override_is_refused(django_user_model, key, value):
    from apps.rag_core.models import CoreConfigOverride

    actor = django_user_model.objects.create_user(
        username="op", password="x", is_superuser=True, is_staff=True
    )
    with pytest.raises(OverrideValidationError) as excinfo:
        set_override(
            actor=actor, scope_type="environment", scope_ref="staging", key=key, value=value
        )
    assert excinfo.value.code in ("key_not_editable", "infrastructure_key_not_overridable")
    # Rien n'est persisté : le refus est antérieur à l'écriture.
    assert not CoreConfigOverride.objects.filter(key=key).exists()


def test_writing_an_out_of_bounds_timeout_is_refused(django_user_model):
    from apps.rag_core.models import CoreConfigOverride

    actor = django_user_model.objects.create_user(
        username="op2", password="x", is_superuser=True, is_staff=True
    )
    for value in ("0", "-1", "9999"):
        with pytest.raises(OverrideValidationError):
            set_override(
                actor=actor,
                scope_type="environment",
                scope_ref="staging",
                key="AI_GATEWAY_TIMEOUT_SECONDS",
                value=value,
            )
    assert not CoreConfigOverride.objects.filter(key="AI_GATEWAY_TIMEOUT_SECONDS").exists()


@override_settings(
    AI_GATEWAY_FALLBACK_PROVIDER="ollama",
    ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=False,
)
def test_enabling_the_fallback_without_a_usable_provider_is_refused(django_user_model):
    """Miroir de `llm_router.E004` appliqué à l'overlay.

    RAGatm Open ne connaît aucun fournisseur de repli : activer le repli avec un
    provider autre que `none` créerait au runtime la configuration exacte que le
    system check refuse au démarrage.
    """
    actor = django_user_model.objects.create_user(
        username="op3", password="x", is_superuser=True, is_staff=True
    )
    with pytest.raises(OverrideValidationError) as excinfo:
        set_override(
            actor=actor,
            scope_type="environment",
            scope_ref="staging",
            key="AI_GATEWAY_FALLBACK_ENABLED",
            value="true",
        )
    assert excinfo.value.code == "fallback_provider_unusable"


@override_settings(AI_GATEWAY_FALLBACK_PROVIDER="none")
def test_enabling_the_fallback_with_provider_none_is_accepted(django_user_model):
    actor = django_user_model.objects.create_user(
        username="op4", password="x", is_superuser=True, is_staff=True
    )
    override, _event = set_override(
        actor=actor,
        scope_type="environment",
        scope_ref="staging",
        key="AI_GATEWAY_FALLBACK_ENABLED",
        value="true",
    )
    assert override.value == "true"


def test_a_legitimate_override_still_works(django_user_model):
    """Le durcissement ne ferme pas l'overlay : il en borne le périmètre."""
    actor = django_user_model.objects.create_user(
        username="op5", password="x", is_superuser=True, is_staff=True
    )
    override, _event = set_override(
        actor=actor,
        scope_type="environment",
        scope_ref="staging",
        key="AI_GATEWAY_TIMEOUT_SECONDS",
        value="45",
    )
    assert override.value == "45"


# ===========================================================================
# 3. Rempart n°2 — la résolution runtime ne retourne jamais la valeur fautive
# ===========================================================================
@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_ENABLED=True,
    DJANGO_CACHE_URL="redis://unused/2",
)
def test_runtime_resolution_never_disables_the_gateway():
    """LE test du trou : override `false` en base, overlay actif, env officiel."""
    _raw_override("environment", "staging", "AI_GATEWAY_ENABLED", "false")

    resolved = runtime_config.get("AI_GATEWAY_ENABLED")

    assert resolved is True, "l'overlay ne doit jamais désactiver la Gateway"


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_BASE_URL="http://ai-gateway:8100",
)
def test_runtime_resolution_ignores_an_overridden_gateway_url():
    _raw_override("environment", "staging", "AI_GATEWAY_BASE_URL", "http://198.51.100.9:9000")

    resolved = runtime_config.get("AI_GATEWAY_BASE_URL")

    assert resolved == "http://ai-gateway:8100"
    assert "198.51.100.9" not in str(resolved)


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_TIMEOUT_SECONDS=180,
)
def test_runtime_resolution_rejects_an_out_of_bounds_timeout():
    """Une ligne hors bornes (écriture SQL directe) retombe sur les settings."""
    _raw_override("environment", "staging", "AI_GATEWAY_TIMEOUT_SECONDS", "99999")

    assert runtime_config.get("AI_GATEWAY_TIMEOUT_SECONDS") == 180


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_TIMEOUT_SECONDS=180,
)
def test_runtime_resolution_accepts_a_valid_timeout_override():
    """Le rempart ne neutralise pas l'overlay légitime."""
    _raw_override("environment", "staging", "AI_GATEWAY_TIMEOUT_SECONDS", "45")

    assert runtime_config.get("AI_GATEWAY_TIMEOUT_SECONDS") == 45


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_FALLBACK_PROVIDER="ollama",
    ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=False,
    AI_GATEWAY_FALLBACK_ENABLED=False,
)
def test_runtime_resolution_rejects_an_unusable_fallback():
    _raw_override("environment", "staging", "AI_GATEWAY_FALLBACK_ENABLED", "true")

    assert runtime_config.get("AI_GATEWAY_FALLBACK_ENABLED") is False


@override_settings(CORE_CONFIG_RUNTIME_ENABLED=False, DJANGO_ENV="staging", AI_GATEWAY_ENABLED=True)
def test_overlay_is_inert_when_the_flag_is_closed():
    _raw_override("environment", "staging", "AI_GATEWAY_ENABLED", "false")

    assert runtime_config.get("AI_GATEWAY_ENABLED") is True


# ===========================================================================
# 4. Les DEUX chemins consommateurs
# ===========================================================================
@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_ENABLED=True,
    AI_GATEWAY_BASE_URL="http://ai-gateway:8100",
)
def test_generation_path_keeps_the_gateway_enabled():
    """`llm_router.router` lit l'overlay : il doit rester sur le chemin Gateway."""
    from apps.llm_router import router

    _raw_override("environment", "staging", "AI_GATEWAY_ENABLED", "false")

    assert router._gateway_enabled() is True


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_ENABLED=True,
    AI_GATEWAY_BASE_URL="http://ai-gateway:8100",
    AI_GATEWAY_TIMEOUT_SECONDS=180,
)
def test_embeddings_gateway_settings_are_not_rerouted_by_an_override():
    """LE test essentiel : l'indexation ne doit pas sortir de la Gateway.

    Le module d'embeddings lit ses réglages Gateway dans les SETTINGS déclarés, jamais
    dans l'overlay runtime : un override hostile ne peut ni éteindre la Gateway ni la
    rediriger — y compris dans un worker Celery, qui emprunte exactement ce chemin.
    """
    from apps.llm_router import embeddings

    _raw_override("environment", "staging", "AI_GATEWAY_ENABLED", "false")
    _raw_override("environment", "staging", "AI_GATEWAY_BASE_URL", "http://198.51.100.9:9000")

    assert embeddings._get_setting("AI_GATEWAY_ENABLED") is True
    assert embeddings._get_setting("AI_GATEWAY_BASE_URL") == "http://ai-gateway:8100"
    assert embeddings._get_setting("AI_GATEWAY_TIMEOUT_SECONDS") == 180


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_ENABLED=True,
    EMBEDDINGS_PROVIDER="ai_gateway",
    AI_GATEWAY_BASE_URL="http://ai-gateway:8100",
)
def test_embed_texts_only_knows_the_gateway_path_under_a_hostile_override(monkeypatch):
    """Preuve de bout en bout : le seul chemin d'encodage est la Gateway gouvernée."""
    from apps.llm_router import embeddings

    _raw_override("environment", "staging", "AI_GATEWAY_ENABLED", "false")

    appels: list[str] = []

    def _gateway(texts, *, model, task, dim, rag_space_id=None, document_id=None):
        appels.append("gateway")
        return embeddings.EmbeddingBatch(
            vectors=[[0.0] * dim for _ in texts], pipeline_id="emb-test", dimension=dim, task=task
        )

    monkeypatch.setattr(embeddings, "embed_via_gateway", _gateway)

    embeddings.embed_texts(["texte"], provider="ai_gateway")
    assert appels == ["gateway"]

    # L'ancien chemin direct n'existe plus : il est REFUSÉ, pas détourné.
    with pytest.raises(embeddings.EmbeddingsError):
        embeddings.embed_texts(["texte"], provider="ollama")
    assert appels == ["gateway"]


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_FALLBACK_ENABLED=False,
    AI_GATEWAY_FALLBACK_PROVIDER="none",
)
def test_egress_path_reads_the_guarded_value():
    """`llm_router.egress` lit aussi l'overlay : le plafond d'egress en dépend."""
    from apps.llm_router import egress

    _raw_override("environment", "staging", "AI_GATEWAY_FALLBACK_ENABLED", "true")

    # `none` est un provider utilisable : l'override est légitime et s'applique.
    assert egress._global_external_allowed() is True
    # …mais la porte reste FERMÉE, car le provider de repli vaut `none`.
    assert egress._global_allowed_providers() == ()


@override_settings(
    CORE_CONFIG_RUNTIME_ENABLED=True,
    DJANGO_ENV="staging",
    AI_GATEWAY_FALLBACK_PROVIDER="none",
)
def test_egress_provider_cannot_be_opened_by_an_override():
    """`AI_GATEWAY_FALLBACK_PROVIDER` n'est pas éditable : pas d'egress par override."""
    from apps.llm_router import egress

    _raw_override("environment", "staging", "AI_GATEWAY_FALLBACK_PROVIDER", "ollama")

    assert egress._global_allowed_providers() == ()


# ===========================================================================
# 5. Le module d'invariants lui-même
# ===========================================================================
def test_policy_refuses_an_unknown_key():
    with pytest.raises(ConfigPolicyViolation) as excinfo:
        config_policy.validate_overlay_value("VARIABLE_INEXISTANTE", "1")
    assert excinfo.value.code == "unknown_key"


def test_policy_types_the_value_before_cross_checks():
    """`bool("false")` vaut True en Python : la coercion doit précéder la garde."""
    with override_settings(AI_GATEWAY_FALLBACK_PROVIDER="ollama"):
        # "false" ne doit PAS déclencher la garde de repli inutilisable.
        config_policy.validate_overlay_value("AI_GATEWAY_FALLBACK_ENABLED", "false")


def test_guard_returns_the_fallback_and_never_raises():
    guarded = config_policy.guard_runtime_value(
        "AI_GATEWAY_ENABLED", False, fallback=True, environment="staging"
    )
    assert guarded is True


def test_guard_passes_a_legitimate_value_through():
    guarded = config_policy.guard_runtime_value(
        "AI_GATEWAY_TIMEOUT_SECONDS", 45, fallback=180, environment="staging"
    )
    assert guarded == 45

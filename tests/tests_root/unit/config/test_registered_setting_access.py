"""Un réglage se lit par le registre, avec UN défaut, ou ne se lit pas.

Ce que ce fichier verrouille, et pourquoi chaque invariant existe :

1. **Un nom inconnu lève.** L'ancien `getattr(settings, nom, défaut)` rendait le
   défaut sans un mot : une faute de frappe produisait un réglage fantôme, et le
   code fonctionnait « normalement » avec une valeur que personne n'avait choisie.

2. **Le défaut vient du registre.** Il en existait jusqu'à trois par paramètre
   (registre, module consommateur, banc de mesure), avec des valeurs
   DIFFÉRENTES — `RAG_QUERY_REWRITE_TIMEOUT_S` valait 6 au registre et 12 dans le
   module. Lequel s'appliquait ? Celui du premier code exécuté.

3. **La valeur est typée.** Un `.env` ne porte que du texte ; un consommateur qui
   attend un entier ne doit pas recevoir `"40"`.

4. **Un paramètre déclaré est projeté.** C'est l'invariant qui manquait : vingt
   variables étaient posées en production sans qu'aucun settings ne les porte.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.test import override_settings

from config import registry
from config.settings.access import UnregisteredSettingError, get_registered_setting


# ===========================================================================
# 1. Un nom inconnu ne passe plus en silence
# ===========================================================================
def test_unknown_name_is_refused():
    with pytest.raises(UnregisteredSettingError):
        get_registered_setting("RAG_CE_NOM_N_EXISTE_PAS")


def test_the_refusal_says_what_to_do():
    with pytest.raises(UnregisteredSettingError) as exc:
        get_registered_setting("RAG_TYPO_INVENTORY_TOPK")
    message = str(exc.value)
    assert "registre" in message
    assert "ConfigDefinition" in message or "Déclarez" in message


def test_a_typo_on_a_real_name_is_caught():
    """Le cas réel : `RAG_INVENTORY_TOP_K` mal orthographié rendait 12 sans rien dire."""
    with pytest.raises(UnregisteredSettingError):
        get_registered_setting("RAG_INVENTORY_TOPK")


# ===========================================================================
# 2. Un seul défaut : celui du registre
# ===========================================================================
@pytest.mark.parametrize(
    "name",
    [
        "RAG_INVENTORY_TOP_K",
        "RAG_LOWCONF_ENABLED",
        "RAG_LOWCONF_FACTOR",
        "RAG_RERANK_POOL_K",
        "CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES",
    ],
)
def test_absent_setting_falls_back_to_the_registry_default(name, monkeypatch):
    definition = registry.require(name)

    class _SansLeNom:
        pass

    assert get_registered_setting(name, settings_module=_SansLeNom()) == definition.default


def test_no_consumer_carries_a_second_default():
    """Le module consommateur ne redit pas le défaut : il n'a pas de quoi le faire."""
    import inspect

    from apps.rag_core.services.query import settings as qs

    source = inspect.getsource(qs.setting)
    assert "default" not in source.replace("défaut", ""), (
        "`setting(name)` ne doit accepter aucun défaut d'appel : c'est ainsi qu'on "
        "garantit qu'il n'en existe qu'un, au registre"
    )


# ===========================================================================
# 3. Typage : env → settings → consommateur
# ===========================================================================
@pytest.mark.parametrize(
    "name,raw,attendu",
    [
        ("RAG_LOWCONF_ENABLED", "0", False),
        ("RAG_LOWCONF_ENABLED", "1", True),
        ("RAG_INVENTORY_TOP_K", "40", 40),
        ("RAG_LOWCONF_FACTOR", "1.5", 1.5),
        ("ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS", "a,b", ["a", "b"]),
    ],
)
def test_environment_value_is_parsed_to_the_declared_type(name, raw, attendu):
    from config.settings.from_registry import env_value

    assert env_value(name, environ={name: raw}) == attendu


def test_absent_environment_value_gives_the_declared_default():
    from config.settings.from_registry import env_value

    assert env_value("RAG_INVENTORY_TOP_K", environ={}) == registry.require(
        "RAG_INVENTORY_TOP_K"
    ).default


@pytest.mark.parametrize("raw", ["quarante", "", "  "])
def test_invalid_integer_never_yields_a_silent_wrong_value(raw):
    """Une valeur invalide retombe sur le défaut CANONIQUE, jamais sur 0 ni sur None."""
    from config.settings.from_registry import env_value
    from django.core.exceptions import ImproperlyConfigured

    if raw.strip() == "":
        assert env_value("RAG_INVENTORY_TOP_K", environ={"RAG_INVENTORY_TOP_K": raw}) == 0
        return
    with pytest.raises(ImproperlyConfigured):
        env_value("RAG_INVENTORY_TOP_K", environ={"RAG_INVENTORY_TOP_K": raw})


@override_settings(RAG_INVENTORY_TOP_K="40")
def test_a_textual_override_is_normalised_not_propagated():
    """Un override de test qui pose du texte ne doit pas contaminer le consommateur."""
    assert get_registered_setting("RAG_INVENTORY_TOP_K") == 40


# ===========================================================================
# 4. Ce qui est déclaré est projeté (et réciproquement)
# ===========================================================================
def test_every_application_read_setting_is_projected():
    """L'invariant central du chantier, testé indépendamment du system check."""
    manquants = []
    for definition in registry.all_definitions():
        if not definition.source.startswith("backend/config/settings/"):
            continue
        if definition.environments == ("test",):
            continue
        lu_par_une_app = any(
            consumer.startswith(("apps.", "api.")) for consumer in definition.consumers
        )
        if lu_par_une_app and not hasattr(settings, definition.name):
            manquants.append(definition.name)
    assert not manquants, (
        "déclarés et lus par une application, mais absents des settings — les poser "
        f"dans un `.env` n'aurait aucun effet : {manquants}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "RAG_INVENTORY_TOP_K",
        "RAG_INVENTORY_VECTOR_TOP_K",
        "RAG_INVENTORY_MIN_UNIQUE_DOCS",
        "RAG_INVENTORY_RERANK_POOL_K",
        "RAG_INVENTORY_RERANK_TAKE",
        "RAG_EXPLAIN_RERANK_POOL_K",
        "RAG_EXPLAIN_RERANK_TAKE",
        "RAG_RERANK_POOL_K",
        "RAG_LOWCONF_ENABLED",
        "RAG_LOWCONF_FACTOR",
        "RAG_LOWCONF_MIN_ABS",
        "RAG_LOWCONF_MIN_SOURCES",
    ],
)
def test_the_twelve_recovered_settings_are_now_effective(name):
    """Ces douze-là étaient lus sans être projetés : les poser ne faisait rien."""
    assert hasattr(settings, name), f"{name} doit être projetée pour être configurable"
    assert registry.get(name) is not None, f"{name} doit être déclarée"


# ===========================================================================
# 5. Ce que le chantier a RETIRÉ ne doit pas revenir
# ===========================================================================
@pytest.mark.parametrize(
    "name",
    [
        "RAG_USE_CORE_RETRIEVE",  # bifurcation supprimée
        "RAG_RRF_K",  # constante d'algorithme
        "RAG_ENABLE_NEWS_HEURISTIC",  # mode non branché
        "RAG_NEWS_TOP_K",
        "RAG_MIN_SOURCE_SCORE",  # epsilon interne
        "RAG_MAX_SNIPPET_CHARS_AUDIT",
        "RAG_COMPARE_PROMPT_PREAMBLE",  # gabarit de prompt
        "RAG_EXPLAIN_PROMPT_PREAMBLE",
        "LLM_LOCAL_BASE_URL",  # service disparu
        "CORPUS_GLOBAL_SEARCH_MAX_ESTIMATED_COST_USD",  # plafond jamais appliqué
        "CHAT_MEMORY_SUMMARY_TARGET_CHARS",  # jamais demandé au modèle
    ],
)
def test_removed_names_are_neither_declared_nor_projected(name):
    assert registry.get(name) is None, f"{name} ne doit plus être déclarée au registre"
    assert not hasattr(settings, name), (
        f"{name} ne doit plus être projetée : la reprojeter recréerait un réglage "
        "que plus aucun code ne lit"
    )

"""Les quatre contrôles de cohérence ajoutés par le chantier de configuration.

Chacun couvre une défaillance MESURÉE, pas une hypothèse :

- `config.E010` — vingt variables posées en production, lues par personne, parce
  qu'aucun settings ne les projetait ;
- `config.W010` — des paramètres déclarés et documentés sans consommateur ;
- `config.W011` — trois noms dépréciés encore posés dans les environnements ;
- `config.W012` — `RAG_USE_CORE_RETRIEVE` survivant dans un `.env` après le
  retrait du flag, et donc sans effet.

Un contrôle qui crie sur une configuration saine finit désactivé : chaque test
vérifie aussi le SILENCE dans le cas nominal.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from config import checks, registry


def _ids(issues):
    return sorted(issue.id for issue in issues)


# ===========================================================================
# config.E010 — déclarée comme projetée, absente des settings
# ===========================================================================
def test_e010_is_silent_on_the_real_configuration():
    """La configuration du dépôt doit passer : sinon le contrôle n'est pas tenable."""
    assert checks.check_declared_but_not_projected(None) == []


def test_e010_catches_a_setting_read_by_an_app_but_not_projected(monkeypatch):
    fantome = registry.ConfigDefinition(
        name="RAG_FANTOME_NON_PROJETE",
        domain="retrieval",
        value_type="int",
        default=7,
        description="paramètre de test",
        source="backend/config/settings/base.py",
        consumers=("apps.rag_core.services.query.orchestrator",),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (fantome,))
    issues = checks.check_declared_but_not_projected(None)
    assert _ids(issues) == ["config.E010"]
    assert "RAG_FANTOME_NON_PROJETE" in issues[0].msg
    assert "aucun effet" in (issues[0].hint or "").lower() or "AUCUN effet" in (issues[0].hint or "")


def test_e010_ignores_a_setting_consumed_inside_the_settings(monkeypatch):
    """`DB_NAME` alimente `DATABASES` : exiger un attribut de même nom produirait
    des dizaines de faux positifs, et un contrôle bruyant finit ignoré."""
    interne = registry.ConfigDefinition(
        name="DB_FANTOME",
        domain="storage",
        value_type="str",
        default="x",
        description="paramètre de test",
        source="backend/config/settings/base.py",
        consumers=("config.settings.base.DATABASES",),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (interne,))
    assert checks.check_declared_but_not_projected(None) == []


def test_e010_ignores_a_setting_carried_by_another_module(monkeypatch):
    """`RAG_QUERY_REWRITE_ENABLED` est lue dans l'environnement par `rewrite.py`."""
    autre = registry.ConfigDefinition(
        name="RAG_PORTE_AILLEURS",
        domain="retrieval",
        value_type="bool",
        default=False,
        description="paramètre de test",
        source="backend/apps/rag_core/services/query/rewrite.py",
        consumers=("apps.rag_core.services.query.rewrite",),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (autre,))
    assert checks.check_declared_but_not_projected(None) == []


def test_e010_ignores_a_key_declared_for_tests_only(monkeypatch):
    """Une définition restreinte à l'environnement `test` n'a pas à être projetée.

    Aucune clé du registre canonique n'est dans ce cas — c'est délibéré : la clé
    de banc d'essai de l'overlay est INJECTÉE par fixture, jamais déclarée (cf.
    `test_synthetic_key_absent_from_production.py`). La branche du contrôle reste
    testée ici, sur une définition fabriquée.
    """
    reservee = registry.ConfigDefinition(
        name="RAG_RESERVEE_AUX_TESTS",
        domain="retrieval",
        value_type="bool",
        default=False,
        description="paramètre de test",
        source="backend/config/settings/base.py",
        consumers=("apps.rag_core.services.query.orchestrator",),
        environments=("test",),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (reservee,))
    assert checks.check_declared_but_not_projected(None) == []


# ===========================================================================
# config.W010 — déclarée sans consommateur
# ===========================================================================
def test_w010_is_silent_on_the_real_configuration():
    assert checks.check_declared_but_unconsumed(None) == []


def test_w010_catches_a_parameter_nobody_reads(monkeypatch):
    orphelin = registry.ConfigDefinition(
        name="RAG_ORPHELIN",
        domain="retrieval",
        value_type="int",
        default=1,
        description="paramètre de test",
        consumers=(),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (orphelin,))
    issues = checks.check_declared_but_unconsumed(None)
    assert _ids(issues) == ["config.W010"]


def test_w010_spares_infrastructure_only_parameters(monkeypatch):
    """Docker, Celery et Gunicorn lisent des variables qu'aucun Python ne voit."""
    infra = registry.ConfigDefinition(
        name="APP_HOST_PORT_FANTOME",
        domain="deployment",
        value_type="int",
        default=6200,
        description="paramètre de test",
        source="docker-compose",
        consumers=(),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (infra,))
    assert checks.check_declared_but_unconsumed(None) == []


def test_w010_does_not_repeat_what_w011_already_says(monkeypatch):
    deprecie = registry.ConfigDefinition(
        name="RAG_ANCIEN_NOM",
        domain="retrieval",
        value_type="int",
        default=1,
        description="paramètre de test",
        deprecated=True,
        replacement="RAG_NOUVEAU_NOM",
        consumers=(),
    )
    monkeypatch.setattr(registry, "all_definitions", lambda: (deprecie,))
    assert checks.check_declared_but_unconsumed(None) == []


# ===========================================================================
# config.W011 — nom déprécié encore posé
# ===========================================================================
def test_w011_is_silent_when_nothing_deprecated_is_set(monkeypatch):
    for alias in registry.DEPRECATED_ALIASES:
        monkeypatch.delenv(alias.old_name, raising=False)
    for definition in registry.all_definitions():
        if definition.deprecated:
            monkeypatch.delenv(definition.name, raising=False)
    assert checks.check_deprecated_names_still_set(None) == []


def test_w011_catches_a_deprecated_alias_still_set(monkeypatch):
    alias = registry.DEPRECATED_ALIASES[0]
    monkeypatch.setenv(alias.old_name, "valeur")
    issues = [i for i in checks.check_deprecated_names_still_set(None) if alias.old_name in i.msg]
    assert issues and issues[0].id == "config.W011"
    hint = issues[0].hint or ""
    assert alias.new_name in hint
    assert alias.remove_after in hint, "la date de fin de compatibilité doit être dite"


def test_w011_reports_a_name_only_once(monkeypatch):
    """Un nom à la fois déprécié ET porteur d'un alias était signalé deux fois."""
    alias = registry.DEPRECATED_ALIASES[0]
    monkeypatch.setenv(alias.old_name, "valeur")
    concernes = [i for i in checks.check_deprecated_names_still_set(None) if alias.old_name in i.msg]
    assert len(concernes) == 1


# ===========================================================================
# config.W012 — posée mais inconnue du registre
# ===========================================================================
def test_w012_catches_the_survivor_of_a_removed_flag(monkeypatch):
    """Le cas réel : `RAG_USE_CORE_RETRIEVE` reste dans un `.env` après le retrait."""
    monkeypatch.setenv("RAG_USE_CORE_RETRIEVE", "0")
    issues = [i for i in checks.check_unknown_app_variables(None) if "RAG_USE_CORE_RETRIEVE" in i.msg]
    assert issues and issues[0].id == "config.W012"
    assert "aucun effet" in issues[0].msg


def test_w012_catches_a_typo_on_an_application_variable(monkeypatch):
    monkeypatch.setenv("RAG_TOPP_K", "40")
    assert any("RAG_TOPP_K" in i.msg for i in checks.check_unknown_app_variables(None))


def test_w012_stays_quiet_on_system_variables(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/lib")
    monkeypatch.setenv("HOSTNAME", "machine")
    assert not any(
        "LD_LIBRARY_PATH" in i.msg or "HOSTNAME" in i.msg
        for i in checks.check_unknown_app_variables(None)
    )


def test_w012_stays_quiet_on_a_declared_variable(monkeypatch):
    monkeypatch.setenv("RAG_TOP_K", "40")
    assert not any("RAG_TOP_K " in i.msg for i in checks.check_unknown_app_variables(None))


# ===========================================================================
# Enregistrement
# ===========================================================================
@pytest.mark.parametrize(
    "check",
    [
        "check_declared_but_not_projected",
        "check_declared_but_unconsumed",
        "check_deprecated_names_still_set",
        "check_unknown_app_variables",
        "check_build_revision",
    ],
)
def test_every_new_check_is_registered(check):
    assert getattr(checks, check) in checks.ALL_CHECKS, (
        "un contrôle non enregistré ne s'exécute jamais — il rassure sans protéger"
    )

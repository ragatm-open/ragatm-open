"""Invariants du registre canonique de configuration (`config.registry`).

Ces tests protègent la propriété qui donne sa valeur au registre : être la SEULE
déclaration. Une définition incomplète, dupliquée ou incohérente avec les
settings réels ferait mentir la documentation, l'audit et l'Admin Core d'un seul
coup.
"""
from __future__ import annotations

import importlib
import os
from unittest import mock

import pytest
from django.conf import settings

from config import parsers, registry
from config.parsers import ConfigValueError
from config.registry.definition import ALL_ENVIRONMENTS, DOMAINS, SCOPES, ConfigDefinition


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------
def test_registry_is_not_empty():
    assert len(registry.all_definitions()) > 200


def test_names_are_unique():
    names = [d.name for d in registry.all_definitions()]
    assert len(names) == len(set(names))


def test_names_are_upper_snake_case():
    for definition in registry.all_definitions():
        assert definition.name.isupper(), definition.name
        assert definition.name.replace("_", "").isalnum(), definition.name


@pytest.mark.parametrize("definition", registry.all_definitions(), ids=lambda d: d.name)
def test_mandatory_metadata(definition: ConfigDefinition):
    assert definition.description.strip(), f"{definition.name} sans description"
    assert definition.domain in DOMAINS
    assert definition.value_type in parsers.VALUE_TYPES
    assert definition.source.strip()
    for environment in definition.environments:
        assert environment in ALL_ENVIRONMENTS


@pytest.mark.parametrize("definition", registry.all_definitions(), ids=lambda d: d.name)
def test_scope_metadata_is_coherent(definition: ConfigDefinition):
    if definition.max_scope is not None:
        assert definition.max_scope in SCOPES
    if definition.runtime_editable:
        assert definition.max_scope is not None, (
            f"{definition.name} est éditable sans portée maximale"
        )


@pytest.mark.parametrize("definition", registry.all_definitions(), ids=lambda d: d.name)
def test_bounds_are_ordered(definition: ConfigDefinition):
    if definition.minimum is not None and definition.maximum is not None:
        assert definition.minimum <= definition.maximum, definition.name


@pytest.mark.parametrize("definition", registry.all_definitions(), ids=lambda d: d.name)
def test_declared_default_respects_its_own_bounds(definition: ConfigDefinition):
    """Un défaut hors de ses propres bornes rendrait le démarrage impossible."""
    if definition.default is None or definition.default_is_dynamic:
        return
    if definition.value_type not in (parsers.TYPE_INT, parsers.TYPE_FLOAT):
        return
    parsers.check_bounds(
        definition.name,
        definition.default,
        minimum=definition.minimum,
        maximum=definition.maximum,
    )


@pytest.mark.parametrize("definition", registry.all_definitions(), ids=lambda d: d.name)
def test_enum_default_is_a_declared_choice(definition: ConfigDefinition):
    if definition.value_type != parsers.TYPE_ENUM:
        return
    assert definition.choices
    if definition.default in (None, ""):
        return
    assert definition.default in definition.choices, definition.name


@pytest.mark.parametrize("definition", registry.deprecated(), ids=lambda d: d.name)
def test_deprecated_definitions_document_the_exit(definition: ConfigDefinition):
    assert definition.replacement or definition.notes, definition.name


def test_deprecated_aliases_point_to_registered_names():
    for alias in registry.DEPRECATED_ALIASES:
        assert registry.get(alias.new_name) is not None, alias.new_name
        assert alias.since and alias.remove_after


# --------------------------------------------------------------------------
# Cohérence avec les settings réellement chargés
# --------------------------------------------------------------------------
def _comparable(definition: ConfigDefinition) -> bool:
    """Une définition est comparable si son défaut n'est ni dérivé, ni secret."""
    return not (
        definition.default_is_dynamic or definition.secret or definition.default is None
    )


def test_registry_defaults_match_the_code_defaults(settings_without_environment):
    """Le défaut déclaré au registre EST celui que le code applique.

    Comparaison faite dans un processus à environnement VIDE : comparer aux
    settings du processus de test ne prouverait rien, puisque `.env` y est chargé.
    C'est ce test qui interdit la dérive « la doc dit 8, le code fait 6 ».
    """
    loaded = settings_without_environment
    divergences: list[str] = []

    for definition in registry.all_definitions():
        if not _comparable(definition) or definition.name not in loaded:
            continue
        actual = loaded[definition.name]
        if actual is None:
            divergences.append(
                f"{definition.name}: le code produit None mais le registre déclare "
                f"{definition.default!r} sans `default_is_dynamic`"
            )
            continue
        expected = definition.default

        if definition.value_type == parsers.TYPE_BOOL:
            ok = bool(actual) == bool(expected)
        elif definition.value_type in (parsers.TYPE_INT, parsers.TYPE_FLOAT):
            ok = float(actual) == pytest.approx(float(expected))
        elif definition.value_type == parsers.TYPE_LIST:
            actual_list = (
                actual
                if isinstance(actual, list)
                else parsers.parse_list(definition.name, actual)
            )
            ok = list(actual_list) == list(expected)
        else:
            ok = str(actual) == str(expected)

        if not ok:
            divergences.append(f"{definition.name}: code={actual!r} registre={expected!r}")

    assert not divergences, "Défauts divergents entre le code et le registre :\n" + "\n".join(
        divergences
    )


def test_registry_covers_the_core_and_gateway_domains_entirely():
    """Exigence du lot : Core et Gateway sont ENTIÈREMENT couverts."""
    core_and_gateway = {
        d.name for d in registry.all_definitions() if d.domain in ("core", "gateway")
    }
    attendu = {
        "AI_GATEWAY_ENABLED",
        "AI_GATEWAY_BASE_URL",
        "AI_GATEWAY_TIMEOUT_SECONDS",
        "AI_GATEWAY_FALLBACK_ENABLED",
        "AI_GATEWAY_FALLBACK_PROVIDER",
        "AI_GATEWAY_PROVIDER",
        "AI_GATEWAY_MODEL",
        "AI_GATEWAY_REQUIRED_BASE_URL",
        "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS",
        "CORE_CONFIG_EDIT_ENABLED",
        "CORE_CONFIG_RUNTIME_ENABLED",
        "DJANGO_CACHE_URL",
        "DJANGO_ENV",
    }
    assert attendu <= core_and_gateway


# --------------------------------------------------------------------------
# Parsing et bornes portés par la définition
# --------------------------------------------------------------------------
def test_definition_parse_applies_bounds():
    definition = registry.require("AI_GATEWAY_TIMEOUT_SECONDS")
    assert definition.parse("30") == 30
    with pytest.raises(ConfigValueError):
        definition.parse("0")
    with pytest.raises(ConfigValueError):
        definition.parse("601")


def test_definition_rejects_scope_above_maximum():
    definition = registry.require("WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT")
    definition.validate_scope("global")
    with pytest.raises(ConfigValueError):
        definition.validate_scope("workspace")


def test_non_editable_definition_refuses_any_scope():
    definition = registry.require("RAG_EMBEDDING_DIM")
    for scope in SCOPES:
        with pytest.raises(ConfigValueError):
            definition.validate_scope(scope)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
def test_secret_definitions_never_expose_their_value():
    for definition in registry.secrets():
        rendered = definition.display_value("valeur-tres-secrete-123")
        assert "valeur-tres-secrete-123" not in rendered, definition.name


def test_secret_default_is_not_serialised_in_metadata():
    for definition in registry.secrets():
        payload = definition.as_dict()
        assert payload["default"] is None, definition.name
        assert payload["default_display"] == "(non affiché)", definition.name


def test_url_secret_keeps_scheme_and_port_only():
    definition = registry.require("AI_GATEWAY_BASE_URL")
    assert definition.display_value("http://ai-gateway:8100") == "http://…:8100"
    assert "192.168" not in definition.display_value("http://ai-gateway:8100")


# --------------------------------------------------------------------------
# Pureté : le registre ne doit pas dépendre de Django
# --------------------------------------------------------------------------
def test_registry_imports_without_django_settings():
    """Un import du registre ne doit déclencher NI Django, NI base, NI réseau."""
    with mock.patch.dict(os.environ, {}, clear=False):
        module = importlib.import_module("config.registry")
        importlib.reload(module)
    assert module.get("AI_GATEWAY_ENABLED") is not None


def test_registry_modules_do_not_import_django():
    import pathlib

    root = pathlib.Path(registry.__file__).parent
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "import django" not in text, path
        assert "from django" not in text, path

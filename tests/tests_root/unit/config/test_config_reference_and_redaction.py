"""Documentation générée et redaction des secrets.

Deux promesses tenues ici :

1. la référence de configuration est **déterministe** et **à jour** — sinon la CI
   ne pourrait pas détecter une documentation périmée ;
2. **aucune valeur sensible** ne franchit une surface d'exposition : API Admin
   Core, documentation générée, sortie JSON de l'audit, messages de checks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings

from config import registry
from config.registry import audit as registry_audit
from config.registry import reference

REPO_ROOT = Path(__file__).resolve().parents[4]
GENERATED = REPO_ROOT / "docs" / "10-reference" / "configuration.md"

#: Valeur sentinelle : si elle apparaît quelque part, une fuite existe.
SECRET_SENTINEL = "sentinelle-a-ne-jamais-afficher-0000"


# ==========================================================================
# Documentation générée
# ==========================================================================
def test_generation_is_deterministic():
    assert reference.render() == reference.render()


def test_generated_file_is_up_to_date():
    assert GENERATED.exists(), "lancez `python manage.py generate_config_reference`"
    assert GENERATED.read_text(encoding="utf-8") == reference.render(), (
        "docs/10-reference/configuration.md n'est plus à jour vis-à-vis du registre. "
        "Régénérez-le et commitez le résultat."
    )


def test_generated_file_lists_every_registered_variable():
    content = reference.render()
    missing = [d.name for d in registry.all_definitions() if f"`{d.name}`" not in content]
    assert not missing, f"variables absentes de la référence : {missing}"


def test_generated_file_marks_deprecations():
    content = reference.render()
    for definition in registry.deprecated():
        assert "DÉPRÉCIÉE" in content
    for alias in registry.DEPRECATED_ALIASES:
        assert alias.old_name in content
        assert alias.new_name in content


def test_generated_file_documents_the_resolution_order():
    content = reference.render()
    assert "ragspace > workspace > environment > global > settings/.env > défaut de code" in content


def test_check_mode_passes_on_the_committed_file():
    call_command("generate_config_reference", "--check")


def test_check_mode_fails_on_a_stale_file(tmp_path: Path):
    stale = tmp_path / "configuration.md"
    stale.write_text("contenu périmé\n", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        call_command("generate_config_reference", "--check", output=str(stale))
    assert "à jour" in str(excinfo.value)


def test_generation_writes_the_expected_content(tmp_path: Path):
    target = tmp_path / "configuration.md"
    call_command("generate_config_reference", output=str(target))
    assert target.read_text(encoding="utf-8") == reference.render()


# ==========================================================================
# Redaction — documentation
# ==========================================================================
def test_generated_documentation_never_shows_a_secret_default():
    content = reference.render()
    for definition in registry.secrets():
        assert definition.name in content
    # Les défauts sensibles connus ne doivent pas apparaître en clair.
    assert "dev-secret-key" not in content


def test_secret_rows_display_no_value():
    content = reference.render()
    for definition in registry.secrets():
        row = next(
            (line for line in content.splitlines() if line.startswith(f"| `{definition.name}` |")),
            None,
        )
        assert row is not None, definition.name
        assert "(non affiché)" in row, definition.name


# ==========================================================================
# Redaction — API Admin Core
# ==========================================================================
@override_settings(AI_GATEWAY_BASE_URL="http://ai-gateway:8100")
def test_admin_core_effective_config_redacts_the_gateway_url(db):
    from apps.rag_core.core.config_resolver import resolve_effective_config

    entries = {e.key: e for e in resolve_effective_config(environment="dev")}
    entry = entries["AI_GATEWAY_BASE_URL"]
    assert entry.redacted is True
    assert entry.effective_value == "http://…:8100"
    assert "192.168" not in json.dumps(entry.as_dict())


@override_settings(AI_GATEWAY_BASE_URL="")
def test_admin_core_marks_unconfigured_secret_as_not_redacted(db):
    from apps.rag_core.core.config_resolver import resolve_effective_config

    entries = {e.key: e for e in resolve_effective_config(environment="dev")}
    entry = entries["AI_GATEWAY_BASE_URL"]
    assert entry.redacted is False
    assert entry.effective_value == ""


def test_admin_core_entries_expose_the_new_metadata(db):
    from apps.rag_core.core.config_resolver import resolve_effective_config

    entries = {e.key: e.as_dict() for e in resolve_effective_config(environment="dev")}
    entry = entries["AI_GATEWAY_TIMEOUT_SECONDS"]
    assert entry["default_value"] == 30
    assert entry["effective_source"] == "settings"
    assert entry["override_value"] is None
    assert entry["override_scope"] is None
    assert entry["runtime_editable"] is True
    assert entry["max_scope"] == "environment"
    assert entry["restart_required"] is False
    assert entry["domain"] == "gateway"


@override_settings(CORE_CONFIG_RUNTIME_ENABLED=False)
def test_override_is_visible_but_not_runtime_active_when_overlay_is_off(db):
    from apps.rag_core.core.config_resolver import resolve_effective_config
    from apps.rag_core.models import CoreConfigOverride

    CoreConfigOverride.objects.create(
        scope_type="environment", scope_ref="dev", key="AI_GATEWAY_TIMEOUT_SECONDS", value="42"
    )
    entries = {e.key: e for e in resolve_effective_config(environment="dev")}
    entry = entries["AI_GATEWAY_TIMEOUT_SECONDS"]

    assert entry.override_value == 42
    assert entry.override_scope == "environment"
    assert entry.effective_source == "db:environment"
    # Visible ≠ actif : l'overlay runtime est éteint.
    assert entry.runtime_active is False


@override_settings(CORE_CONFIG_RUNTIME_ENABLED=True, DJANGO_CACHE_URL="redis://redis:6379/2")
def test_override_is_runtime_active_when_overlay_is_on(db):
    from apps.rag_core.core.config_resolver import resolve_effective_config
    from apps.rag_core.models import CoreConfigOverride

    CoreConfigOverride.objects.create(
        scope_type="environment", scope_ref="dev", key="AI_GATEWAY_TIMEOUT_SECONDS", value="42"
    )
    entries = {e.key: e for e in resolve_effective_config(environment="dev")}
    assert entries["AI_GATEWAY_TIMEOUT_SECONDS"].runtime_active is True
    # RAG_EMBEDDING_DIM reste exclue de l'overlay, flag ou pas.
    assert entries["RAG_EMBEDDING_DIM"].runtime_active is False


def test_exposed_keys_cover_every_editable_definition():
    from apps.rag_core.core.config_resolver import EXPOSED_KEYS

    editable = {d.name for d in registry.runtime_editable()}
    assert editable <= set(EXPOSED_KEYS)


# ==========================================================================
# Redaction — sortie de l'audit
# ==========================================================================
def test_audit_json_output_contains_no_secret_value(tmp_path: Path):
    (tmp_path / ".env.example").write_text(
        f"DJANGO_SECRET_KEY={SECRET_SENTINEL}\n"
        f"EMAIL_HOST_PASSWORD={SECRET_SENTINEL}\n",
        encoding="utf-8",
    )
    report = registry_audit.run_audit(
        tmp_path, files=((".env.example", "dev"),), scan_code=False
    )
    payload = json.dumps(report.as_dict(), ensure_ascii=False)
    assert SECRET_SENTINEL not in payload
    assert any(f.code == "env.secret_leak" for f in report.errors)


def test_registry_metadata_export_contains_no_secret_default():
    payload = json.dumps(
        [d.as_dict() for d in registry.all_definitions()], ensure_ascii=False
    )
    assert "dev-secret-key" not in payload

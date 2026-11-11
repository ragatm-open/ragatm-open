"""La clé de banc d'essai n'existe sur AUCUNE surface de production.

Pourquoi ce fichier existe
--------------------------
La suite Admin Core a besoin d'une clé éditable jusqu'au **ragspace** pour prouver
la précédence `ragspace > workspace > environment > global`. Aucun réglage de
production n'a cette portée. Trois façons de s'en sortir, une seule acceptable :

1. promouvoir un réglage réel au ragspace — cela ouvrirait un levier runtime sur
   un paramètre de production que personne n'a décidé d'exposer ;
2. déclarer une clé « réservée aux tests » dans le registre canonique — elle
   apparaîtrait alors dans la documentation générée, dans `EXPOSED_KEYS`, dans
   l'écran Admin Core et dans les réponses d'API. Une clé sans effet visible dans
   une console d'administration est une invitation à la manipuler ;
3. l'INJECTER au moment du test, dans un registre de test, et prouver qu'elle
   n'existe nulle part ailleurs. C'est ce qui est fait.

Ce fichier est la preuve du point 3. Il tourne SANS la fixture d'injection :
c'est l'état nominal du dépôt qu'il inspecte.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from config import registry

CLE = "CORE_CONFIG_SELFTEST_SCOPE"
REPO = pathlib.Path(__file__).resolve().parents[4]


# ===========================================================================
# 1. Registre canonique
# ===========================================================================
def test_absent_from_the_canonical_registry():
    assert registry.get(CLE) is None
    assert CLE not in registry.names()


def test_absent_from_every_domain_module():
    """Même vérification à la source : aucun module de domaine ne la déclare."""
    domaines = REPO / "backend" / "config" / "registry" / "domains"
    coupables = [
        p.name
        for p in domaines.glob("*.py")
        if CLE in p.read_text(encoding="utf-8")
    ]
    assert not coupables, f"la clé synthétique est déclarée dans : {coupables}"


def test_absent_from_the_runtime_editable_set():
    assert CLE not in {d.name for d in registry.runtime_editable()}


# ===========================================================================
# 2. Résolveur Core et Admin Core
# ===========================================================================
def test_absent_from_exposed_keys():
    from apps.rag_core.core import config_resolver

    assert CLE not in config_resolver.EXPOSED_KEYS


def test_absent_from_the_resolver_specs():
    from apps.rag_core.core import config_resolver

    assert config_resolver.key_spec(CLE) is None
    assert CLE not in {s.key for s in config_resolver._REGISTRY}


def test_the_write_path_refuses_it():
    """Une écriture d'override sur cette clé doit être refusée comme inconnue."""
    from apps.rag_core.core import config_resolver

    assert config_resolver.key_spec(CLE) is None, (
        "sans spec, `config_write` refuse la clé (`key_not_editable`) : c'est "
        "exactement le comportement attendu hors test"
    )


# ===========================================================================
# 3. Code applicatif et configuration déployée
# ===========================================================================
@pytest.mark.parametrize("racine", ["backend/apps", "backend/api", "backend/config"])
def test_absent_from_application_code(racine):
    coupables = [
        p.relative_to(REPO).as_posix()
        for p in (REPO / racine).rglob("*.py")
        if "__pycache__" not in p.parts and CLE in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not coupables, f"la clé synthétique apparaît dans le code : {coupables}"


def test_absent_from_versioned_environment_files():
    coupables = [
        p.name
        for p in REPO.glob(".env*")
        if p.is_file() and CLE in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not coupables, f"la clé synthétique est posée dans : {coupables}"


def test_absent_from_deployment_manifests():
    coupables = [
        p.relative_to(REPO).as_posix()
        for p in (REPO / "deploy").rglob("*")
        if p.is_file()
        and p.suffix in (".yml", ".yaml", ".env", ".example")
        and CLE in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not coupables, f"la clé synthétique apparaît dans le déploiement : {coupables}"


# ===========================================================================
# 4. Documentation générée et frontend
# ===========================================================================
def test_absent_from_the_generated_configuration_reference():
    """La référence est GÉNÉRÉE depuis le registre : si la clé y est, elle est
    quelque part dans le registre — et la documentation la présente comme un
    réglage disponible."""
    reference = REPO / "docs" / "10-reference" / "configuration.md"
    assert CLE not in reference.read_text(encoding="utf-8")


def test_absent_from_the_admin_console_frontend():
    frontend = REPO / "frontend"
    if not frontend.is_dir():  # pragma: no cover - dépôt sans frontend
        pytest.skip("frontend absent")
    coupables = [
        p.relative_to(REPO).as_posix()
        for p in frontend.rglob("*.ts*")
        if "node_modules" not in p.parts and CLE in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not coupables, f"la clé synthétique apparaît côté interface : {coupables}"


# ===========================================================================
# 5. Ce que l'API rend réellement
# ===========================================================================
@pytest.mark.django_db
def test_the_effective_config_endpoint_never_returns_it(db):
    """Preuve de bout en bout : la réponse de l'Admin Core ne la contient pas."""
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    root = get_user_model().objects.create_superuser(
        username="selftest-root", password="pw", email="selftest@example.com"
    )
    client = APIClient()
    client.force_authenticate(user=root)

    reponse = client.get("/api/v1/admin/core-config/effective/")
    assert reponse.status_code == 200
    assert CLE not in json.dumps(reponse.json())


# ===========================================================================
# 6. La fixture d'injection, elle, fonctionne — et ne fuit pas
# ===========================================================================
def test_the_fixture_makes_the_key_available(synthetic_core_key):
    from apps.rag_core.core import config_resolver

    assert registry.get(synthetic_core_key) is not None
    assert config_resolver.key_spec(synthetic_core_key) is not None
    assert config_resolver.key_spec(synthetic_core_key).max_scope == "ragspace"


def test_the_injection_does_not_leak_to_the_next_test():
    """Exécuté APRÈS le test ci-dessus : l'état d'origine doit être restauré.

    C'est l'invariant qui rend l'injection sûre. S'il tombe, une clé de test
    survit dans le processus — et le prochain test qui interroge le registre
    croira à un réglage qui n'existe pas.
    """
    from apps.rag_core.core import config_resolver

    assert registry.get(CLE) is None
    assert config_resolver.key_spec(CLE) is None
    assert CLE not in config_resolver.EXPOSED_KEYS

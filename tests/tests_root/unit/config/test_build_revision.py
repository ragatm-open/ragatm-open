"""La révision annoncée est celle qui tourne.

`APP_GIT_SHA` était posée à la main dans `.env`. En production elle a annoncé
pendant plusieurs jours un commit ancien alors que le code déployé
était plus récent de plusieurs dizaines de révisions. Aucune erreur, aucun avertissement :
rien ne comparait jamais l'annonce à la réalité. Les incidents, les diagnostics et
les mesures de performance étaient donc attribués au mauvais code.

Le lot déplace la source de vérité vers le BUILD (argument, label OCI, fichier
`/app/BUILD_REVISION`) et ajoute le contrôle qui manquait.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from config import checks


@pytest.fixture
def revision_construite(monkeypatch):
    """Simule le fichier écrit par le Dockerfile."""

    def _poser(valeur: str | None):
        monkeypatch.setattr(checks, "_built_revision", lambda: valeur or "")

    return _poser


def _ids(issues):
    return sorted({issue.id for issue in issues})


# ===========================================================================
# Hors image : rien à confronter, donc rien à dire
# ===========================================================================
@override_settings(APP_GIT_SHA="")
def test_outside_an_image_nothing_is_claimed(revision_construite):
    revision_construite(None)
    assert checks.check_build_revision(None) == []


@override_settings(APP_GIT_SHA="abcdef1234")
def test_a_sha_without_a_built_revision_is_not_an_error(revision_construite):
    """Développement local : le SHA vient du dépôt, il n'y a pas d'image à contredire."""
    revision_construite(None)
    assert checks.check_build_revision(None) == []


# ===========================================================================
# Dans l'image : l'annonce doit correspondre
# ===========================================================================
@override_settings(APP_GIT_SHA="aaaa1111bbbb2222cccc3333dddd4444eeee5555")
def test_matching_revisions_are_silent(revision_construite):
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    assert checks.check_build_revision(None) == []


@override_settings(APP_GIT_SHA="0123456789abcdef0123456789abcdef01234567")
def test_the_measured_production_defect_is_now_caught(revision_construite):
    """Le cas réel : `.env` annonce 0123456, l'image contient aaaa111."""
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    issues = checks.check_build_revision(None)
    assert _ids(issues) == ["config.E011"]
    message = issues[0].msg
    assert "0123456789ab" in message and "aaaa1111bbbb" in message


@override_settings(APP_GIT_SHA="")
def test_an_empty_sha_inside_an_image_loses_the_provenance(revision_construite):
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    issues = checks.check_build_revision(None)
    assert _ids(issues) == ["config.E011"]
    assert "vide" in issues[0].msg


# ===========================================================================
# Arbre sale : identifiable, jamais présenté comme un commit
# ===========================================================================
@override_settings(APP_GIT_SHA="aaaa1111bbbb2222cccc3333dddd4444eeee5555-dirty", DJANGO_ENV="dev")
def test_a_dirty_tree_is_a_warning_in_development(revision_construite):
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555-dirty")
    issues = checks.check_build_revision(None)
    assert _ids(issues) == ["config.W013"]
    assert "MODIFIÉ" in issues[0].msg


@override_settings(APP_GIT_SHA="aaaa1111bbbb2222cccc3333dddd4444eeee5555-dirty", DJANGO_ENV="prod")
def test_a_dirty_tree_is_an_error_in_production(revision_construite):
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555-dirty")
    issues = checks.check_build_revision(None)
    assert issues and issues[0].__class__.__name__ == "Error"


@override_settings(APP_GIT_SHA="aaaa111-dirty")
def test_a_dirty_sha_is_never_reported_as_a_mismatch(revision_construite):
    """Un arbre sale est une anomalie de PROVENANCE, pas de correspondance : le
    dire deux fois avec deux diagnostics différents brouillerait le message."""
    revision_construite("autre-chose")
    assert _ids(checks.check_build_revision(None)) == ["config.W013"]


# ===========================================================================
# Le message doit conduire à l'action
# ===========================================================================
@override_settings(APP_GIT_SHA="0123456789abcdef0123456789abcdef01234567")
def test_the_hint_names_the_tool_that_fixes_it(revision_construite):
    revision_construite("aaaa1111bbbb2222cccc3333dddd4444eeee5555")
    hint = checks.check_build_revision(None)[0].hint or ""
    assert "build-env.sh" in hint
    assert ".env" in hint


def test_the_check_is_registered():
    assert checks.check_build_revision in checks.ALL_CHECKS

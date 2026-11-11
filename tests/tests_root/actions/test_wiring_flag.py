"""Flags ACTIONS_* : défaut applicatif RÉEL, indépendant du `.env` local (F7).

Le défaut est figé à l'import des settings depuis ``os.getenv`` ; en DEV, le `.env`
est chargé (``load_dotenv``) et peut poser ces flags à 1 sur la machine du dev. Pour
tester le DÉFAUT DU CODE (variable absente), on lance un SUBPROCESS hermétique :
- ``DJANGO_ENV=test`` -> ``base.py`` NE charge PAS le `.env` (garde ligne 11) ;
- variables retirées de l'environnement -> le parseur applique son défaut.
Ainsi le test est fiable quel que soit l'environnement parent (y compris pollué).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from apps.actions import wiring

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../backend
_FLAGS = ("ACTIONS_ROUTER_ENABLED", "ACTIONS_LLM_CLASSIFIER_ENABLED")

_PROBE = (
    "from django.conf import settings as s;"
    "print(int(bool(s.ACTIONS_ROUTER_ENABLED)));"
    "print(int(bool(s.ACTIONS_LLM_CLASSIFIER_ENABLED)))"
)


def _probe(*, overrides: dict | None = None, parent_pollution: dict | None = None) -> tuple[bool, bool]:
    """Retourne (router, classifier) tels que résolus par les settings dans un
    subprocess hermétique (sans chargement du `.env`)."""
    env = os.environ.copy()
    # simule un environnement parent pollué AVANT nettoyage (Cas 5)
    for k, v in (parent_pollution or {}).items():
        env[k] = v
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.dev"
    env["DJANGO_ENV"] = "test"   # NE charge PAS le .env (base.py:11)
    env["LOAD_DOTENV"] = "0"
    for k in _FLAGS:             # environnement hermétique : variables retirées
        env.pop(k, None)
    for k, v in (overrides or {}).items():
        env[k] = v
    r = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=_BACKEND, env=env,
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"probe failed: {r.stderr}"
    router, clf = r.stdout.split()
    return router == "1", clf == "1"


# ---- Cas 1 : variables absentes -> défaut applicatif = désactivé --------------
def test_default_is_off_when_env_absent():
    router, clf = _probe()
    assert router is False
    assert clf is False


# ---- Cas 2 : variables à 1 -> activées ----------------------------------------
def test_env_true_enables():
    router, clf = _probe(overrides={"ACTIONS_ROUTER_ENABLED": "1", "ACTIONS_LLM_CLASSIFIER_ENABLED": "1"})
    assert router is True
    assert clf is True


# ---- Cas 3 : variables à 0 -> désactivées -------------------------------------
def test_env_zero_disables():
    router, clf = _probe(overrides={"ACTIONS_ROUTER_ENABLED": "0", "ACTIONS_LLM_CLASSIFIER_ENABLED": "0"})
    assert router is False
    assert clf is False


# ---- Cas 4 : valeur invalide -> convention `== "1"` => faux -------------------
@pytest.mark.parametrize("bad", ["unexpected", "true", "TRUE", "yes", "2", ""])
def test_invalid_value_is_false(bad):
    router, _ = _probe(overrides={"ACTIONS_ROUTER_ENABLED": bad})
    assert router is False, f"valeur {bad!r} devrait être fausse (convention ==\"1\")"


# ---- Cas 5 : environnement parent pollué -> le test du défaut reste fiable -----
def test_default_reliable_despite_polluted_parent():
    router, clf = _probe(parent_pollution={"ACTIONS_ROUTER_ENABLED": "1", "ACTIONS_LLM_CLASSIFIER_ENABLED": "1"})
    # le probe retire les flags de l'env hermétique -> défaut appliqué malgré la pollution
    assert router is False
    assert clf is False


# ---- Baseline suite : le conftest force OFF (opt-in), overridable par test -----
def test_suite_baseline_is_off():
    # Dans la suite (fixture autouse _deterministic_llm_baseline), les flags sont OFF.
    assert wiring.is_router_enabled() is False


def test_llm_seam_disabled_in_wiring():
    assert wiring._router._llm is None

"""F5-A/C : un échec d'écriture cache dans runtime_config est VISIBLE (log) et ne
retourne pas un succès mensonger — la valeur correcte (settings) est renvoyée."""
from __future__ import annotations

import logging

import pytest

from apps.rag_core.core import runtime_config as rc


@pytest.fixture(autouse=True)
def _inject_synthetic_key(synthetic_core_key):
    """`CORE_CONFIG_SELFTEST_SCOPE` est une clé de BANC D'ESSAI.

    Elle n'existe pas dans le registre canonique : sans cette injection, ces
    tests échoueraient — et c'est voulu. Aucun réglage de production ne monte
    jusqu'au ragspace, et en promouvoir un pour les besoins du test aurait ouvert
    un levier runtime sur un paramètre réel.
    """



def test_cache_set_failure_is_logged_and_value_returned(settings, monkeypatch, caplog):
    settings.CORE_CONFIG_RUNTIME_ENABLED = True

    # isole le chemin cache.set : cache.get -> miss, _compute -> valeur, cache.set -> échoue
    monkeypatch.setattr(rc.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(rc, "_compute", lambda *a, **k: False)

    def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(rc.cache, "set", boom)

    with caplog.at_level(logging.WARNING, logger="ragatm.core.runtime_config"):
        value = rc.get("CORE_CONFIG_SELFTEST_SCOPE", default=True)

    # échec rendu visible (pas de pass silencieux)
    assert "core_config_cache_set_failed" in caplog.text
    # pas de succès mensonger : la valeur settings correcte est renvoyée
    assert value is False


def test_flag_off_returns_settings_without_cache(settings, monkeypatch):
    settings.CORE_CONFIG_RUNTIME_ENABLED = False
    settings.CORE_CONFIG_SELFTEST_SCOPE = True
    # aucun accès cache attendu -> si get/set étaient appelés, on le saurait
    monkeypatch.setattr(rc.cache, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache used")))
    assert rc.get("CORE_CONFIG_SELFTEST_SCOPE", default=False) is True

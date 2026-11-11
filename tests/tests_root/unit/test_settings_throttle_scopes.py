"""Aucun débit nommé ne doit disparaître entre `base.py` et `prod.py`.

POURQUOI CE TEST EXISTE
-----------------------
`prod.py` REMPLAÇAIT `DEFAULT_THROTTLE_RATES` en bloc, en recopiant à la main les
scopes de `base.py`. Tout scope oublié dans cette recopie disparaissait en
staging **et** en production. Or DRF ne dégrade pas : il lève
``ImproperlyConfigured: No default throttle rate set for '<scope>'``, ce qui
produit un **HTTP 500 sur chaque appel** de l'endpoint concerné.

Le défaut est INVISIBLE en développement, qui n'utilise que `base.py` — il ne se
manifeste qu'une fois déployé. Constaté en qualification staging du 2026-07-28 :
`feedback_create` manquait, donc tout clic sur le pouce aurait renvoyé 500 en
production.

Ce test verrouille l'invariant : **prod ⊇ base**. Il échoue à l'ajout du prochain
scope si quelqu'un revient à un remplacement en bloc.
"""
from __future__ import annotations

import importlib

import pytest
from django.conf import settings

#: Variables minimales pour que `prod.py` s'importe jusqu'au bout.
_PROD_ENV = {
    "DJANGO_ENV": "prod",
    "DJANGO_ALLOWED_HOSTS": "app.example.test",
    "DJANGO_SECRET_KEY": "test-only-secret-key-for-prod-settings-reload-0123456789",
    "EMBEDDINGS_PROVIDER": "ai_gateway",
    "FRONTEND_APP_ORIGIN": "https://app.example.test",
}


@pytest.fixture
def prod_settings(monkeypatch):
    for key, value in _PROD_ENV.items():
        monkeypatch.setenv(key, value)
    import config.settings.prod as prod

    return importlib.reload(prod)


def _base_scopes() -> set[str]:
    import config.settings.base as base

    return set((base.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES") or {}).keys())


def test_prod_keeps_every_named_scope_declared_in_base(prod_settings):
    """`prod` doit CONTENIR tous les scopes de `base`, jamais les remplacer."""
    base_scopes = _base_scopes()
    prod_scopes = set(prod_settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].keys())

    missing = base_scopes - prod_scopes
    assert not missing, (
        f"Scopes perdus entre base.py et prod.py : {sorted(missing)}. "
        "DRF lèverait ImproperlyConfigured (HTTP 500) sur les endpoints concernés."
    )


def test_feedback_scope_has_a_rate_in_production(prod_settings):
    """Régression nommée : le clic sur le pouce ne doit pas renvoyer 500."""
    rates = prod_settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    assert "feedback_create" in rates
    assert rates["feedback_create"], "un débit vide désactiverait le garde-fou"


def test_every_scoped_view_has_a_matching_rate(prod_settings):
    """Toute vue portant un `throttle_scope` doit trouver son débit en production.

    Le contrôle part des VUES, pas d'une liste tenue à la main : c'est ce qui le
    rend valable pour les scopes qui n'existent pas encore.
    """
    from rest_framework.views import APIView

    def _walk(cls):
        yield cls
        for sub in cls.__subclasses__():
            yield from _walk(sub)

    # Importer les URLconf peuple le graphe de sous-classes d'APIView.
    importlib.import_module("api.v1.urls")

    rates = prod_settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    orphans = sorted(
        {
            scope
            for cls in _walk(APIView)
            if (scope := getattr(cls, "throttle_scope", None))
            and scope not in rates
        }
    )
    assert not orphans, (
        f"Vues avec un `throttle_scope` sans débit en production : {orphans}."
    )


def test_dev_also_declares_the_feedback_rate():
    """En développement le débit vient de `base.py` — d'où l'angle mort d'origine."""
    assert "feedback_create" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

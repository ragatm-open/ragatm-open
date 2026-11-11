"""P-A — Validation settings de FRONTEND_APP_ORIGIN.

- dev/base : valeur lue depuis l'environnement avec défaut explicite ;
- prod : variable OBLIGATOIRE, le démarrage échoue si absente / vide / non absolue.

On recharge `config.settings.prod` sous un environnement contrôlé. Les autres
gardes prod (SECRET_KEY, ALLOWED_HOSTS, EMBEDDINGS_PROVIDER) sont satisfaites
pour isoler la validation de FRONTEND_APP_ORIGIN.
"""
import importlib

import pytest

# Variables requises pour que prod.py aille jusqu'à la validation FRONTEND_APP_ORIGIN.
_PROD_ENV_OK = {
    "DJANGO_ENV": "prod",
    "DJANGO_ALLOWED_HOSTS": "app.example.test",
    "DJANGO_SECRET_KEY": "test-only-secret-key-for-prod-settings-reload-0123456789",
    "EMBEDDINGS_PROVIDER": "ai_gateway",
}


def _reload_prod(monkeypatch, overrides):
    env = {**_PROD_ENV_OK, **overrides}
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import config.settings.prod as prod_settings

    return importlib.reload(prod_settings)


def test_base_reads_frontend_origin_from_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_APP_ORIGIN", "http://localhost:4321")
    import config.settings.base as base_settings

    reloaded = importlib.reload(base_settings)
    try:
        assert reloaded.FRONTEND_APP_ORIGIN == "http://localhost:4321"
    finally:
        # Recharge avec le défaut pour ne pas polluer les autres tests.
        monkeypatch.delenv("FRONTEND_APP_ORIGIN", raising=False)
        importlib.reload(base_settings)


def test_base_has_explicit_dev_default(monkeypatch):
    monkeypatch.delenv("FRONTEND_APP_ORIGIN", raising=False)
    import config.settings.base as base_settings

    reloaded = importlib.reload(base_settings)
    assert reloaded.FRONTEND_APP_ORIGIN == "http://127.0.0.1:6300"


def test_prod_requires_frontend_origin(monkeypatch):
    with pytest.raises(RuntimeError, match="FRONTEND_APP_ORIGIN"):
        _reload_prod(monkeypatch, {"FRONTEND_APP_ORIGIN": None})


def test_prod_rejects_empty_frontend_origin(monkeypatch):
    with pytest.raises(RuntimeError, match="FRONTEND_APP_ORIGIN"):
        _reload_prod(monkeypatch, {"FRONTEND_APP_ORIGIN": "   "})


def test_prod_rejects_non_absolute_frontend_origin(monkeypatch):
    with pytest.raises(RuntimeError, match="FRONTEND_APP_ORIGIN"):
        _reload_prod(monkeypatch, {"FRONTEND_APP_ORIGIN": "ragatm.example.org"})


def test_prod_accepts_valid_frontend_origin(monkeypatch):
    prod = _reload_prod(monkeypatch, {"FRONTEND_APP_ORIGIN": "https://app.example.test/"})
    # rstrip("/") appliqué côté settings.
    assert prod.FRONTEND_APP_ORIGIN == "https://app.example.test"

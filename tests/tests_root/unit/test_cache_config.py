"""Tests — config CACHES partagée (Redis) pour l'epoch RagATM_Core runtime.

On teste le builder ``_build_caches`` directement (les settings sont chargés une
seule fois) : sans DJANGO_CACHE_URL => LocMemCache explicite ; avec => django-redis.
"""
from pathlib import Path

from config.settings.base import _build_caches


def test_build_caches_locmem_without_url():
    caches = _build_caches("")
    assert caches["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"
    # LocMem explicite (pas le défaut implicite anonyme).
    assert caches["default"]["LOCATION"] == "ragatm-locmem"


def test_build_caches_redis_with_url():
    caches = _build_caches("redis://redis:6379/2")
    d = caches["default"]
    assert d["BACKEND"] == "django_redis.cache.RedisCache"
    assert d["LOCATION"] == "redis://redis:6379/2"
    assert d["OPTIONS"]["CLIENT_CLASS"] == "django_redis.client.DefaultClient"


def test_env_example_documents_cache_url():
    root = Path(__file__).resolve().parents[3]
    text = (root / ".env.example").read_text(encoding="utf-8")
    assert "DJANGO_CACHE_URL=" in text


def test_runtime_config_epoch_roundtrip_with_active_cache(db):
    # runtime_config continue de fonctionner avec le cache actif (LocMem en test).
    from django.core.cache import cache

    from apps.rag_core.core import runtime_config

    cache.clear()
    before = runtime_config.current_epoch()
    runtime_config.bump_epoch()
    assert runtime_config.current_epoch() == before + 1
    cache.clear()


def test_settings_caches_default_present(settings):
    # Le projet définit explicitement CACHES["default"] (plus de défaut implicite).
    assert "default" in settings.CACHES
    assert "BACKEND" in settings.CACHES["default"]

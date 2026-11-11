"""system check ``llm_router.E007`` (trois URL Gateway) et
préflight de résolution des endpoints (lecture seule).

E002 (génération : vide/loopback) n'est PAS affaibli ; E007 l'étend aux deux autres URL et
refuse Ollama direct. Édition Open : un hôte est un pool s'il figure dans
``AI_GATEWAY_POOL_HOSTS`` ; toute autre URL http(s) est une Gateway directe.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.llm_router.checks import check_ai_gateway_base_url, check_gateway_endpoints
from apps.llm_router.gateway import endpoints as EP

POOL = "http://ai-gateway-pool:8080"
DIRECT_STG = "http://ai-gateway:8100"
DIRECT_PROD = "http://ai-gateway:8100"


def _conf(settings, env="staging", base=DIRECT_STG, emb=DIRECT_STG, rr=DIRECT_STG):
    settings.DJANGO_ENV = env
    settings.ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = False
    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = base
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = emb
    settings.RAG_RERANKER_BASE_URL = rr
    settings.AI_GATEWAY_POOL_HOSTS = ["ai-gateway-pool"]


def _ids(errors):
    return [e.id for e in errors]


class TestCheckE007:
    def test_gateway_directe_acceptee(self, settings):
        _conf(settings)
        assert check_gateway_endpoints(None) == [] and check_ai_gateway_base_url(None) == []

    def test_pool_interne_staging_accepte(self, settings):
        _conf(settings, base=POOL, emb=POOL, rr=POOL)
        assert check_gateway_endpoints(None) == [] and check_ai_gateway_base_url(None) == []

    def test_e002_inchange_loopback_generation(self, settings):
        _conf(settings, base="http://127.0.0.1:8200")
        assert _ids(check_ai_gateway_base_url(None)) == ["llm_router.E002"]
        assert check_gateway_endpoints(None) == []                    # pas de double signalement

    @pytest.mark.parametrize("field", ["emb", "rr"])
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "0.0.0.0"])
    def test_loopback_embeddings_rerank_refuse(self, settings, field, host):
        _conf(settings, **{field: f"http://{host}:8200"})
        assert _ids(check_gateway_endpoints(None)) == ["llm_router.E007"]

    @pytest.mark.parametrize("field", ["base", "emb", "rr"])
    def test_ollama_direct_refuse(self, settings, field):
        _conf(settings, **{field: "http://ollama:11434"})
        assert "llm_router.E007" in _ids(check_gateway_endpoints(None))

    @pytest.mark.parametrize("bad", ["ai-gateway-pool:8200", "ftp://x:8200", "http://", "http://h:99999"])
    def test_url_invalide_refusee(self, settings, bad):
        _conf(settings, rr=bad)
        assert _ids(check_gateway_endpoints(None)) == ["llm_router.E007"]

    def test_url_vide_laissee_aux_checks_dedies(self, settings):
        _conf(settings, rr="")
        assert check_gateway_endpoints(None) == []

    def test_gateway_desactivee_ou_env_non_officiel(self, settings):
        _conf(settings, emb="http://127.0.0.1:1")
        settings.AI_GATEWAY_ENABLED = False
        assert check_gateway_endpoints(None) == []
        settings.AI_GATEWAY_ENABLED = True
        settings.DJANGO_ENV = "base"
        assert check_gateway_endpoints(None) == []

    def test_check_enregistre(self):
        from django.core.checks import registry

        assert check_gateway_endpoints in registry.registry.registered_checks


# --------------------------------------------------------------------------- préflight
def _s(env="staging", base=DIRECT_STG, emb=DIRECT_STG, rr=DIRECT_STG):
    return SimpleNamespace(DJANGO_ENV=env, AI_GATEWAY_BASE_URL=base, AI_GATEWAY_EMBEDDINGS_BASE_URL=emb,
                           RAG_RERANKER_BASE_URL=rr, CORE_CONFIG_RUNTIME_ENABLED=False,
                           AI_GATEWAY_POOL_HOSTS=["ai-gateway-pool"])


class _R:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body

    def json(self):
        return self._b


def _fake_get(pool: bool, env="staging"):
    def get(url, timeout=None):
        if url.endswith("/health/runtime"):
            return _R(200, {"environment": env, "build": {"git_sha": "0ba5e0ba5e0b"}})
        if url.endswith("/pool/health/ready") and pool:
            return _R(200, {"status": "ready", "environment": env, "routing_policy": "SINGLE_NODE",
                            "job_affinity_store": "ok"})
        return _R(404, {"detail": "Not Found"})
    return get


class TestPreflight:
    def test_direct_actuel(self):
        r = EP.build_preflight(settings_obj=_s(), read_overlay=False)
        assert r.status == "PASS" and {e.route for e in r.endpoints} == {"direct"}
        assert "GATEWAY_ROUTES=direct" in r.as_lines()

    def test_pool_prepare_avec_sonde(self):
        r = EP.build_preflight(settings_obj=_s(base=POOL, emb=POOL, rr=POOL), probe=True,
                               get=_fake_get(pool=True), read_overlay=False)
        assert r.status == "PASS", r.problems
        assert all(e.probe["via_pool"] == "yes" and e.probe["gateway_environment"] == "staging" for e in r.endpoints)

    def test_hote_pool_sans_reponse_de_pool(self):
        r = EP.build_preflight(settings_obj=_s(base=POOL, emb=POOL, rr=POOL), probe=True,
                               get=_fake_get(pool=False), read_overlay=False)
        assert r.status == "FAIL" and any("pool_host_without_pool_answer" in p for p in r.problems)

    def test_mauvais_environnement_gateway(self):
        r = EP.build_preflight(settings_obj=_s(), probe=True, get=_fake_get(pool=False, env="production"),
                               read_overlay=False)
        assert any("gateway_environment_mismatch" in p for p in r.problems)

    def test_url_vide_ou_invalide(self):
        r = EP.build_preflight(settings_obj=_s(rr="", emb="ftp://autre:8200"), read_overlay=False)
        assert "RAG_RERANKER_BASE_URL:empty" in r.problems and "AI_GATEWAY_EMBEDDINGS_BASE_URL:invalid_url" in r.problems

    def test_hote_quelconque_est_une_gateway_directe(self):
        r = EP.build_preflight(settings_obj=_s(emb="http://autre-gateway:9000"), read_overlay=False)
        assert r.status == "PASS" and {e.route for e in r.endpoints} == {"direct"}

    def test_routes_mixtes_refusees(self):
        r = EP.build_preflight(settings_obj=_s(base=POOL), read_overlay=False)
        assert any(p.startswith("mixed_gateway_routes") for p in r.problems)

    def test_sonde_jamais_health_ready(self):
        seen = []
        EP.build_preflight(settings_obj=_s(), probe=True, read_overlay=False,
                           get=lambda url, timeout=None: seen.append(url) or _R(404, None))
        paths = [u.split("8100", 1)[1] for u in seen]
        assert paths == ["/health/runtime", "/pool/health/ready"]   # jamais `/health/ready` de la Gateway
        # 3 URL identiques : sondées une seule fois.

    @pytest.mark.django_db
    def test_overlay_en_base_signale(self, settings):
        from apps.rag_core.models.core_config import CoreConfigOverride

        CoreConfigOverride.objects.create(scope_type="global", scope_ref="", key="AI_GATEWAY_BASE_URL",
                                          value=DIRECT_STG)
        s = _s()
        s.CORE_CONFIG_RUNTIME_ENABLED = True
        r = EP.build_preflight(settings_obj=s)
        assert r.overlay_gateway_overrides == 1 and "db_overlay_overrides_ai_gateway_base_url" in r.problems

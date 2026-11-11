"""refus PROPRES à AI Gateway Pool vus par chaque client RAGatm.

Forme des refus (code déployé du pool, révision de référence, et relevé LIVE du 07/11/2026 depuis
l'image backend RAGatm staging sur le réseau du pool) : en-tête ``X-AIGP-Error: <CODE>`` +
corps ``{"error": {"code", "message", "request_id"[, "compensation"]}}``.

Ces tests SIMULENT le pool : ils prouvent l'interprétation côté client, pas l'intégration
live (génération, vision et StructX n'ont pas été exécutés en live via le pool).
"""
from __future__ import annotations

import io
import urllib.error

import pytest
import requests

from apps.llm_router.gateway import pool_errors as PE
from apps.llm_router.gateway import vision_v2
from apps.llm_router.gateway.client import AIGatewayClient
from apps.llm_router.gateway.dto import GatewayGenerationRequest
from apps.llm_router.gateway.exceptions import (
    GatewayContractError,
    GatewayStreamError,
    GatewayTemporaryUnavailable,
    GatewayTimeoutError,
    classify_http_status,
    is_fallback_eligible,
)
from apps.llm_router.gateway.vision import classify_vision_error


def pool_body(code: str, rid: str = "req-abc123", **extra) -> dict:
    return {"error": {"code": code, "message": "texte libre jamais lu", "request_id": rid, **extra}}


def pool_headers(code: str, **extra) -> dict:
    return {"X-AIGP-Error": code, "X-Request-ID": "req-abc123", "content-type": "application/json", **extra}


class _Resp:
    def __init__(self, status, body=None, headers=None, lines=None):
        self.status_code = status
        self._body = body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self._lines = lines or []
        self.text = "" if body is None else str(body)
        self.raw = None

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def close(self):
        pass


# --------------------------------------------------------------------------- parseur
class TestParser:
    @pytest.mark.parametrize("code,status", [(c, s[0]) for c, s in PE.POOL_CODES.items()])
    def test_vocabulaire_connu_normalise_sans_synonymie(self, code, status):
        info = PE.parse_pool_error(status, pool_headers(code), pool_body(code))
        assert info is not None and info.known
        assert info.code == code                                   # code ORIGINAL conservé
        assert info.normalized == "pool_" + code.lower()           # jamais un code Gateway
        assert info.http_status == status
        assert info.request_id == "req-abc123"

    def test_reponse_gateway_relayee_sans_entete_n_est_pas_un_refus_du_pool(self):
        # La Gateway peut répondre `{"error": {...}}` : sans X-AIGP-Error, rien n'est conclu.
        assert PE.parse_pool_error(404, {}, {"error": {"code": "JOB_NOT_FOUND"}}) is None
        assert PE.parse_pool_error(503, {"Retry-After": "30"}, {"detail": {"code": "resource_saturated"}}) is None

    def test_entete_malforme_ignore(self):
        assert PE.parse_pool_error(503, {"X-AIGP-Error": "upstream down"}, None) is None
        assert PE.parse_pool_error(503, {"X-AIGP-Error": ""}, None) is None

    def test_code_inconnu_garde_le_statut_sans_presumer(self):
        info = PE.parse_pool_error(503, pool_headers("NEW_FUTURE_CODE"), pool_body("NEW_FUTURE_CODE"))
        assert info.known is False and info.outcome == PE.UNCERTAIN and info.transient is False
        assert info.normalized == "pool_new_future_code" and info.http_status == 503

    def test_retry_after_et_request_id_preserves(self):
        info = PE.parse_pool_error(503, pool_headers("NODE_UNAVAILABLE", **{"Retry-After": "17"}), None)
        assert info.retry_after_s == 17 and info.request_id == "req-abc123"   # repli sur l'en-tête

    def test_request_id_hors_motif_ecarte(self):
        info = PE.parse_pool_error(404, {"X-AIGP-Error": "JOB_AFFINITY_UNKNOWN"},
                                   {"error": {"request_id": "<script>"}})
        assert info.request_id is None

    def test_compensation_lue_identifiant_orphelin_ignore(self):
        body = pool_body("AFFINITY_STORE_UNAVAILABLE", compensation="failed", gateway_job_id="J", node_id="n")
        info = PE.parse_pool_error(503, pool_headers("AFFINITY_STORE_UNAVAILABLE"), body)
        assert info.compensation == "failed" and info.outcome == PE.UNCERTAIN
        assert "J" not in info.log_fields()

    @pytest.mark.parametrize("code,outcome", [
        ("MODEL_REQUIRED", PE.NOT_EXECUTED), ("MODEL_NOT_AVAILABLE", PE.NOT_EXECUTED),
        ("JOB_AFFINITY_UNKNOWN", PE.NOT_EXECUTED), ("NODE_UNAVAILABLE", PE.NOT_EXECUTED),
        ("UPSTREAM_TIMEOUT", PE.UNCERTAIN), ("UPSTREAM_UNAVAILABLE", PE.UNCERTAIN),
        ("JOB_AFFINITY_CONFLICT", PE.UNCERTAIN), ("AFFINITY_STORE_UNAVAILABLE", PE.UNCERTAIN),
    ])
    def test_issue_amont_prouvee(self, code, outcome):
        assert PE.parse_pool_error(PE.POOL_CODES[code][0], pool_headers(code), None).outcome == outcome

    def test_message_du_pool_jamais_dans_le_journal(self):
        info = PE.parse_pool_error(404, pool_headers("MODEL_NOT_AVAILABLE"), pool_body("MODEL_NOT_AVAILABLE"))
        assert "texte libre" not in info.log_fields()


# --------------------------------------------------------------------------- génération
@pytest.fixture
def gw():
    return AIGatewayClient(base_url="http://ai-gateway-pool:8080", timeout_s=5, enabled=True)


def _req(model="qwen3:30b-instruct"):
    return GatewayGenerationRequest(prompt="Bonjour", model=model)


class TestGeneration:
    @pytest.mark.parametrize("code,status,cls,eligible", [
        ("MODEL_REQUIRED", 400, GatewayContractError, False),
        ("MODEL_NOT_AVAILABLE", 404, GatewayContractError, False),
        ("CONFIGURATION_INVALID", 500, GatewayContractError, False),   # 500 mais défaut de config
        ("POOL_ROUTING_ERROR", 500, GatewayContractError, False),
        ("ROUTING_NOT_READY", 501, GatewayContractError, False),
        ("NODE_UNAVAILABLE", 503, GatewayTemporaryUnavailable, True),
        ("UPSTREAM_UNAVAILABLE", 503, GatewayTemporaryUnavailable, True),
        ("UPSTREAM_TIMEOUT", 504, GatewayTemporaryUnavailable, True),
    ])
    def test_refus_du_pool_classe_statut_et_code_conserves(self, gw, monkeypatch, code, status, cls, eligible):
        calls = []
        monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or _Resp(status, pool_body(code), pool_headers(code)))
        with pytest.raises(cls) as e:
            gw.generate(_req())
        assert type(e.value) is cls
        assert e.value.status_code == status
        assert e.value.pool_error.code == code and e.value.pool_error.request_id == "req-abc123"
        assert f"pool_code={code}" in str(e.value) and "texte libre" not in str(e.value)
        assert is_fallback_eligible(e.value) is eligible
        assert calls == [1]                                            # AUCUN rejeu du POST

    def test_erreur_gateway_relayee_inchangee(self, gw, monkeypatch):
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(503, {"detail": "busy"}, {"Retry-After": "5"}))
        with pytest.raises(GatewayTemporaryUnavailable) as e:
            gw.generate(_req())
        assert e.value.pool_error is None and e.value.status_code == 503

    def test_classification_historique_sans_pool_inchangee(self):
        for status, cls in ((400, GatewayContractError), (404, GatewayContractError), (500, GatewayTemporaryUnavailable),
                            (503, GatewayTemporaryUnavailable)):
            exc = classify_http_status(status)
            assert type(exc) is cls and exc.pool_error is None

    def test_modele_explicite_toujours_envoye(self, gw, monkeypatch):
        sent = {}
        monkeypatch.setattr(requests, "post", lambda url, json=None, **k: sent.update(json) or _Resp(
            200, {"text": "ok", "model": "qwen3:30b-instruct", "provider": "ai_gateway"}))
        gw.generate(_req())
        assert sent["model"] == "qwen3:30b-instruct"

    def test_flux_refus_du_pool_avant_le_premier_fragment(self, gw, monkeypatch):
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(504, pool_body("UPSTREAM_TIMEOUT"), pool_headers("UPSTREAM_TIMEOUT")))
        with pytest.raises(GatewayTemporaryUnavailable) as e:
            list(gw.generate_stream(_req()))
        assert e.value.pool_error.code == "UPSTREAM_TIMEOUT"

    def test_flux_interrompu_sans_done_jamais_complete(self, gw, monkeypatch):
        lines = [b'{"type":"delta","delta":"Bon"}', requests.ConnectionError("reset")]
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, lines=lines))
        out = []
        with pytest.raises(GatewayStreamError):
            for ev in gw.generate_stream(_req()):
                out.append(ev)
        assert out == [{"type": "delta", "delta": "Bon"}]             # aucun `result` inventé

    def test_flux_termine_sans_done(self, gw, monkeypatch):
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, lines=[b'{"type":"delta","delta":"x"}']))
        with pytest.raises(GatewayStreamError):
            list(gw.generate_stream(_req()))

    def test_timeout_client(self, gw, monkeypatch):
        def boom(*a, **k):
            raise requests.Timeout()
        monkeypatch.setattr(requests, "post", boom)
        with pytest.raises(GatewayTimeoutError):
            gw.generate(_req())


# --------------------------------------------------------------------------- vision / StructX
class TestVision:
    def test_v1_indisponibilite_du_pool_reste_relancable_code_client_inchange(self):
        err = classify_vision_error(503, pool_body("NODE_UNAVAILABLE"), pool_headers("NODE_UNAVAILABLE", **{"Retry-After": "12"}))
        assert err.code == "visual_gateway_http_error" and err.retryable is True
        assert err.pool_error.code == "NODE_UNAVAILABLE" and err.request_id == "req-abc123"
        assert err.retry_after_s == 12.0                               # Retry-After préservé

    def test_v1_defaut_de_configuration_du_pool_jamais_relance(self):
        err = classify_vision_error(500, pool_body("CONFIGURATION_INVALID"), pool_headers("CONFIGURATION_INVALID"))
        assert err.retryable is False and err.pool_error.category == "configuration"

    def test_v1_modele_absent_du_pool(self):
        err = classify_vision_error(404, pool_body("MODEL_NOT_AVAILABLE"), pool_headers("MODEL_NOT_AVAILABLE"))
        assert err.retryable is False and err.http_status == 404 and err.pool_error.code == "MODEL_NOT_AVAILABLE"

    def test_v1_reponse_gateway_relayee_classement_historique(self):
        body = {"detail": {"code": "vision_busy", "retryable": True, "request_id": "r1"}}
        err = classify_vision_error(503, body, {"Retry-After": "9"})
        assert err.code == "vision_busy" and err.retryable and err.pool_error is None and err.retry_after_s == 9.0

    def test_v2_refus_du_pool(self):
        err = vision_v2.classify_error(504, pool_body("UPSTREAM_TIMEOUT"), pool_headers("UPSTREAM_TIMEOUT"),
                                       request_id="vr-1")
        assert err.retryable is True and err.http_status == 504
        assert err.pool_error.code == "UPSTREAM_TIMEOUT" and err.request_id == "vr-1"

    def test_v2_model_required_non_relancable(self):
        err = vision_v2.classify_error(400, pool_body("MODEL_REQUIRED"), pool_headers("MODEL_REQUIRED"), request_id=None)
        assert err.retryable is False and err.pool_error.code == "MODEL_REQUIRED" and err.request_id == "req-abc123"


# --------------------------------------------------------------------------- embeddings / rerank
class TestEmbeddingsRerank:
    def test_embeddings_gouvernes_refus_du_pool(self, monkeypatch, settings):
        from apps.llm_router import embeddings_gateway as EG

        settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
        settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://ai-gateway-pool:8080"
        monkeypatch.setattr(EG.requests, "post", lambda *a, **k: _Resp(404, pool_body("MODEL_NOT_AVAILABLE"),
                                                                        pool_headers("MODEL_NOT_AVAILABLE")))
        with pytest.raises(EG.GatewayEmbeddingsError) as e:
            EG.embed_via_gateway(["x"], model="legacy-768", task=EG.TASK_QUERY, dim=768)
        assert e.value.code == "http_404"                              # contrat des appelants inchangé
        assert e.value.pool_error.code == "MODEL_NOT_AVAILABLE" and "pool_code=MODEL_NOT_AVAILABLE" in str(e.value)

    def test_rerank_refus_du_pool_prefixe_inchange(self, monkeypatch, settings):
        from apps.rag_core.services import rerank_gateway as RG

        settings.RAG_RERANKER_BASE_URL = "http://ai-gateway-pool:8080"

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "x", pool_headers("UPSTREAM_UNAVAILABLE"), io.BytesIO(b"{}"))
        monkeypatch.setattr(RG.urllib.request, "urlopen", boom)
        with pytest.raises(RG.RerankGatewayError) as e:
            RG._post_rerank("q", [{"id": "a", "text": "t"}])
        assert str(e.value) == "HTTP 503 pool=UPSTREAM_UNAVAILABLE"

    def test_rerank_erreur_gateway_relayee_message_historique(self, monkeypatch, settings):
        from apps.rag_core.services import rerank_gateway as RG

        settings.RAG_RERANKER_BASE_URL = "http://ai-gateway:8100"

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b"{}"))
        monkeypatch.setattr(RG.urllib.request, "urlopen", boom)
        with pytest.raises(RG.RerankGatewayError) as e:
            RG._post_rerank("q", [{"id": "a", "text": "t"}])
        assert str(e.value) == "HTTP 503"

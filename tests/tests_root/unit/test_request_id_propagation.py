"""Qualification — propagation de `X-Request-ID` RAGatm → AI Gateway (/ Pool).

Exploitabilité : l'identifiant de requête du backend n'atteignait pas la Gateway.
Ces tests verrouillent la correction :

- middleware : grammaire du contrat (``[A-Za-z0-9._:-]{1,128}``) ; un identifiant externe
  non conforme est REMPLACÉ (jamais relayé) ; l'identifiant courant est exposé par ContextVar
  pendant la requête et effacé ensuite ;
- clients Gateway (génération unaire et flux, embeddings, reranking) : l'identifiant part en
  EN-TÊTE, le corps JSON (schéma public du contrat) est inchangé ;
- aucun prompt ni document dans les journaux de corrélation.

Aucun réseau : ``requests.post`` / ``urllib.request.urlopen`` sont remplacés par des doubles.
"""
from __future__ import annotations

import json
import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from config.middleware import request_id as mw
from apps.llm_router.gateway import client as client_mod
from apps.llm_router.gateway import request_id as rid_mod
from apps.llm_router.gateway.dto import GatewayGenerationRequest

UUID_LEN = 36


# --------------------------------------------------------------------------- doubles
class _Resp:
    def __init__(self, status=200, body=None, lines=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {"text": "ok", "model": "m", "provider": "ollama"}
        self._lines = lines or []
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        pass


def _capture_post(monkeypatch, module, resp):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["headers"] = dict(kw.get("headers") or {})
        captured["json"] = kw.get("json")
        return resp

    monkeypatch.setattr(module.requests, "post", fake_post)
    return captured


@pytest.fixture
def no_current_request_id():
    tok = mw.bind_request_id("")
    yield
    mw.unbind_request_id(tok)


# --------------------------------------------------------------------------- middleware
@pytest.mark.parametrize("value", ["abc-123", "abc.123:xyz-9", "a" * 128, "0d1f6a3e-7c2b-4c1e-9a2d-1f2e3d4c5b6a"])
def test_middleware_keeps_conforming_external_id(value):
    rf = RequestFactory()
    req = rf.get("/x", HTTP_X_REQUEST_ID=value)
    m = mw.RequestIdMiddleware(lambda r: HttpResponse("ok"))
    resp = m(req)
    assert req.request_id == value
    assert resp["X-Request-ID"] == value


@pytest.mark.parametrize("value", ["bad id with spaces", "a" * 129, "été", "x;y", "a\tb", "a/b"])
def test_middleware_replaces_non_conforming_external_id(value):
    rf = RequestFactory()
    req = rf.get("/x", HTTP_X_REQUEST_ID=value)
    m = mw.RequestIdMiddleware(lambda r: HttpResponse("ok"))
    resp = m(req)
    assert req.request_id != value and len(req.request_id) == UUID_LEN
    assert resp["X-Request-ID"] == req.request_id
    assert mw.is_valid_request_id(req.request_id)


def test_middleware_generates_when_missing():
    rf = RequestFactory()
    req = rf.get("/x")
    resp = mw.RequestIdMiddleware(lambda r: HttpResponse("ok"))(req)
    assert len(req.request_id) == UUID_LEN and resp["X-Request-ID"] == req.request_id


def test_middleware_exposes_then_clears_current_request_id(no_current_request_id):
    seen = {}

    def view(r):
        seen["during"] = mw.get_current_request_id()
        return HttpResponse("ok")

    rf = RequestFactory()
    req = rf.get("/x", HTTP_X_REQUEST_ID="tr-ctx-1")
    mw.RequestIdMiddleware(view)(req)
    assert seen["during"] == "tr-ctx-1"
    assert mw.get_current_request_id() == ""


# --------------------------------------------------------------------------- résolution
def test_resolve_prefers_explicit_then_context_then_nothing(no_current_request_id):
    assert rid_mod.resolve_request_id("explicit-1") == "explicit-1"
    tok = mw.bind_request_id("ctx-1")
    try:
        assert rid_mod.resolve_request_id(None) == "ctx-1"
        assert rid_mod.resolve_request_id("explicit-2") == "explicit-2"
        # explicite NON conforme : jamais mis sur le fil, repli sur le contexte
        assert rid_mod.resolve_request_id("bad id") == "ctx-1"
    finally:
        mw.unbind_request_id(tok)
    assert rid_mod.resolve_request_id(None) == ""
    assert rid_mod.correlation_headers(None) == {}
    generated = rid_mod.resolve_request_id(None, generate=True)
    assert len(generated) == UUID_LEN and mw.is_valid_request_id(generated)


# --------------------------------------------------------------------------- client génération
def _client():
    return client_mod.AIGatewayClient(base_url="http://gw.test", timeout_s=5.0, enabled=True)


def test_generate_sends_request_id_header_not_body(monkeypatch, no_current_request_id):
    cap = _capture_post(monkeypatch, client_mod, _Resp())
    req = GatewayGenerationRequest(prompt="p", model="compact-generation-8", request_id="tr-gen-1")
    _client().generate(req)
    assert cap["headers"]["X-Request-ID"] == "tr-gen-1"
    assert "request_id" not in cap["json"]  # schéma public du corps inchangé


def test_generate_stream_sends_request_id_header_not_body(monkeypatch, no_current_request_id):
    lines = [json.dumps({"type": "delta", "delta": "a"}).encode(), json.dumps({"type": "done"}).encode()]
    cap = _capture_post(monkeypatch, client_mod, _Resp(lines=lines))
    req = GatewayGenerationRequest(prompt="p", model="compact-generation-8", request_id="tr-gen-2")
    events = list(_client().generate_stream(req))
    assert cap["headers"]["X-Request-ID"] == "tr-gen-2"
    assert cap["headers"]["Accept"] == "application/x-ndjson"
    assert "request_id" not in cap["json"]
    assert [e["type"] for e in events] == ["delta", "result"]


def test_generate_falls_back_to_current_http_request_id(monkeypatch, no_current_request_id):
    cap = _capture_post(monkeypatch, client_mod, _Resp())
    tok = mw.bind_request_id("tr-http-7")
    try:
        _client().generate(GatewayGenerationRequest(prompt="p", model="m"))
    finally:
        mw.unbind_request_id(tok)
    assert cap["headers"]["X-Request-ID"] == "tr-http-7"


def test_generate_without_any_id_sends_no_header(monkeypatch, no_current_request_id):
    cap = _capture_post(monkeypatch, client_mod, _Resp())
    _client().generate(GatewayGenerationRequest(prompt="p", model="m"))
    assert "X-Request-ID" not in cap["headers"]


def test_generate_never_relays_non_conforming_explicit_id(monkeypatch, no_current_request_id):
    cap = _capture_post(monkeypatch, client_mod, _Resp())
    _client().generate(GatewayGenerationRequest(prompt="p", model="m", request_id="bad id\n"))
    assert "X-Request-ID" not in cap["headers"]


# --------------------------------------------------------------------------- routeur → client
def test_router_passes_context_correlation_id(monkeypatch, settings, no_current_request_id):
    from apps.llm_router import router
    from apps.llm_router.egress import LlmExecutionContext

    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw.test"
    settings.AI_GATEWAY_FALLBACK_PROVIDER = "none"
    cap = _capture_post(monkeypatch, client_mod, _Resp(body={"text": "réponse", "model": "m", "provider": "ollama"}))
    ctx = LlmExecutionContext.system_local_only(operation="unit.test", correlation_id="tr-chat-42") \
        if hasattr(LlmExecutionContext, "system_local_only") else LlmExecutionContext(operation="unit.test", correlation_id="tr-chat-42")
    out = router.generate_text(prompt="question", context=ctx)
    assert out["text"] == "réponse"
    assert cap["headers"]["X-Request-ID"] == "tr-chat-42"
    assert "request_id" not in cap["json"]


def test_build_gateway_call_sets_dto_request_id(settings):
    from apps.llm_router import router

    settings.AI_GATEWAY_ENABLED = True
    settings.AI_GATEWAY_BASE_URL = "http://gw.test"
    _client_obj, req = router._build_gateway_call(prompt="p", provider=None, model=None, request_id="tr-dto-1")
    assert req.request_id == "tr-dto-1"
    assert "request_id" not in req.to_payload()


# --------------------------------------------------------------------------- embeddings
def test_embeddings_send_request_id_header_and_log_only_ids(monkeypatch, settings, caplog, no_current_request_id):
    from apps.llm_router import embeddings_gateway as eg

    settings.AI_GATEWAY_EMBEDDINGS_ENABLED = True
    settings.AI_GATEWAY_EMBEDDINGS_BASE_URL = "http://gw.test"
    monkeypatch.setattr(eg, "gateway_embeddings_enabled", lambda: True)
    monkeypatch.setattr(eg, "gateway_embeddings_base_url", lambda: "http://gw.test")
    body = {
        "model": "fr-en-retrieval", "model_logical": "fr-en-retrieval", "dimension": 3,
        "pipeline_id": "pipe", "model_digest": "sha256:" + "0" * 64, "identity_verified": True,
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
    }
    cap = _capture_post(monkeypatch, eg, _Resp(body=body))
    caplog.set_level(logging.INFO, logger=eg.logger.name)

    tok = mw.bind_request_id("tr-emb-1")
    try:
        try:
            eg.embed_via_gateway(["texte secret à ne jamais journaliser"], model="fr-en-retrieval", task="document", dim=3)
        except Exception:
            pass  # la validation de la réponse factice n'est pas l'objet du test
    finally:
        mw.unbind_request_id(tok)
    assert cap["headers"]["X-Request-ID"] == "tr-emb-1"
    assert "request_id" not in (cap["json"] or {})
    assert "texte secret" not in caplog.text
    recs = [r for r in caplog.records if r.getMessage() == "embedding_gateway_call"]
    assert recs and all(getattr(r, "request_id", "") == "tr-emb-1" for r in recs)


def test_embeddings_outside_http_request_generate_a_fresh_id(monkeypatch, settings, no_current_request_id):
    from apps.llm_router import embeddings_gateway as eg

    monkeypatch.setattr(eg, "gateway_embeddings_enabled", lambda: True)
    monkeypatch.setattr(eg, "gateway_embeddings_base_url", lambda: "http://gw.test")
    cap = _capture_post(monkeypatch, eg, _Resp(body={}))
    try:
        eg.embed_via_gateway(["t"], model="fr-en-retrieval", task="document", dim=3)
    except Exception:
        pass
    rid = cap["headers"].get("X-Request-ID", "")
    assert len(rid) == UUID_LEN and mw.is_valid_request_id(rid)


# --------------------------------------------------------------------------- reranking
def test_rerank_sends_current_request_id_header(monkeypatch, no_current_request_id):
    import urllib.request

    from apps.rag_core.services import rerank_gateway as rg

    captured = {}

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

        def read(self):
            return json.dumps({"results": []}).encode()

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Ctx()

    monkeypatch.setattr(rg, "_base_url", lambda: "http://gw.test")
    monkeypatch.setattr(rg, "_timeout", lambda: 1.0)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    tok = mw.bind_request_id("tr-rr-1")
    try:
        rg._post_rerank("q", [{"id": "1", "text": "d"}])
    finally:
        mw.unbind_request_id(tok)
    assert captured["headers"]["x-request-id"] == "tr-rr-1"

    rg._post_rerank("q", [{"id": "1", "text": "d"}])
    assert "x-request-id" not in captured["headers"]

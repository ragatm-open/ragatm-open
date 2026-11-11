"""Client Vision V2 et portes K1/K2/K3 — HTTP simulé, aucune Gateway.

La réponse de référence ``fixtures/vision_v2/describe_success_v2.json`` est une réponse
RÉELLE d'une Gateway de qualification sur une image synthétique 160×120.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import requests

from apps.llm_router.gateway import vision_contract_v2 as contract
from apps.llm_router.gateway import vision_v2 as v2

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "vision_v2"
SUCCESS = json.loads((FIX / "describe_success_v2.json").read_text())
OVERLOADED = json.loads((FIX / "overloaded_503.json").read_text())
FP = SUCCESS["pipeline"]["fingerprint"]
SRC = SUCCESS["image"]["source_sha256"]


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b"<html>"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def _payload(request_id="rgv2-fixture"):
    return {"schema_version": "2", "task": "describe", "locale": "fr", "request_id": request_id,
            "image": {"data": "iVBORw0KGgo=", "mime_type": "image/png"},
            "context": {"caption": None, "alt_text": None, "nearby_text": None}}


def _client(**kw):
    args = dict(base_url="http://gw.test:8200", timeout_s=75, enabled=True, allowed_fingerprints=[FP])
    args.update(kw)
    return v2.VisionV2Client(**args)


@pytest.fixture
def post(monkeypatch):
    def install(*responses):
        rec = Recorder(*responses)
        monkeypatch.setattr(v2.requests, "post", rec)
        return rec
    return install


def _ok(mutate=None):
    body = copy.deepcopy(SUCCESS)
    if mutate:
        mutate(body)
    return FakeResp(200, body)


# -- succès -----------------------------------------------------------------------
def test_v2_success_returns_complete_validated_result(post):
    rec = post(_ok())
    res = _client().analyze(_payload(), expected_source_sha256=SRC)
    assert len(rec.calls) == 1 and rec.calls[0]["json"]["schema_version"] == "2"
    assert rec.calls[0]["timeout"] == (10.0, 75.0)
    assert (res.schema_version, res.task, res.visual_kind) == ("2", "describe", "photo")
    assert res.source_sha256 == SRC and res.fingerprint == FP
    assert res.normalized_sha256 == SUCCESS["image"]["normalized_sha256"]
    assert res.image["normalizer_id"] == "vision-image-normalizer/1"
    assert set(res.model) == {"logical", "concrete", "digest"} and len(res.model["digest"]) == 64
    assert res.usage["attempts"] == 1 and res.request_id == "rgv2-fixture"
    assert res.raw == SUCCESS and json.loads(json.dumps(res.raw)) == SUCCESS   # sérialisable


# -- K2 ---------------------------------------------------------------------------
def test_v1_response_is_rejected_without_fallback(post):
    def to_v1(b):
        b["schema_version"] = "1"
        for k in ("source_sha256", "normalized_sha256", "normalized_format", "normalizer_id"):
            del b["image"][k]
        for k in ("fingerprint", "system_prompt_sha256", "task_prompt_sha256", "model_schema_sha256",
                  "model_schema_hash_algorithm", "inference", "retry"):
            del b["pipeline"][k]
        del b["usage"]["attempts"]
    rec = post(_ok(to_v1))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR and not e.value.retryable and len(rec.calls) == 1


@pytest.mark.parametrize("path", [
    "image.source_sha256", "image.normalized_sha256", "image.normalized_format", "image.normalizer_id",
    "pipeline.fingerprint", "pipeline.pipeline_id", "pipeline.system_prompt_sha256",
    "pipeline.task_prompt_sha256", "pipeline.model_schema_sha256", "pipeline.model_schema_hash_algorithm",
    "pipeline.inference", "pipeline.retry", "pipeline.inference.seed", "pipeline.retry.max_attempts",
    "model.digest", "usage.attempts", "latency_ms", "truncated",
])
def test_missing_mandatory_field_fails_closed(post, path):
    def drop(b):
        target = b
        for part in path.split(".")[:-1]:
            target = target[part]
        del target[path.split(".")[-1]]
    post(_ok(drop))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR and not e.value.retryable


@pytest.mark.parametrize("path", ["", "image", "pipeline", "pipeline.inference", "usage", "model"])
def test_unexpected_field_is_drift_never_ignored(post, path):
    def add(b):
        target = b
        for part in [p for p in path.split(".") if p]:
            target = target[part]
        target["extra"] = 1
    post(_ok(add))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR


@pytest.mark.parametrize("path", ["image.source_sha256", "image.normalized_sha256", "model.digest",
                                  "pipeline.system_prompt_sha256", "pipeline.task_prompt_sha256",
                                  "pipeline.model_schema_sha256"])
@pytest.mark.parametrize("bad", ["ab" * 31, "AB" * 32, "zz" * 32, "ab" * 33, "", None, 12])
def test_malformed_hash_fails_closed(post, path, bad):
    def corrupt(b):
        a, c = path.split(".")
        b[a][c] = bad
    post(_ok(corrupt))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR


@pytest.mark.parametrize("fp", ["vision-v2:" + "a" * 64, "vision-v1:" + "A" * 64, "vision-v1:abc", "a" * 64])
def test_malformed_fingerprint_is_contract_error(post, fp):
    post(_ok(lambda b: b["pipeline"].__setitem__("fingerprint", fp)))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR


def test_task_and_request_id_must_echo_the_request(post):
    post(_ok(lambda b: b.__setitem__("request_id", "autre")))
    with pytest.raises(v2.VisionV2Error):
        _client().analyze(_payload(), expected_source_sha256=SRC)


# -- K1 / K3 --------------------------------------------------------------------------
def test_source_hash_mismatch_is_provenance_failure(post):
    rec = post(_ok())
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256="0" * 64)
    assert e.value.code == v2.PROVENANCE_INTEGRITY_FAILURE and not e.value.retryable and len(rec.calls) == 1


def test_unknown_fingerprint_is_not_allowed_even_if_well_formed(post):
    post(_ok(lambda b: b["pipeline"].__setitem__("fingerprint", "vision-v1:" + "b" * 64)))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.PIPELINE_NOT_ALLOWED and not e.value.retryable


def test_several_fingerprints_may_be_allowed(post):
    post(_ok())
    res = _client(allowed_fingerprints=["vision-v1:" + "c" * 64, FP]).analyze(
        _payload(), expected_source_sha256=SRC)
    assert res.fingerprint == FP


def test_empty_allowlist_or_disabled_gateway_never_calls(post):
    rec = post(_ok())
    for client, code in ((_client(allowed_fingerprints=[]), v2.VISION_V2_NOT_CONFIGURED),
                         (_client(base_url=""), v2.VISION_V2_NOT_CONFIGURED),
                         (_client(enabled=False), v2.VISION_GATEWAY_DISABLED)):
        with pytest.raises(v2.VisionV2Error) as e:
            client.analyze(_payload(), expected_source_sha256=SRC)
        assert e.value.code == code
    assert rec.calls == []


def test_v1_request_is_refused_locally(post):
    rec = post(_ok())
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(dict(_payload(), schema_version="1"), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_REQUEST_INVALID and rec.calls == []


# -- erreurs HTTP -----------------------------------------------------------------
def _err(status, code, retryable, extra=None, headers=None):
    detail = {"status": "error", "provider": "ai_gateway", "message": "m", "code": code,
              "retryable": retryable, "request_id": "rgv2-fixture"}
    detail.update(extra or {})
    return FakeResp(status, {"detail": detail}, headers)


@pytest.mark.parametrize("status,gw,retryable,expected", [
    (503, "vision_overloaded", True, v2.VISION_GATEWAY_OVERLOADED),
    (504, "vision_timeout", True, v2.VISION_GATEWAY_TIMEOUT),
    (502, "vision_model_invalid_output", False, v2.VISION_UPSTREAM_ERROR),
    (502, "vision_upstream_unavailable", True, v2.VISION_UPSTREAM_ERROR),
    (413, "image_too_large", False, v2.VISION_REQUEST_INVALID),
    (400, "vision_task_not_qualified", False, v2.VISION_REQUEST_INVALID),
    (422, "validation_error", True, v2.VISION_REQUEST_INVALID),        # 4xx jamais relançable
])
def test_error_classification_reads_the_envelope(post, status, gw, retryable, expected):
    post(_err(status, gw, retryable))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == expected and e.value.gateway_code == gw and e.value.http_status == status
    assert e.value.retryable is (retryable and status >= 500)


def test_real_overloaded_envelope_and_retry_after(post):
    post(FakeResp(503, copy.deepcopy(OVERLOADED), {"Retry-After": "10"}))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert (e.value.code, e.value.retryable, e.value.retry_after_s) == (v2.VISION_GATEWAY_OVERLOADED, True, 10.0)


@pytest.mark.parametrize("header,body_value,expected", [
    (None, 7, 7.0), ("12", None, 12.0), ("soon", None, None),
    ("Wed, 21 Oct 2026 07:28:00 GMT", None, None), ("-1", None, None), ("99999", None, None),
    (None, True, None),
])
def test_retry_after_is_parsed_defensively(post, header, body_value, expected):
    extra = {"retry_after_seconds": body_value} if body_value is not None else {}
    post(_err(503, "vision_overloaded", True, extra, {"Retry-After": header} if header else {}))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.retry_after_s == expected


@pytest.mark.parametrize("body", [
    {"detail": {"status": "error", "provider": "ai_gateway", "code": "vision_timeout", "retryable": True,
                "request_id": None}},                                            # message absent
    {"detail": {"status": "error", "provider": "other", "message": "m", "code": "vision_timeout",
                "retryable": True, "request_id": None}},
    {"detail": {"status": "error", "provider": "ai_gateway", "message": "m", "code": "Bad Code",
                "retryable": True, "request_id": None}},
    {"detail": {"status": "error", "provider": "ai_gateway", "message": "m", "code": "vision_timeout",
                "retryable": "yes", "request_id": None}},
    {"detail": {"status": "error", "provider": "ai_gateway", "message": "m", "code": "vision_timeout",
                "retryable": True, "request_id": None, "unexpected": 1}},
])
def test_malformed_error_envelope_is_contract_error(post, body):
    post(FakeResp(504, body))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_V2_CONTRACT_ERROR and not e.value.retryable


def test_proxy_error_without_envelope_is_classified_by_status(post):
    post(FakeResp(502, None))
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert e.value.code == v2.VISION_UPSTREAM_ERROR and e.value.retryable


@pytest.mark.parametrize("exc,code,retryable", [
    (requests.ReadTimeout(), v2.VISION_CLIENT_TIMEOUT, True),
    (requests.ConnectionError(), v2.VISION_GATEWAY_UNAVAILABLE, True),
    (requests.TooManyRedirects(), v2.VISION_TRANSPORT_ERROR, False),
])
def test_transport_errors_never_leak_raw_exceptions(post, exc, code, retryable):
    post(exc)
    with pytest.raises(v2.VisionV2Error) as e:
        _client().analyze(_payload(), expected_source_sha256=SRC)
    assert (e.value.code, e.value.retryable) == (code, retryable)


# -- timeout ----------------------------------------------------------------------
def test_client_timeout_leaves_room_for_the_gateway_structured_504():
    from django.conf import settings

    assert v2.GATEWAY_VISION_BUDGET_S == 60
    assert float(settings.AI_GATEWAY_VISION_TIMEOUT_SECONDS) >= v2.GATEWAY_VISION_BUDGET_S + v2.CLIENT_TIMEOUT_MARGIN_S
    for too_short in (30, 60, 69):
        with pytest.raises(v2.VisionV2Error) as e:
            _client(timeout_s=too_short).ensure_ready()
        assert e.value.code == v2.VISION_V2_NOT_CONFIGURED
    _client(timeout_s=75).ensure_ready()          # valeur staging

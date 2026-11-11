"""Sondes lecture seule du rail V2 (CV-3E1) : capacité consultative, préflight
``/health/vision``, gigue des reports. HTTP simulé : aucune Gateway."""
from __future__ import annotations

import copy

import pytest
import requests

from apps.llm_router.gateway import vision_probe as P
from apps.rag_core.services.raster import orchestration as O

CAPACITY = {"provider": "ai_gateway", "contract_version": "1", "scope": "instance", "environment": "staging",
            "advisory": True,
            "vision": {"enabled": True, "status": "available", "max_concurrency": 1, "in_flight": 0,
                       "available_slots": 1, "retry_after_seconds": 10},
            "model": {"logical": "vision-large", "resident": False, "status": "ok"}}
FP = "vision-v1:9c68b7db03303789ffb1b34d5ff0da811a8c042cfc912eab89db0fd702dced39"
HEALTH = {"status": "ok", "provider": "ai_gateway", "enabled": True, "environment": "staging",
          "default_model": "vision-large",
          "models": [{"model_logical": "vision-large", "installed": True, "resident": True,
                      "tasks": ["describe"], "schema_versions": ["1", "2"],
                      "pipelines": {"describe": {"fingerprint": FP}}}]}


def _cap(**vision):
    body = copy.deepcopy(CAPACITY)
    body["vision"].update(vision)
    return body


class _R:
    def __init__(self, status, body):
        import json

        self.status_code, self._b = status, body
        self.content = json.dumps(body).encode() if body is not None else b"x"

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


# -- capacité --------------------------------------------------------------------------
def test_available_and_resident_false_is_not_saturation():
    probe = P.parse_capacity(CAPACITY)
    assert probe.decision == P.CAPACITY_AVAILABLE and probe.retry_after_s == 10.0


@pytest.mark.parametrize("vision", [{"status": "saturated", "available_slots": 0, "in_flight": 1},
                                    {"status": "available", "available_slots": 0}])
def test_saturated_or_zero_slots(vision):
    probe = P.parse_capacity(_cap(**vision))
    assert probe.decision == P.CAPACITY_SATURATED and probe.retry_after_s == 10.0


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(contract_version="2"),
    lambda b: b.update(scope="cluster"),
    lambda b: b.update(advisory=False),
    lambda b: b.update(provider="other"),
    lambda b: b.pop("vision"),
    lambda b: b["vision"].update(status="degraded"),
    lambda b: b["vision"].update(available_slots=True),
    lambda b: b["vision"].update(available_slots=-1),
    lambda b: b["vision"].update(retry_after_seconds="10"),
    lambda b: b["vision"].update(max_concurrency=0),
    lambda b: b["vision"].update(enabled=False),
    lambda b: b.update(model={"resident": "no"}),
])
def test_invalid_or_unusable_capacity_is_unknown(mutate):
    body = copy.deepcopy(CAPACITY)
    mutate(body)
    assert P.parse_capacity(body).decision == P.CAPACITY_UNKNOWN


@pytest.mark.parametrize("behaviour,reason", [
    (requests.Timeout(), "timeout"), (requests.ConnectionError(), "connection"),
    (_R(500, {"detail": "x"}), "http_500"), (_R(200, None), "invalid_json"),
])
def test_probe_failure_falls_back_to_unknown(monkeypatch, behaviour, reason):
    calls = []

    def get(url, **kw):
        calls.append((url, kw["timeout"]))
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    monkeypatch.setattr(P.requests, "get", get)
    probe = P.probe_capacity("http://gw.test:8200/")
    assert probe.decision == P.CAPACITY_UNKNOWN and probe.reason == reason
    assert calls == [("http://gw.test:8200/v1/vision/capacity", (2.0, 5.0))]       # UNE sonde


def test_probe_ok(monkeypatch):
    monkeypatch.setattr(P.requests, "get", lambda url, **kw: _R(200, _cap(status="saturated", available_slots=0)))
    assert P.probe_capacity("http://gw").decision == P.CAPACITY_SATURATED


# -- gigue -------------------------------------------------------------------------------
@pytest.mark.parametrize("u", [0.0, 0.25, 0.5, 0.999999])
def test_jitter_bounds(u):
    d = O.defer_delay(10, rng=lambda: u)
    assert 8.0 <= d <= 13.0 and d == pytest.approx(10 * (0.8 + 0.5 * u))


def test_jitter_cap_floor_and_default():
    assert O.defer_delay(1000, rng=lambda: 0.999) == 120.0
    assert O.defer_delay(0, rng=lambda: 0.0) == 1.0
    assert O.defer_delay(None, rng=lambda: 0.0) == 8.0          # défaut 10 s quand absent


# -- préflight /health/vision -----------------------------------------------------------
def _pre(body, allowed=(FP,), model=None):
    return P.parse_health_vision(body, task="describe", logical_model=model, allowed_fingerprints=allowed)


def test_preflight_ok_returns_served_allowlisted_fingerprint():
    assert _pre(HEALTH) == P.V2Preflight(True, fingerprint=FP)


@pytest.mark.parametrize("mutate,reason", [
    (lambda b: b["models"][0].update(schema_versions=["1"]), "schema_v2_absent"),
    (lambda b: b["models"][0].update(tasks=["ocr"]), "task_not_served"),
    (lambda b: b["models"][0]["pipelines"]["describe"].update(fingerprint="vision-v1:" + "0" * 64),
     "fingerprint_not_allowed"),
    (lambda b: b.update(enabled=False), "vision_disabled"),
    (lambda b: b.update(default_model="other"), "model_not_served"),
])
def test_preflight_refusals(mutate, reason):
    body = copy.deepcopy(HEALTH)
    mutate(body)
    assert _pre(body) == P.V2Preflight(False, reason=reason)


def test_empty_allowlist_is_fail_closed_without_network(monkeypatch):
    monkeypatch.setattr(P.requests, "get", lambda *a, **k: pytest.fail("réseau sollicité"))
    assert P.preflight_v2("http://gw", task="describe", logical_model=None,
                          allowed_fingerprints=[]).reason == "fingerprint_allowlist_empty"


def test_preflight_health_unreachable(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(P.requests, "get", boom)
    assert P.preflight_v2("http://gw", task="describe", logical_model=None,
                          allowed_fingerprints=[FP]) == P.V2Preflight(False, reason="health_connection")


# -- classes de tentative d'analyze_crop (crochets d'orchestration) ------------------------
def _e(code, status, gateway_code=None, retry_after=None):
    from apps.llm_router.gateway import vision_v2 as v2

    return v2.VisionV2Error(code, retryable=True, http_status=status, gateway_code=gateway_code,
                            retry_after_s=retry_after)


@pytest.mark.parametrize("exc,kind,consumed", [
    (None, "success", True),
    (_e("vision_gateway_overloaded", 503, "vision_overloaded", 5.0), "overloaded_503", False),
    (_e("vision_upstream_error", 503), "upstream_error", True),          # 503 sans enveloppe : ambigu
    (_e("vision_gateway_timeout", 504, "vision_timeout"), "model_timeout_504", True),
    (_e("vision_request_invalid", 422, "invalid_image"), "rejected_4xx", True),
    (_e("visual_client_timeout", None), "transport_ambiguous", True),
    (_e("visual_gateway_unavailable", None), "transport_ambiguous", True),
    (_e("provenance_integrity_failure", 200), "provenance_failure", True),
    (_e("pipeline_not_allowed", 200), "provenance_failure", True),
    (_e("vision_v2_contract_error", 200), "provenance_failure", True),
    (_e("vision_upstream_error", 502), "upstream_error", True),
])
def test_attempt_classification(exc, kind, consumed):
    from apps.rag_core.services.raster import vision as V

    out = V.classify_attempt(2, exc)
    assert (out.attempt, out.kind, out.model_consumed) == (2, kind, consumed)

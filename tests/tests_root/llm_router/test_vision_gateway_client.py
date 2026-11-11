"""contrat propriétaire du client vision de l'AI Gateway.

Le client consomme le contrat serveur GELÉ `POST /v1/vision/analyze` (schéma V1).
Il ne décide rien pour un document : il envoie une image, valide une réponse fermée,
et classe les échecs par `code` et `retryable` — jamais par le texte d'un message.

Aucun réseau : `requests.post` est remplacé dans le module du client. L'import du
module est fait DANS le corps des tests, pour que son absence produise un échec de
test (couvert par xfail) et non une erreur de collecte.
"""
from __future__ import annotations

import copy
import json
import logging
import pathlib

import pytest
import requests

from tests_root.unit.visual import _vision_mocks as M

BASE_URL = "http://gateway.test:8100"


# --------------------------------------------------------------------------- #
# Aides
# --------------------------------------------------------------------------- #
def _vision():
    from apps.llm_router.gateway import vision

    return vision


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode("utf-8")

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return copy.deepcopy(self._body)


class _Recorder:
    def __init__(self, response=None, raises=None):
        self.calls = []
        self.response = response
        self.raises = raises

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.response


def _client(**kw):
    v = _vision()
    params = {"base_url": BASE_URL, "timeout_s": 45.0, "enabled": True}
    params.update(kw)
    return v.VisionGatewayClient(**params)


def _request(**payload_overrides):
    v = _vision()
    payload = M.request_payload(**payload_overrides)
    return v.VisionAnalyzeRequest(payload=payload)


def _install(monkeypatch, recorder):
    monkeypatch.setattr("apps.llm_router.gateway.vision.requests.post", recorder)
    return recorder


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
class TestTransport:
    def test_exact_endpoint_and_json_body(self, monkeypatch):
        rec = _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        _client().analyze(_request())
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["url"] == f"{BASE_URL}/v1/vision/analyze"
        assert call["headers"]["Content-Type"] == "application/json"
        assert call["json"] == M.request_payload()

    def test_timeout_is_explicit(self, monkeypatch):
        rec = _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        _client(timeout_s=45.0).analyze(_request())
        timeout = rec.calls[0]["timeout"]
        assert isinstance(timeout, tuple) and timeout[1] == 45.0 and timeout[0] <= 45.0

    def test_returns_validated_result(self, monkeypatch):
        _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        result = _client().analyze(_request())
        assert result.data["visual_kind"] == "photo"
        assert result.http_status == 200

    def test_disabled_gateway_makes_no_network_call(self, monkeypatch):
        v = _vision()
        rec = _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client(enabled=False).analyze(_request())
        assert rec.calls == []
        assert exc.value.code == "visual_gateway_disabled"
        assert exc.value.retryable is False

    def test_empty_base_url_makes_no_network_call(self, monkeypatch):
        v = _vision()
        rec = _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client(base_url="").analyze(_request())
        assert rec.calls == []
        assert exc.value.code == "visual_gateway_misconfigured"
        assert exc.value.retryable is False

    def test_from_settings_reads_gateway_settings(self, settings):
        v = _vision()
        settings.AI_GATEWAY_ENABLED = True
        settings.AI_GATEWAY_BASE_URL = BASE_URL + "/"
        settings.AI_GATEWAY_VISION_TIMEOUT_SECONDS = 77
        client = v.VisionGatewayClient.from_settings()
        assert client.base_url == BASE_URL
        assert client.enabled is True
        assert client.timeout_s == 77.0


# --------------------------------------------------------------------------- #
# Aucune relance cachée
# --------------------------------------------------------------------------- #
class TestNoHiddenRetry:
    @pytest.mark.parametrize("status,code", [(503, "vision_overloaded"), (504, "vision_timeout"),
                                             (502, "vision_upstream_error")])
    def test_retryable_error_is_raised_after_exactly_one_call(self, monkeypatch, status, code):
        v = _vision()
        rec = _install(monkeypatch, _Recorder(_Resp(status, M.error(code, retryable=True))))
        with pytest.raises(v.VisionGatewayError):
            _client().analyze(_request())
        assert len(rec.calls) == 1

    def test_network_error_is_raised_after_exactly_one_call(self, monkeypatch):
        v = _vision()
        rec = _install(monkeypatch, _Recorder(raises=requests.ConnectionError("refused")))
        with pytest.raises(v.VisionGatewayError):
            _client().analyze(_request())
        assert len(rec.calls) == 1


# --------------------------------------------------------------------------- #
# Classification des erreurs Gateway
# --------------------------------------------------------------------------- #
FROZEN_ERRORS = [
    (400, "vision_schema_version_unsupported", False),
    (400, "unsupported_image_type", False),
    (400, "image_decode_failed", False),
    (413, "image_too_large", False),
    (400, "vision_model_not_allowed", False),
    (400, "vision_task_not_qualified", False),
    (503, "vision_not_enabled", False),
    (503, "vision_model_unavailable", True),
    (503, "vision_model_unavailable", False),
    (503, "vision_overloaded", True),
    (504, "vision_timeout", True),
    (502, "vision_upstream_error", True),
    (502, "vision_invalid_output", False),
    (502, "vision_schema_validation_failed", False),
    (502, "vision_truncated_output", False),
]


class TestGatewayErrors:
    @pytest.mark.parametrize("status,code,retryable", FROZEN_ERRORS)
    def test_code_preserved_and_retryable_consumed(self, monkeypatch, status, code, retryable):
        v = _vision()
        _install(monkeypatch, _Recorder(_Resp(status, M.error(code, retryable=retryable))))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client().analyze(_request())
        assert exc.value.code == code
        assert exc.value.retryable is retryable
        assert exc.value.http_status == status
        assert exc.value.request_id == M.REQUEST_ID

    def test_4xx_is_never_retryable_even_if_body_claims_it(self, monkeypatch):
        """§111 : piège — message contenant « timeout » ET retryable=true sur un 400."""
        v = _vision()
        body = M.error("vision_task_not_qualified", retryable=True,
                       message="upstream timeout, temporarily unavailable, retry later (503)")
        _install(monkeypatch, _Recorder(_Resp(400, body)))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client().analyze(_request())
        assert exc.value.retryable is False
        assert exc.value.code == "vision_task_not_qualified"

    def test_generic_validation_400_without_code(self, monkeypatch):
        v = _vision()
        body = {"detail": [{"loc": ["body", "image", "data"], "msg": "timeout field required",
                            "type": "missing"}]}
        _install(monkeypatch, _Recorder(_Resp(400, body)))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client().analyze(_request())
        assert exc.value.code == "vision_request_invalid"
        assert exc.value.retryable is False

    def test_5xx_without_gateway_envelope(self, monkeypatch):
        v = _vision()
        _install(monkeypatch, _Recorder(_Resp(502, "<html>bad gateway</html>")))
        with pytest.raises(v.VisionGatewayError) as exc:
            _client().analyze(_request())
        assert exc.value.code == "visual_gateway_http_error"
        assert exc.value.retryable is True
        assert exc.value.http_status == 502

    def test_message_never_controls_classification(self):
        """Deux corps identiques au message près doivent être classés identiquement."""
        v = _vision()
        a = v.classify_vision_error(503, M.error("vision_overloaded", retryable=True, message="x"))
        b = v.classify_vision_error(503, M.error("vision_overloaded", retryable=True,
                                                 message="permanent failure, do not retry"))
        assert (a.code, a.retryable) == (b.code, b.retryable) == ("vision_overloaded", True)

    def test_errors_are_one_typed_family(self):
        v = _vision()
        from apps.llm_router.gateway.exceptions import GatewayError

        assert issubclass(v.VisionGatewayError, GatewayError)
        assert issubclass(v.VisionContractDrift, v.VisionGatewayError)
        assert v.VisionGatewayError.fallback_eligible is False


# --------------------------------------------------------------------------- #
# Erreurs réseau
# --------------------------------------------------------------------------- #
class TestNetworkErrors:
    @pytest.mark.parametrize("exc,code", [
        (requests.ConnectionError("dns failure"), "visual_gateway_unavailable"),
        (requests.ConnectTimeout("connect timeout"), "visual_client_timeout"),
        (requests.ReadTimeout("read timeout"), "visual_client_timeout"),
    ])
    def test_typed_and_retryable(self, monkeypatch, exc, code):
        v = _vision()
        _install(monkeypatch, _Recorder(raises=exc))
        with pytest.raises(v.VisionGatewayError) as err:
            _client().analyze(_request())
        assert err.value.code == code
        assert err.value.retryable is True
        assert err.value.http_status is None


# --------------------------------------------------------------------------- #
# Réponse : contrat V1 fermé
# --------------------------------------------------------------------------- #
class TestResponseContract:
    @pytest.mark.parametrize("kind", ["photo", "table", "diagram", "chart", "technical_drawing",
                                      "mixed", "unknown", "decorative"])
    def test_every_legal_kind_is_accepted(self, monkeypatch, kind):
        _install(monkeypatch, _Recorder(_Resp(200, M.success(kind))))
        assert _client().analyze(_request()).data["visual_kind"] == kind

    @pytest.mark.parametrize("name", sorted(M.DRIFT_CASES))
    def test_contract_drift_is_rejected(self, monkeypatch, name):
        v = _vision()
        _install(monkeypatch, _Recorder(_Resp(200, M.DRIFT_CASES[name]())))
        with pytest.raises(v.VisionContractDrift) as exc:
            _client().analyze(_request())
        assert exc.value.code == "visual_contract_drift"
        assert exc.value.retryable is False

    def test_task_mismatch_is_drift(self, monkeypatch):
        """Réponse servie sous une AUTRE tâche que celle demandée : la charge utile vient
        d'un autre profil, la normaliser serait une erreur."""
        v = _vision()
        body = M.success("photo")
        body["task"] = "describe"
        _install(monkeypatch, _Recorder(_Resp(200, body)))
        with pytest.raises(v.VisionContractDrift):
            _client().analyze(_request())

    def test_request_id_mismatch_is_drift(self, monkeypatch):
        v = _vision()
        body = M.success("photo")
        body["request_id"] = "rgv1-somebody-else"
        _install(monkeypatch, _Recorder(_Resp(200, body)))
        with pytest.raises(v.VisionContractDrift):
            _client().analyze(_request())

    def test_non_json_success_is_drift(self, monkeypatch):
        v = _vision()
        _install(monkeypatch, _Recorder(_Resp(200, "{not json")))
        with pytest.raises(v.VisionContractDrift):
            _client().analyze(_request())


# --------------------------------------------------------------------------- #
# Confidentialité des journaux
# --------------------------------------------------------------------------- #
class TestLogPrivacy:
    def test_no_image_context_or_payload_in_logs(self, monkeypatch, caplog):
        _install(monkeypatch, _Recorder(_Resp(200, M.success("photo"))))
        caplog.set_level(logging.DEBUG)
        _client().analyze(_request())
        blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert M.IMAGE_B64 not in blob
        assert M.CONTEXT_SENTINEL not in blob
        assert M.PAYLOAD_SENTINEL not in blob

    def test_error_path_does_not_log_body(self, monkeypatch, caplog):
        v = _vision()
        body = M.error("vision_invalid_output", retryable=False, message=M.PAYLOAD_SENTINEL)
        _install(monkeypatch, _Recorder(_Resp(502, body)))
        caplog.set_level(logging.DEBUG)
        with pytest.raises(v.VisionGatewayError) as exc:
            _client().analyze(_request())
        blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert M.PAYLOAD_SENTINEL not in blob
        assert M.PAYLOAD_SENTINEL not in str(exc.value)


# --------------------------------------------------------------------------- #
# Aucune voie Ollama directe
# --------------------------------------------------------------------------- #
def test_vision_client_source_has_no_direct_ollama_path():
    src = pathlib.Path(_vision().__file__).read_text(encoding="utf-8")
    for forbidden in ("11434", "/api/generate", "/api/chat", "OLLAMA_BASE_URL",
                      "llm_router.router", "llm_router.embeddings", "import ollama"):
        assert forbidden not in src, forbidden

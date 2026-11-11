"""compatibilité avec le contrat ``ai-gateway-visual-contract/22A.1``.

Les réponses testées sont les EXEMPLES RÉELS du contrat (instance Gateway de qualification, images synthétiques QP-22A),
copiés tels quels : ``fixtures/gateway_22A1_response_examples.json`` (empreinte = celle du manifeste du contrat,
vérifiée ci-dessous). Aucun objet de réponse n'est recopié à la main ; seuls des cas DÉRIVÉS (champ retiré ou
altéré) sont construits à partir de ces exemples.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.llm_router.gateway import structured_extract as SX
from apps.llm_router.gateway.exceptions import VisionContractDrift, VisionGatewayError
from apps.llm_router.gateway.vision import classify_vision_error
from apps.rag_core.services.ingest.visual import structured as S
from apps.rag_core.services.query import numeric_evidence as NE

FIXTURE = Path(__file__).parent / "fixtures" / "gateway_22A1_response_examples.json"
#: Empreinte de la fixture publiée (exemples du contrat 22A.1, identifiants d'instance neutralisés) :
#: toute modification du fichier doit être volontaire et re-figée ici.
CONTRACT_SHA256 = "5f408d5d924e72bf81fc975ecc8a774e3b2e3fade68161283145f6d1c5a3aea1"
EX = json.loads(FIXTURE.read_bytes())

V2_TABLE = EX["structured_extract_v2_table"]
V2_CHART_ANOMALY = EX["structured_extract_v2_chart_with_detected_anomaly"]
V2_CHART_REPEAT = EX["structured_extract_v2_chart_repeated_value_anomaly"]
V1_TABLE = EX["structured_extract_v1_table_unchanged_contract"]


def test_fixture_is_the_contract_file():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == CONTRACT_SHA256


# --------------------------------------------------------------------------- #
# Sélection de version et validation des réponses
# --------------------------------------------------------------------------- #
class TestSchemaSelection:
    @pytest.mark.parametrize("version", ["1", "2"])
    def test_request_carries_the_configured_version(self, version):
        body = SX.build_request(task="table", content=b"\x89PNG", mime_type="image/png", schema_version=version)
        assert body["schema_version"] == version

    @pytest.mark.parametrize("version", ["3", "", "v2", None])
    def test_unknown_version_never_reaches_the_network(self, version):
        with pytest.raises(VisionGatewayError) as e:
            SX.build_request(task="table", content=b"x", mime_type="image/png", schema_version=version)
        assert e.value.code == "gateway_misconfigured" or e.value.retryable is False

    def test_default_is_v1(self, settings):
        assert settings.STRUCTURED_VISUAL_SCHEMA_VERSION == "1"
        assert SX.StructuredExtractClient.from_settings().schema_version == "1"

    def test_configured_v2(self, settings):
        settings.STRUCTURED_VISUAL_SCHEMA_VERSION = "2"
        assert SX.StructuredExtractClient.from_settings().schema_version == "2"


class TestValidation:
    @pytest.mark.parametrize("example,task", [(V2_TABLE, "table"), (V2_CHART_ANOMALY, "chart"),
                                              (V2_CHART_REPEAT, "chart")])
    def test_v2_examples_are_valid_under_v2(self, example, task):
        SX.validate_response(example, task=task, request_id=example["request_id"], schema_version="2")

    def test_v1_example_is_valid_under_v1(self):
        SX.validate_response(V1_TABLE, task="table", request_id=V1_TABLE["request_id"], schema_version="1")

    @pytest.mark.parametrize("example,expected", [(V1_TABLE, "2"), (V2_TABLE, "1")])
    def test_version_mismatch_is_drift(self, example, expected):
        with pytest.raises(VisionContractDrift):
            SX.validate_response(example, task="table", request_id=None, schema_version=expected)

    @pytest.mark.parametrize("mutate", [
        pytest.param(lambda d: d.pop("evidence"), id="evidence-missing"),
        pytest.param(lambda d: d.__setitem__("evidence", "certified"), id="evidence-not-object"),
        pytest.param(lambda d: d["evidence"].__setitem__("certified", "false"), id="certified-not-bool"),
        pytest.param(lambda d: d.pop("qualification"), id="qualification-missing"),
        pytest.param(lambda d: d.pop("items"), id="items-missing"),
    ])
    def test_incomplete_v2_response_is_drift(self, mutate):
        data = copy.deepcopy(V2_TABLE)
        mutate(data)
        with pytest.raises(VisionContractDrift):
            SX.validate_response(data, task="table", request_id=None, schema_version="2")

    def test_unknown_fields_are_ignored(self):
        data = copy.deepcopy(V2_TABLE)
        data["future_additive_field"] = {"x": 1}
        SX.validate_response(data, task="table", request_id=None, schema_version="2")


class _Resp:
    def __init__(self, status, body=None, raw=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content = raw if raw is not None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError("invalid JSON")
        return self._body


class TestClient:
    def _client(self, version="2"):
        return SX.StructuredExtractClient("http://gateway.test", 180, True, schema_version=version)

    def test_v2_round_trip(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(SX.requests, "post",
                            lambda url, **kw: sent.update(kw["json"]) or _Resp(200, V2_TABLE))
        res = self._client().extract(task="table", content=b"\x89PNG", mime_type="image/png",
                                     request_id=V2_TABLE["request_id"])
        assert sent["schema_version"] == "2" and res.data["evidence"]["certified"] is False

    def test_invalid_json_is_drift(self, monkeypatch):
        monkeypatch.setattr(SX.requests, "post", lambda url, **kw: _Resp(200, None, raw=b"{not json"))
        with pytest.raises(VisionContractDrift):
            self._client().extract(task="table", content=b"\x89PNG", mime_type="image/png")

    def test_production_400_on_v2_is_not_retryable(self, monkeypatch):
        monkeypatch.setattr(SX.requests, "post", lambda url, **kw: _Resp(400, {"detail": [{"msg": "bad"}]}))
        with pytest.raises(VisionGatewayError) as e:
            self._client().extract(task="table", content=b"\x89PNG", mime_type="image/png")
        assert e.value.retryable is False and e.value.http_status == 400

    def test_timeout_is_retryable_client_side(self, monkeypatch):
        def boom(url, **kw):
            raise SX.requests.Timeout()
        monkeypatch.setattr(SX.requests, "post", boom)
        with pytest.raises(VisionGatewayError) as e:
            self._client().extract(task="table", content=b"\x89PNG", mime_type="image/png")
        assert e.value.retryable is True


# --------------------------------------------------------------------------- #
# Erreurs : `retryable` et `Retry-After` du contrat
# --------------------------------------------------------------------------- #
class TestErrors:
    def test_contract_error_examples(self):
        gpu = classify_vision_error(503, EX["error_503_structx_gpu_busy"])
        host = classify_vision_error(503, EX["error_503_describe_host_capacity_deferred_staging"])
        assert (gpu.code, gpu.retryable, gpu.retry_after_s) == ("structx_gpu_busy", True, 10)
        assert (host.code, host.retryable, host.retry_after_s) == ("host_capacity_deferred", True, 3)

    @pytest.mark.parametrize("status,code", [(502, "vision_truncated_output"), (502, "vision_invalid_output"),
                                             (503, "structx_model_identity_mismatch"),
                                             (503, "structured_extract_not_enabled")])
    def test_declared_non_retryable_stays_non_retryable(self, status, code):
        err = classify_vision_error(status, {"detail": {"code": code, "retryable": False}})
        assert err.retryable is False

    def test_describe_deterministic_502_is_never_replayed(self):
        from apps.rag_core.services.ingest.visual.rail import analyze_asset_with_retry

        calls, sleeps = [], []

        class Client:
            timeout_s = 60

            def analyze(self, request):
                calls.append(1)
                raise classify_vision_error(502, {"detail": {"code": "vision_truncated_output", "retryable": False}})

        with pytest.raises(VisionGatewayError):
            analyze_asset_with_retry(Client(), object(), sleep=sleeps.append)
        assert len(calls) == 1 and sleeps == []

    def _asset(self):
        return SimpleNamespace(content=b"\x89PNG", mime_type="image/png")

    def test_structx_busy_retryable_is_retried_once_after_retry_after(self):
        calls, sleeps = [], []

        class Client:
            def extract(self, **kw):
                calls.append(1)
                if len(calls) == 1:
                    raise classify_vision_error(503, EX["error_503_structx_gpu_busy"])
                return "ok"

        assert S.call_with_one_retry(Client(), task="table", asset=self._asset(), request_id="r",
                                     sleep=sleeps.append) == ("ok", 2, 1)
        assert sleeps == [10.0]

    def test_busy_code_declared_non_retryable_is_not_retried(self):
        calls = []

        class Client:
            def extract(self, **kw):
                calls.append(1)
                raise classify_vision_error(503, {"detail": {"code": "structx_gpu_busy", "retryable": False}})

        with pytest.raises(VisionGatewayError):
            S.call_with_one_retry(Client(), task="table", asset=self._asset(), request_id="r", sleep=lambda s: None)
        assert len(calls) == 1

    def test_never_more_than_one_retry(self):
        calls = []

        class Client:
            def extract(self, **kw):
                calls.append(1)
                raise classify_vision_error(503, EX["error_503_structx_gpu_busy"])

        with pytest.raises(VisionGatewayError):
            S.call_with_one_retry(Client(), task="table", asset=self._asset(), request_id="r", sleep=lambda s: None)
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Provenance persistée et preuve numérique, depuis les VRAIES réponses
# --------------------------------------------------------------------------- #
ASSET = SimpleNamespace(content_hash="a" * 64, ordinal=1, source_page=4, source_locator=None)


def _meta(example, task, i=0):
    return S.visual_meta(ASSET, example, task=task, item_index=i, item=example["items"][i], response_sha256="f" * 64)


class TestProvenanceAndEvidence:
    def test_v2_blocks_are_persisted_with_real_field_names(self):
        prov = _meta(V2_TABLE, "table")["visual"][S.STRUCTURED_KEY]
        assert prov["schema_version"] == "2" and prov["request_id"] == V2_TABLE["request_id"]
        assert prov["evidence"]["certified"] is False and prov["evidence"]["numeric_claims_supported"] is False
        assert prov["evidence"]["evidence_level"] == "non_probative"
        assert prov["qualification"]["task_status"] == "PARTIAL"
        assert prov["qualification"]["business_numeric_use_authorized"] is False
        assert prov["item_consistency"] == {"status": "no_anomaly_detected", "anomalies": []}
        assert prov["gateway_build"] == V2_TABLE["gateway_build"] and prov["engine_versions"]

    def test_chart_anomaly_is_persisted(self):
        prov = _meta(V2_CHART_ANOMALY, "chart")["visual"][S.STRUCTURED_KEY]
        assert prov["item_consistency"] == {"status": "anomalies_detected", "anomalies": ["duplicate_categories"]}

    def test_v1_has_no_evidence_block(self):
        prov = _meta(V1_TABLE, "table")["visual"][S.STRUCTURED_KEY]
        assert prov["schema_version"] == "1" and prov["evidence"] is None and prov["qualification"] is None

    @pytest.mark.parametrize("example,task", [(V2_TABLE, "table"), (V2_CHART_ANOMALY, "chart"),
                                              (V2_CHART_REPEAT, "chart"), (V1_TABLE, "table")])
    def test_no_real_contract_output_is_probative(self, example, task):
        assert S.structured_evidence_decision(_meta(example, task))[0] is False

    def test_qualified_engine_with_uncertified_evidence_is_not_probative(self):
        meta = _meta(V2_TABLE, "table")
        prov = meta["visual"][S.STRUCTURED_KEY]
        prov.update(experimental=False, qualified_for_production=True)  # moteur hypothétiquement qualifié
        assert S.structured_evidence_decision(meta) == (False, S.EVIDENCE_NOT_CERTIFIED)

    def test_v1_is_never_more_probative_than_v2(self):
        v1, v2 = _meta(V1_TABLE, "table"), _meta(V2_TABLE, "table")
        for m in (v1, v2):
            m["visual"][S.STRUCTURED_KEY].update(experimental=False, qualified_for_production=True)
        assert S.structured_evidence_decision(v1)[0] is False and S.structured_evidence_decision(v2)[0] is False

    def test_hypothetical_certification_still_requires_no_anomaly(self):
        meta = _meta(V2_CHART_ANOMALY, "chart")
        prov = meta["visual"][S.STRUCTURED_KEY]
        prov.update(experimental=False, qualified_for_production=True)
        prov["evidence"].update(certified=True, numeric_claims_supported=True)
        prov["qualification"]["business_numeric_use_authorized"] = True
        assert S.structured_evidence_decision(meta) == (False, S.EVIDENCE_CONSISTENCY_ANOMALY)

    def test_guard_withholds_a_number_read_only_from_a_real_v2_table(self):
        meta = _meta(V2_TABLE, "table")
        rows = V2_TABLE["items"][0]["rows"]
        value = next(c for r in rows[1:] for c in r[1:] if any(ch.isdigit() for ch in str(c)))
        label = rows[1][0]
        chunk = {"chunk_id": "sx", "text": f"Tableau (extraction structurée expérimentale)\n{label} | {value}",
                 "source": {"document_id": "d", "page_number": 4, "filename": "f.pdf", "meta": meta}}
        [ann] = NE.annotate_numeric_evidence([chunk], table_pages_by_doc={})
        assert ann["source"]["meta"][NE.META_KEY] == NE.STRUCTURED_UNQUALIFIED
        d = NE.guard_answer(f"La valeur de {label} est {value} [1].", question=f"Quelle est la valeur de {label} ?",
                            sources=[ann], language="fr")
        assert d.verdict == NE.VERDICT_BLOCK and str(value) not in d.answer

"""client ASR face à AI Gateway Pool (SANS base).

Règles prouvées :
- seule la Gateway peut déclarer un job mort : un refus du POOL sur une relecture n'est
  jamais terminal (``JOB_AFFINITY_UNKNOWN`` compris) ;
- un refus du pool sur un DÉPÔT n'est jamais rejouable (``AFFINITY_STORE_UNAVAILABLE`` et
  ``JOB_AFFINITY_CONFLICT`` surviennent APRÈS l'acceptation par la Gateway) ;
- un DELETE refusé par le pool n'est pas une suppression confirmée ;
- le contrat de la Gateway DIRECTE est inchangé (404/410/codes terminaux = job mort ;
  ``resource_saturated`` = seul dépôt rejouable).
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.audio import gateway_asr as G
from apps.rag_core.services.ingest.audio import orchestrator as O


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def _pool(status, code, **extra):
    body = {"error": {"code": code, "message": "m", "request_id": "pool-rid-1", **extra}}
    return _Resp(status, body, {"X-AIGP-Error": code, "X-Request-ID": "pool-rid-1"})


@pytest.fixture(autouse=True)
def _url(settings):
    settings.AI_GATEWAY_BASE_URL = "http://ai-gateway-pool:8080"


@pytest.fixture
def part(tmp_path):
    p = tmp_path / "part.flac"
    p.write_bytes(b"F" * 64)
    return str(p)


def _counting(monkeypatch, resp):
    calls = []

    def fake(method, url, **kw):
        if "data" in kw and hasattr(kw["data"], "__iter__"):
            b"".join(kw["data"])
        calls.append((method, url))
        return resp
    monkeypatch.setattr(G.requests, "request", fake)
    return calls


class TestDepotViaPool:
    @pytest.mark.parametrize("status,code", [
        (503, "AFFINITY_STORE_UNAVAILABLE"),   # Redis du pool tombé (avant OU après création)
        (409, "JOB_AFFINITY_CONFLICT"),        # collision d'identifiant, compensée
        (504, "UPSTREAM_TIMEOUT"),             # la Gateway peut avoir admis le job
        (503, "UPSTREAM_UNAVAILABLE"),
        (503, "NODE_UNAVAILABLE"),
        (404, "MODEL_NOT_AVAILABLE"),
        (400, "MODEL_REQUIRED"),
    ])
    def test_aucun_refus_du_pool_n_est_rejouable(self, monkeypatch, part, status, code):
        calls = _counting(monkeypatch, _pool(status, code, compensation="deleted"))
        with pytest.raises(G.AsrError) as e:
            G.submit(part, size=64, model_logical="asr-whisper-turbo")
        err = e.value
        assert err.code == "pool_" + code.lower() and err.status == status
        assert err.retryable is False and err.terminal is False
        assert err.pool.code == code and err.request_id == "pool-rid-1"
        assert [m for m, _ in calls] == ["POST"]                       # UN SEUL dépôt, aucun rejeu

    def test_compensation_echouee_tracee_sans_identifiant(self, monkeypatch, part, caplog):
        _counting(monkeypatch, _pool(503, "AFFINITY_STORE_UNAVAILABLE", compensation="failed",
                                     gateway_job_id="orphan-123", node_id="le serveur IA-staging"))
        with pytest.raises(G.AsrError) as e:
            G.submit(part, size=64, model_logical="asr-whisper-turbo")
        assert e.value.pool.compensation == "failed"
        assert "compensation=failed" in caplog.text and "orphan-123" not in caplog.text

    def test_saturation_gateway_relayee_par_le_pool_reste_rejouable(self, monkeypatch, part):
        # Le pool relaie la réponse de la Gateway À L'IDENTIQUE (pas d'X-AIGP-Error).
        body = {"detail": {"code": "resource_saturated", "retryable": True}}
        _counting(monkeypatch, _Resp(503, body, {"Retry-After": "40"}))
        with pytest.raises(G.AsrError) as e:
            G.submit(part, size=64, model_logical="asr-whisper-turbo")
        assert e.value.code == "resource_saturated" and e.value.retryable and e.value.retry_after == 40
        assert e.value.pool is None

    @pytest.mark.parametrize("exc", [
        G.requests.ReadTimeout("lecture"),                       # dépôt envoyé, réponse jamais lue
        G.requests.ConnectionError("Connection aborted: RemoteDisconnected"),   # coupure après envoi
        G.requests.exceptions.ChunkedEncodingError("x"),
    ])
    def test_reponse_perdue_au_depot_jamais_rejouable(self, monkeypatch, part, exc):
        calls = []

        def boom(method, url, **kw):
            calls.append(method)
            raise exc
        monkeypatch.setattr(G.requests, "request", boom)
        with pytest.raises(G.AsrError) as e:
            G.submit(part, size=64, model_logical="asr-whisper-turbo")
        assert calls == ["POST"]
        assert e.value.code == "asr_submit_outcome_unknown" and e.value.retryable is False
        assert O.user_code(e.value.code) == "asr_unavailable"

    @pytest.mark.parametrize("make", [
        lambda: G.requests.ConnectTimeout("connect"),
        lambda: G.requests.ConnectionError(_max_retry(_new_conn())),
    ])
    def test_echec_prouve_avant_envoi_reste_rejouable(self, monkeypatch, part, make):
        def boom(method, url, **kw):
            raise make()
        monkeypatch.setattr(G.requests, "request", boom)
        with pytest.raises(G.AsrError) as e:
            G.submit(part, size=64, model_logical="asr-whisper-turbo")
        assert e.value.retryable is True and e.value.code in ("asr_unavailable", "asr_timeout_transport")

    def test_releve_delai_de_lecture_reste_relancable(self, monkeypatch):
        def boom(method, url, **kw):
            raise G.requests.ReadTimeout("lecture")
        monkeypatch.setattr(G.requests, "request", boom)
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")               # GET idempotent : comportement inchangé
        assert e.value.retryable is True and e.value.terminal is False


def _new_conn():
    from urllib3.exceptions import NewConnectionError

    return NewConnectionError(None, "Connection refused")


def _max_retry(reason):
    from urllib3.exceptions import MaxRetryError

    return MaxRetryError(None, "/v1/audio/transcriptions", reason)


class TestReleveViaPool:
    def test_affinite_inconnue_jamais_terminale(self, monkeypatch):
        _counting(monkeypatch, _pool(404, "JOB_AFFINITY_UNKNOWN"))
        with pytest.raises(G.AsrError) as e:
            G.fetch("job-direct-1", model_logical="asr-whisper-turbo")
        assert e.value.code == "pool_job_affinity_unknown"
        assert e.value.terminal is False and e.value.retryable is True and e.value.status == 404

    @pytest.mark.parametrize("status,code", [(503, "NODE_UNAVAILABLE"), (503, "AFFINITY_STORE_UNAVAILABLE"),
                                             (504, "UPSTREAM_TIMEOUT"), (404, "ROUTE_NOT_IN_CONTRACT"),
                                             (500, "CONFIGURATION_INVALID")])
    def test_aucun_refus_du_pool_ne_tue_le_job(self, monkeypatch, status, code):
        _counting(monkeypatch, _pool(status, code))
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.terminal is False

    def test_retry_after_du_pool_preserve(self, monkeypatch):
        r = _pool(503, "NODE_UNAVAILABLE")
        r.headers["Retry-After"] = "25"
        _counting(monkeypatch, r)
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.retry_after == 25

    @pytest.mark.parametrize("status,body", [
        (404, {"detail": {"code": "job_not_found"}}),     # Gateway directe OU relayée : job absent
        (410, {"detail": {"code": "job_expired"}}),
        (409, {"detail": {"code": "job_cancelled"}}),
        (404, {"detail": "Not Found"}),                   # 404 nu de la Gateway : contrat historique
        (410, None),
    ])
    def test_contrat_gateway_inchange_job_mort(self, monkeypatch, status, body):
        _counting(monkeypatch, _Resp(status, body))
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.terminal is True and e.value.pool is None

    def test_majuscules_sans_entete_pool_ne_valent_pas_un_code(self, monkeypatch):
        # Un corps « à la pool » SANS X-AIGP-Error n'est pas un refus du pool : statut seul.
        _counting(monkeypatch, _Resp(404, {"error": {"code": "JOB_AFFINITY_UNKNOWN"}}))
        with pytest.raises(G.AsrError) as e:
            G.fetch("j1", model_logical="m")
        assert e.value.code == "http_404" and e.value.terminal is True


class TestSuppression:
    def test_delete_refuse_par_le_pool_non_confirme(self, monkeypatch):
        _counting(monkeypatch, _pool(404, "JOB_AFFINITY_UNKNOWN"))
        assert G.delete("job-direct-1") is False

    def test_delete_gateway_404_reste_tolere(self, monkeypatch):
        _counting(monkeypatch, _Resp(404, {"detail": {"code": "job_not_found"}}))
        assert G.delete("j1") is True


class TestCodesUtilisateur:
    def test_refus_du_pool_ne_sont_jamais_un_defaut_du_fichier(self):
        for code in ("pool_node_unavailable", "pool_upstream_timeout", "pool_affinity_store_unavailable",
                     "pool_upstream_unavailable", "pool_no_eligible_node", "pool_capability_not_available"):
            assert O.user_code(code) == "asr_unavailable"
        assert O.user_code("pool_job_affinity_unknown") == "asr_job_tracking_lost"
        assert "asr_job_tracking_lost" in O.MESSAGES
        assert O.user_code("pool_model_not_available") == "asr_failed"

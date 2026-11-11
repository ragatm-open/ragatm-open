"""Export des compteurs d'exploitation hors du processus.

Le point qui justifie ces tests : la politique de reranking est **fail-open**. Une panne de
la passerelle ne casse aucune requête — elle dégrade la qualité en silence. Sans surface de
lecture externe, cette dégradation serait invisible.

`TestAggregationHonesty` est le test le plus important : il vérifie que l'export **dit** si
les valeurs couvrent tous les workers. Une métrique agrégée à tort serait pire qu'une
métrique absente.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.cache import cache

from tests_root.cache_isolation import caches_cloisonnes

from apps.rag_core.services import ops_counters
from apps.rag_core.services import rerank_gateway as RG
from apps.rag_core.services.ingest.extraction.ocr import OCR_COUNTER_NAMES
from apps.rag_core.services.ingest.process_isolation import COUNTER_FORK_REFUSE

#: Surface EXACTE attendue de l'export. Le reranker n'est plus le seul producteur de
#: compteurs : l'OCR en publie depuis OCR-TIMEOUT-HARDENING-02. Cette liste reste
#: exhaustive et fermée — c'est elle, et non l'ajout d'un producteur, qui borne la
#: cardinalité. Un nouveau compteur doit passer par ici, donc être vu en revue.
#: Vision V2 orchestrée (CV-3E1) : seuls producteurs ÉTIQUETÉS, à étiquettes FERMÉES
#: (codes d'erreur et raisons d'inéligibilité énumérés dans le code, jamais un identifiant).
_VISUAL_V2 = (
    "visual_crops_total", "visual_success_total", "visual_capacity_deferred_total",
    "visual_parked_total", "visual_gateway_503_total", "visual_gateway_504_total",
    *(f'visual_terminal_failure_total{{code="{c}"}}' for c in (
        "vision_gateway_timeout", "visual_client_timeout", "visual_gateway_unavailable",
        "visual_gateway_transport_error", "vision_upstream_error", "vision_request_invalid",
        "vision_v2_contract_error", "provenance_integrity_failure", "pipeline_not_allowed",
        "local_crop_integrity_failure", "pipeline_fingerprint_mismatch", "context_mismatch",
        "model_budget_exhausted", "other")),
    *(f'visual_ineligible_total{{reason="{r}"}}' for r in ("crop_encoded_oversize", "crop_not_sendable", "other")),
    # CV-3E4A : balayeur et fermetures de porte avant tentative.
    "visual_sweeper_runs_total", "visual_scope_closed_attempt_total",
    *(f'visual_sweeper_recovered_jobs_total{{reason="{r}"}}' for r in (
        "pending_unenqueued", "pending_lost", "deferred_overdue", "lease_expired")),
    *(f'visual_sweeper_recovered_prepares_total{{reason="{r}"}}' for r in ("pending_lost", "running_stalled")),
)
_COMPTEURS_ATTENDUS = (
    tuple(RG.COUNTER_NAMES) + tuple(OCR_COUNTER_NAMES) + (COUNTER_FORK_REFUSE,) + _VISUAL_V2
)

pytestmark = pytest.mark.django_db

#: Hors de `/api/` **délibérément** : le reverse-proxy proxifie `/api/` vers le frontend,
#: qui réécrit vers le backend. Une route sous `/api/` serait publique et anonyme — c'est
#: ce qui a été constaté en staging avant correction. Voir `TestExposureSurface`.
METRICS_URL = "/ops/metrics"


@pytest.fixture(autouse=True)
def _clean():
    RG.reset_counters()
    yield
    RG.reset_counters()


def _caches_de_test(backend: str, location: str) -> dict:
    """`CACHES` de test portant les DEUX alias et le préfixe du worker.

    Deux précautions, chacune pour une raison distincte :

    * l'alias ``ops`` est conservé — le laisser disparaître ferait retomber
      ``ops_counters`` sur ``default``. Ce repli est licite en production, mais
      ici il ferait mesurer au test autre chose que ce qu'il annonce ;
    * le ``KEY_PREFIX`` du worker est posé par ``caches_cloisonnes`` — une
      reconstruction qui l'oublie rouvre le cloisonnement fermé lors de la qualification.
    """
    conf = {"BACKEND": backend, "LOCATION": location}
    return caches_cloisonnes({"default": dict(conf), "ops": dict(conf)})

def _chunk(cid, fusion, score, text="texte"):
    return {"chunk_id": cid, "fusion_score": fusion, "score": score, "text": text}


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(scores):
    return _Resp({"model": "m", "model_logical": "rerank-default", "model_revision": "r",
                  "provider": "ai_gateway", "upstream_provider": "x",
                  "results": [{"index": i, "id": k, "relevance_score": v}
                              for i, (k, v) in enumerate(scores.items())]})


def _read(client, fmt=None):
    url = METRICS_URL + ("?format=json" if fmt == "json" else "")
    r = client.get(url)
    assert r.status_code == 200
    return json.loads(r.content) if fmt == "json" else r.content.decode()


# --------------------------------------------------------------------------- #
# Le point décisif : ne jamais présenter un compteur local comme global
# --------------------------------------------------------------------------- #
class TestAggregationHonesty:
    def test_l_export_declare_sa_portee(self, client):
        body = _read(client, "json")
        assert "aggregation" in body["meta"]
        assert body["meta"]["aggregation"] in ("shared", "process_local", "unknown")
        assert isinstance(body["meta"]["aggregated_across_workers"], bool)

    def test_le_texte_expose_aussi_la_portee(self, client):
        text = _read(client)
        assert "rag_ops_counters_aggregated_across_workers" in text, (
            "la supervision doit pouvoir constater elle-même si la valeur couvre tous les workers"
        )

    def test_locmem_est_signale_comme_local(self, settings):
        settings.CACHES = _caches_de_test(
            "django.core.cache.backends.locmem.LocMemCache", "t")
        assert ops_counters.aggregation_mode() == "process_local"
        assert ops_counters.describe()["aggregated_across_workers"] is False

    def test_redis_est_signale_comme_partage(self, settings):
        settings.CACHES = _caches_de_test(
            "django_redis.cache.RedisCache", "redis://x/2")
        assert ops_counters.aggregation_mode() == "shared"
        assert ops_counters.describe()["aggregated_across_workers"] is True

    def test_la_durabilite_n_est_pas_surestimee(self):
        d = ops_counters.describe()
        assert d["durable_across_store_restart"] is False, (
            "prétendre que les compteurs survivent au redémarrage du magasin serait faux"
        )


# --------------------------------------------------------------------------- #
# Sémantique des compteurs
# --------------------------------------------------------------------------- #
class TestCounterSemantics:
    def _chunks(self):
        return [_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)]

    def test_nominal(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=self._chunks())
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_success_total"] == 1
        assert c["rag_reranker_failure_total"] == 0
        assert c["rag_reranker_fallback_total"] == 0

    def test_gateway_injoignable(self, settings, client):
        import urllib.error
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_open_rrf"

        def _boom(*a, **k):
            raise urllib.error.URLError("injoignable")

        chunks = [_chunk("a", 0.01, 0.9), _chunk("b", 0.03, 0.1)]
        with patch("urllib.request.urlopen", _boom):
            out, info = RG.rerank_chunks(question="q", chunks=chunks)
        assert [x["chunk_id"] for x in out] == ["b", "a"], "ordre RRF canonique restauré"
        assert info["fallback"] is True
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_failure_total"] == 1
        assert c["rag_reranker_fallback_total"] == 1
        assert c["rag_reranker_success_total"] == 0

    def test_timeout_est_distingue(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _to(*a, **k):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", _to):
            RG.rerank_chunks(question="q", chunks=self._chunks())
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_timeout_total"] == 1, "un exploitant doit distinguer une passerelle lente"
        assert c["rag_reranker_failure_total"] == 1
        assert c["rag_reranker_fallback_total"] == 1
        assert c["rag_reranker_invalid_response_total"] == 0

    def test_reponse_invalide_est_distinguee(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        bad = _Resp({"results": [{"id": "a", "relevance_score": 1.0}]})  # cardinalité 1 != 2
        with patch("urllib.request.urlopen", lambda r, timeout=None: bad):
            RG.rerank_chunks(question="q", chunks=self._chunks())
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_invalid_response_total"] == 1
        assert c["rag_reranker_timeout_total"] == 0
        assert c["rag_reranker_fallback_total"] == 1

    def test_pas_de_double_comptage(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _to(*a, **k):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", _to):
            RG.rerank_chunks(question="q", chunks=self._chunks())
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_failure_total"] == 1, "un échec ne se compte qu'une fois"
        assert c["rag_reranker_success_total"] == 0

    def test_totaux_sur_appels_mixtes(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _to(*a, **k):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=self._chunks())
            RG.rerank_chunks(question="q", chunks=self._chunks())
        with patch("urllib.request.urlopen", _to):
            RG.rerank_chunks(question="q", chunks=self._chunks())
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] == 3
        assert c["rag_reranker_success_total"] == 2
        assert c["rag_reranker_failure_total"] == 1
        assert c["rag_reranker_fallback_total"] == 1

    def test_monotonie(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=self._chunks())
            first = _read(client, "json")["counters"]["rag_reranker_attempted_total"]
            RG.rerank_chunks(question="q", chunks=self._chunks())
            second = _read(client, "json")["counters"]["rag_reranker_attempted_total"]
        assert second > first


# --------------------------------------------------------------------------- #
# Drapeau éteint : silence, et silence DISTINGUABLE d'une panne
# --------------------------------------------------------------------------- #
class TestFlagOff:
    def test_aucune_activite_quand_le_drapeau_est_eteint(self, settings, client):
        settings.RAG_RERANKER_ENABLED = False
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] == 0
        assert c["rag_reranker_fallback_total"] == 0

    def test_drapeau_eteint_discernable_d_une_panne(self, settings, client):
        """`attempted == 0` signifie désactivé ; une panne donne `attempted > 0`."""
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _boom(*a, **k):
            raise TimeoutError("timed out")

        c_off = _read(client, "json")["counters"]
        with patch("urllib.request.urlopen", _boom):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.01, 0.9)])
        c_fail = _read(client, "json")["counters"]
        assert c_off["rag_reranker_attempted_total"] == 0
        assert c_fail["rag_reranker_attempted_total"] == 1


# --------------------------------------------------------------------------- #
# Surface externe, confidentialité, cardinalité
# --------------------------------------------------------------------------- #
class TestExternalSurface:
    def test_lecture_sans_shell_python(self, client):
        """Un exploitant lit les compteurs par HTTP, pas en inspectant un processus."""
        text = _read(client)
        for name in RG.COUNTER_NAMES:
            assert name in text

    def test_aucune_donnee_sensible(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw-secret.internal:9999"
        with patch("urllib.request.urlopen",
                   lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="quelle est la valorisation de la societe X ?",
                             chunks=[_chunk("chunk-prive-1", 0.03, 0.5, "texte confidentiel"),
                                     _chunk("chunk-prive-2", 0.02, 0.4, "autre texte")])
        text = _read(client)
        for interdit in ("valorisation", "confidentiel", "chunk-prive", "gw-secret", "9999"):
            assert interdit not in text, f"fuite dans l'export : {interdit!r}"

    def test_cardinalite_bornee(self, client):
        """Aucune dimension libre : le nombre de séries est fixe."""
        text = _read(client)
        series = [l for l in text.splitlines() if l and not l.startswith("#")]
        assert len(series) == len(_COMPTEURS_ATTENDUS) + 1, (
            "le nombre de séries doit être constant, indépendant du trafic"
        )
        # Étiquettes : uniquement les séries FERMÉES déclarées ci-dessus (jamais un
        # identifiant de document, de crop, ni une empreinte).
        labelled = [l.split(" ", 1)[0] for l in series if "{" in l]
        assert labelled and set(labelled) <= set(_VISUAL_V2)
        assert [l.split(" ", 1)[0] for l in series][:-1] == list(_COMPTEURS_ATTENDUS)

    def test_taux_derivables(self, settings, client):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        c = _read(client, "json")["counters"]
        assert c["rag_reranker_attempted_total"] > 0
        taux = c["rag_reranker_fallback_total"] / c["rag_reranker_attempted_total"]
        assert taux == 0.0, "le taux de repli doit être calculable par l'exploitant"


# --------------------------------------------------------------------------- #
# Surface d'exposition : où la route est joignable, et par qui
# --------------------------------------------------------------------------- #
class TestExposureSurface:
    """Régression de sécurité.

    En staging, l'endpoint avait d'abord été servi sous ``/api/ops/metrics`` : le
    reverse-proxy proxifie ``/api/`` vers le frontend, qui réécrit vers le backend, si
    bien que la route répondait **200 en anonyme depuis Internet**. Les compteurs ne
    portent aucun texte métier, mais la volumétrie d'usage est elle-même une information.
    """

    def test_route_hors_du_prefixe_api(self, client):
        assert not METRICS_URL.startswith("/api/"), (
            "sous /api/, le proxy public rendrait les compteurs anonymement lisibles"
        )
        assert client.get("/api/ops/metrics").status_code == 404

    def test_sans_jeton_configure_la_route_repond(self, settings, client):
        settings.RAG_OPS_METRICS_TOKEN = ""
        assert client.get(METRICS_URL).status_code == 200

    def test_jeton_configure_refuse_l_anonyme(self, settings, client):
        settings.RAG_OPS_METRICS_TOKEN = "jeton-de-test"
        assert client.get(METRICS_URL).status_code == 403
        assert client.get(METRICS_URL, HTTP_AUTHORIZATION="Bearer mauvais").status_code == 403
        r = client.get(METRICS_URL, HTTP_AUTHORIZATION="Bearer jeton-de-test")
        assert r.status_code == 200
        assert "rag_reranker_attempted_total" in r.content.decode()

    def test_refus_ne_divulgue_aucun_compteur(self, settings, client):
        settings.RAG_OPS_METRICS_TOKEN = "jeton-de-test"
        corps = client.get(METRICS_URL).content.decode()
        for name in RG.COUNTER_NAMES:
            assert name not in corps


# --------------------------------------------------------------------------- #
# L'export ne change pas le comportement
# --------------------------------------------------------------------------- #
class TestNoBehaviourChange:
    def test_une_panne_de_compteur_ne_casse_pas_le_reranking(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _cache_ko(*a, **k):
            raise RuntimeError("magasin indisponible")

        with patch.object(cache, "incr", _cache_ko), patch.object(cache, "add", _cache_ko):
            with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
                out, info = RG.rerank_chunks(
                    question="q", chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        assert info["success"] is True, "l'observabilité ne doit jamais coûter une requête"
        assert [c["chunk_id"] for c in out] == ["b", "a"]

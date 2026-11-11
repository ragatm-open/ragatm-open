"""Écriture groupée des compteurs d'exploitation.

Ce que ce fichier verrouille, et pourquoi il existe.

Le lot d'attribution a établi que les compteurs adossés à Redis étaient le premier
contributeur à la queue de latence du reranking — **52,4 %** du résiduel de la cohorte
haute. Le mécanisme est double : `django_redis.cache.incr` impose un `EXISTS` avant chaque
`INCRBY` (huit allers-retours par événement au lieu d'un), et le serveur persiste en
`appendfsync everysec`, si bien que chaque écriture est une occasion de heurter une `fsync`
en cours.

Le correctif regroupe les compteurs d'un même événement en **un seul aller-retour**. Ces
tests garantissent que le regroupement n'a rien changé de ce qui compte : les valeurs
exportées, l'agrégation entre workers, et le fait qu'une panne du magasin ne coûte jamais
une requête.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.cache import caches

from apps.rag_core.services import ops_counters as OC
from apps.rag_core.services import rerank_gateway as RG

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean():
    RG.reset_counters()
    yield
    RG.reset_counters()


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


# --------------------------------------------------------------------------- #
# 1. Les valeurs exportées sont EXACTEMENT celles d'avant
# --------------------------------------------------------------------------- #
class TestExactIncrements:
    def test_succes_produit_les_quatre_compteurs(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        c = RG.counters_snapshot()
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_success_total"] == 1
        assert c["rag_reranker_documents_total"] == 2
        assert c["rag_reranker_duration_ms_total"] >= 0
        assert c["rag_reranker_failure_total"] == 0
        assert c["rag_reranker_fallback_total"] == 0

    def test_repli_produit_attempted_failure_fallback(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _ko(r, timeout=None):
            raise OSError("injoignable")

        with patch("urllib.request.urlopen", _ko):
            out, info = RG.rerank_chunks(question="q",
                                         chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        assert info["fallback"] is True
        c = RG.counters_snapshot()
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_failure_total"] == 1
        assert c["rag_reranker_fallback_total"] == 1
        assert c["rag_reranker_success_total"] == 0

    def test_timeout_incremente_sa_sous_categorie_une_seule_fois(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _to(r, timeout=None):
            raise TimeoutError("timed out")

        with patch("urllib.request.urlopen", _to):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5)])
        c = RG.counters_snapshot()
        assert c["rag_reranker_timeout_total"] == 1
        assert c["rag_reranker_invalid_response_total"] == 0
        assert c["rag_reranker_failure_total"] == 1

    def test_pas_de_double_comptage_sur_dix_appels(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0})):
            for _ in range(10):
                RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5)])
        c = RG.counters_snapshot()
        assert c["rag_reranker_attempted_total"] == 10
        assert c["rag_reranker_success_total"] == 10
        assert c["rag_reranker_documents_total"] == 10

    def test_fail_closed_ecrit_les_compteurs_avant_de_lever(self, settings):
        """Une politique `fail_closed` remonte l'erreur — mais l'événement reste compté."""
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_closed"

        def _ko(r, timeout=None):
            raise OSError("injoignable")

        with patch("urllib.request.urlopen", _ko):
            with pytest.raises(RG.RerankGatewayError):
                RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5)])
        c = RG.counters_snapshot()
        assert c["rag_reranker_attempted_total"] == 1
        assert c["rag_reranker_failure_total"] == 1
        assert c["rag_reranker_fallback_total"] == 0, "fail_closed ne replie pas"


# --------------------------------------------------------------------------- #
# 2. Le regroupement lui-même
# --------------------------------------------------------------------------- #
class TestBatching:
    def test_un_evenement_reussi_ecrit_en_une_seule_fois(self, settings):
        """C'est l'objet du lot : un aller-retour, pas quatre."""
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        appels = []
        vrai = OC.incr_many

        def _spy(pairs):
            appels.append(dict(pairs))
            return vrai(pairs)

        with patch.object(OC, "incr_many", _spy), \
             patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        assert len(appels) == 1, "les compteurs d'un événement partent ensemble"
        assert set(appels[0]) == {
            "rag_reranker_attempted_total", "rag_reranker_success_total",
            "rag_reranker_documents_total", "rag_reranker_duration_ms_total",
        }

    def test_un_repli_ecrit_aussi_en_une_seule_fois(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        appels = []
        vrai = OC.incr_many

        def _spy(pairs):
            appels.append(dict(pairs))
            return vrai(pairs)

        def _ko(r, timeout=None):
            raise OSError("injoignable")

        with patch.object(OC, "incr_many", _spy), patch("urllib.request.urlopen", _ko):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.03, 0.5)])
        assert len(appels) == 1
        assert set(appels[0]) == {
            "rag_reranker_attempted_total", "rag_reranker_failure_total",
            "rag_reranker_fallback_total",
        }

    def test_incr_many_additionne_les_doublons(self):
        OC.incr_many({"rag_reranker_documents_total": 3})
        OC.incr_many({"rag_reranker_documents_total": 4})
        assert OC.get("rag_reranker_documents_total") == 7

    def test_incr_many_vide_ne_fait_rien(self):
        OC.incr_many({})
        assert OC.get("rag_reranker_attempted_total") == 0

    def test_incr_delegue_a_incr_many(self):
        """Un seul chemin d'écriture : `incr` ne doit pas diverger du groupé."""
        OC.incr("rag_reranker_attempted_total", 2)
        assert OC.get("rag_reranker_attempted_total") == 2


# --------------------------------------------------------------------------- #
# 3. Une panne du magasin ne coûte jamais une requête
# --------------------------------------------------------------------------- #
class TestFailOpen:
    def test_magasin_indisponible_le_reranking_aboutit(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _boom(*a, **k):
            raise RuntimeError("magasin indisponible")

        with patch.object(OC, "incr_many", _boom):
            with pytest.raises(RuntimeError):
                OC.incr_many({"x": 1})

        # Le vrai chemin : `_Counters.flush` passe par `incr_many`, qui absorbe.
        with patch.object(OC, "_pipeline_writer", _boom), \
             patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"a": 1.0, "b": 2.0})):
            out, info = RG.rerank_chunks(
                question="q", chunks=[_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4)])
        assert info["success"] is True, "l'observabilité ne coûte jamais une requête"
        assert [c["chunk_id"] for c in out] == ["b", "a"], "l'ordre rendu est intact"

    def test_echec_du_pipeline_ne_tente_aucun_repli(self):
        """Un repli après exécution partielle produirait un DOUBLE comptage."""
        class _Pipe:
            def incrby(self, *a):
                return self

            def execute(self):
                raise RuntimeError("timeout Redis")

        class _Client:
            def pipeline(self, transaction=False):
                return _Pipe()

        with patch.object(OC, "_pipeline_writer", lambda: (_Client(), lambda k: k)):
            OC.incr_many({"rag_reranker_attempted_total": 1})
        assert OC.get("rag_reranker_attempted_total") == 0, (
            "après un échec de pipeline, aucun second chemin d'écriture ne doit s'exécuter"
        )

    def test_le_timeout_dedie_est_declare_et_borne(self, settings):
        from config import registry

        d = registry.require("OPS_COUNTERS_IO_TIMEOUT_SECONDS")
        assert d.value_type == "float"
        assert d.default == 0.25
        assert d.default < settings.RAG_RERANKER_TIMEOUT_SECONDS, (
            "le budget de l'observabilité doit être strictement plus court que celui du "
            "reranking : il ne doit jamais devenir le facteur limitant"
        )


# --------------------------------------------------------------------------- #
# 4. Ce que le lot ne doit PAS avoir changé
# --------------------------------------------------------------------------- #
class TestUnchangedContract:
    def test_les_series_exportees_sont_identiques(self):
        assert RG.COUNTER_NAMES == (
            "rag_reranker_attempted_total", "rag_reranker_success_total",
            "rag_reranker_failure_total", "rag_reranker_fallback_total",
            "rag_reranker_timeout_total", "rag_reranker_invalid_response_total",
            "rag_reranker_documents_total", "rag_reranker_duration_ms_total",
        )

    def test_endpoint_metrics_expose_toujours_tout(self, client, settings):
        settings.RAG_OPS_METRICS_TOKEN = ""
        texte = client.get("/ops/metrics").content.decode()
        for name in RG.COUNTER_NAMES:
            assert name in texte
        assert "rag_ops_counters_aggregated_across_workers" in texte

    def test_alias_dedie_vise_le_meme_magasin_que_default(self, settings):
        """Aucune instance Redis nouvelle : seul le profil de panne diffère."""
        conf = settings.CACHES
        if "ops" not in conf:
            pytest.skip("alias `ops` absent de cette configuration")
        assert conf["ops"]["BACKEND"] == conf["default"]["BACKEND"]
        # L'égalité de LOCATION ne porte QUE sur la configuration Redis : c'est là
        # que « viser le même magasin » veut dire quelque chose. En LocMem — la
        # configuration de la CI, sans `DJANGO_CACHE_URL` — les deux alias sont
        # de toute façon locaux au process, et l'espace de noms distinct est voulu.
        if "redis" in conf["default"]["BACKEND"].lower():
            assert conf["ops"].get("LOCATION") == conf["default"]["LOCATION"]

    def test_le_cache_default_n_est_pas_modifie(self, settings):
        """Le cache applicatif ne doit pas hériter du délai court de l'observabilité."""
        opts = (settings.CACHES["default"].get("OPTIONS") or {})
        assert "SOCKET_TIMEOUT" not in opts

    def test_la_portee_d_agregation_reste_declaree(self):
        meta = OC.describe()
        assert set(meta) >= {
            "aggregation", "aggregated_across_workers",
            "durable_across_app_restart", "durable_across_store_restart", "semantics",
        }
        assert OC.aggregation_mode() in ("shared", "process_local", "unknown")

    def test_les_compteurs_restent_lisibles_apres_ecriture_groupee(self):
        """Écriture par pipeline, lecture par l'API de cache : même clé physique."""
        OC.incr_many({"rag_reranker_attempted_total": 5, "rag_reranker_success_total": 2})
        assert OC.get("rag_reranker_attempted_total") == 5
        assert OC.get("rag_reranker_success_total") == 2
        snap = OC.snapshot(list(RG.COUNTER_NAMES))
        assert snap["rag_reranker_attempted_total"] == 5

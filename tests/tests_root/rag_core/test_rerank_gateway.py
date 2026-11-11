"""Client de reranking via l'AI Gateway.

Le test central est `TestPoolIdentity` : il reproduit le défaut mesuré lors du replay —
la liste `chunks` est dans l'ordre d'HYGIÈNE, pas dans l'ordre RRF — et verrouille le fait
que ce sont bien les N premiers du pool RRF qui partent au reranker. Sans lui, la
régression est silencieuse : elle ne casse rien, elle dégrade.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apps.rag_core.services import rerank_gateway as RG


def _chunk(cid, fusion, score, text="texte"):
    return {"chunk_id": cid, "fusion_score": fusion, "score": score, "text": text}


def _resp(scores):
    return {
        "model": "BAAI/bge-reranker-v2-m3",
        "model_logical": "rerank-default",
        "model_revision": "deadbeef",
        "provider": "ai_gateway",
        "upstream_provider": "local_cross_encoder",
        "results": [{"index": i, "id": k, "relevance_score": v} for i, (k, v) in enumerate(scores.items())],
    }


class _FakeResp:
    def __init__(self, payload, code=200):
        self._b = json.dumps(payload).encode()
        self._c = code

    def read(self):
        return self._b

    def getcode(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --------------------------------------------------------------------------- #
# Le défaut que ce lot doit rendre impossible
# --------------------------------------------------------------------------- #
class TestPoolIdentity:
    def test_le_pool_soumis_suit_l_ordre_RRF_et_non_l_ordre_de_la_liste(self, settings):
        """Reproduction du défaut mesuré : hygiène ≠ RRF.

        `score` (hygiène) et `fusion_score` (RRF) sont ici ANTI-corrélés. Un `chunks[:2]`
        naïf prendrait `h0, h1` ; le pool RRF attendu est `r0, r1`.
        """
        settings.RAG_RERANKER_POOL_K = 2
        chunks = [
            _chunk("h0", fusion=0.001, score=0.99),   # 1er en hygiène, dernier en RRF
            _chunk("h1", fusion=0.002, score=0.98),
            _chunk("r1", fusion=0.030, score=0.10),
            _chunk("r0", fusion=0.040, score=0.05),   # 1er en RRF, dernier en hygiène
        ]
        head, tail = RG.select_rrf_pool(chunks)

        assert [c["chunk_id"] for c in head] == ["r0", "r1"], (
            "le pool soumis doit être le top-N par RANG RRF, pas les N premiers de la liste"
        )
        assert [c["chunk_id"] for c in tail] == ["h1", "h0"]

    def test_le_naif_chunks_slice_donnerait_un_autre_pool(self, settings):
        """Contrôle négatif explicite : le test précédent ne passerait pas par accident."""
        settings.RAG_RERANKER_POOL_K = 2
        chunks = [
            _chunk("h0", fusion=0.001, score=0.99),
            _chunk("h1", fusion=0.002, score=0.98),
            _chunk("r1", fusion=0.030, score=0.10),
            _chunk("r0", fusion=0.040, score=0.05),
        ]
        naif = [c["chunk_id"] for c in chunks[:2]]
        rrf = [c["chunk_id"] for c in RG.select_rrf_pool(chunks)[0]]
        assert naif != rrf, "les deux ordres doivent différer, sinon le test ne prouve rien"

    def test_ids_soumis_a_la_gateway(self, settings):
        settings.RAG_RERANKER_POOL_K = 2
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        chunks = [
            _chunk("h0", 0.001, 0.99), _chunk("h1", 0.002, 0.98),
            _chunk("r1", 0.030, 0.10), _chunk("r0", 0.040, 0.05),
        ]
        captured = {}

        def _fake(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(_resp({"r0": 2.0, "r1": 1.0}))

        with patch("urllib.request.urlopen", _fake):
            RG.rerank_chunks(question="q", chunks=chunks)

        assert [d["id"] for d in captured["body"]["documents"]] == ["r0", "r1"]


# --------------------------------------------------------------------------- #
# Contrat client
# --------------------------------------------------------------------------- #
class TestContract:
    def _chunks(self):
        return [_chunk("a", 0.03, 0.5), _chunk("b", 0.02, 0.4), _chunk("c", 0.01, 0.3)]

    def test_un_seul_appel_http_pour_n_passages(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        calls = []

        def _fake(req, timeout=None):
            calls.append(req)
            return _FakeResp(_resp({"a": 1.0, "b": 3.0, "c": 2.0}))

        with patch("urllib.request.urlopen", _fake):
            out, info = RG.rerank_chunks(question="q", chunks=self._chunks())

        assert len(calls) == 1, "un appel par POOL, jamais un appel par passage"
        assert info["documents"] == 3
        assert [c["chunk_id"] for c in out] == ["b", "c", "a"]

    def test_ordre_porte_par_rerank_rank_sans_ecraser_les_scores(self, settings):
        """`diversify_ranked_results` retrie : l'ordre doit voyager dans la clé de tri.

        Il y voyage désormais par `rerank_rank`, un RANG assumé, et non plus par un
        ordinal écrit dans `fusion_score`/`score`. Ce test remplace celui qui verrouillait
        la réécriture : il vérifie la même propriété de classement, et en plus que les
        métadonnées du retrieval survivent.
        """
        from apps.rag_core.services.query.diversify import rrf_sort_key

        settings.RAG_RERANKER_BASE_URL = "http://gw"
        avant = {c["chunk_id"]: (c.get("fusion_score"), c.get("score")) for c in self._chunks()}
        with patch("urllib.request.urlopen", lambda r, timeout=None: _FakeResp(_resp({"a": 1.0, "b": 3.0, "c": 2.0}))):
            out, _ = RG.rerank_chunks(question="q", chunks=self._chunks())

        # 1. la clé de tri reste strictement décroissante — la propriété d'origine
        cles = [rrf_sort_key(c) for c in out]
        assert cles == sorted(cles, reverse=True)
        # 2. le rang est explicite et 1-indexé
        assert [c["rerank_rank"] for c in out] == [1, 2, 3]
        # 3. le score du cross-encoder est le vrai
        assert out[0]["rerank_score"] == 3.0
        # 4. AUCUNE métadonnée de retrieval n'a été écrasée
        apres = {c["chunk_id"]: (c.get("fusion_score"), c.get("score")) for c in out}
        assert apres == avant

    def test_ids_preserves(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _FakeResp(_resp({"a": 1.0, "b": 3.0, "c": 2.0}))):
            out, _ = RG.rerank_chunks(question="q", chunks=self._chunks())
        assert {c["chunk_id"] for c in out} == {"a", "b", "c"}

    @pytest.mark.parametrize("payload,motif", [
        ({"results": "pas une liste"}, "results"),
        ({"results": [{"id": "a", "relevance_score": 1.0}]}, "cardinalite"),
        ({"results": [{"id": "a", "relevance_score": 1.0}, {"id": "a", "relevance_score": 2.0},
                      {"id": "c", "relevance_score": 3.0}]}, "duplique"),
        ({"results": [{"id": "zz", "relevance_score": 1.0}, {"id": "b", "relevance_score": 2.0},
                      {"id": "c", "relevance_score": 3.0}]}, "inconnu"),
        ({"results": [{"id": "a", "relevance_score": "x"}, {"id": "b", "relevance_score": 2.0},
                      {"id": "c", "relevance_score": 3.0}]}, "numerique"),
        ({"results": [{"id": "a", "relevance_score": float("nan")}, {"id": "b", "relevance_score": 2.0},
                      {"id": "c", "relevance_score": 3.0}]}, "fini"),
        ({"results": [{"id": "a", "relevance_score": float("inf")}, {"id": "b", "relevance_score": 2.0},
                      {"id": "c", "relevance_score": 3.0}]}, "fini"),
    ])
    def test_reponses_non_conformes_refusees(self, settings, payload, motif):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_closed"
        with patch("urllib.request.urlopen", lambda r, timeout=None: _FakeResp(payload)):
            with pytest.raises(RG.RerankGatewayError):
                RG.rerank_chunks(question="q", chunks=self._chunks())

    def test_json_invalide_refuse(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_closed"

        class _Bad(_FakeResp):
            def read(self):
                return b"<html>pas du json</html>"

        with patch("urllib.request.urlopen", lambda r, timeout=None: _Bad({})):
            with pytest.raises(RG.RerankGatewayError):
                RG.rerank_chunks(question="q", chunks=self._chunks())


# --------------------------------------------------------------------------- #
# Politique d'échec
# --------------------------------------------------------------------------- #
class TestFailurePolicy:
    def _boom(self, *a, **k):
        raise TimeoutError("timeout")

    def test_fail_open_conserve_l_ordre_RRF_et_le_SIGNALE(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_open_rrf"
        RG.reset_counters()
        chunks = [_chunk("a", 0.01, 0.9), _chunk("b", 0.03, 0.1)]
        with patch("urllib.request.urlopen", self._boom):
            out, info = RG.rerank_chunks(question="q", chunks=chunks)
        assert [c["chunk_id"] for c in out] == ["b", "a"], "repli sur l'ordre RRF, pas sur l'ordre reçu"
        assert info["fallback"] is True and info["success"] is False
        assert RG.counters_snapshot().get("rag_reranker_fallback_total") == 1, (
            "un repli invisible serait pire qu'une erreur"
        )

    def test_fail_closed_leve(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_closed"
        with patch("urllib.request.urlopen", self._boom):
            with pytest.raises(RG.RerankGatewayError):
                RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.01, 0.9)])

    @pytest.mark.parametrize("code", [400, 502, 503])
    def test_erreurs_http(self, settings, code):
        import urllib.error
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_open_rrf"

        def _raise(*a, **k):
            raise urllib.error.HTTPError("http://gw", code, "err", {}, None)

        with patch("urllib.request.urlopen", _raise):
            out, info = RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.01, 0.9)])
        assert info["fallback"] is True

    def test_valeur_de_politique_inconnue_retombe_sur_fail_open(self, settings):
        settings.RAG_RERANKER_FAILURE_POLICY = "n_importe_quoi"
        assert RG.failure_policy() == "fail_open_rrf"


# --------------------------------------------------------------------------- #
# Drapeau et découplage
# --------------------------------------------------------------------------- #
class TestFlagAndCoupling:
    def test_drapeau_off_par_defaut(self):
        from django.conf import settings as dj
        assert getattr(dj, "RAG_RERANKER_ENABLED", False) is False

    def test_aucun_couplage_au_backend(self):
        """RagATM ne doit connaître ni le dépôt HF, ni TEI, ni un port de backend."""
        src = open(RG.__file__, encoding="utf-8").read()
        for interdit in ("BAAI/", "huggingface", "text-embeddings-inference", "8300"):
            assert interdit not in src, f"couplage interdit au backend : {interdit!r}"

    def test_modele_designe_par_son_nom_logique(self, settings):
        settings.RAG_RERANKER_MODEL_LOGICAL = "rerank-default"
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        captured = {}

        def _fake(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(_resp({"a": 1.0}))

        with patch("urllib.request.urlopen", _fake):
            RG.rerank_chunks(question="q", chunks=[_chunk("a", 0.01, 0.9)])
        assert captured["body"]["model"] == "rerank-default"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
class TestProvenance:
    def test_lecture_de_l_identite_par_http(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"
        runtime = {"environment": "staging",
                   "build": {"git_sha": "abc123", "version": "2.0.0", "image_digest": "not_reported"},
                   "rerank": {"enabled": True,
                              "models": [{"provider": "local_cross_encoder", "artifact": "X", "revision": "rev1"}]}}
        seen = []

        def _fake(url, timeout=None):
            seen.append(str(url))
            return _FakeResp(runtime)

        with patch("urllib.request.urlopen", _fake):
            p = RG.provenance()
        # /health/rerank CALCULE un reclassement ; le
        # diagnostic ne lit plus que /health/runtime.
        assert seen and all(u.endswith("/health/runtime") for u in seen)
        s = p["summary"]
        assert s["environment"] == "staging"
        assert s["gateway_git_sha"] == "abc123"
        assert s["backend_implementation"] == "local_cross_encoder"
        assert s["model_revision"] == "rev1"
        assert s["rerank_health"] == "declared"

    def test_provenance_ne_leve_jamais(self, settings):
        settings.RAG_RERANKER_BASE_URL = "http://gw"

        def _boom(*a, **k):
            raise OSError("injoignable")

        with patch("urllib.request.urlopen", _boom):
            p = RG.provenance()
        assert p["reachable"] is False, "un diagnostic ne doit pas coûter une requête utilisateur"

"""Un champ ne change jamais de sens selon l'étape du pipeline.

Le reranking passait l'ordre final en écrasant `score` et `fusion_score` par un ordinal
(`n - i`). C'était efficace et faux : un seuil aval qui lisait `score` comparait alors un
RANG à une PERTINENCE. Le drapeau de confiance faible, qui teste
`best_score < seuil`, ne pouvait plus se déclencher — 40,0 dépasse n'importe quel seuil.

Ces tests verrouillent la séparation : le reranking ENRICHIT (`rerank_score`,
`rerank_rank`), il ne détruit plus les métadonnées du retrieval.
"""
import pytest

from apps.rag_core.services import rerank_gateway as RRG
from apps.rag_core.services.query.diversify import rrf_sort_key


def _chunk(cid, fusion, score, text=0.1, vect=0.5):
    return {"chunk_id": cid, "document_id": "d-%s" % cid, "text": "t",
            "fusion_score": fusion, "score": score,
            "text_score": text, "vector_score": vect}


@pytest.fixture
def pool():
    #: fusion décroissant : c'est l'ordre RRF canonique d'entrée.
    return [_chunk("a", 0.030, 0.89), _chunk("b", 0.020, 0.85), _chunk("c", 0.010, 0.80)]


class TestTheRerankerEnrichesRatherThanOverwrites:
    def test_fusion_score_survives_the_rerank(self, pool):
        """La valeur RRF calculée avant reranking reste lisible après."""
        avant = {c["chunk_id"]: c["fusion_score"] for c in pool}
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        assert {c["chunk_id"]: c["fusion_score"] for c in out} == avant

    def test_score_survives_the_rerank(self, pool):
        """`score` porte la pertinence du retrieval — le reranking n'y touche pas.

        C'est le champ que lit le seuil de confiance faible. L'écraser par un ordinal
        désactivait ce signal en silence."""
        avant = {c["chunk_id"]: c["score"] for c in pool}
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        assert {c["chunk_id"]: c["score"] for c in out} == avant

    def test_lexical_and_dense_scores_survive(self, pool):
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        assert all(c["text_score"] == 0.1 and c["vector_score"] == 0.5 for c in out)

    def test_no_synthetic_ordinal_is_written_anywhere(self, pool):
        """Aucun champ ne doit valoir `n - i`. C'est la régression à empêcher."""
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        n = len(out)
        ordinaux = {float(n - i) for i in range(n)}
        for i, c in enumerate(out):
            assert c["score"] not in ordinaux or c["score"] == pool[0]["score"]
            assert c["fusion_score"] not in ordinaux


class TestTheRerankerMetadata:
    def test_rerank_score_is_the_gateway_value(self, pool):
        """Aucune transformation : la valeur du cross-encoder est transportée telle quelle."""
        scores = {"a": 1.0, "b": 3.0, "c": 2.0}
        out = RRG._apply_order(pool, scores)
        assert {c["chunk_id"]: c["rerank_score"] for c in out} == scores

    def test_rerank_rank_is_one_based_and_follows_the_cross_encoder(self, pool):
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        assert [(c["chunk_id"], c["rerank_rank"]) for c in out] == [("b", 1), ("c", 2), ("a", 3)]

    def test_the_final_order_is_the_cross_encoder_order(self, pool):
        out = RRG._apply_order(pool, {"a": 1.0, "b": 3.0, "c": 2.0})
        assert [c["chunk_id"] for c in out] == ["b", "c", "a"]

    @pytest.mark.parametrize("scores,attendu", [
        ({"a": -1.0, "b": -5.0, "c": -0.5}, ["c", "a", "b"]),   # tous négatifs
        ({"a": 12.0, "b": 3.0, "c": 7.5}, ["a", "c", "b"]),     # bien au-delà de 1
        ({"a": 0.0, "b": 0.0, "c": 0.0}, ["a", "b", "c"]),      # tous égaux
    ])
    def test_unusual_values_are_handled(self, pool, scores, attendu):
        """Un cross-encoder n'est PAS une probabilité : son contrat ne borne pas [0,1].

        Le cas « tous égaux » vérifie aussi le départage : à score identique, l'ordre
        d'entrée — l'ordre RRF canonique — est conservé."""
        out = RRG._apply_order(pool, scores)
        assert [c["chunk_id"] for c in out] == attendu

    def test_the_tie_break_is_deterministic_and_rrf_based(self, pool):
        """Départage stable, reproductible, et sans dépendance à l'ordre d'un dict."""
        premier = [c["chunk_id"] for c in RRG._apply_order(pool, {"a": 2.0, "b": 2.0, "c": 2.0})]
        second = [c["chunk_id"] for c in RRG._apply_order(list(pool), {"c": 2.0, "b": 2.0, "a": 2.0})]
        assert premier == second == ["a", "b", "c"]


class TestTheSortKeyCarriesTheOrder:
    """L'ordre autoritaire du reranking passe par `rerank_rank`, plus par un faux score."""

    def test_a_reranked_candidate_outranks_an_unreranked_one(self):
        """Propriété que l'ancien ordinal garantissait : la tête précède la queue.

        Elle doit tenir même quand le candidat non reranké porte un `fusion_score`
        bien plus élevé — c'est précisément le cas que l'ordinal traitait."""
        tete = {"chunk_id": "h", "fusion_score": 0.001, "score": 0.1, "rerank_rank": 40}
        queue = {"chunk_id": "t", "fusion_score": 0.999, "score": 0.99}
        assert rrf_sort_key(tete) > rrf_sort_key(queue)

    def test_reranked_candidates_order_by_rank(self):
        a = {"chunk_id": "a", "fusion_score": 0.001, "score": 0.1, "rerank_rank": 1}
        b = {"chunk_id": "b", "fusion_score": 0.900, "score": 0.9, "rerank_rank": 2}
        assert rrf_sort_key(a) > rrf_sort_key(b)

    def test_without_rerank_the_historical_key_is_unchanged(self):
        """Sans reranking, le classement reste exactement celui d'avant."""
        a = {"chunk_id": "a", "fusion_score": 0.03, "score": 0.5}
        b = {"chunk_id": "b", "fusion_score": 0.02, "score": 0.9}
        assert rrf_sort_key(a) > rrf_sort_key(b)          # fusion prime
        c = {"chunk_id": "c", "fusion_score": 0.02, "score": 0.95}
        assert rrf_sort_key(c) > rrf_sort_key(b)          # à fusion égale, score départage

    def test_a_malformed_rank_falls_back_instead_of_raising(self):
        c = {"chunk_id": "x", "fusion_score": 0.02, "score": 0.5, "rerank_rank": "??"}
        assert rrf_sort_key(c) == (0.0, 0.02, 0.5)


class TestFailOpen:
    """En repli, le classement RRF canonique revient — et AUCUN faux score n'est fabriqué."""

    @pytest.mark.parametrize("panne", ["timeout", "HTTP 503", "cardinalite 3/4", "reponse sans liste results"])
    def test_the_rrf_order_is_restored_and_no_rerank_field_is_invented(self, settings, monkeypatch, pool, panne):
        settings.RAG_RERANKER_ENABLED = True
        settings.RAG_RERANKER_BASE_URL = "http://gateway.invalide:8200"
        settings.RAG_RERANKER_FAILURE_POLICY = "fail_open_rrf"

        def _boom(*a, **k):
            raise RRG.RerankGatewayError(panne)

        monkeypatch.setattr(RRG, "_post_rerank", _boom)
        out, info = RRG.rerank_chunks(question="q", chunks=list(pool))

        assert info["fallback"] is True and not info.get("success")
        # ordre RRF canonique rendu
        assert [c["chunk_id"] for c in out] == ["a", "b", "c"]
        # fusion_score intact
        assert [c["fusion_score"] for c in out] == [0.030, 0.020, 0.010]
        # aucun champ reranker fabriqué
        assert all("rerank_rank" not in c for c in out)
        assert all(c.get("rerank_score") is None for c in out)

"""Pondération lexicale conditionnelle de la fusion RRF (P1.5).

Pour une requête à identifiant fort (« Series A-1 »), le signal lexical exact est plus
fiable que le vectoriel sémantique. On renforce alors la contribution lexicale de la
fusion — et surtout AVANT la troncature candidate_k, car le chunk réponse de
« Series A-2 » était à un rang de fusion 27 > candidate_k, donc coupé avant toute
diversité. Un reweight postérieur ne l'aurait jamais récupéré.

Sans identifiant : poids (1, 1), fusion historique inchangée, bit pour bit.
"""
from apps.rag_core.services.query.merge import RRF_K, _rrf, _safe_weight, merge_hybrid


def _c(cid, score=0.0):
    return {"chunk_id": cid, "score": score}


class TestSafeWeight:
    def test_a_valid_weight_is_kept(self):
        assert _safe_weight(1.5) == 1.5
        assert _safe_weight("2") == 2.0

    def test_invalid_weights_fall_back_to_neutral(self):
        for bad in (0, -1, float("nan"), float("inf"), float("-inf"), "x", None):
            assert _safe_weight(bad) == 1.0


class TestTheFormula:
    def test_absent_rank_contributes_nothing(self):
        assert _rrf(None, 5.0) == 0.0

    def test_the_weight_scales_the_contribution(self):
        assert _rrf(0, 2.0) == 2.0 / (RRF_K + 0)
        assert _rrf(3, 1.5) == 1.5 / (RRF_K + 3)


class TestBaselineIsUnchanged:
    def test_default_weights_reproduce_the_historic_fusion(self):
        text = [_c("a"), _c("b")]
        vector = [_c("b"), _c("c")]
        out = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=3)
        # b présent dans les deux -> cumule les deux votes -> gagne.
        assert out[0]["chunk_id"] == "b"
        assert out[0]["fusion_score"] == _rrf(2, 1.0) + _rrf(1, 1.0)
        assert out[1]["fusion_score"] == _rrf(1, 1.0)  # 'a', lexical rang 1

    def test_weight_1_1_is_identical_to_no_weight(self):
        text = [_c(x) for x in "abcd"]
        vector = [_c(x) for x in "cdef"]
        base = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=6)
        weighted = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=6,
                                lexical_weight=1.0, vector_weight=1.0)
        assert [c["chunk_id"] for c in base] == [c["chunk_id"] for c in weighted]
        assert [c["fusion_score"] for c in base] == [c["fusion_score"] for c in weighted]


class TestLexicalWeighting:
    def test_a_lexical_only_chunk_is_promoted_by_the_weight(self):
        # 'lex' : lexical rang 3, absent du vectoriel.
        # 'vec' : vectoriel rang 1, absent du lexical.
        text = [_c("x"), _c("y"), _c("lex")]
        vector = [_c("vec"), _c("x"), _c("y")]
        base = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=4)
        boosted = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=4,
                               lexical_weight=2.0)
        # Le poids lexical fait remonter 'lex' au-dessus de 'vec'.
        rank = lambda out, cid: [c["chunk_id"] for c in out].index(cid)
        assert rank(boosted, "lex") < rank(base, "lex")

    def test_a_vector_only_chunk_keeps_its_own_contribution(self):
        text = [_c("lex")]
        vector = [_c("vec")]
        out = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=2,
                           lexical_weight=2.0, vector_weight=1.0)
        vec = next(c for c in out if c["chunk_id"] == "vec")
        assert vec["fusion_score"] == _rrf(1, 1.0)   # poids vectoriel inchangé

    def test_a_bichannel_chunk_sums_weighted_contributions(self):
        text = [_c("both")]
        vector = [_c("both")]
        out = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=1,
                           lexical_weight=1.5, vector_weight=1.0)
        assert out[0]["fusion_score"] == _rrf(1, 1.5) + _rrf(1, 1.0)

    def test_an_invalid_weight_falls_back_to_baseline(self):
        text = [_c(x) for x in "ab"]
        vector = [_c(x) for x in "bc"]
        base = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=3)
        nan = merge_hybrid(text_chunks=text, vector_chunks=vector, top_k=3,
                           lexical_weight=float("nan"))
        assert [c["fusion_score"] for c in base] == [c["fusion_score"] for c in nan]

    def test_ties_stay_deterministic(self):
        # Deux chunks lexicaux au même rang relatif : l'ordre d'entrée est préservé.
        text = [_c("x"), _c("y")]
        out = merge_hybrid(text_chunks=text, vector_chunks=[], top_k=2, lexical_weight=2.0)
        assert [c["chunk_id"] for c in out] == ["x", "y"]

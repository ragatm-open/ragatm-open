"""`dedupe_preserving_order` — le nettoyage qui ne classe pas.

Module pur (aucune base) : on teste le CONTRAT. Son application au pipeline réel est
vérifiée par `tests/tests_root/integration/test_rerank_order_preserved.py`.

La distinction que ces tests verrouillent : **nettoyer ≠ classer**. `clean_candidates`
fait les deux, ce qui le rend inutilisable après une étape dont l'ordre fait autorité.
"""
import pytest

from apps.rag_core.services.query.hygiene import (
    clean_candidates,
    dedupe_preserving_order,
)


def _c(cid, *, score=0.5, rerank=None, text="du texte"):
    d = {"chunk_id": cid, "text": text, "score": score}
    if rerank is not None:
        d["rerank_score"] = rerank
    return d


class TestOrderIsPreserved:
    def test_input_order_is_the_output_order(self):
        """Même quand il contredit frontalement le score de retrieval."""
        out = dedupe_preserving_order([_c("B", score=0.1), _c("A", score=0.9), _c("C", score=0.5)])
        assert [c["chunk_id"] for c in out] == ["B", "A", "C"]

    def test_never_sorts_by_score(self):
        out = dedupe_preserving_order([_c("low", score=0.01), _c("high", score=0.99)])
        assert [c["chunk_id"] for c in out] == ["low", "high"]

    def test_clean_candidates_does_sort_the_contrast(self):
        """La raison d'être de la nouvelle fonction : l'ancienne réordonne."""
        cands = [_c("low", score=0.01), _c("high", score=0.99)]
        assert [c["chunk_id"] for c in clean_candidates(cands, min_score=0.0)] == ["high", "low"]
        assert [c["chunk_id"] for c in dedupe_preserving_order(cands)] == ["low", "high"]


class TestFilteringIsStable:
    def test_invalid_removed_survivors_keep_their_order(self):
        """[B, A, C] avec A invalide -> [B, C], jamais [C, B]."""
        out = dedupe_preserving_order([
            _c("B"), {"text": "sans chunk_id", "score": 1.0}, _c("C"),
        ])
        assert [c["chunk_id"] for c in out] == ["B", "C"]

    def test_non_dict_entries_are_dropped(self):
        out = dedupe_preserving_order([_c("B"), "pas un dict", None, 42, _c("C")])
        assert [c["chunk_id"] for c in out] == ["B", "C"]

    def test_empty_chunk_id_is_invalid(self):
        out = dedupe_preserving_order([_c("B"), _c(""), _c(None), _c("C")])
        assert [c["chunk_id"] for c in out] == ["B", "C"]

    def test_accepts_id_as_an_alias(self):
        """`clean_candidates` tolère `id` : le contrat ne doit pas diverger."""
        out = dedupe_preserving_order([{"id": "X", "text": "t", "score": 1.0}])
        assert len(out) == 1


class TestNoScoreFilter:
    def test_a_tiny_ts_rank_is_never_wiped(self):
        """LE second défaut : `clean_candidates` sans `min_score` comparait un ts_rank
        (~0,06) au seuil VECTORIEL (0,76) et anéantissait la liste entière."""
        out = dedupe_preserving_order([_c("a", score=0.0669), _c("b", score=0.0952)])
        assert len(out) == 2

    def test_zero_score_survives(self):
        assert len(dedupe_preserving_order([_c("a", score=0.0)])) == 1

    def test_missing_score_survives(self):
        assert len(dedupe_preserving_order([{"chunk_id": "a", "text": "t"}])) == 1


class TestDeduplication:
    def test_first_occurrence_wins(self):
        """Dans une liste rerankée, la première EST la meilleure selon le reranker."""
        out = dedupe_preserving_order([
            _c("B", rerank=9.0), _c("A", rerank=5.0), _c("B", rerank=-1.0),
        ])
        assert [c["chunk_id"] for c in out] == ["B", "A"]
        assert out[0]["rerank_score"] == 9.0, "le doublon moins bon a écrasé le meilleur"

    def test_deduplication_does_not_reorder(self):
        out = dedupe_preserving_order([_c("B"), _c("A"), _c("B"), _c("C")])
        assert [c["chunk_id"] for c in out] == ["B", "A", "C"]

    def test_id_is_compared_as_a_string(self):
        out = dedupe_preserving_order([
            {"chunk_id": 7, "text": "t"}, {"chunk_id": "7", "text": "t"},
        ])
        assert len(out) == 1


class TestTextNormalisation:
    def test_text_is_stripped(self):
        out = dedupe_preserving_order([_c("a", text="  du texte  ")])
        assert out[0]["text"] == "du texte"

    def test_non_string_text_is_coerced(self):
        out = dedupe_preserving_order([{"chunk_id": "a", "text": 123}])
        assert out[0]["text"] == "123"

    def test_missing_text_becomes_empty_string(self):
        out = dedupe_preserving_order([{"chunk_id": "a"}])
        assert out[0]["text"] == ""


class TestScoresArePreserved:
    def test_both_scores_survive_distinctly(self):
        """Le score retrieval ne doit jamais être écrasé par le rerank_score."""
        out = dedupe_preserving_order([_c("a", score=0.06, rerank=9.0)])
        assert out[0]["score"] == 0.06
        assert out[0]["rerank_score"] == 9.0

    def test_fusion_score_survives(self):
        c = {"chunk_id": "a", "text": "t", "score": 0.5, "fusion_score": 0.03,
             "rerank_score": 1.0}
        assert dedupe_preserving_order([c])[0]["fusion_score"] == 0.03


class TestDegenerate:
    @pytest.mark.parametrize("v", [None, [], [None], ["x"], [{}]])
    def test_never_raises(self, v):
        assert isinstance(dedupe_preserving_order(v), list)

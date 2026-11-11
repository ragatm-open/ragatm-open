"""Diversification en deux passes du pool de candidats.

Après le découpage des feuilles larges, le bon chunk d'un classeur passe du rang lexical
52 au rang 11 — mais le retrieval tronquait à top_k=8 AVANT toute diversification. Un
chunk au rang 11 ne pouvait donc jamais entrer, même quand plusieurs chunks d'une MÊME
feuille occupaient les huit premières places. On élargit le pool, on diversifie, puis on
tronque.
"""
from apps.rag_core.services.query.diversify import (
    build_diversity_group,
    diversify_by_document,
    diversify_ranked_results,
)


def _c(cid, *, doc="d1", sheet=None, group=None, score=0.0, fusion=None):
    meta = {}
    # `column_group` ne discrimine que si une feuille existe (cf. build_diversity_group) :
    # un groupe de colonnes n'a de sens qu'au sein d'une feuille.
    if group is not None and sheet is None:
        sheet = "S"
    if sheet is not None:
        meta["sheet_name"] = sheet
    if group is not None:
        meta["column_group"] = group
    out = {"chunk_id": cid, "score": score,
           "source": {"document_id": doc, "meta": meta}}
    if fusion is not None:
        out["fusion_score"] = fusion
    return out


def _ids(chunks):
    return [c["chunk_id"] for c in chunks]


class TestTheGroupKey:
    def test_excel_key_is_document_sheet_group(self):
        a = _c("1", doc="D", sheet="Detailed", group="Premoney")
        b = _c("2", doc="D", sheet="Detailed", group="AntiDil")
        c = _c("3", doc="D", sheet="Summary", group="Premoney")
        assert build_diversity_group(a) != build_diversity_group(b)
        assert build_diversity_group(a) != build_diversity_group(c)

    def test_falls_back_to_document_sheet_then_document(self):
        no_group = _c("1", doc="D", sheet="S")
        no_sheet = _c("2", doc="D")
        assert build_diversity_group(no_group) == build_diversity_group(_c("9", doc="D", sheet="S"))
        assert build_diversity_group(no_sheet) == "d"

    def test_the_pdf_page_is_never_the_group(self):
        """Chaque page deviendrait un groupe : un PDF long raflerait la diversité."""
        p1 = {"chunk_id": "1", "source": {"document_id": "D", "page_number": 1, "meta": {}}}
        p2 = {"chunk_id": "2", "source": {"document_id": "D", "page_number": 2, "meta": {}}}
        assert build_diversity_group(p1) == build_diversity_group(p2) == "d"

    def test_trivial_variations_do_not_split_a_group(self):
        a = _c("1", doc="D", sheet="Summary", group="Premoney")
        b = _c("2", doc="D", sheet=" summary ", group="PREMONEY")
        assert build_diversity_group(a) == build_diversity_group(b)


class TestTwoPassSelection:
    def test_a_dominant_group_is_capped_then_others_fill(self):
        pool = ([_c(f"a{i}", group="A", fusion=1.0 - i * 0.01) for i in range(6)]
                + [_c("b", group="B", fusion=0.90)]
                + [_c("c", group="C", fusion=0.80)])
        sel = diversify_ranked_results(pool, top_k=5, max_per_group=2)
        # A capé à 2, puis B, C, puis complétion par le meilleur A restant.
        assert _ids(sel) == ["a0", "a1", "b", "c", "a2"]

    def test_first_pass_alone_fills_the_top_k(self):
        pool = [_c("a", group="A", fusion=0.9), _c("b", group="B", fusion=0.8),
                _c("c", group="C", fusion=0.7)]
        assert _ids(diversify_ranked_results(pool, top_k=3, max_per_group=2)) == ["a", "b", "c"]

    def test_second_pass_ignores_the_cap_to_reach_top_k(self):
        """Pas assez de groupes distincts : le plafond est SOUPLE, pas un mur."""
        pool = [_c("a0", group="A", fusion=0.9), _c("a1", group="A", fusion=0.8),
                _c("a2", group="A", fusion=0.7), _c("a3", group="A", fusion=0.6)]
        assert _ids(diversify_ranked_results(pool, top_k=3, max_per_group=1)) == ["a0", "a1", "a2"]

    def test_no_chunk_appears_twice(self):
        pool = [_c("a0", group="A", fusion=0.9), _c("a1", group="A", fusion=0.8),
                _c("b", group="B", fusion=0.7)]
        sel = diversify_ranked_results(pool, top_k=3, max_per_group=1)
        assert len(sel) == len(set(_ids(sel)))

    def test_the_rrf_order_is_restored_even_if_input_is_score_sorted(self):
        """L'hygiène re-trie par `score` brut (échelles incomparables). La diversité
        doit rétablir l'ordre RRF, sinon un chunk vectoriel écraserait le lexical."""
        # Entrée triée par SCORE (le vectoriel devant), mais fusion_score dit l'inverse.
        pool = [_c("vec", group="V", score=0.75, fusion=0.010),
                _c("lex", group="L", score=0.26, fusion=0.016)]
        sel = diversify_ranked_results(pool, top_k=2, max_per_group=2)
        assert _ids(sel) == ["lex", "vec"]     # RRF gagne, pas le score brut

    def test_stable_order_on_equal_rank(self):
        pool = [_c("x", group="A", fusion=0.5), _c("y", group="B", fusion=0.5)]
        assert _ids(diversify_ranked_results(pool, top_k=2, max_per_group=1)) == ["x", "y"]

    def test_max_per_group_zero_is_a_plain_truncation(self):
        pool = [_c("a", group="A", fusion=0.9), _c("b", group="A", fusion=0.8)]
        assert _ids(diversify_ranked_results(pool, top_k=1, max_per_group=0)) == ["a"]


class TestInventoryUnchanged:
    def test_one_chunk_per_document_then_fill(self):
        pool = [_c("d1a", doc="D1", fusion=0.9), _c("d1b", doc="D1", fusion=0.85),
                _c("d2", doc="D2", fusion=0.8), _c("d3", doc="D3", fusion=0.7)]
        sel = diversify_by_document(pool, top_k=3, min_unique_docs=3)
        assert _ids(sel) == ["d1a", "d2", "d3"]

    def test_fills_to_top_k_when_few_documents(self):
        pool = [_c("d1a", doc="D1", fusion=0.9), _c("d1b", doc="D1", fusion=0.8),
                _c("d2", doc="D2", fusion=0.7)]
        sel = diversify_by_document(pool, top_k=3, min_unique_docs=5)
        assert set(_ids(sel)) == {"d1a", "d2", "d1b"}
        assert len(sel) == 3

"""Ordre total du classement — la règle de départage, testée en isolation.

Module pur (aucune base) : on vérifie ici la RÈGLE. Son application au chemin de
production réel est vérifiée par `tests/tests_root/integration/test_retrieval_determinism.py`.
"""

from apps.rag_core.domain.ranking import (
    SQL_TIE_BREAK,
    sort_candidates,
    tie_break_key,
)


def _c(chunk_id, *, doc="d1", idx=0, score=1.0, **extra):
    return {
        "chunk_id": chunk_id,
        "chunk_index": idx,
        "score": score,
        "source": {"document_id": doc},
        **extra,
    }


class TestTieBreakKey:
    def test_documentary_order_comes_first(self):
        """À pertinence égale, on lit un document dans l'ordre."""
        assert tie_break_key(_c("z", doc="d1", idx=0)) < tie_break_key(_c("a", doc="d1", idx=1))

    def test_document_id_outranks_chunk_index(self):
        assert tie_break_key(_c("a", doc="d1", idx=99)) < tie_break_key(_c("b", doc="d2", idx=0))

    def test_chunk_id_closes_the_total_order(self):
        """`(document_id, chunk_index)` est unique en pratique, mais aucune contrainte
        de base ne l'impose : sans cette clé, l'ordre ne serait total que par convention."""
        a = tie_break_key(_c("aaa", doc="d1", idx=0))
        b = tie_break_key(_c("bbb", doc="d1", idx=0))
        assert a < b
        assert a != b

    def test_reads_document_id_from_source(self):
        assert tie_break_key({"chunk_id": "x", "source": {"document_id": "d9"}})[0] == "d9"

    def test_falls_back_to_root_document_id(self):
        assert tie_break_key({"chunk_id": "x", "document_id": "d9"})[0] == "d9"

    def test_missing_chunk_index_does_not_fake_a_head_position(self):
        """Un candidat sans position ne doit pas passer devant le premier chunk d'un
        document. Il retombe sur `chunk_id`, qui reste total."""
        with_idx = tie_break_key(_c("a", doc="d1", idx=0))
        without = tie_break_key({"chunk_id": "a", "source": {"document_id": "d1"}})
        assert without > with_idx

    def test_boolean_is_not_an_index(self):
        """En Python `True` est un `int` : sans garde, il vaudrait la position 1."""
        k = tie_break_key({"chunk_id": "a", "chunk_index": True, "source": {"document_id": "d"}})
        assert k[1] != 1

    def test_never_raises_on_garbage(self):
        for junk in ({}, {"chunk_id": None}, {"source": "pas un dict"},
                     {"chunk_index": "trois"}, {"source": {"document_id": None}}):
            assert isinstance(tie_break_key(junk), tuple)


class TestSortCandidates:
    def test_relevance_still_decides_first(self):
        """Le correctif ne change PAS la pertinence : elle décide seule tant qu'elle
        discrimine."""
        out = sort_candidates(
            [_c("a", score=0.1, doc="d1", idx=0), _c("b", score=0.9, doc="d9", idx=99)],
            relevance_key=lambda x: float(x["score"]),
        )
        assert [c["chunk_id"] for c in out] == ["b", "a"]

    def test_ties_are_broken_in_documentary_order(self):
        out = sort_candidates(
            [_c("c", score=0.5, doc="d1", idx=2),
             _c("a", score=0.5, doc="d1", idx=0),
             _c("b", score=0.5, doc="d1", idx=1)],
            relevance_key=lambda x: float(x["score"]),
        )
        assert [c["chunk_index"] for c in out] == [0, 1, 2]

    def test_tie_break_is_ascending_even_though_relevance_is_descending(self):
        """Le piège que `sort_candidates` existe pour éviter : un `sorted(reverse=True)`
        unique inverserait aussi le départage, et ferait remonter le DERNIER chunk d'un
        document avant le premier."""
        out = sort_candidates(
            [_c("a", score=1.0, doc="d1", idx=5), _c("b", score=1.0, doc="d1", idx=1)],
            relevance_key=lambda x: float(x["score"]),
        )
        assert [c["chunk_index"] for c in out] == [1, 5]

    def test_tuple_relevance_key_is_supported(self):
        out = sort_candidates(
            [_c("a", fusion_score=0.1, score=0.9, doc="d1", idx=0),
             _c("b", fusion_score=0.2, score=0.1, doc="d1", idx=1)],
            relevance_key=lambda x: (float(x.get("fusion_score") or 0), float(x["score"])),
        )
        assert [c["chunk_id"] for c in out] == ["b", "a"]

    def test_input_order_never_influences_the_output(self):
        """LE test : la même entrée, dans n'importe quel ordre, sort identique."""
        import itertools

        base = [_c("a", score=0.5, doc="d1", idx=0),
                _c("b", score=0.5, doc="d1", idx=1),
                _c("c", score=0.5, doc="d2", idx=0)]
        expected = [c["chunk_id"] for c in
                    sort_candidates(base, relevance_key=lambda x: float(x["score"]))]
        for perm in itertools.permutations(base):
            got = [c["chunk_id"] for c in
                   sort_candidates(list(perm), relevance_key=lambda x: float(x["score"]))]
            assert got == expected

    def test_empty_list(self):
        assert sort_candidates([], relevance_key=lambda x: 0.0) == []


class TestSqlFragment:
    def test_ends_with_a_unique_column(self):
        """Le fragment DOIT se terminer par une colonne unique, sinon l'ordre n'est pas
        total et le correctif ne corrige rien."""
        assert SQL_TIE_BREAK.strip().endswith("c.id ASC")

    def test_documentary_order_precedes_the_unique_id(self):
        assert SQL_TIE_BREAK.index("c.document_id") < SQL_TIE_BREAK.index("c.chunk_index")
        assert SQL_TIE_BREAK.index("c.chunk_index") < SQL_TIE_BREAK.index("c.id ASC")

    def test_carries_no_parameter(self):
        """Concaténé dans une f-string SQL : aucun placeholder, aucune donnée externe."""
        assert "%" not in SQL_TIE_BREAK
        assert "{" not in SQL_TIE_BREAK

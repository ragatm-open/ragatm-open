"""Tests de keywordize (pur) et build_queries_for_search (LLM mocké)."""
import apps.rag_core.services.query.rewrite as rewrite_mod
from apps.rag_core.services.query.rewrite import build_queries_for_search, keywordize_query


class TestKeywordize:
    def test_empty(self):
        assert keywordize_query("") == ""

    def test_drops_stopwords_and_short_tokens(self):
        assert keywordize_query("Explique-moi la capitale de la France") == "capitale france"

    def test_lowercased(self):
        assert keywordize_query("PARIS FRANCE") == "paris france"


class TestBuildQueries:
    def test_monomode_returns_only_question(self):
        out = build_queries_for_search(question="Quelle capitale ?", explanatory_mode=False)
        assert out == ["Quelle capitale ?"]

    def test_empty_question_returns_empty(self):
        assert build_queries_for_search(question="", explanatory_mode=True) == []

    def test_explanatory_mode_dedups_and_limits(self, monkeypatch):
        # mock le rewrite LLM pour qu'il soit déterministe
        monkeypatch.setattr(rewrite_mod, "rewrite_query_with_llm", lambda q: "")
        out = build_queries_for_search(question="Explique la capitale", explanatory_mode=True)
        assert out[0] == "Explique la capitale"
        assert len(out) == len(set(x.lower() for x in out))  # pas de doublon
        assert len(out) <= 3

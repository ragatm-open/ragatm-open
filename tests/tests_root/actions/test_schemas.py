"""Validation stricte des paramètres (audit §16)."""
from __future__ import annotations

import pytest
from rest_framework.exceptions import ValidationError

from apps.actions.schemas import QUESTION_MAX_CHARS, validate_action_input

_RS = "00000000-0000-0000-0000-000000000001"


def test_question_max_length_enforced():
    ok = validate_action_input("search.answer", {"rag_space_id": _RS, "question": "x" * 100})
    assert ok["question"] == "x" * 100
    with pytest.raises(ValidationError):
        validate_action_input(
            "search.answer", {"rag_space_id": _RS, "question": "x" * (QUESTION_MAX_CHARS + 1)}
        )


def test_question_required_non_blank():
    with pytest.raises(ValidationError):
        validate_action_input("search.answer", {"rag_space_id": _RS, "question": "   "})


def test_news_clamps_bounds():
    with pytest.raises(ValidationError):
        validate_action_input(
            "search.news", {"rag_space_id": _RS, "term": "Nordexia", "time_window_days": 5}
        )
    with pytest.raises(ValidationError):
        validate_action_input(
            "search.news", {"rag_space_id": _RS, "term": "Nordexia", "top_n_docs": 99}
        )
    ok = validate_action_input("search.news", {"rag_space_id": _RS, "term": "Nordexia"})
    assert ok["time_window_days"] == 120 and ok["top_n_docs"] == 5


def test_list_documents_official_fields():
    ok = validate_action_input(
        "search.list_documents",
        {"rag_space_id": _RS, "term": "Nordexia", "fts": "true", "lang": "french", "limit": 50},
    )
    assert ok["fts"] is True and ok["lang"] == "french" and ok["limit"] == 50
    with pytest.raises(ValidationError):  # lang hors liste
        validate_action_input(
            "search.list_documents", {"rag_space_id": _RS, "term": "Nordexia", "lang": "klingon"}
        )
    with pytest.raises(ValidationError):  # limit > 1000
        validate_action_input(
            "search.list_documents", {"rag_space_id": _RS, "term": "Nordexia", "limit": 99999}
        )


def test_summarize_requires_a_target():
    with pytest.raises(ValidationError):
        validate_action_input("document.summarize", {"rag_space_id": _RS})
    ok = validate_action_input(
        "document.summarize", {"rag_space_id": _RS, "document_reference": "Rapport.pdf"}
    )
    assert ok["scope"] == "document"
    with pytest.raises(ValidationError):  # scope=page sans page
        validate_action_input(
            "document.summarize",
            {"rag_space_id": _RS, "document_id": _RS, "scope": "page"},
        )

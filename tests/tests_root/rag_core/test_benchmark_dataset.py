"""Tests unitaires du chargement/validation du dataset benchmark (sans DB)."""
import json

import pytest

from apps.rag_core.benchmark.dataset import DatasetError, parse_case, parse_jsonl


def _line(**kw):
    base = {
        "id": "q1",
        "rag_space": "demo-fr",
        "question": "Une question ?",
        "should_answer": True,
        "expected_documents": ["doc.pdf"],
    }
    base.update(kw)
    return json.dumps(base)


def test_parse_minimal_positive_case():
    c = parse_case(json.loads(_line()))
    assert c.id == "q1"
    assert c.rag_space == "demo-fr"
    assert c.expected_documents == ("doc.pdf",)
    assert c.must_cite is True  # défaut = should_answer
    assert c.is_no_answer is False


def test_no_answer_case_needs_no_expected_docs():
    c = parse_case(
        json.loads(
            json.dumps(
                {
                    "id": "q99",
                    "rag_space": "demo-fr",
                    "question": "?",
                    "should_answer": False,
                }
            )
        )
    )
    assert c.is_no_answer is True
    assert c.must_cite is False


def test_positive_without_truth_is_rejected():
    with pytest.raises(DatasetError):
        parse_case(
            {
                "id": "q1",
                "rag_space": "demo-fr",
                "question": "?",
                "should_answer": True,
                "expected_documents": [],
            }
        )


@pytest.mark.parametrize("missing", ["id", "rag_space", "question", "should_answer"])
def test_required_fields(missing):
    obj = json.loads(_line())
    obj.pop(missing)
    with pytest.raises(DatasetError):
        parse_case(obj)


def test_should_answer_must_be_bool():
    obj = json.loads(_line(should_answer="yes"))
    with pytest.raises(DatasetError):
        parse_case(obj)


def test_parse_jsonl_ignores_comments_and_blanks():
    lines = [
        "# commentaire",
        "",
        _line(id="q1"),
        "   ",
        _line(id="q2"),
    ]
    cases = parse_jsonl(lines)
    assert [c.id for c in cases] == ["q1", "q2"]


def test_parse_jsonl_rejects_duplicate_ids():
    with pytest.raises(DatasetError):
        parse_jsonl([_line(id="dup"), _line(id="dup")])


def test_parse_jsonl_empty_dataset_raises():
    with pytest.raises(DatasetError):
        parse_jsonl(["# only comment"])


def test_invalid_json_line_raises():
    with pytest.raises(DatasetError):
        parse_jsonl(["{not json"])

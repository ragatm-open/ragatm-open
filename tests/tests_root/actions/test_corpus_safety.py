"""Le corpus d'évaluation ne doit JAMAIS produire une action interdite (§18)."""
from __future__ import annotations

import json
import pathlib

import pytest

from apps.actions.router import ActionRouter

_FIX = (
    pathlib.Path(__file__).resolve().parents[3]
    / "shared" / "contracts" / "action_intents" / "classification_corpus.json"
)
_CASES = json.loads(_FIX.read_text(encoding="utf-8"))["cases"]
_router = ActionRouter()  # déterministe


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_never_predicts_forbidden_action(case):
    pred = _router.route(case["text"]).action
    assert pred not in set(case.get("must_not_execute") or []), f"{case['id']} -> {pred}"
    # jamais une mutation, quelle qu'elle soit
    assert pred in {"search.answer", "search.list_documents", "search.news", "document.summarize"}


def test_deterministic_baseline_reasonable():
    correct = 0
    for c in _CASES:
        pred = _router.route(c["text"]).action
        if pred == c["expected_intent"] or pred in set(c.get("allowed_alternatives") or []):
            correct += 1
    # baseline déterministe (classifieur off) : doit être correcte sur la majorité.
    assert correct / len(_CASES) >= 0.85

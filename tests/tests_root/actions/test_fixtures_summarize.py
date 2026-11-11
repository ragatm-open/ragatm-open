"""Corpus commun front/back (shared/contracts/action_intents/summarize.json).

Côté backend : le routeur (avec garde-fous) doit décider ``backend_action``.
Le pendant TS (tests/frontend/lib/action_intents.test.ts) vérifie ``frontend_is_summary``.
Les divergences front/back sont encodées dans le fichier et donc documentées.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from apps.actions.router import ActionRouter

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "shared" / "contracts" / "action_intents" / "summarize.json"
)
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
_router = ActionRouter()


@pytest.mark.parametrize("case", _CASES, ids=[c["text"] for c in _CASES])
def test_backend_router_matches_fixture(case):
    assert _router.route(case["text"]).action == case["backend_action"], case["note"]


def test_positives_agree_both_sides():
    for c in _CASES:
        if c["category"] == "positive":
            assert c["frontend_is_summary"] is True
            assert c["backend_action"] == "document.summarize"


def test_negatives_are_backend_answer_documented_divergence():
    # Les négations sont sur-déclenchées par le frontend (sans garde) mais
    # rétrogradées par le backend -> divergence explicitement documentée.
    for c in _CASES:
        if c["category"] == "negative":
            assert c["backend_action"] == "search.answer"
            assert "DIVERGENCE" in c["note"]

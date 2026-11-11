"""Tests négatifs / ambiguïtés (audit §12, §24).

Aucune de ces phrases ne doit produire une action autre que search.answer.
"""
from __future__ import annotations

import pytest

from apps.actions.router import ActionRouter

router = ActionRouter()


@pytest.mark.parametrize(
    "text",
    [
        "Explique comment lister les documents.",       # explicatif
        "Comment résumer un document ?",                # explicatif -> pas de résumé
        "Que signifie résumer un document ?",           # explicatif
        "Je ne veux pas la liste des documents.",       # négation
        "Ne résume pas ce fichier.",                    # négation
        "Ne supprime surtout pas ce document.",         # négation (et delete non exposé)
        'Le document contient « liste des articles ».', # citation
        'Il a écrit "résume le rapport" dans sa note.', # citation
    ],
)
def test_no_action_triggered(text):
    plan = router.route(text)
    assert plan.action == "search.answer", f"{text!r} -> {plan.action}"


def test_suppressed_reason_is_recorded():
    plan = router.route("Comment résumer un document ?")
    assert plan.parameters.get("reason") == "explanatory"
    assert plan.parameters.get("suppressed_from") == "document.summarize"


def test_empty_input_is_answer():
    assert router.route("").action == "search.answer"
    assert router.route("   ").action == "search.answer"

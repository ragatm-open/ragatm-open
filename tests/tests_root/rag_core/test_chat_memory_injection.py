"""Tests de l'injection de la mémoire de conversation dans le prompt RAG.

Régression : la mémoire injectée EN TÊTE de la question diluait l'ancrage aux
sources et provoquait des refus abusifs (« Je ne sais pas. ») sur les modèles
stricts (mistral), malgré des sources pertinentes. Le helper place désormais la
mémoire APRÈS la question, cadrée pour la seule résolution de références.
"""
from __future__ import annotations

from apps.rag_core.services.query.prompt_builder import _inject_conversation_context

_Q = "Question: décris moi les activités de Valteria"
_CC = "utilisateur: explique Tessaly\nassistant: Je ne sais pas.\n"


def test_empty_context_unchanged():
    assert _inject_conversation_context(_Q, None) == _Q
    assert _inject_conversation_context(_Q, "   ") == _Q


def test_memory_placed_after_question():
    out = _inject_conversation_context(_Q, _CC)
    # la question courante vient AVANT la mémoire (plus en tête)
    assert out.index(_Q) < out.index("Contexte de conversation antérieure")
    assert out.index("Valteria") < out.index("Tessaly")


def test_scoping_instruction_present():
    out = _inject_conversation_context(_Q, _CC)
    low = out.lower()
    assert "uniquement pour résoudre les références" in low
    assert "à partir des sources" in low
    assert "ne réponds pas à ces échanges" in low
    # la mémoire brute reste présente (résolution de références possible)
    assert _CC.strip() in out


def test_question_not_duplicated():
    out = _inject_conversation_context(_Q, _CC)
    assert out.count(_Q) == 1

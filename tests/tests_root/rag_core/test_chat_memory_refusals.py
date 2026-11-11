"""Tests : exclusion des refus (« Je ne sais pas ») de la mémoire de conversation.

Un refus réinjecté dans le prompt pousse le modèle à répéter le refus (anchoring),
verrouillant le thread malgré un retrieval pertinent. La fenêtre récente ET le
transcript de résumé doivent donc écarter ces tours.
"""
from __future__ import annotations

import pytest

from apps.chat.services.chat_threads import _is_refusal_answer, append_message, get_thread_memory_context


def test_is_refusal_answer():
    assert _is_refusal_answer("Je ne sais pas.")
    assert _is_refusal_answer("je ne sais pas\n\nSources: [1], [2]")
    assert _is_refusal_answer("  Je ne sais pas  ")
    assert not _is_refusal_answer("Valteria est une coopérative française.")
    assert not _is_refusal_answer("")


@pytest.mark.django_db
def test_memory_window_excludes_refusals(user, rag_space):
    from apps.chat.models import ChatThread

    thread = ChatThread.objects.create(user=user, rag_space=rag_space)
    append_message(thread=thread, role="user", content="explique moi Valteria")
    append_message(thread=thread, role="assistant", content="Je ne sais pas.\n\nSources: [1], [2]")
    append_message(thread=thread, role="user", content="et ses activités ?")
    append_message(thread=thread, role="assistant", content="Valteria est active dans la gestion de flottes.")

    ctx = get_thread_memory_context(thread=thread)
    # le refus est écarté ; la vraie réponse et les questions utilisateur restent
    assert "Je ne sais pas" not in ctx
    assert "gestion de flottes" in ctx
    assert "explique moi Valteria" in ctx
    assert "et ses activités ?" in ctx

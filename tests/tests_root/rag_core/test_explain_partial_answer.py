"""Test du preamble « explain » : réponse partielle ancrée aux sources.

Le preamble doit autoriser une réponse PARTIELLE (au lieu d'un refus abusif) tout
en conservant les garde-fous anti-hallucination.
"""
from __future__ import annotations

from apps.rag_core.services.query.prompt_builder import DEFAULT_EXPLAIN_PREAMBLE


def test_preamble_allows_partial_answer():
    low = DEFAULT_EXPLAIN_PREAMBLE.lower()
    assert "partiellement" in low
    assert "dans les sources" in low
    assert "ce qui manque" in low


def test_preamble_keeps_anti_hallucination_guards():
    low = DEFAULT_EXPLAIN_PREAMBLE.lower()
    assert "n'invente rien" in low
    assert "connaissances générales hors sources" in low
    # refus réservé au cas où AUCUNE source n'est pertinente
    assert "aucune source" in low


def test_preamble_keeps_structure():
    for section in ("Contexte", "Points clés", "Impacts", "Synthèse"):
        assert section in DEFAULT_EXPLAIN_PREAMBLE

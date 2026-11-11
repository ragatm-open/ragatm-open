"""Lot 3 — respect d'une demande explicite de format tableau.

Couvre la détection de préférence (B1), l'adaptation du prompt news (B2/B3),
le prompt générique (B4) et la conservation de la limite historique (B5).
"""

from __future__ import annotations

import pytest

from apps.chat.services.format_prefs import escape_table_cell, wants_table_format
from apps.chat.services.intents import news as news_intent
from apps.chat.services.prompt_templates import DEFAULT_PROMPT_TEMPLATE_CONTENT

pytestmark = pytest.mark.django_db


FACTS = [
    {
        "rank": 1,
        "term": "unicredit",
        "document_date": "2026-06-12",
        "filename": "rapport|annuel.pdf",
        "page_number": 24,
        "snippet": "UniCredit annonce un résultat en hausse.",
    }
]


# ---------------------------------------------------------------- B1

@pytest.mark.parametrize(
    "question",
    [
        "affiche les news unicredit sous forme de tableau",
        "en tableau",
        "présente en tableau les actualités",
        "affiche sous forme de tableau",
        "show the news as a table",
        "give me this in a table",
        "use a tabular format",
        "table format please",
    ],
)
def test_b1_detects_explicit_table_request(question):
    assert wants_table_format(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "donne-moi les actualités",
        "donne-moi les news unicredit",
        "table des matières du document",
        "quelles sont les dernières nouvelles ?",
        "",
    ],
)
def test_b1_avoids_false_positives(question):
    assert wants_table_format(question) is False


def test_b1_escape_table_cell():
    assert escape_table_cell("A | B") == r"A \| B"
    assert escape_table_cell("") == ""


# ---------------------------------------------------------------- B2 / B3 / B5

def _capture_prompt(monkeypatch) -> dict:
    captured: dict = {}

    def fake_generate_text(*, prompt, provider, model, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"text": "ok"}

    monkeypatch.setattr(news_intent, "generate_text", fake_generate_text)
    return captured


def test_b2_news_prompt_requests_a_valid_gfm_table(monkeypatch):
    captured = _capture_prompt(monkeypatch)

    news_intent.summarize_news_with_llm(
        question="affiche les news unicredit sous forme de tableau",
        term="unicredit",
        facts=FACTS,
        llm_provider="stub",
        llm_model="stub",
    )

    prompt = captured["prompt"]
    assert "tableau Markdown GFM valide" in prompt
    assert "| Date | Résumé | Source |" in prompt
    assert "Ligne de séparation obligatoire" in prompt
    assert r"'\|'" in prompt  # consigne d'échappement des pipes
    assert "Ordre chronologique inverse" in prompt

    # Le format historique ne doit pas être imposé en même temps.
    assert "5 puces maximum" not in prompt

    # B5 — la limite historique de 5 éléments est conservée en mode tableau.
    assert "5 lignes maximum" in prompt

    # Les valeurs injectées sont pré-échappées : le pipe du nom de fichier
    # ne peut pas décaler une colonne.
    assert r"rapport\\|annuel.pdf" in prompt or r"rapport\|annuel.pdf" in prompt


def test_b3_news_prompt_keeps_bullets_without_explicit_request(monkeypatch):
    captured = _capture_prompt(monkeypatch)

    news_intent.summarize_news_with_llm(
        question="donne-moi les news unicredit",
        term="unicredit",
        facts=FACTS,
        llm_provider="stub",
        llm_model="stub",
    )

    prompt = captured["prompt"]
    # B5 — la limite historique de 5 éléments reste conservée en mode puces.
    assert "5 puces maximum" in prompt
    assert "tableau Markdown GFM valide" not in prompt
    assert "| Date | Résumé | Source |" not in prompt


def test_b3_news_without_facts_is_unchanged(monkeypatch):
    _capture_prompt(monkeypatch)
    out = news_intent.summarize_news_with_llm(
        question="affiche les news unicredit sous forme de tableau",
        term="unicredit",
        facts=[],
        llm_provider="stub",
        llm_model="stub",
    )
    assert "aucune information" in out.lower()


# ---------------------------------------------------------------- B4

def test_b4_generic_prompt_mentions_table_only_as_conditional():
    content = DEFAULT_PROMPT_TEMPLATE_CONTENT

    assert "Si l'utilisateur demande explicitement un tableau" in content
    assert "tableau Markdown GFM valide" in content

    # La consigne reste conditionnelle : les autres réponses gardent leur format.
    assert "Réponse claire et concise." in content
    assert "Quand c'est utile, structure en puces." in content

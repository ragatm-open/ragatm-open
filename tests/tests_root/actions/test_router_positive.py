"""Corpus positif par intention (audit §23) via le routeur central."""
from __future__ import annotations

import pytest

from apps.actions.router import ActionRouter

router = ActionRouter()  # déterministe pur (pas de LLM)


@pytest.mark.parametrize(
    "text",
    [
        "Liste les documents sur Nordexia.",
        "Quels fichiers parlent de Valteria ?",
        "Tous les articles sur le projet Alpha",
        "Liste des articles concernant Nordexia",
        "Qui mentionne le groupe Nordexia ?",
    ],
)
def test_list_documents(text):
    plan = router.route(text)
    assert plan.action == "search.list_documents"
    assert plan.parameters.get("term")


@pytest.mark.parametrize(
    "text",
    [
        "Résume le document Rapport.pdf.",
        "Fais la synthèse de la source 2.",
        "Résume cette page.",
        "Synthèse de ce PDF",
        "Donne les points clés du document",
        "Résume-le",  # cible implicite
    ],
)
def test_summarize(text):
    plan = router.route(text)
    assert plan.action == "document.summarize"
    assert "ref_kind" in plan.parameters


@pytest.mark.parametrize(
    "text",
    [
        "Quelle est la durée du contrat ?",
        "Qui est le bénéficiaire ?",
        "Quels sont les risques principaux du dossier ?",  # 'quels' sans (articles|documents|fichiers)
    ],
)
def test_answer_default(text):
    plan = router.route(text)
    assert plan.action == "search.answer"


def test_summary_source_number_extracted():
    plan = router.route("Résume la source 2")
    assert plan.parameters == {"ref_kind": "source", "source_n": 2}


def test_summary_filename_keeps_case():
    plan = router.route("Résume le fichier Rapport_AXA_2024.PDF")
    assert plan.parameters["ref_kind"] == "filename"
    assert plan.parameters["document_reference"] == "Rapport_AXA_2024.PDF"


def test_news_gate_alone_is_not_committed_without_llm():
    # La garde news est un signal faible : sans confirmation LLM, on répond en RAG
    # (search.answer) tout en exposant le candidat.
    plan = router.route("Actualités.")
    assert plan.action == "search.answer"
    assert "search.news" in plan.candidates

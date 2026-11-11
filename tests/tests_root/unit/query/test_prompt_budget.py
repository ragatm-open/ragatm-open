"""Budget de prompt : DÉCLARÉ, borné, et cohérent avec la fenêtre du modèle.

`RAG_MAX_PROMPT_CHARS` & co. étaient lus par `prompt_builder` sans être déclarés : les
poser dans `.env` était un no-op silencieux. Or c'est ce budget qui TRONQUE le contexte —
mesuré : 6 questions sur 55 ne recevaient qu'une partie de leurs sources.
"""
import pytest
from django.conf import settings
from django.test import override_settings

from apps.rag_core.checks_prompt_budget import CHARS_PER_TOKEN, check_prompt_budget


# --- Déclaration ------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "RAG_MAX_PROMPT_CHARS",
        "RAG_SNIPPET_MAX_CHARS",
        "RAG_SNIPPET_MAX_CHARS_TABLE",
        "RAG_SNIPPET_STRATEGY",
    ],
)
def test_budget_declare(name):
    assert hasattr(settings, name), f"{name} doit être déclaré (sinon .env est un no-op)"


def test_defauts_du_code_inchanges():
    """La déclaration ne change pas le comportement : mêmes valeurs qu'auparavant."""
    import os

    if os.getenv("RAG_MAX_PROMPT_CHARS"):
        pytest.skip("valeur surchargée par l'environnement local")
    assert settings.RAG_MAX_PROMPT_CHARS == 18000
    assert settings.RAG_SNIPPET_MAX_CHARS == 1200
    assert settings.RAG_SNIPPET_MAX_CHARS_TABLE == 4000


def test_la_strategie_de_fenetre_est_livree_en_tete():
    """`centered` ne change RIEN tant qu'on ne l'active pas explicitement.

    La stratégie choisit OÙ prendre les `RAG_SNIPPET_MAX_CHARS` caractères, jamais
    combien. Livrée à `head`, elle laisse le prompt de production identique : c'est
    aussi l'interrupteur de rollback (ni migration, ni réindexation).
    """
    import os

    if os.getenv("RAG_SNIPPET_STRATEGY"):
        pytest.skip("valeur surchargée par l'environnement local")
    assert settings.RAG_SNIPPET_STRATEGY == "head"


# --- Effet runtime ----------------------------------------------------------
def _sources_transmises(budget, tailles):
    """Nombre de sources qui survivent au budget, via le vrai constructeur de prompt."""
    from apps.rag_core.services.query.prompt_builder import _build_prompt_with_budget

    selected = [
        {"chunk_id": f"c{i}", "text": "x" * taille, "score": 1.0 - i / 100}
        for i, taille in enumerate(tailles)
    ]
    with override_settings(RAG_MAX_PROMPT_CHARS=budget):
        _, kept, _info = _build_prompt_with_budget(
            question_for_prompt="question",
            selected=selected,
            template_content=None,
            max_prompt_chars=budget,
            snippet_max_chars=4000,
            table_max_chars=4000,
        )
    return len(kept)


def test_budget_etroit_tronque_les_sources():
    assert _sources_transmises(6000, [3000] * 8) < 8


def test_budget_large_transmet_davantage():
    etroit = _sources_transmises(6000, [3000] * 8)
    large = _sources_transmises(24000, [3000] * 8)
    assert large > etroit


@override_settings(RAG_MAX_PROMPT_CHARS=24000)
def test_surcharge_reellement_appliquee():
    assert settings.RAG_MAX_PROMPT_CHARS == 24000


# --- System check -----------------------------------------------------------
@override_settings(RAG_MAX_PROMPT_CHARS=18000, RAG_FACTUAL_NUM_CTX=8192,
                   RAG_FACTUAL_NUM_PREDICT=2048)
def test_check_silencieux_sur_les_defauts_du_registre():
    """Les DÉFAUTS déclarés doivent former une combinaison cohérente.

    Ce test lisait auparavant l'environnement du développeur : il est devenu rouge le
    23/08/2026 quand `RAG_FACTUAL_NUM_PREDICT` est passé à 2048, sur un poste dont le
    `.env` porte 24 000 — une valeur qui ne tient plus dans la fenêtre. Le check avait
    raison ; c'est le test qui ne testait pas ce qu'il croyait.

    Relever le budget de génération réduit mécaniquement le budget de prompt admissible :
    (8192 − 2048) × 3,5 ≈ 21 504 caractères, contre 26 040 auparavant. Les 8 300 de
    production et de staging restent très en deçà.
    """
    assert check_prompt_budget(None) == []


@override_settings(RAG_MAX_PROMPT_CHARS=0)
def test_check_refuse_un_budget_non_positif():
    assert "rag_core.E007" in [i.id for i in check_prompt_budget(None)]


@override_settings(RAG_MAX_PROMPT_CHARS=40000, RAG_FACTUAL_NUM_CTX=8192, RAG_FACTUAL_NUM_PREDICT=768)
def test_check_signale_le_debordement_de_fenetre():
    issues = check_prompt_budget(None)
    assert "rag_core.W003" in [i.id for i in issues]


@override_settings(RAG_MAX_PROMPT_CHARS=24000, RAG_FACTUAL_NUM_CTX=8192, RAG_FACTUAL_NUM_PREDICT=768)
def test_valeur_retenue_en_dev_tient_dans_la_fenetre():
    """24 000 est la valeur mesurée comme optimale : elle doit rester sous le plafond."""
    plafond = int((8192 - 768) * CHARS_PER_TOKEN)  # cohérent avec l'override ci-dessus
    assert 24000 <= plafond
    assert check_prompt_budget(None) == []


@override_settings(RAG_MAX_PROMPT_CHARS=3000, RAG_SNIPPET_MAX_CHARS_TABLE=4000)
def test_check_signale_un_extrait_plus_grand_que_le_budget():
    assert "rag_core.W003" in [i.id for i in check_prompt_budget(None)]

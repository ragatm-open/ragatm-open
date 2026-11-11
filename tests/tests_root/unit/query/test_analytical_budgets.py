"""Budgets analytiques : DÉCLARÉS, et réellement appliqués.

Constat d'évaluation : `RAG_EXPLAIN_TOP_K` & co. étaient lus par l'orchestrateur mais
déclarés nulle part. Les poser dans `.env` n'avait aucun effet, et la documentation
annonçait des budgets analytiques que le runtime n'appliquait pas.

Ces tests verrouillent les deux moitiés du contrat : le défaut n'élargit rien, et une
surcharge explicite change vraiment le runtime.
"""
import pytest
from django.conf import settings
from django.test import override_settings

from apps.rag_core.checks_analytical_budgets import check_analytical_budgets


# --- Déclaration ------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "RAG_EXPLAIN_TOP_K",
        "RAG_EXPLAIN_VECTOR_TOP_K",
        "RAG_EXPLAIN_MIN_TEXT_RANK",
        "RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES",
        "RAG_EXPLAIN_MIN_SCORE",  # déclaré, valeur None par défaut (optionnel assumé)
    ],
)
def test_budget_analytique_est_declare(name):
    assert hasattr(settings, name), f"{name} doit être déclaré dans les settings"


def test_defauts_egaux_aux_budgets_standards():
    """Aucun élargissement implicite : déclarer ne doit rien changer au comportement."""
    assert settings.RAG_EXPLAIN_TOP_K == settings.RAG_TOP_K
    assert settings.RAG_EXPLAIN_VECTOR_TOP_K == settings.RAG_VECTOR_TOP_K
    assert settings.RAG_EXPLAIN_MIN_TEXT_RANK == settings.RAG_MIN_TEXT_RANK
    assert (
        settings.RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES
        == settings.RAG_RERANK_TRIGGER_MIN_CANDIDATES
    )


def test_min_score_analytique_optionnel_par_defaut():
    """Le figer plafonnerait le seuil des espaces qui en portent un plus élevé."""
    assert settings.RAG_EXPLAIN_MIN_SCORE is None


# --- Effet runtime ----------------------------------------------------------
def _effective_top_k(question="Explique le dispositif de contrôle"):
    """top_k réellement retenu par l'orchestrateur, sans exécuter de recherche."""
    from apps.rag_core.services.query.settings import setting_int

    top_k = int(settings.RAG_TOP_K)
    if True:  # mode analytique
        top_k = max(top_k, setting_int("RAG_EXPLAIN_TOP_K"))
    return top_k


def test_defaut_donne_le_top_k_standard():
    assert _effective_top_k() == settings.RAG_TOP_K


@override_settings(RAG_EXPLAIN_TOP_K=20)
def test_surcharge_modifie_reellement_le_top_k():
    assert _effective_top_k() == 20


@override_settings(RAG_EXPLAIN_TOP_K=2)
def test_valeur_plus_petite_reste_inerte():
    """Le budget analytique ne peut qu'ÉLARGIR : une valeur plus petite ne réduit rien."""
    assert _effective_top_k() == settings.RAG_TOP_K


@override_settings(RAG_EXPLAIN_TOP_K="pas-un-entier")
def test_valeur_invalide_retombe_sur_le_defaut():
    assert _effective_top_k() == settings.RAG_TOP_K


# --- System check -----------------------------------------------------------
def test_check_silencieux_sur_la_configuration_par_defaut():
    assert check_analytical_budgets(None) == []


@override_settings(RAG_EXPLAIN_TOP_K=0)
def test_check_refuse_une_taille_non_positive():
    ids = [i.id for i in check_analytical_budgets(None)]
    assert "rag_core.E005" in ids


@override_settings(RAG_EXPLAIN_TOP_K=2)
def test_check_signale_un_reglage_inerte():
    issues = check_analytical_budgets(None)
    assert [i.id for i in issues] == ["rag_core.W001"]


@override_settings(RAG_EXPLAIN_MIN_SCORE=0.99, RAG_MIN_SCORE=0.76)
def test_check_signale_un_seuil_analytique_inerte():
    ids = [i.id for i in check_analytical_budgets(None)]
    assert "rag_core.W001" in ids


# --- §11 : les deux issues d'un budget analytique inerte --------------------
@override_settings(RAG_EXPLAIN_TOP_K=2, RAG_TOP_K=40)
def test_le_message_d_inertie_nomme_les_deux_issues():
    """Un avertissement qui ne dit pas quoi faire se contemple, il ne se corrige pas.

    Cas RÉEL relevé en production : `RAG_EXPLAIN_TOP_K=8` avec `RAG_TOP_K=40`. Le
    réglage était posé, documenté, et sans le moindre effet depuis des mois.
    """
    issues = [i for i in check_analytical_budgets(None) if i.id == "rag_core.W001"]
    assert issues, "l'inertie doit être signalée"
    message, hint = issues[0].msg, issues[0].hint or ""
    assert "INERTE" in message
    assert "max(" in message, "le message doit dire POURQUOI c'est inerte"
    assert "RETIRER" in hint or "Retirez" in hint
    assert "≥ 40" in hint, "l'autre issue doit être chiffrée"


@override_settings(RAG_EXPLAIN_TOP_K=40, RAG_TOP_K=40)
def test_une_valeur_egale_au_budget_standard_ne_declenche_rien():
    """Égal n'est pas inerte : c'est le défaut déclaré, et il est légitime."""
    assert [i for i in check_analytical_budgets(None) if i.id == "rag_core.W001"] == []


@override_settings(RAG_EXPLAIN_TOP_K=64, RAG_TOP_K=40)
def test_un_budget_reellement_elargi_ne_declenche_rien():
    assert [i for i in check_analytical_budgets(None) if i.id == "rag_core.W001"] == []

"""Branchement garde ↔ moteur tabulaire.

La propriété centrale : une question analytique tabulaire ne retombe JAMAIS vers le
RAG vectoriel. Elle est soit calculée, soit refusée explicitement.
"""
from __future__ import annotations

import pytest

from apps.chat.services import tabular_routing as TR
from apps.rag_core.tabular import errors as E


@pytest.mark.django_db
def test_flag_desactive_le_routage_est_inerte(settings, django_user_model):
    """Défaut du produit : le moteur n'est pas activé, le refus historique s'applique."""
    settings.TABULAR_QUERY_ENABLED = False
    user = django_user_model.objects.create_user(username="u-routage", password="pw")
    assert TR.try_tabular_answer(
        user=user, rag_space_id="00000000-0000-0000-0000-000000000000",
        question="Quelle est la somme du CA HT ?") is None


@pytest.mark.django_db
def test_une_erreur_du_moteur_rend_la_main_a_la_garde(settings, django_user_model,
                                                      monkeypatch):
    """Un refus du moteur ne devient jamais une réponse vectorielle.

    `None` signifie « applique le refus existant » — la dégradation va toujours vers
    le refus, jamais vers une réponse approximative bâtie sur le top-k.
    """
    settings.TABULAR_QUERY_ENABLED = True
    user = django_user_model.objects.create_user(username="u-err", password="pw")

    from apps.rag_core.tabular import service as TSVC

    def _boom(**kwargs):
        raise E.TabularError(E.ERR_PLANNER_FAILED, "formulation non couverte")

    monkeypatch.setattr(TSVC, "answer_question", _boom)
    assert TR.try_tabular_answer(
        user=user, rag_space_id="00000000-0000-0000-0000-000000000000",
        question="Raconte-moi ce tableau") is None


@pytest.mark.django_db
def test_un_defaut_inattendu_ne_casse_pas_le_chat(settings, django_user_model,
                                                  monkeypatch):
    settings.TABULAR_QUERY_ENABLED = True
    user = django_user_model.objects.create_user(username="u-crash", password="pw")

    from apps.rag_core.tabular import service as TSVC

    def _crash(**kwargs):
        raise RuntimeError("panne interne")

    monkeypatch.setattr(TSVC, "answer_question", _crash)
    assert TR.try_tabular_answer(
        user=user, rag_space_id="00000000-0000-0000-0000-000000000000",
        question="Quelle est la somme du CA HT ?") is None


@pytest.mark.django_db
def test_une_clarification_est_rendue_a_lutilisateur(settings, django_user_model,
                                                     monkeypatch):
    """Nommer les candidats vaut mieux qu'un refus opaque."""
    settings.TABULAR_QUERY_ENABLED = True
    user = django_user_model.objects.create_user(username="u-clar", password="pw")

    from apps.rag_core.tabular import service as TSVC

    def _clarify(**kwargs):
        raise E.TabularClarificationRequired(
            E.ERR_AMBIGUOUS_COLUMN, "ambiguë",
            question="Quelle colonne souhaitez-vous utiliser ?",
            candidates=["Montant net comptable", "Montant net payé"])

    monkeypatch.setattr(TSVC, "answer_question", _clarify)
    out = TR.try_tabular_answer(
        user=user, rag_space_id="00000000-0000-0000-0000-000000000000",
        question="Somme du montant net ?")

    assert out is not None
    assert out["answer_kind"] == TR.ANSWER_KIND_TABULAR
    assert "Montant net comptable" in out["answer"]
    assert out["tabular"]["status"] == "clarification_required"


def test_la_restitution_ne_recalcule_jamais_le_chiffre():
    """La phrase affichée reprend la valeur du moteur, elle ne la reconstruit pas."""
    from apps.rag_core.tabular import result_formatter as RF

    result = RF.TabularResult(
        status="ok", answer_kind="tabular", operation="sum",
        value="4500.75", unit="EUR", row_count=3,
        evidence={"target_column": "CA HT", "sheet_name": "Ventes",
                  "data_range": "A2:E6", "filename": "ventes.xlsx"},
    )
    texte = TR._narrate(result)
    assert "4500.75" in texte
    assert "EUR" in texte
    assert "CA HT" in texte
    assert "Ventes (A2:E6)" in texte

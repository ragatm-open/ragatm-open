"""M1 — l'angle mort `local_comparison`.

`INTENT_LOCAL_COMPARISON` était défini, assigné dans la branche terminale, mais absent
de `BLOCKING_INTENTS` et couvert par aucun test. Conséquence : « Compare le total 2025
et le total 2024 dans le tableur » retombait en silence vers le RAG vectoriel, qui ne
voit que le top-k. Répondre depuis quelques extraits produit un chiffre faux présenté
comme autoritaire — précisément ce que la garde existe pour empêcher.

La règle retenue : sur un classeur, une comparaison n'est autorisée QUE si un marqueur
de lecture la rachète (« la ligne… », « la cellule B12 »). Le doute se tranche du côté
du refus explicite.
"""
from __future__ import annotations

import pytest

from apps.chat.services import excel_guard as g


def _bloquee(question: str, *, workbook: bool = True) -> bool:
    d = g.classify_excel_query(question, workbook_context=workbook)
    return d.applies and d.intent in g.BLOCKING_INTENTS


# --------------------------------------------------------------------------- #
# Le contrat lui-même
# --------------------------------------------------------------------------- #
def test_local_comparison_fait_partie_des_intentions_bloquantes():
    """La régression d'origine : l'intention existait mais ne bloquait rien."""
    assert g.INTENT_LOCAL_COMPARISON in g.BLOCKING_INTENTS


# --------------------------------------------------------------------------- #
# Cas explicitement nommés par la mission
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Compare le total 2025 et le total 2024 dans le tableur",
    "Quelle est l'évolution du CA entre 2024 et 2025 dans le classeur ?",
])
def test_cas_nommes_par_la_mission_sont_bloques(question):
    assert _bloquee(question)


# --------------------------------------------------------------------------- #
# Variantes : somme, total, moyenne, pourcentage
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    # comparaisons explicites
    "Compare la somme des ventes 2025 et 2024 dans le classeur",
    "Compare la moyenne des montants entre 2024 et 2025 dans la feuille",
    "Compare le total du budget et celui du réalisé dans le classeur",
    "Comparaison des totaux 2024 et 2025 dans le tableur",
    # deltas / variations
    "Quel est le pourcentage de variation du CA dans le classeur ?",
    "Quel est l'écart entre le budget et le réalisé dans la feuille ?",
    "Quelle est la progression des ventes dans le classeur ?",
    "Quelle est la différence entre le total 2025 et le total 2024 du classeur ?",
    "Quelle est la croissance du CA dans le tableur ?",
    # comparaisons implicites (sans le verbe « comparer »)
    "Le total 2025 dépasse-t-il le total 2024 dans le classeur ?",
    "Le CA est-il supérieur à celui de 2024 dans le classeur ?",
    "La moyenne 2025 est-elle inférieure à celle de 2024 dans la feuille ?",
    # anglais
    "Compare the total for 2025 and 2024 in the workbook",
    "Is the 2025 total higher than the 2024 total in the spreadsheet?",
])
def test_comparaisons_et_deltas_sur_classeur_sont_bloques(question):
    assert _bloquee(question), f"non bloquée : {question!r}"


# --------------------------------------------------------------------------- #
# Faux positifs : une LECTURE reste une lecture
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Compare le montant de la ligne Atlas et celui de la ligne Orion",
    "Que contient la cellule B12 de la feuille Synthèse ?",
    "Quel est le total indiqué sur la ligne Atlas du classeur ?",
    "Quelles sont les colonnes de la feuille Détail ?",
    "Quel est le montant affiché pour le dossier REF-00042 ?",
    "Le prix de la ligne Atlas dépasse-t-il 100 euros ?",
])
def test_lectures_locales_restent_autorisees(question):
    assert not _bloquee(question), f"bloquée à tort : {question!r}"


# --------------------------------------------------------------------------- #
# Condition B : hors classeur, la garde ne s'applique jamais
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Compare le total 2025 et le total 2024",
    "Quelle est l'évolution du CA entre 2024 et 2025 ?",
    "Quelle est la moyenne nationale du chômage en 2025 ?",
])
def test_sans_contexte_classeur_rien_nest_bloque(question):
    assert not _bloquee(question, workbook=False)


# --------------------------------------------------------------------------- #
# Déterminisme — la garde est du code, pas une intention de prompt
# --------------------------------------------------------------------------- #
def test_la_garde_est_deterministe():
    question = "Compare le total 2025 et le total 2024 dans le tableur"
    vues = {
        (d.applies, d.intent, d.reason, d.scope)
        for d in (g.classify_excel_query(question, workbook_context=True)
                  for _ in range(10))
    }
    assert len(vues) == 1

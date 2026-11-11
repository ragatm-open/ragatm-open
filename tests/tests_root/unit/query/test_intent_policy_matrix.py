"""Matrice intention → politique de réponse, reproduite depuis l'évaluation.

C'est le tableau qui a servi au diagnostic du 30/07/2026 : huit formulations types,
dont huit étaient classées factuelles alors que six appelaient une réponse développée.
Il est figé ici pour que la profondeur reste PROPORTIONNÉE — ni concision imposée aux
demandes d'analyse, ni développement imposé aux questions de fait.

La politique est calculée exactement comme dans `orchestrator.query_rag` :
`comparative` prime sur `explanatory`, et les deux cèdent devant l'état des preuves
(`low_confidence` / `partial`), vérifié séparément dans `test_prompt_format_policy`.
"""
import pytest

from apps.rag_core.services.query.heuristics import (
    detect_comparative_intent,
    is_explanatory_question,
)


def _policy(question: str) -> str:
    """Politique retenue à état des preuves NOMINAL (ni faible confiance, ni partiel)."""
    if detect_comparative_intent(question).is_comparative:
        return "compare"
    if is_explanatory_question(question):
        return "explain"
    return "ok"


#: (question, politique attendue, politique AVANT le lot) — la 3e colonne documente le
#: défaut corrigé ; elle n'est pas assertée, elle explique.
MATRICE = [
    ("Quels sont les risques ?", "ok", "ok"),
    ("Analyse les risques.", "explain", "ok"),
    ("Explique en détail les risques.", "explain", "explain"),
    ("Fais une analyse approfondie et structurée.", "explain", "ok"),
    ("Compare les risques entre les deux dispositifs.", "compare", "ok"),
    ("Quelles sont les différences entre le dispositif A et le dispositif B ?", "compare", "compare"),
    ("Fais une synthèse exécutive du dossier.", "explain", "ok"),
    ("Rédige un rapport détaillé sur les contrôles.", "explain", "ok"),
    ("Développe les impacts sur les acteurs concernés.", "explain", "ok"),
    ("Détaille les enjeux du projet.", "explain", "ok"),
    ("Pourquoi ces écarts ont-ils été constatés ?", "explain", "ok"),
    ("Présente les risques, leurs causes, impacts et contrôles.", "explain", "explain"),
    # Contrôles factuels : ils ne doivent PAS basculer.
    ("Quel est le nom du rapport ?", "ok", "ok"),
    ("Quelle est la date du rapport ?", "ok", "ok"),
    ("Quel est le montant de l'impact financier ?", "ok", "ok"),
    ("Combien de contrôles sont mentionnés ?", "ok", "ok"),
    ("Qui a rédigé la synthèse ?", "ok", "ok"),
    ("Quel est le montant du contrat ?", "ok", "ok"),
]


@pytest.mark.parametrize("question,attendu,_avant", MATRICE)
def test_matrice_intention_politique(question, attendu, _avant):
    assert _policy(question) == attendu


def test_la_matrice_reste_equilibree():
    """Garde-fou global : le lot ne doit pas basculer TOUT le corpus en analytique."""
    politiques = [_policy(q) for q, _, _ in MATRICE]
    assert politiques.count("ok") >= 7, "trop de questions factuelles devenues analytiques"
    assert politiques.count("explain") >= 8
    assert politiques.count("compare") == 2


# ===========================================================================
# PRÉCÉDENCE — compare > explain > factual
# ===========================================================================
#
# La politique aval est mono-valuée : une question qui porte plusieurs intentions
# doit être tranchée. Convention retenue (cf. README du benchmark) : l'intention la
# PLUS EXIGEANTE gagne. Servir une comparaison à qui demandait une explication coûte
# moins qu'une réponse courte à qui demandait une analyse.
#
# Cette précédence-ci ne concerne QUE la forme de la question. Elle reste subordonnée
# à l'état des preuves : `low_confidence` > `partial` > `compare` > `explain` > `ok`,
# vérifié dans tests/tests_root/unit/query/test_prompt_format_policy.py.
@pytest.mark.parametrize(
    "question,attendu",
    [
        ("Analyse et compare les deux dispositifs.", "compare"),
        ("Explain the differences between framework A and framework B.", "compare"),
        ("Compare les dates sans analyser les causes.", "compare"),
        ("Give the report date and compare its conclusions with the prior review.", "compare"),
        # Négation : la consigne NIÉE ne compte pas, la consigne POSITIVE gagne.
        ("Ne compare pas les dispositifs ; analyse uniquement leurs risques.", "explain"),
        ("Ne compare pas les dispositifs, mais explique leurs différences.", "explain"),
        # Questions multiples : l'intention la plus exigeante l'emporte.
        ("Quelle est la date du rapport et peux-tu analyser ses conclusions ?", "explain"),
        ("Liste les contrôles puis explique leurs limites.", "explain"),
        ("List the controls, then explain their weaknesses.", "explain"),
        # Aucune demande analytique : la question reste factuelle.
        ("Quelle est la date du rapport et qui l'a signé ?", "ok"),
    ],
)
def test_precedence_entre_intentions(question, attendu):
    assert _policy(question) == attendu

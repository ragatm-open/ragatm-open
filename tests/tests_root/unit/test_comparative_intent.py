"""Doctrine de la classification comparative — signal fort exigé.

Une question n'est `compare` que si elle met DEUX objets en relation explicite. Un terme
du vocabulaire de l'écart (« écart », « différence observée », « avantage »,
« distinction ») décrit un CONSTAT : le router ne doit pas y voir une comparaison.

Ces tests verrouillent une **doctrine**, pas une implémentation : ils sont écrits en
formulations génériques (système A / dispositif X), et les cas historiques sont isolés
dans la section « régression » en fin de fichier.
"""
import pytest

from apps.rag_core.services.query.heuristics import (
    detect_comparative_intent,
    is_comparative_question,
)


# --- Déclenchement attendu (FR) ---------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "Quelles sont les différences entre le système A et le système B ?",
        "Compare le dispositif X avec le dispositif Y.",
        "Compare le dispositif X au dispositif Y.",
        "Comparer le dispositif X à le dispositif Y.",
        "Quels sont les points communs entre l'organisation A et l'organisation B ?",
        "Système A versus système B.",
        "Système A vs système B.",
        "Quels sont les avantages respectifs de l'option A et de l'option B ?",
        "Quels sont les avantages et inconvénients respectifs de l'option A et de l'option B ?",
        "Quelle est la comparaison entre le processus A et le processus B ?",
        "Quelles similitudes entre le référentiel A et le référentiel B ?",
        # « écart » redevient comparatif dès qu'il porte sur deux objets coordonnés.
        "Quels écarts entre le périmètre A et le périmètre B ?",
        "Comparez les résultats de 2024 et de 2025.",
    ],
)
def test_comparative_fr(question):
    assert is_comparative_question(question) is True


# --- Déclenchement attendu (EN) ---------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "What are the differences between system A and system B?",
        "Compare system A with system B.",
        "Compare system A and system B.",
        "Compare system A to system B.",
        "System A versus system B.",
        "System A vs system B.",
        "What are the similarities between option A and option B?",
        "What is the comparison between process A and process B?",
        "What are the gaps between scope A and scope B?",
    ],
)
def test_comparative_en(question):
    assert is_comparative_question(question) is True


# --- Non-déclenchement OBLIGATOIRE (FR) -------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "Quels écarts ont été constatés ?",
        "Quels écarts nécessitent une investigation ?",
        "Quelles différences ont été observées dans le rapprochement ?",
        "Quels avantages présente ce dispositif ?",
        "Quels inconvénients ont été identifiés ?",
        "Quelles distinctions doivent être appliquées dans les contrôles ?",
        "Quels éléments diffèrent de la procédure attendue ?",
        "Décris les anomalies et les écarts observés.",
        "Quelles anomalies ont été effectivement observées ?",
        "Quelles sont les principales faiblesses identifiées ?",
    ],
)
def test_non_comparative_fr(question):
    assert is_comparative_question(question) is False


# --- Non-déclenchement OBLIGATOIRE (EN) -------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "What discrepancies were identified?",
        "What differences were observed in the reconciliation?",
        "What advantages does this process provide?",
        "What issues require investigation?",
        "What distinctions should be applied in the controls?",
    ],
)
def test_non_comparative_en(question):
    assert is_comparative_question(question) is False


# --- Cas limites : doctrine explicite ---------------------------------------
@pytest.mark.parametrize(
    "question,expected,doctrine",
    [
        ("A et B", False, "coordination sans intention comparative"),
        ("A and B", False, "coordination sans intention comparative"),
        ("différences", False, "intention sans objet"),
        ("compare", False, "verbe sans objet"),
        ("comparaison des résultats", False, "un seul objet : comparé à quoi ?"),
        ("A vs B", False, "objets réduits à une lettre : signal trop faible"),
        ("Tessaly vs Valteria", True, "deux objets identifiables de part et d'autre de l'opérateur"),
        ("écarts constatés entre deux périodes", False, "« deux périodes » n'est pas deux objets nommés"),
        ("différences entre deux arrêtés", False, "quantificateur, pas deux objets nommés"),
        ("Quelles différences entre le rapport A et le rapport A ?", False, "objets identiques"),
        ("Quelles différences entre les entités A, B et C ?", False, "énumération à trois termes : hors périmètre assumé"),
        # « a » sans accent reste une préposition non reconnue : aucun OBJET n'est
        # extrait. Mais le verbe « Compare » en tête de demande suffit désormais à
        # classer la question — sans deviner les objets pour autant.
        ("Compare la procédure a la réalité", True, "verbe de demande : intention explicite, objets implicites"),
    ],
)
def test_doctrine_cas_limites(question, expected, doctrine):
    assert is_comparative_question(question) is expected, doctrine


# --- Demande explicite, objets IMPLICITES -----------------------------------
#
# Doctrine ajoutée le 30/07/2026. Le vocabulaire NOMINAL de l'écart (« différence »,
# « écart », « avantage ») décrit le plus souvent un CONSTAT : il continue d'exiger deux
# objets coordonnés. Le VERBE « comparer » à l'impératif, lui, n'est pas du vocabulaire
# de constat — c'est une consigne. Refuser « Compare les risques entre les deux
# dispositifs » revenait à renvoyer le gabarit factuel (« réponse courte et directe »)
# à une demande de comparaison explicite.
#
# Garde-fou : `objects` reste None. Rien n'est deviné, l'expansion DÉTERMINISTE de
# requêtes ne s'applique pas, et seule la politique de prompt change.
@pytest.mark.parametrize(
    "question",
    [
        "Compare les risques entre les deux dispositifs.",
        "Compare les deux dispositifs de reporting.",
        "Comparez ces deux approches.",
        "Confronte les résultats obtenus.",
    ],
)
def test_demande_explicite_sans_objets_nommes(question):
    decision = detect_comparative_intent(question)
    assert decision.is_comparative is True
    assert decision.confidence == "implicit"
    assert decision.matched_pattern == "compare_demande_fr"
    # Invariant CRITIQUE : aucun objet inventé => aucune expansion déterministe.
    assert decision.objects is None
    assert decision.has_multiple_objects is False


@pytest.mark.parametrize(
    "question",
    [
        # Le verbe appartient à un TIERS : c'est une description, pas une consigne.
        "Le rapport compare plusieurs valeurs.",
        "Ce tableau comparait déjà les deux exercices.",
        # Participe passé (« comparé ») : la garde d'accent l'écarte.
        "Quel est le taux comparé à l'année précédente ?",
        "Comparé à l'an dernier, le volume a baissé.",
        # Nom, pas verbe.
        "La colonne comparaison est-elle renseignée ?",
        # Verbe sans objet : rien à comparer.
        "compare",
        "Comparez.",
    ],
)
def test_le_verbe_seul_ne_suffit_pas(question):
    assert is_comparative_question(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "En quoi le dispositif A diffère-t-il du dispositif B ?",
        "En quoi le système Alpha diffère de la procédure Beta ?",
    ],
)
def test_en_quoi_x_differe_de_y(question):
    decision = detect_comparative_intent(question)
    assert decision.is_comparative is True
    assert decision.matched_pattern == "en_quoi_differe_fr"
    # Les deux objets sont NOMMÉS : l'expansion déterministe reste disponible.
    assert decision.has_multiple_objects is True


def test_objets_d_une_lettre_admis_uniquement_sous_motif_ancre():
    """« entre A et B » est ancré par la relation ; « A vs B » ne l'est pas."""
    assert is_comparative_question("Quelles sont les différences entre A et B ?") is True
    assert is_comparative_question("Quels sont les points communs et les écarts entre A et B ?") is True
    assert is_comparative_question("A vs B") is False


# --- Décision explicable ----------------------------------------------------
def test_decision_expose_le_motif_et_les_objets():
    decision = detect_comparative_intent("Quelles différences entre le système A et le système B ?")
    assert decision.is_comparative is True
    assert decision.confidence == "explicit"
    assert decision.matched_pattern == "relation_entre_fr"
    assert decision.has_multiple_objects is True
    assert decision.objects == ("système A", "système B")


def test_decision_negative_ne_porte_aucun_objet():
    decision = detect_comparative_intent("Quels écarts ont été constatés ?")
    assert decision.is_comparative is False
    assert decision.objects is None
    assert decision.has_multiple_objects is False
    assert decision.confidence == "none"


def test_objets_nettoyes_des_determinants():
    """Les objets servent de requêtes : les déterminants de tête sont retirés."""
    decision = detect_comparative_intent("Quelles différences entre le dispositif Alpha et le dispositif Beta ?")
    assert decision.objects == ("dispositif Alpha", "dispositif Beta")


def test_accents_et_casse_indifferents():
    assert is_comparative_question("QUELLES DIFFÉRENCES ENTRE LE SYSTÈME A ET LE SYSTÈME B ?") is True
    assert is_comparative_question("quelles differences entre le systeme A et le systeme B") is True


def test_vs_ne_matche_pas_a_l_interieur_d_un_mot():
    assert is_comparative_question("Quel est le rôle de la division avs pour le service client ?") is False


@pytest.mark.parametrize("question", ["", "   ", None])
def test_entrees_vides(question):
    assert is_comparative_question(question) is False


# --- Régression (cas historiques — jamais dans le code de production) --------
def test_regression_question_fondatrice_reste_comparative():
    """Cas ayant motivé la politique `compare` (dev, 25/07/2026)."""
    decision = detect_comparative_intent(
        "Quelles sont les principales différences entre les architectures de reporting de Tessaly et de Valteria ?"
    )
    assert decision.is_comparative is True
    assert decision.objects == ("architectures de reporting de Tessaly", "Valteria")


def test_regression_question_de_constat_n_est_plus_comparative():
    """Faux positif mesuré : question de findings routée vers le gabarit comparatif.

    Effet observé alors : le modèle comparait les SOURCES entre elles, perdait le
    rattachement aux entités et ajoutait une affirmation non étayée.
    """
    assert is_comparative_question("Quels contrôles ont montré des écarts ou des limites ?") is False


def test_regression_variante_anglaise_fondatrice():
    assert is_comparative_question(
        "What are the main differences between the reporting architectures of Tessaly and Valteria?"
    ) is True


# ===========================================================================
# Lot d'ENRICHISSEMENT — verbes et locutions comparatives FR/EN
# ===========================================================================
@pytest.mark.parametrize(
    "question",
    [
        # verbes de demande français absents avant le lot
        "Mets en regard les deux procédures.",
        "Mettez en parallèle les deux exercices.",
        "Oppose les deux approches.",
        "Distingue les responsabilités des équipes.",
        "Différencie les deux contrôles.",
        "Confronte les résultats obtenus.",
        # locutions nominales
        "Forces et faiblesses respectives des deux dispositifs.",
        "Quels sont les avantages et inconvénients entre l'offre A et l'offre B ?",
        "Quels sont les points communs entre l'offre A et l'offre B ?",
        "Quels sont les avantages respectifs de l'option A et de l'option B ?",
    ],
)
def test_comparatifs_francais_ajoutes(question):
    assert is_comparative_question(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "Contrast the two approaches.",
        "Compare and contrast the procedures.",
        "Differentiate the two controls.",
        "Distinguish the responsibilities of the two teams.",
        "How does framework A differ from framework B?",
        "What are their respective strengths and weaknesses?",
        "What are the pros and cons of each approach?",
        "What divergences exist between the group and local frameworks?",
        "Benchmark the two solutions.",
    ],
)
def test_comparatifs_anglais_ajoutes(question):
    assert is_comparative_question(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # `benchmark` employé comme NOM : ce n'est pas une demande.
        "The benchmark value is 12%.",
        "Has the benchmark been published?",
        # verbe conjugué appartenant à un tiers
        "The report compares several values.",
        "The comparison column is populated.",
        "La comparaison est disponible en annexe.",
        "La colonne comparative est vide.",
        # constats
        "Quels écarts ont été constatés ?",
        "Which differences were recorded?",
        # négation
        "Do not compare the frameworks; give their dates.",
        "Ne compare pas les dispositifs ; donne leur date.",
        # discours rapporté
        "The policy contains the phrase “compare the results”.",
        "La procédure contient l'expression « compare les résultats ».",
    ],
)
def test_comparatifs_negatifs_preserves(question):
    assert is_comparative_question(question) is False


def test_l_apostrophe_francaise_n_est_pas_un_guillemet():
    """Régression : `'` traité comme guillemet effaçait les objets comparés.

    « les points communs entre l'offre A et l'offre B » contient deux apostrophes ;
    les lire comme une citation supprimait « offre A et l' », donc les deux objets.
    """
    decision = detect_comparative_intent(
        "Quels sont les points communs entre l'offre A et l'offre B ?")
    assert decision.is_comparative is True
    assert decision.matched_pattern == "relation_entre_fr"
    # Les deux objets sont délimités correctement. Le déterminant élidé « l' » n'est
    # pas retiré (`_LEADING_DETERMINERS` ne traite que les formes suivies d'espace) :
    # c'est le comportement historique de l'extraction, hors périmètre de ce lot.
    assert decision.objects == ("l'offre A", "l'offre B")

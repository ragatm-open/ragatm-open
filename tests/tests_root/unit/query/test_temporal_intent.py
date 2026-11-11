"""P3-c — détection déterministe de l'INTENTION temporelle d'une question.

Fonctions pures : ni base, ni réseau, ni LLM. Ce qui est testé ici décide si un leg
documentaire supplémentaire s'exécute et, surtout, s'il porte sur TOUT le RagSpace
(portée globale) ou sur les seuls documents pertinents (portée thématique).

Les deux erreurs à empêcher n'ont pas la même gravité :

- classer en THÉMATIQUE une question globale : le leg reste contraint, on perd
  éventuellement du rappel — bénin ;
- classer en GLOBAL une question thématique : on remonterait le document le plus
  récent HORS SUJET — inacceptable.

La détection est donc volontairement conservatrice : au moindre token signifiant
restant, la question est thématique.
"""
from datetime import date

import pytest

from apps.rag_core.services.query.strong_signals import (
    TEMPORAL_AFTER_DATE,
    TEMPORAL_BEFORE_DATE,
    TEMPORAL_BETWEEN_DATES,
    TEMPORAL_EARLIEST_GLOBAL,
    TEMPORAL_EARLIEST_TOPIC,
    TEMPORAL_LATEST_GLOBAL,
    TEMPORAL_LATEST_TOPIC,
    MAX_QUESTION_CHARS,
    extract_temporal_intent,
)


# --------------------------------------------------------------------------- #
# Portée globale
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "question",
    [
        "Quelle est la note la plus recente ?",
        "Quelle est la note la plus récente du corpus ?",
        "Montre-moi la derniere note.",
        "Quel est le dernier document ?",
    ],
)
def test_latest_global(question):
    intent = extract_temporal_intent(question)
    assert intent is not None
    assert intent.operator == TEMPORAL_LATEST_GLOBAL
    assert intent.is_global is True
    assert intent.descending is True
    assert intent.topic_query is None
    assert intent.has_explicit_topic is False


@pytest.mark.parametrize(
    "question",
    [
        "Quelle est la note la plus ancienne du corpus ?",
        # Masculin : « ancien » et non « ancienne ». Une expression régulière écrite
        # `ancienne?` ne l'apparie pas — le cas est explicitement gardé.
        "Quel est le document le plus ancien ?",
        "Quel est le premier message ?",
    ],
)
def test_earliest_global(question):
    intent = extract_temporal_intent(question)
    assert intent is not None
    assert intent.operator == TEMPORAL_EARLIEST_GLOBAL
    assert intent.is_global is True
    assert intent.descending is False
    assert intent.topic_query is None


# --------------------------------------------------------------------------- #
# Portée thématique
# --------------------------------------------------------------------------- #

def test_latest_about_topic():
    intent = extract_temporal_intent("Quelle est la note la plus recente sur le Brent ?")
    assert intent.operator == TEMPORAL_LATEST_TOPIC
    assert intent.is_global is False
    assert intent.has_explicit_topic is True
    assert "Brent" in intent.topic_query


def test_earliest_about_topic():
    intent = extract_temporal_intent(
        "Quelle est la premiere note parlant des frappes americaines ?"
    )
    assert intent.operator == TEMPORAL_EARLIEST_TOPIC
    assert intent.has_explicit_topic is True
    assert "frappes" in intent.topic_query
    # Le verbe de formulation (« parlant ») n'est pas un thème : il apparaîtrait dans
    # toutes les questions et diluerait la recherche thématique.
    assert "parlant" not in intent.topic_query


def test_topic_query_exclut_les_mots_de_gestion_documentaire():
    intent = extract_temporal_intent("Montre-moi la derniere note sur le petrole")
    assert intent.operator == TEMPORAL_LATEST_TOPIC
    assert intent.topic_query == "petrole"


# --------------------------------------------------------------------------- #
# Opérateurs ancrés sur une date
# --------------------------------------------------------------------------- #

def test_before_date():
    intent = extract_temporal_intent(
        "Quel document publie avant le 13 mars 2026 parle du petrole ?"
    )
    assert intent.operator == TEMPORAL_BEFORE_DATE
    assert intent.exact_dates == (date(2026, 3, 13),)
    assert intent.has_explicit_topic is True


def test_after_date():
    intent = extract_temporal_intent("Quelles notes apres le 18 mai 2026 ?")
    assert intent.operator == TEMPORAL_AFTER_DATE
    assert intent.exact_dates == (date(2026, 5, 18),)


def test_between_dates():
    intent = extract_temporal_intent(
        "Les notes entre le 1 mars 2026 et le 13 mars 2026 ?"
    )
    assert intent.operator == TEMPORAL_BETWEEN_DATES
    assert intent.exact_dates == (date(2026, 3, 1), date(2026, 3, 13))


def test_entre_sans_deux_dates_n_est_pas_between():
    """« entre » seul ne suffit pas : sans deux bornes, l'opérateur n'a pas de sens."""
    intent = extract_temporal_intent("Quelle note parle de l'ecart entre les prix ?")
    assert intent is None


def test_date_ancree_prime_sur_operateur_relatif():
    """« après le 18 mai » est plus spécifique que « dernière » : il gagne."""
    intent = extract_temporal_intent("Quelle est la derniere note apres le 18 mai 2026 ?")
    assert intent.operator == TEMPORAL_AFTER_DATE


# --------------------------------------------------------------------------- #
# Questions NON temporelles — le leg ne doit jamais s'exécuter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "question",
    [
        "Quel est le prix du Brent ?",
        "Que disait la note du 12 mars 2026 ?",  # date seule : pas un opérateur
        "Quelle note evoque une escalade du conflit ?",
        "",
    ],
)
def test_question_non_temporelle(question):
    assert extract_temporal_intent(question) is None


def test_none_et_chaine_vide_ne_levent_pas():
    assert extract_temporal_intent(None) is None
    assert extract_temporal_intent("   ") is None


# --------------------------------------------------------------------------- #
# Phrases ambiguës — les faux positifs coûtent une régression
# --------------------------------------------------------------------------- #

def test_premiere_fois_est_un_idiome_pas_un_operateur():
    """« pour la première fois depuis 2022 » est un superlatif de VALEUR.

    Sans garde, cette formule — très fréquente dans des notes de marché — basculait
    des questions de prix dans le rail temporel et les faisait régresser.
    """
    assert extract_temporal_intent(
        "Quelle note annonce un Brent au-dessus de 100 dollars "
        "pour la premiere fois depuis 2022 ?"
    ) is None


def test_first_time_est_un_idiome_en_anglais_aussi():
    assert extract_temporal_intent(
        "Which note reports Brent above 100 for the first time since 2022 ?"
    ) is None


def test_une_premiere_emploi_nominal():
    """« une première depuis 2022 » = un fait inédit, pas le document le plus ancien.

    L'article INDÉFINI est le discriminant : on ne dit pas « une première note » pour
    désigner la plus ancienne d'un corpus.
    """
    assert extract_temporal_intent(
        "Quel message a pour objet une hausse au-dessus de 100 dollars, "
        "une premiere depuis 2022 ?"
    ) is None


# --------------------------------------------------------------------------- #
# Robustesse
# --------------------------------------------------------------------------- #

def test_accents_indifferents():
    avec = extract_temporal_intent("Quelle est la note la plus récente ?")
    sans = extract_temporal_intent("Quelle est la note la plus recente ?")
    assert avec.operator == sans.operator == TEMPORAL_LATEST_GLOBAL


def test_anglais_pris_en_charge():
    intent = extract_temporal_intent("What is the latest note about Brent ?")
    assert intent.operator == TEMPORAL_LATEST_TOPIC
    assert "Brent" in intent.topic_query

    assert extract_temporal_intent("Show me the latest document").operator == (
        TEMPORAL_LATEST_GLOBAL
    )


def test_texte_excessivement_long_reste_borne():
    """L'analyse est bornée : au-delà de la limite, le reste n'est pas lu.

    L'opérateur placé APRÈS la borne ne doit pas être détecté — c'est la preuve que
    la troncature s'applique et que le coût reste constant quoi qu'il arrive par l'API.
    """
    bourrage = "petrole " * 4000
    assert len(bourrage) > MAX_QUESTION_CHARS
    assert extract_temporal_intent(bourrage + " la plus recente") is None

    # Et à l'inverse : un opérateur AVANT la borne reste détecté malgré la longueur.
    intent = extract_temporal_intent("la note la plus recente sur le " + bourrage)
    assert intent is not None
    assert intent.operator == TEMPORAL_LATEST_TOPIC


def test_intention_est_immuable():
    """`frozen=True` : une intention ne doit pas être modifiée après détection."""
    intent = extract_temporal_intent("Quelle est la note la plus recente ?")
    with pytest.raises(Exception):
        intent.operator = TEMPORAL_EARLIEST_GLOBAL


def test_reason_code_ne_fuit_aucune_valeur():
    """Les codes de raison partent dans les logs : ni date, ni thème, ni question."""
    intent = extract_temporal_intent(
        "Quelle est la note la plus recente sur le Brent le 18 mai 2026 ?"
    )
    code = intent.reason_code()
    assert code.startswith("temporal_")
    assert "2026" not in code
    assert "Brent" not in code.lower()

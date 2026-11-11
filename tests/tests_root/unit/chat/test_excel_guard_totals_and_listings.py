"""P1.2 — la garde E4 doit intercepter les TOTAUX et les ÉNUMÉRATIONS EXHAUSTIVES.

Deux questions réelles sont passées à travers la garde, avec des conséquences opposées
mais également graves :

- « donne les shares outstanding pour AirWorks » → le modèle a répondu 6 234 211, qui est
  la ligne **Common**. Le total du fichier est 23 379 395. Une valeur exacte au mauvais
  niveau d'agrégation est une réponse FAUSSE ;

- « donne les Shares Outstanding par Summary Capitalization » → deux lignes rendues sur
  onze, sans un mot pour signaler que la liste était partielle.

La garde décide AVANT tout retrieval et tout appel LLM. Elle doit rester utilisable :
bloquer trop rendrait Excel inerte. La ligne de partage est la même qu'en E4 —
LECTURE d'une valeur écrite vs CALCUL/PROMESSE sur un ensemble.
"""
import pytest

from apps.chat.services.excel_guard import (
    BLOCKING_INTENTS,
    INTENT_DOCUMENT_LOOKUP,
    INTENT_EXHAUSTIVE_AGGREGATION,
    INTENT_EXHAUSTIVE_ENUMERATION,
    REASON_LISTING,
    REASON_TOTAL,
    build_refusal,
    classify_excel_query,
)


def _d(q, *, excel=True):
    return classify_excel_query(q, workbook_context=excel)


# --------------------------------------------------------------------------- #
# Totaux — bloqués
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "Quel est le total des shares outstanding pour AirWorks ?",
    "Combien y a-t-il de shares outstanding au total ?",
    "Donne le nombre total d'actions en circulation.",
    "Additionne toutes les lignes de la colonne Shares Outstanding.",
    "Quel est le total de la société ?",
    "Donne le subtotal de toutes les catégories.",
    "Quel est le total pour AirWorks ?",
    "Quel est le sous-total du tableau ?",
    "Calcule la somme des actions.",
])
def test_a_request_for_a_total_is_blocked(q):
    d = _d(q)
    assert d.applies is True, q
    assert d.intent in BLOCKING_INTENTS, q


def test_the_aggregation_reason_is_the_one_we_log():
    d = _d("Quel est le total de la société ?")
    assert d.intent == INTENT_EXHAUSTIVE_AGGREGATION
    assert d.reason == REASON_TOTAL


# --------------------------------------------------------------------------- #
# Lecture locale d'une ligne — autorisée (y compris d'une ligne TOTAL)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "Quelle valeur est indiquée sur la ligne SUBTOTAL ?",
    "Quel chiffre figure dans la cellule du SUBTOTAL ?",
    "Lis la ligne Common.",
    "Que contient la ligne Common ?",
    "Quelle valeur est indiquée pour Series A ?",
    "Quel chiffre apparaît pour Series A ?",
    "Quel est le total indiqué dans la cellule B15 ?",
    "Montre-moi la ligne « Classement 2025 ».",
])
def test_reading_an_existing_row_stays_documentary(q):
    d = _d(q)
    assert d.applies is False, q


def test_reading_the_total_row_is_a_lookup_not_a_computation():
    """Le classeur PORTE une ligne de total : la lire est documentaire.

    C'est la distinction centrale — et c'est elle qui rend la garde utilisable :
    lire « 23 379 395 » écrit en B15 est licite ; DÉCIDER que c'est le total métier
    de l'entreprise ne l'est pas.
    """
    assert _d("Quelle valeur est indiquée sur la ligne SUBTOTAL ?").applies is False
    assert _d("Quel est le total de la société ?").applies is True


# --------------------------------------------------------------------------- #
# Énumérations exhaustives — bloquées
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "Donne toutes les lignes de Shares Outstanding.",
    "Énumère toutes les catégories et leurs valeurs.",
    "Liste l'intégralité des shares outstanding.",
    "Donne le détail complet par type de titre.",
    "Quels sont tous les éléments de la colonne ?",
    "Recense chaque ligne du tableau.",
    "Liste toutes les catégories.",
    "Donne toutes les lignes et leurs valeurs.",
    "Énumère l'intégralité de la colonne.",
    "Recense chaque type de titre.",
    "Montre chaque ligne du classeur, sans omission.",
])
def test_an_exhaustive_listing_is_blocked(q):
    d = _d(q)
    assert d.applies is True, q
    assert d.intent in BLOCKING_INTENTS, q


def test_the_enumeration_intent_and_reason_are_dedicated():
    d = _d("Liste toutes les catégories.")
    assert d.intent == INTENT_EXHAUSTIVE_ENUMERATION
    assert d.reason == REASON_LISTING
    assert d.intent != INTENT_DOCUMENT_LOOKUP


def test_an_aggregate_wins_over_an_enumeration():
    """« Donne le total de toutes les catégories » est un CALCUL, pas une liste.

    Les deux sont bloqués, mais le message de refus doit parler du bon danger.
    """
    d = _d("Donne le total de toutes les catégories.")
    assert d.intent == INTENT_EXHAUSTIVE_AGGREGATION


# --------------------------------------------------------------------------- #
# Listes locales — autorisées
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "Quelles catégories apparaissent dans cet extrait ?",
    "Retrouve les lignes Series A.",
    "Donne deux exemples de catégories.",
    "Donne quelques exemples de catégories présentes dans les extraits.",
    "Quelles lignes concernent Series A ?",
    "Retrouve la ligne Common.",
    "Quels éléments apparaissent dans l'extrait cité ?",
    "Donne toutes les valeurs de la ligne Common.",
    "Montre toutes les colonnes de la cellule B15.",
])
def test_a_bounded_listing_stays_documentary(q):
    d = _d(q)
    assert d.applies is False, q


def test_the_word_list_alone_never_blocks():
    assert _d("Cette liste est-elle à jour ?").applies is False
    assert _d("Montre-moi la liste des prix.").applies is False


# --------------------------------------------------------------------------- #
# Sans contexte classeur — la garde ne s'applique jamais
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "Liste toutes les sections de ce court document PDF.",
    "Quel est le total indiqué dans ce contrat ?",
    "Additionne toutes les clauses.",
])
def test_without_workbook_context_nothing_is_blocked(q):
    assert _d(q, excel=False).applies is False, q


# --------------------------------------------------------------------------- #
# Messages de refus
# --------------------------------------------------------------------------- #
class TestRefusalMessages:
    def test_the_total_refusal_names_the_actual_danger(self):
        answer, suggestions = build_refusal(_d("Quel est le total de la société ?"))
        assert "total" in answer.lower()
        assert "faux" in answer.lower()
        assert suggestions

    def test_the_listing_refusal_admits_it_could_omit_rows(self):
        answer, _ = build_refusal(_d("Liste toutes les catégories."))
        low = answer.lower()
        assert "exhaustive" in low or "exhaustif" in low
        assert "omettre" in low or "partiel" in low

    def test_no_refusal_leaks_internals(self):
        for q in ["Quel est le total de la société ?", "Liste toutes les catégories."]:
            answer, suggestions = build_refusal(_d(q))
            blob = (answer + " ".join(suggestions)).lower()
            for leak in ("top-k", "top k", "rrf", "chunk", "embedding", "vecteur"):
                assert leak not in blob, (q, leak)


# --------------------------------------------------------------------------- #
# Le total ADJECTIVAL — la forme anglaise, sans préposition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("q", [
    "What is the total shares outstanding for AirWorks?",
    "TOTAL SHARES OUTSTANDING?",
    "What are the total shares issued?",
    "How much is the total capitalization?",
])
def test_an_adjectival_total_is_blocked_too(q):
    """L'anglais construit le total en ADJECTIF (« the total shares »), sans « of ».

    `_AGG_OF_SET` attend une préposition : il ne voyait rien. Un anglophone demandant un
    total obtenait donc un chiffre — le défaut même que la garde existe pour empêcher.
    """
    assert _d(q).applies is True, q


@pytest.mark.parametrize("q", [
    "Quelle valeur est indiquée dans la colonne % Total Fully Diluted pour Common ?",
    "Que contient la colonne % Total Fully Diluted ?",
    "What value is shown on the SUBTOTAL row?",
])
def test_a_total_inside_a_column_label_stays_readable(q):
    """Le classeur porte littéralement une colonne « % Total Fully Diluted ».

    C'est pourquoi la forme adjectivale n'est reconnue qu'en TÊTE de question : un
    « total » au milieu d'une phrase est le plus souvent un libellé, pas une opération.
    """
    assert _d(q).applies is False, q

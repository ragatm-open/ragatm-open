"""P3 — détection déterministe des signaux forts d'une question.

Fonctions pures : ni base, ni réseau, ni LLM. Ce qui est testé ici décide si un boost
conditionnel s'applique — une détection trop large changerait le comportement de
requêtes ordinaires, ce que la mission interdit explicitement.
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.rag_core.services.query.strong_signals import (
    TEMPORAL_AFTER,
    TEMPORAL_BEFORE,
    TEMPORAL_EARLIEST,
    TEMPORAL_LATEST,
    canonical_date_terms,
    extract_signals,
)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question,attendu",
    [
        ("Que disait la note du 12 mars 2026 ?", date(2026, 3, 12)),
        ("la note du 1er avril 2026", date(2026, 4, 1)),
        ("note du 2026-03-12", date(2026, 3, 12)),
        ("note du 12/03/2026", date(2026, 3, 12)),
        ("note du 12.03.2026", date(2026, 3, 12)),
        ("the March 12, 2026 note", date(2026, 3, 12)),
        ("the March 12 2026 note", date(2026, 3, 12)),
        # Accents : « février » et « fevrier » doivent donner le même résultat.
        ("la note du 3 février 2026", date(2026, 2, 3)),
        ("la note du 3 fevrier 2026", date(2026, 2, 3)),
    ],
)
def test_dates_completes_reconnues(question, attendu):
    assert extract_signals(question).exact_dates == (attendu,)


def test_mois_annee_sans_jour():
    signals = extract_signals("Quelles notes de mars 2026 parlent du Brent ?")
    assert signals.month_years == ((2026, 3),)
    assert signals.exact_dates == ()


def test_date_complete_nest_pas_recomptee_en_mois_annee():
    """« 12 mars 2026 » est UNE date, pas une date plus un mois/année."""
    signals = extract_signals("la note du 12 mars 2026")
    assert signals.exact_dates == (date(2026, 3, 12),)
    assert signals.month_years == ()


def test_date_impossible_ignoree():
    """31 février n'existe pas : on n'invente pas une date approchante."""
    assert extract_signals("la note du 31 fevrier 2026").exact_dates == ()


def test_dates_multiples_dedupliquees():
    signals = extract_signals("entre le 12 mars 2026 et le 12 mars 2026")
    assert signals.exact_dates == (date(2026, 3, 12),)


# --------------------------------------------------------------------------- #
# Opérateurs temporels — jamais convertis en date absolue
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question,attendu",
    [
        ("Quelle est la note la plus recente ?", TEMPORAL_LATEST),
        ("Quelle est la derniere note ?", TEMPORAL_LATEST),
        ("Quelle est la note la plus ancienne ?", TEMPORAL_EARLIEST),
        ("Quelle est la premiere note mentionnant 100 dollars ?", TEMPORAL_EARLIEST),
        ("la note publiee avant celle du 13 mars 2026", TEMPORAL_BEFORE),
        ("la note publiee apres celle du 13 mars 2026", TEMPORAL_AFTER),
        ("Que dit la note sur le Brent ?", None),
    ],
)
def test_operateurs_temporels(question, attendu):
    assert extract_signals(question).temporal_operator == attendu


def test_operateur_relatif_ne_produit_aucune_date():
    """« la plus récente » n'a de sens que face à un corpus : pas de date inventée."""
    signals = extract_signals("Quelle est la note la plus recente ?")
    assert signals.exact_dates == ()
    assert signals.month_years == ()
    assert signals.has_strong_signal is False


# --------------------------------------------------------------------------- #
# Valeurs numériques
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question,attendu",
    [
        ("un Brent proche de 80 dollars", Decimal("80")),
        ("le Brent depasse 100 dollars", Decimal("100")),
        ("un Brent proche de 111 dollars le baril", Decimal("111")),
        ("$98/bl a l'ouverture", Decimal("98")),
        ("98 $/bl a l'ouverture", Decimal("98")),
        ("une hausse de 4,5 pourcent", Decimal("4.5")),
        ("une hausse de 4.5 pourcent", Decimal("4.5")),
    ],
)
def test_valeurs_numeriques_reconnues(question, attendu):
    assert attendu in extract_signals(question).numeric_values


def test_100_et_111_ne_sont_pas_confondus():
    assert extract_signals("proche de 111 dollars").numeric_values == (Decimal("111"),)
    assert extract_signals("au-dessus de 100 dollars").numeric_values == (Decimal("100"),)


def test_annee_nest_pas_une_valeur_metier():
    """« depuis 2022 » est un repère temporel, pas un prix."""
    signals = extract_signals("au-dessus de 100 dollars pour la premiere fois depuis 2022")
    assert Decimal("100") in signals.numeric_values
    assert Decimal("2022") not in signals.numeric_values


def test_chiffres_dune_date_ne_deviennent_pas_des_valeurs():
    signals = extract_signals("la note du 12 mars 2026")
    assert signals.numeric_values == ()


def test_decimales_preservees():
    assert Decimal("96.2") in extract_signals("a 96,2 dollars le baril").numeric_values


# --------------------------------------------------------------------------- #
# Absence de signal — le cas qui doit laisser le pipeline intact
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        "Que disent les notes sur le petrole ?",
        "Resume les analyses de marche.",
        "Quelles sont les perspectives sur les taux ?",
        "",
    ],
)
def test_aucun_signal_detecte(question):
    signals = extract_signals(question)
    assert signals.has_strong_signal is False
    assert signals.has_any_signal is False
    assert signals.reason_codes() == ()


# --------------------------------------------------------------------------- #
# Robustesse
# --------------------------------------------------------------------------- #


def test_question_tres_longue_est_bornee():
    """L'analyse doit rester bornée quoi qu'il arrive par l'API."""
    signals = extract_signals("a " * 50_000 + "12 mars 2026")
    # Au-delà de la borne, la date finale n'est pas lue — et surtout rien n'explose.
    assert signals.exact_dates == ()


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE rag_core_chunk; --",
        "note du 12 mars 2026' OR '1'='1",
        "\x00\x01\x02 note du 12 mars 2026",
        "note & | ! ( ) : * du 12 mars 2026",
        "<script>alert(1)</script> 100 dollars",
    ],
)
def test_chaines_hostiles_ne_font_rien_exploser(hostile):
    """La détection ne construit aucune requête : elle ne fait que lire une chaîne."""
    signals = extract_signals(hostile)
    assert isinstance(signals.reason_codes(), tuple)


# --------------------------------------------------------------------------- #
# Formes canoniques ajoutées à la recherche lexicale
# --------------------------------------------------------------------------- #


def test_forme_canonique_iso():
    signals = extract_signals("Que disait la note du 18 mai 2026 ?")
    assert canonical_date_terms(signals) == ("2026-05-18",)


def test_forme_canonique_mois():
    signals = extract_signals("les notes de mars 2026")
    assert canonical_date_terms(signals) == ("2026-03",)


def test_aucune_forme_canonique_sans_date():
    assert canonical_date_terms(extract_signals("Que dit la note ?")) == ()

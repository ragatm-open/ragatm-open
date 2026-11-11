"""Références à slash (`FIN01/FIN01-01`) — le lexème que le plan FTS ne pouvait matcher.

Le défaut, mesuré en P2.5-b
---------------------------
PostgreSQL en configuration `simple` tokenise `FIN01/FIN01-01` en **un seul lexème**
`'fin01/fin01-01'` (règle `file`). Il n'émet ni `fin01`, ni `fin01-01`. Or
`_IDENTIFIER_RE` s'appuie sur `\\b` : sur cette référence il ne capture que le fragment
`FIN01-01`, et le plan devenait `références:* & fin01:* & fin01-01` — une conjonction de
deux termes ABSENTS du vecteur, donc sans match possible :

    to_tsquery('simple', 'fin01/fin01-01')     -> match
    to_tsquery('simple', 'fin01 & fin01-01')   -> AUCUN match

Le document contenait la référence, était indexé, et restait introuvable. 7 des 13 échecs
Mission de calibration portaient une référence de cette forme ; les corriger a porté le
Recall@5 de 0,745 à 0,882 sans perdre un seul cas.

Ces tests sont 100 % synthétiques — aucune donnée du corpus client.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import override_settings

from apps.rag_core.repositories.chunk_repository import (
    build_tsquery_plan,
    extract_slash_identifiers,
)


# --------------------------------------------------------------------------
# Extraction : ce qu'on reconnaît, et ce qu'on refuse de reconnaître
# --------------------------------------------------------------------------
def test_extrait_une_reference_a_slash():
    assert extract_slash_identifiers("champ Références pour FIN01/FIN01-01 ?") == [
        "'fin01/fin01-01'"
    ]


def test_le_lexeme_est_quote():
    """Le quotage est structurel : `/` n'est pas un opérateur tsquery valide hors quotes."""
    (tok,) = extract_slash_identifiers("voir TRA51/TRA51-04a")
    assert tok.startswith("'") and tok.endswith("'")


def test_suffixe_alphanumerique_conserve():
    assert extract_slash_identifiers("voir TRA51/TRA51-04a") == ["'tra51/tra51-04a'"]


def test_sans_slash_rien_extrait():
    """`FIN01-01` seul relève de `extract_identifiers` — pas de doublon de responsabilité."""
    assert extract_slash_identifiers("la référence FIN01-01") == []


def test_une_date_n_est_pas_une_reference():
    assert extract_slash_identifiers("l'arrêté du 31/12/2025") == []


def test_un_mot_compose_n_est_pas_une_reference():
    assert extract_slash_identifiers("le rapport et/ou l'annexe") == []


def test_sans_chiffre_rien_extrait():
    assert extract_slash_identifiers("voir ABC/DEF-GH") == []


def test_dedoublonne_et_borne():
    q = " ".join(f"REF{i:02d}/REF{i:02d}-01" for i in range(8)) + " FIN01/FIN01-01 FIN01/FIN01-01"
    out = extract_slash_identifiers(q)
    assert len(out) <= 4
    assert len(out) == len(set(out))


def test_apostrophe_ne_peut_pas_s_echapper_du_lexeme():
    """Sécurité : une quote injectée refermerait le lexème et livrerait to_tsquery.

    Sans le filtre de charset, `FIN01/FIN01' | x:*'` deviendrait une expression tsquery
    arbitraire. On vérifie qu'il ne reste aucune quote AU MILIEU du jeton.
    """
    out = extract_slash_identifiers("voir FIN01/FIN01' | evil:*'-01")
    for tok in out:
        assert "'" not in tok[1:-1]


# --------------------------------------------------------------------------
# Plan de requête : le flag, et sa neutralité par défaut
# --------------------------------------------------------------------------
@override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=False)
def test_flag_off_plan_historique_inchange():
    """Défaut : aucun changement de comportement, au caractère près."""
    q = "Quelle référence est indiquée au champ « Références » pour « FIN01/FIN01-01 » ?"
    assert not any("/" in p for p in build_tsquery_plan(q))


@override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=True)
def test_flag_on_requete_slash_en_tete_de_plan():
    """La plus sélective d'abord : `search_chunks_hybrid` s'arrête au premier match."""
    q = "Quelle référence est indiquée au champ « Références » pour « FIN01/FIN01-01 » ?"
    plan = build_tsquery_plan(q)
    assert plan[0] == "'fin01/fin01-01'"


@override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=True)
def test_flag_on_le_plan_historique_survit_en_repli():
    """Purement additif : on ajoute une requête, on n'en retire aucune."""
    q = "Quelle référence est indiquée au champ « Références » pour « FIN01/FIN01-01 » ?"
    with override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=False):
        avant = build_tsquery_plan(q)
    apres = build_tsquery_plan(q)
    assert apres[1:] == avant


@override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=True)
def test_flag_on_sans_reference_plan_inchange():
    """Une question ordinaire ne doit pas voir son plan bouger."""
    q = "Quel est le port local du service ?"
    with override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=False):
        avant = build_tsquery_plan(q)
    assert build_tsquery_plan(q) == avant


# --------------------------------------------------------------------------
# Le fait PostgreSQL qui fonde tout le lot — vérifié, pas supposé
# --------------------------------------------------------------------------
@pytest.mark.django_db
def test_postgres_tokenise_la_reference_en_un_seul_lexeme():
    """Le fait mesuré : `fin01` et `fin01-01` n'existent PAS dans le vecteur."""
    with connection.cursor() as cur:
        cur.execute("SELECT to_tsvector('simple', %s)::text", ["Référence FIN01/FIN01-01 active"])
        vec = cur.fetchone()[0]
    assert "'fin01/fin01-01'" in vec
    assert "'fin01-01'" not in vec


@pytest.mark.django_db
def test_le_lexeme_matche_la_ou_la_decomposition_echoue():
    """Le avant/après du lot, sur le même texte : c'est ce qui débloque les 7 cas."""
    texte = "Point de contrôle FIN01/FIN01-01 — reporting national de taux."
    with connection.cursor() as cur:
        cur.execute(
            "SELECT to_tsvector('simple',%s) @@ to_tsquery('simple',%s),"
            "       to_tsvector('simple',%s) @@ to_tsquery('simple',%s)",
            [texte, "'fin01/fin01-01'", texte, "fin01 & fin01-01"],
        )
        avec_lexeme, avec_decomposition = cur.fetchone()
    assert avec_lexeme is True
    assert avec_decomposition is False


@pytest.mark.django_db
@override_settings(RAG_FTS_SLASH_IDENTIFIERS_ENABLED=True)
def test_le_plan_produit_est_une_requete_tsquery_valide():
    """Garde-fou syntaxe : un plan qui lève est un plan qui casse la recherche."""
    q = "Quelle référence est indiquée au champ « Références » pour « FIN01/FIN01-01 » ?"
    with connection.cursor() as cur:
        for tsq in build_tsquery_plan(q):
            cur.execute("SELECT to_tsquery('simple', %s) IS NOT NULL", [tsq])
            assert cur.fetchone()[0] is True

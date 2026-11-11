"""Normalisation de requête lexicale V2 (`RAG_FTS_QUERY_NORMALIZATION_V2`).

Le défaut corrigé : la configuration FTS `french` ne reconnaît pas `what` / `does` /
`the` comme mots vides. L'entrée « sélective » du plan historique se réduisait à
`what:*`, ramenait des résultats NON VIDES, et le repli — conditionné à zéro résultat —
n'était donc jamais atteint.

Ces tests vérifient la **requête construite**, pas seulement le résultat final : c'est
la requête qui portait le défaut.
"""
import pytest

from apps.rag_core.repositories import chunk_repository as repo


def _plan(question, enabled, settings):
    settings.RAG_FTS_QUERY_NORMALIZATION_V2 = enabled
    return repo.build_tsquery_plan(question)


def _terms(tsq):
    return [t.strip().removesuffix(":*") for t in tsq.split("|")]


# --------------------------------------------------------------------------- #
# Le cas qui a révélé le défaut
# --------------------------------------------------------------------------- #


def test_b009_le_plan_historique_est_domine_par_un_mot_banal(settings):
    """Sentinelle B009 — sans la correction, l'entrée sélective se réduit à `what`."""
    q = 'What does the expression "option pool shuffle" refer to when reading a financing offer?'
    plan = _plan(q, False, settings)

    assert plan, "le plan historique ne doit pas être vide"
    assert "what:*" in plan[0], (
        "le défaut attendu : l'entrée SÉLECTIVE du plan est dominée par un mot vide "
        f"anglais non reconnu par la configuration française — obtenu {plan[0]!r}"
    )


def test_b009_la_normalisation_preserve_les_termes_discriminants(settings):
    """Sentinelle B009 — la correction conserve `option`, `pool`, `shuffle`."""
    q = 'What does the expression "option pool shuffle" refer to when reading a financing offer?'
    plan = _plan(q, True, settings)

    assert len(plan) == 1, "la normalisation V2 produit UNE requête, sans repli de plan"
    terms = _terms(plan[0])
    assert {"option", "pool", "shuffle"} <= set(terms)
    assert "what" not in terms and "does" not in terms and "the" not in terms


def test_b027_valley_of_death_conserve_ses_termes_rares(settings):
    """Sentinelle B027.

    F1 rend la preuve VISIBLE ; elle ne prétend pas la classer en tête — le classement
    lexical reste `ts_rank`, et le lot rare-term a mesuré que le corriger ne marche pas.
    Ce test vérifie donc la présence des termes, pas un rang.
    """
    q = "What exactly does the expression valley of death designate in the life of a start-up?"
    terms = _terms(_plan(q, True, settings)[0])

    assert "valley" in terms and "death" in terms
    assert "start-up" in terms, "le tiret est porteur : il ne doit pas être détruit"


# --------------------------------------------------------------------------- #
# Propriétés de la variante qualifiée — les modifier produirait une autre variante
# --------------------------------------------------------------------------- #


def test_les_tirets_et_apostrophes_survivent(settings):
    """`bring-along`, `anti-dilution`, `d'ajustement` sont les termes les plus
    discriminants de leur question. `_sanitize_ts_token` les détruirait."""
    q = "Que permet une clause d'obligation de sortie conjointe, aussi appelée bring-along ?"
    terms = _terms(_plan(q, True, settings)[0])

    assert "bring-along" in terms
    assert "d'obligation" in terms


def test_les_mots_vides_francais_ne_sont_PAS_filtres(settings):
    """La variante qualifiée ne filtre que l'ANGLAIS.

    La prose d'un artefact de campagne affirmait « anglais et français » ; les traces
    gelées prouvent le contraire. Implémenter le filtrage français produirait une
    variante NON évaluée — ce test verrouille le comportement réellement mesuré.
    """
    q = "Quels sont les deux mécanismes d'ajustement utilisés par une clause anti-dilution ?"
    terms = _terms(_plan(q, True, settings)[0])

    assert "quels" in terms and "sont" in terms and "les" in terms
    assert "anti-dilution" in terms


def test_aucune_deduplication(settings):
    """La variante qualifiée ne déduplique pas ; `to_tsquery` s'en charge."""
    terms = _terms(_plan("venture capital and venture debt", True, settings)[0])
    assert terms.count("venture") == 2


def test_bornes_longueur_et_nombre_de_termes(settings):
    """Longueur minimale 3, plafond de 12 termes.

    Le seuil ne s'applique que lorsqu'il reste au moins un terme long : une question
    entièrement composée de jetons courts emprunte le repli (test dédié ci-dessous).
    """
    terms = _terms(_plan("un ok de la ab cd valorisation", True, settings)[0])
    assert terms == ["valorisation"], f"jetons trop courts retenus : {terms}"

    longue = " ".join(f"terme{i}" for i in range(30))
    assert len(_terms(_plan(longue, True, settings)[0])) == 12


def test_repli_sur_jetons_bruts_quand_tout_est_trop_court(settings):
    """Une question de jetons courts ne doit pas annuler la recherche."""
    terms = _terms(_plan("un ok de la ab cd", True, settings)[0])
    assert terms == ["un", "ok", "de", "la", "ab", "cd"]


def test_question_composee_uniquement_de_mots_vides(settings):
    """Ne jamais produire une requête vide : repli sur les premiers jetons bruts."""
    terms = _terms(_plan("What is the and or of", True, settings)[0])
    assert terms, "une question de mots vides ne doit pas annuler la recherche"
    assert len(terms) <= 6


def test_question_vide_ne_leve_pas(settings):
    assert _plan("", True, settings) == []
    assert _plan("   ", True, settings) == []


def test_casse_normalisee_en_minuscules(settings):
    terms = _terms(_plan("Option Pool SHUFFLE", True, settings)[0])
    assert terms == ["option", "pool", "shuffle"]


def test_ponctuation_et_nombres(settings):
    terms = _terms(_plan("Combien de fonds (656) ont dépassé 7,5x en 2020 ?", True, settings)[0])
    assert "656" in terms and "2020" in terms
    assert all("(" not in t and ")" not in t for t in terms)


def test_separateur_en_bord_ne_produit_pas_de_syntaxe_invalide(settings):
    """Garde-fou : `to_tsquery('start-:*')` lèverait une erreur de syntaxe.

    Aucun des 63 jeux de traces gelés ne contient ce cas — le rognage est un no-op sur
    la variante évaluée et supprime un mode d'échec jamais exercé.
    """
    terms = _terms(_plan("le terme start- up et -dilution", True, settings)[0])
    assert all(not t.startswith("-") and not t.endswith("-") for t in terms), terms
    assert all(t for t in terms), "aucun terme vide ne doit subsister"


# --------------------------------------------------------------------------- #
# Drapeau et non-régression F0
# --------------------------------------------------------------------------- #


def test_drapeau_off_par_defaut(settings):
    """Le comportement historique reste le défaut tant que V2 n'est pas qualifiée."""
    from django.conf import settings as dj

    assert getattr(dj, "RAG_FTS_QUERY_NORMALIZATION_V2", False) is False


def test_drapeau_off_conserve_le_plan_historique(settings):
    """F0 doit rester strictement lui-même : plan multi-entrées, repli disponible."""
    q = "Que dit-on de ORVANIS dans le rapport 2020 ?"
    plan_v1 = _plan(q, False, settings)

    assert len(plan_v1) >= 1
    # l'entrée sélective historique reste en tête
    assert "orvanis" in plan_v1[0]


def test_les_deux_magasins_partagent_la_construction(settings):
    """Le leg privé et le leg corpus métier consomment le MÊME constructeur.

    C'est ce qui rend impossible la divergence qui avait déjà privé le corpus de tout
    repli (correctif D1).
    """
    from apps.corpus.services import retrieval as corpus_retrieval

    assert corpus_retrieval.build_tsquery_plan_with_telemetry is (
        repo.build_tsquery_plan_with_telemetry
    )


def test_telemetrie_sans_contenu_lexical(settings):
    """Les compteurs sont exposés ; les jetons eux-mêmes ne le sont jamais."""
    settings.RAG_FTS_QUERY_NORMALIZATION_V2 = True
    plan, mode, raw_n, norm_n = repo.build_tsquery_plan_with_telemetry(
        "What does the option pool shuffle mean?"
    )

    assert mode == "v2"
    assert raw_n == 7 and norm_n == 4
    assert len(plan) == 1

    settings.RAG_FTS_QUERY_NORMALIZATION_V2 = False
    _plan_v1, mode_v1, raw_v1, _norm_v1 = repo.build_tsquery_plan_with_telemetry("test query")
    assert mode_v1 == "v1" and raw_v1 == 2

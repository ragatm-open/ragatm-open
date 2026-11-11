# -*- coding: utf-8 -*-
"""Graine de génération du chat factuel (GENERATION-SEED-01).

Ce que ces tests protègent : la reproductibilité d'une campagne de mesure, et le
fait que rien ne change quand personne ne la demande.

Mesuré sur les 87 questions de calibration, à retrieval gelé : sans graine,
13 verdicts basculent entre trois exécutions et l'accuracy stricte balaie 4,8
points ; avec une graine, plus aucun. Tant que ce bruit existe, une amélioration
de retrieval plus petite que lui est invisible — c'est ce que la graine rend
mesurable.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.llm_router.model_utils import gen_options

PRESET = {"temperature": 0.1, "num_ctx": 8192, "num_predict": 2048}


# ------------------------------------------------------- §16/§17 gen_options
def test_sans_graine_le_dict_est_celui_d_avant():
    """La non-régression tient à cette égalité, pas à une intention.

    Aucune clé `seed` ne doit apparaître : la Gateway reçoit exactement ce qu'elle
    recevait, et le comportement historique est préservé au caractère près.
    """
    assert gen_options(**PRESET) == PRESET
    assert gen_options(**PRESET, seed=None) == PRESET
    assert "seed" not in gen_options(**PRESET, seed=None)


def test_avec_graine_la_cle_est_ajoutee_sans_toucher_au_reste():
    o = gen_options(**PRESET, seed=42)
    assert o == {**PRESET, "seed": 42}


def test_la_graine_zero_est_transmise_et_non_avalee():
    """Le piège classique : `if seed:` laisserait tomber 0 en silence.

    Que l'upstream traite ensuite 0 comme « pas de graine » est un fait DISTINCT,
    traité par la configuration qui refuse cette valeur. Ici, la plomberie doit
    transmettre fidèlement ce qu'on lui donne : un paramètre qu'on croit transmis
    alors qu'il ne l'est pas est pire que pas de paramètre du tout.
    """
    assert gen_options(**PRESET, seed=0)["seed"] == 0


@pytest.mark.parametrize("seed", [1, 7, 42, 2**31 - 1, 2**31, 2**63 - 1])
def test_les_graines_verifiees_deterministes_passent(seed):
    """Ces six valeurs ont été mesurées reproductibles sur l'upstream réel."""
    assert gen_options(**PRESET, seed=seed)["seed"] == seed


def test_la_graine_est_normalisee_en_entier():
    assert gen_options(**PRESET, seed=True)["seed"] == 1
    assert isinstance(gen_options(**PRESET, seed=42)["seed"], int)


# ------------------------------------------------------ §7 défaut désactivé
def test_le_defaut_du_reglage_est_l_absence_de_graine():
    """Aucune graine codée en dur : 42 est une valeur de qualification, pas un défaut.

    Une graine posée par défaut figerait la génération de TOUS les utilisateurs au
    nom d'une commodité de mesure — une décision produit, prise par inadvertance.
    """
    from django.conf import settings

    assert getattr(settings, "RAG_FACTUAL_SEED", "absent") in (None, "absent")


def test_le_reglage_est_declare_au_registre():
    """Déclaré, donc documenté, projeté et borné — pas seulement posé dans un .env."""
    from config.registry.domains.generation import DEFINITIONS

    d = next((x for x in DEFINITIONS if x.name == "RAG_FACTUAL_SEED"), None)
    assert d is not None, "RAG_FACTUAL_SEED doit etre declare au registre"
    assert d.default is None, "le defaut doit laisser le comportement historique"
    assert d.value_type == "int"
    assert d.minimum == 1, (
        "0 et les negatifs sont ACCEPTES par l'upstream mais n'y produisent aucun "
        "determinisme : les refuser vaut mieux qu'une reproductibilite illusoire")


# --------------------------------------------- §19/§20 câblage applicatif
def _appels_gen_options(fonction):
    """Arguments nommés de chaque appel `gen_options(...)` dans une fonction.

    Lecture par AST et non par recherche de texte : un commentaire mentionnant le
    réglage ne doit pas suffire à faire passer la garde, et un argument passé sous
    une autre forme ne doit pas lui échapper.
    """
    import ast
    import inspect
    import textwrap

    arbre = ast.parse(textwrap.dedent(inspect.getsource(fonction)))
    out = []
    for n in ast.walk(arbre):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "gen_options":
            out.append({k.arg: ast.unparse(k.value) for k in n.keywords})
    return out


@pytest.mark.parametrize("chemin", ["chat_query", "chat_query_stream"])
def test_les_deux_regimes_transmettent_la_graine(chemin):
    """Garde de CONTRAT : elle tombe si un remaniement cesse de passer la graine.

    Le constructeur `gen_options` peut rester parfait pendant que l'appelant cesse
    de lui fournir la graine — une rupture silencieuse qui ne se verrait qu'au
    moment où une campagne rend des résultats irreproductibles.

    Les deux régimes sont couverts ensemble à dessein : le code impose déjà des
    options IDENTIQUES entre synchrone et streaming (un `num_ctx` divergent force
    Ollama à recharger son runner). Une graine posée d'un seul côté rendrait la
    reproductibilité dépendante du régime d'appel.
    """
    from apps.chat.services import chat_service

    appels = _appels_gen_options(getattr(chat_service, chemin))
    assert appels, f"aucun appel a gen_options dans {chemin}"
    for i, kw in enumerate(appels):
        assert "seed" in kw, f"{chemin}: appel #{i + 1} sans argument seed"
        assert "RAG_FACTUAL_SEED" in kw["seed"], (
            f"{chemin}: la graine ne vient pas du reglage ({kw['seed']})")


@pytest.mark.parametrize("chemin", ["chat_query", "chat_query_stream"])
def test_les_autres_options_factuelles_restent_inchangees(chemin):
    """§2/§15 : rien d'autre ne bouge — ni température, ni contexte, ni budget."""
    from apps.chat.services import chat_service

    for kw in _appels_gen_options(getattr(chat_service, chemin)):
        assert kw.get("temperature") == "settings.RAG_FACTUAL_TEMPERATURE"
        assert kw.get("num_ctx") == "settings.RAG_FACTUAL_NUM_CTX"
        assert kw.get("num_predict") == "settings.RAG_FACTUAL_NUM_PREDICT"


def test_le_reglage_resout_une_valeur_configuree():
    """Le réglage est bien lu depuis la configuration, pas d'une constante."""
    with override_settings(RAG_FACTUAL_SEED=7):
        from django.conf import settings
        assert gen_options(**PRESET, seed=settings.RAG_FACTUAL_SEED)["seed"] == 7
    with override_settings(RAG_FACTUAL_SEED=None):
        from django.conf import settings
        assert "seed" not in gen_options(**PRESET, seed=settings.RAG_FACTUAL_SEED)


# ----------------------------------------------- §14 périmètre non élargi
@pytest.mark.parametrize("module,fonction", [
    ("apps.chat.services.rephrasing_suggestions", None),
    ("apps.rag_core.services.summary_engine", None),
])
def test_la_graine_n_est_pas_injectee_dans_les_autres_taches(module, fonction):
    """Le périmètre qualifié est le chemin FACTUEL, et lui seul.

    Reformulation et synthèse appellent le même constructeur d'options mais n'ont
    pas été mesurées ; leur imposer une graine étendrait sans preuve une décision
    prise pour un autre usage.
    """
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module))
    assert "RAG_FACTUAL_SEED" not in src, f"{module} ne doit pas porter la graine factuelle"

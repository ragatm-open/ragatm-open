"""Le modèle des embeddings gouvernés doit être EXPLICITE — et le défaut est un piège.

`AI_GATEWAY_EMBEDDINGS_MODEL` vaut `fr-en-retrieval` par défaut. Ce modèle est refusé
par la Gateway de PRODUCTION. Activer les embeddings gouvernés sans poser ce réglage
produit un environnement syntaxiquement valide et fonctionnellement mort : la
configuration passe, chaque document échoue — après coup.

Le mode de défaillance que ce check attrape n'est pas une erreur : c'est un défaut
dangereux appliqué en silence.
"""
import pytest
from django.test import override_settings

from apps.rag_core.checks_embeddings import check_governed_embeddings_model


def _ids(errs):
    return sorted(e.id for e in errs)


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=False,
                   AI_GATEWAY_EMBEDDINGS_MODEL="fr-en-retrieval")
def test_inactif_ne_bloque_rien():
    """§69 — une installation qui n'utilise pas les embeddings gouvernés n'est pas
    concernée, même avec le défaut dangereux posé."""
    assert check_governed_embeddings_model(None) == []


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=True,
                   AI_GATEWAY_EMBEDDINGS_MODEL="legacy-768")
def test_actif_avec_un_modele_declare_est_accepte():
    assert check_governed_embeddings_model(None) == []


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=True, AI_GATEWAY_EMBEDDINGS_MODEL="")
def test_actif_sans_modele_est_refuse():
    assert _ids(check_governed_embeddings_model(None)) == ["rag_core.E010"]


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=True,
                   AI_GATEWAY_EMBEDDINGS_MODEL="   ")
def test_un_modele_blanc_vaut_un_modele_absent():
    assert _ids(check_governed_embeddings_model(None)) == ["rag_core.E010"]


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=True,
                   AI_GATEWAY_EMBEDDINGS_MODEL="un-modele-jamais-declare")
def test_un_modele_hors_table_de_capacites_est_refuse():
    """Un modèle dont le plafond n'est pas déclaré ferait retomber le découpage sur la
    borne conservatrice sans qu'aucune mesure ne le justifie."""
    assert _ids(check_governed_embeddings_model(None)) == ["rag_core.E011"]


@override_settings(AI_GATEWAY_EMBEDDINGS_ENABLED=True,
                   AI_GATEWAY_EMBEDDINGS_MODEL="fr-en-retrieval")
def test_le_defaut_reste_accepte_la_ou_il_est_legitime():
    """`fr-en-retrieval` est déclaré et légitime en staging. Le check est LOCAL : il ne
    connaît pas l'autorisation par environnement, qui relève du préflight
    `check_embedding_capabilities`. Le prétendre ici ferait dépendre le démarrage d'un
    appel réseau — §68."""
    assert check_governed_embeddings_model(None) == []


def test_le_check_ne_joint_pas_la_gateway():
    """§68 — le démarrage ne doit pas dépendre d'un appel réseau."""
    import ast
    import inspect

    arbre = ast.parse(inspect.getsource(check_governed_embeddings_model))
    interdits = {"urlopen", "get", "post", "request", "gateway_models", "requests"}
    vus = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Call):
            nom = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nom in interdits:
                vus.add(nom)
    assert not vus, f"le check joint le réseau : {sorted(vus)}"


def test_le_check_est_enregistre():
    """Un check non enregistré ne protège rien."""
    from django.core.checks import registry

    noms = {getattr(c, "__name__", "") for c in registry.registry.get_checks()}
    assert "check_governed_embeddings_model" in noms

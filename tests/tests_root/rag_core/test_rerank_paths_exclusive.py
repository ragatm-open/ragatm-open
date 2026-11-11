"""Les DEUX chemins de reranking ne peuvent plus être actifs en même temps.

Ce que ce fichier verrouille
----------------------------
Le dépôt porte deux mécanismes de reranking, pilotés par deux drapeaux dont les
noms ne diffèrent que d'une lettre :

- ``RAG_RERANK_ENABLED``  — reranking HISTORIQUE, modèle chargé dans le process
  (``services/rerank.py``, appelé depuis ``services/query/candidate_reranker.py``) ;
- ``RAG_RERANKER_ENABLED`` — reranking via l'AI Gateway (``POST /v1/rerank``,
  ``services/rerank_gateway.py``), aucun modèle local.

Rien n'empêchait de les activer ensemble. La chaîne devenait alors : fusion RRF →
reranker Gateway → diversification → reranker historique → sélection. Le second
reranker réécrit le classement du premier — celui qui, par contrat, fait autorité.

Le coût n'est pas une panne : c'est une MESURE FAUSSE. Une campagne qui croit
qualifier le reranker Gateway mesurerait la composition des deux, et sa conclusion
serait promue en production comme si elle portait sur un seul mécanisme. Le mode de
panne est silencieux, ce qui justifie un refus au démarrage plutôt qu'un warning.

Ce fichier NE fige aucun classement : il ne teste que la garde de configuration.
"""
from __future__ import annotations

import pytest
from django.core.checks import Error

from apps.rag_core.checks_rerank import (
    ID_BOTH_RERANKERS,
    check_rerank_paths_exclusive,
)


# ===========================================================================
# 1. La table de vérité complète
# ===========================================================================
@pytest.mark.parametrize(
    "legacy,gateway",
    [
        (False, False),  # aucun reranking — l'état des deux environnements
        (True, False),   # reranker historique seul — configuration légitime
        (False, True),   # reranker AI Gateway seul — la cible de qualification
    ],
)
def test_the_three_legitimate_configurations_stay_valid(legacy, gateway, settings):
    """0/0, 1/0 et 0/1 restent valides : la garde refuse UNE combinaison, pas trois."""
    settings.RAG_RERANK_ENABLED = legacy
    settings.RAG_RERANKER_ENABLED = gateway

    assert check_rerank_paths_exclusive(None) == []


def test_both_enabled_is_refused(settings):
    settings.RAG_RERANK_ENABLED = True
    settings.RAG_RERANKER_ENABLED = True

    erreurs = check_rerank_paths_exclusive(None)

    assert len(erreurs) == 1
    assert isinstance(erreurs[0], Error)
    assert erreurs[0].id == ID_BOTH_RERANKERS == "rag_core.E008"


# ===========================================================================
# 2. Le message doit suffire à un exploitant qui découvre l'erreur
# ===========================================================================
def test_the_message_names_both_flags_and_which_is_which(settings):
    """Un exploitant doit pouvoir agir sans ouvrir le code : les deux noms se
    ressemblent trop pour qu'un message vague soit exploitable."""
    settings.RAG_RERANK_ENABLED = True
    settings.RAG_RERANKER_ENABLED = True

    erreur = check_rerank_paths_exclusive(None)[0]
    texte = f"{erreur.msg} {erreur.hint}"

    assert "RAG_RERANK_ENABLED" in texte and "RAG_RERANKER_ENABLED" in texte
    # lequel est l'historique, lequel est le Gateway
    assert "rerank.py" in texte and "rerank_gateway.py" in texte
    assert "/v1/rerank" in texte
    # et l'action attendue
    assert "RAG_RERANK_ENABLED=0" in texte and "RAG_RERANKER_ENABLED=0" in texte


# ===========================================================================
# 3. La garde lit les MÊMES sources que le pipeline
# ===========================================================================
def test_the_gateway_flag_is_read_through_the_client_own_reader(settings, monkeypatch):
    """Le check n'a pas sa propre résolution : il interroge `reranker_enabled()`,
    la fonction que l'orchestrateur appelle lui-même. Une garde qui lirait la
    configuration autrement pourrait diverger du chemin qu'elle prétend protéger."""
    from apps.rag_core.services import rerank_gateway

    settings.RAG_RERANK_ENABLED = True
    settings.RAG_RERANKER_ENABLED = False
    assert check_rerank_paths_exclusive(None) == []

    monkeypatch.setattr(rerank_gateway, "reranker_enabled", lambda: True)
    assert len(check_rerank_paths_exclusive(None)) == 1


def test_the_legacy_flag_is_the_one_the_retrieval_policy_uses():
    """La garde doit lire le drapeau qui DÉCIDE réellement de l'exécution.

    `candidate_reranker.rank_candidates` ne consulte pas les settings : il lit
    `policy.rerank_enabled`, dont le défaut est `DEFAULT_RERANK_ENABLED`, lui-même
    dérivé de `RAG_RERANK_ENABLED` (égalité verrouillée par
    `test_policies_no_ghost_defaults`). La chaîne est donc bien la même.
    """
    from apps.rag_core.domain import policies

    assert policies.RagPolicy().rerank_enabled is policies.DEFAULT_RERANK_ENABLED


# ===========================================================================
# 4. Résolution par le REGISTRE, pas seulement par `override_settings`
# ===========================================================================
@pytest.mark.parametrize(
    "legacy_env,gateway_env,attendu_refus",
    [
        ({}, {}, False),
        ({"RAG_RERANK_ENABLED": "1"}, {}, False),
        ({}, {"RAG_RERANKER_ENABLED": "1"}, False),
        ({"RAG_RERANK_ENABLED": "1"}, {"RAG_RERANKER_ENABLED": "1"}, True),
    ],
)
def test_the_truth_table_holds_on_values_resolved_by_the_registry(
    legacy_env, gateway_env, attendu_refus, settings
):
    """Les valeurs viennent du registre — la même résolution qu'au démarrage réel —
    et non d'un booléen Python écrit à la main dans le test."""
    from config.settings.from_registry import env_value

    environ = {**legacy_env, **gateway_env}
    settings.RAG_RERANK_ENABLED = env_value("RAG_RERANK_ENABLED", environ=environ)
    settings.RAG_RERANKER_ENABLED = env_value("RAG_RERANKER_ENABLED", environ=environ)

    erreurs = check_rerank_paths_exclusive(None)
    assert bool(erreurs) is attendu_refus


def test_an_empty_environment_enables_neither_reranker():
    """L'invariant de sûreté : déployer ce code sans poser aucune variable ne peut
    pas produire la combinaison interdite."""
    from config.settings.from_registry import env_value

    assert env_value("RAG_RERANK_ENABLED", environ={}) is False
    assert env_value("RAG_RERANKER_ENABLED", environ={}) is False


# ===========================================================================
# 5. La garde est réellement branchée au démarrage
# ===========================================================================
def test_the_check_is_registered_with_django():
    """Un check non enregistré est un commentaire : `manage.py check` doit le jouer."""
    from django.core.checks import registry as checks_registry

    enregistres = {c.__name__ for c in checks_registry.registry.get_checks()}
    assert "check_rerank_paths_exclusive" in enregistres


# ===========================================================================
# 6. Les traces des deux chemins ne partagent plus un seul nom
# ===========================================================================
# Dans `query_rag`, le dict de trace du reranking Gateway et l'objet `RerankInfo`
# du reranking historique s'appelaient tous deux `rerank_info`, dans la MÊME
# portée : la seconde affectation écrasait la première. Aucun classement n'en
# dépendait — l'audit persisté est bien celui du chemin historique — mais un nom
# qui désigne deux choses selon l'endroit de la fonction est une trace qu'on ne
# peut pas lire, et un écrasement qu'on ne peut pas voir venir.
def _noms_affectes_dans_query_rag() -> set[str]:
    """Noms liés par affectation dans `query_rag`, relus du source."""
    import ast
    import pathlib

    chemin = (
        pathlib.Path(__file__).resolve().parents[3]
        / "backend/apps/rag_core/services/query/orchestrator.py"
    )
    arbre = ast.parse(chemin.read_text(encoding="utf-8"))
    fonction = next(
        n for n in ast.walk(arbre)
        if isinstance(n, ast.FunctionDef) and n.name == "query_rag"
    )
    noms: set[str] = set()
    for noeud in ast.walk(fonction):
        if isinstance(noeud, ast.Name) and isinstance(noeud.ctx, ast.Store):
            noms.add(noeud.id)
        elif isinstance(noeud, ast.AnnAssign) and isinstance(noeud.target, ast.Name):
            noms.add(noeud.target.id)
    return noms


def test_the_two_rerank_traces_have_distinct_names():
    noms = _noms_affectes_dans_query_rag()

    assert "gateway_rerank_info" in noms, "la trace du chemin Gateway doit se nommer"
    assert "legacy_rerank_info" in noms, "la trace du chemin historique doit se nommer"
    assert "rerank_info" not in noms, (
        "`rerank_info` désignait tour à tour les deux chemins dans la même portée : "
        "le nom ne doit plus être réintroduit"
    )


# ===========================================================================
# 7. Reranking Gateway activé sans adresse de Gateway (rag_core.E009)
# ===========================================================================
# Cette configuration ne casse rien : sous `fail_open_rrf`, chaque requête tente
# l'appel, échoue, et l'ordre RRF est rendu. Le service répond, les compteurs
# montent. C'est précisément ce qui la rend dangereuse — une campagne croirait
# mesurer un pipeline reranké et mesurerait la baseline, avec un drapeau à 1 pour
# l'attester.
from apps.rag_core.checks_rerank import (  # noqa: E402
    ID_RERANKER_WITHOUT_GATEWAY,
    check_reranker_gateway_address,
)


def test_an_enabled_reranker_without_an_address_is_refused(settings):
    settings.RAG_RERANKER_ENABLED = True
    settings.RAG_RERANKER_BASE_URL = ""

    erreurs = check_reranker_gateway_address(None)

    assert len(erreurs) == 1
    assert erreurs[0].id == ID_RERANKER_WITHOUT_GATEWAY == "rag_core.E009"


@pytest.mark.parametrize("adresse", ["", "   ", None])
def test_a_blank_address_counts_as_absent(adresse, settings):
    """Une adresse faite d'espaces n'est pas une adresse : `_base_url()` la réduit à
    la chaîne vide, et le comportement est celui d'une variable absente."""
    settings.RAG_RERANKER_ENABLED = True
    settings.RAG_RERANKER_BASE_URL = adresse

    assert len(check_reranker_gateway_address(None)) == 1


def test_an_enabled_reranker_with_an_address_passes(settings):
    settings.RAG_RERANKER_ENABLED = True
    settings.RAG_RERANKER_BASE_URL = "http://gateway.invalide:8200"

    assert check_reranker_gateway_address(None) == []


def test_a_disabled_reranker_never_requires_an_address(settings):
    """Drapeau éteint : la valeur est inerte. Exiger une URL d'un environnement qui
    ne rerank pas n'aurait aucun sens."""
    settings.RAG_RERANKER_ENABLED = False
    settings.RAG_RERANKER_BASE_URL = ""

    assert check_reranker_gateway_address(None) == []


def test_the_address_check_is_registered_with_django():
    from django.core.checks import registry as checks_registry

    enregistres = {c.__name__ for c in checks_registry.registry.get_checks()}
    assert "check_reranker_gateway_address" in enregistres


def test_the_message_explains_what_a_missing_address_would_produce(settings):
    """Le risque n'est pas « ça ne marche pas » : c'est une baseline servie sous
    l'étiquette « rerankée ». Le message doit le dire, sinon il sera lu comme une
    coquille de configuration sans conséquence."""
    settings.RAG_RERANKER_ENABLED = True
    settings.RAG_RERANKER_BASE_URL = ""

    erreur = check_reranker_gateway_address(None)[0]
    texte = f"{erreur.msg} {erreur.hint}"

    assert "fail_open_rrf" in texte and "fail_closed" in texte
    assert "RAG_RERANKER_BASE_URL" in texte and "RAG_RERANKER_ENABLED=0" in texte

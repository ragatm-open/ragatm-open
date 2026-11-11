"""System checks du reranking — provider valide (D1) et exclusion mutuelle des deux chemins.

Le rerank ne reconnaît que ``stub`` et ``cross_encoder``. Toute autre valeur
(``local``, ``crossencoder`` mal orthographié…) faisait tourner le **stub**
silencieusement : la qualité du reranking était dégradée sans que rien ne le
signale, et la trace d'exécution était indistinguable d'un stub légitime.

Journaliser après coup ne suffit pas pour une configuration de production
invalide : on refuse le démarrage. Le check est **bloquant uniquement quand le
reranking est activé** (``RAG_RERANK_ENABLED=1``) — si le reranking est éteint,
la valeur du provider n'a aucun effet et ne doit pas empêcher de démarrer.

Aucune valeur sensible n'est affichée : seul le nom du provider demandé, qui
n'est pas un secret.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error

from apps.rag_core.services.rerank import RERANK_PROVIDERS, normalize_rerank_provider


def check_rerank_provider(app_configs, **kwargs):
    """Refuse un provider de rerank inconnu lorsque le reranking est activé."""
    errors = []

    if not bool(getattr(settings, "RAG_RERANK_ENABLED", False)):
        # Reranking désactivé : la valeur est inerte, pas de blocage.
        return errors

    requested = normalize_rerank_provider(getattr(settings, "RAG_RERANK_PROVIDER", "stub"))
    if requested not in RERANK_PROVIDERS:
        errors.append(
            Error(
                f"RAG_RERANK_PROVIDER={requested!r} n'est pas un provider de reranking "
                f"reconnu, alors que RAG_RERANK_ENABLED=1.",
                hint=(
                    "Valeurs acceptées : "
                    f"{', '.join(sorted(RERANK_PROVIDERS))}. "
                    "Une valeur inconnue ferait tourner le stub à la place du "
                    "cross-encoder, dégradant la pertinence sans erreur visible."
                ),
                id="rag_core.E004",
            )
        )
    return errors


# ---------------------------------------------------------------------------
# Exclusion mutuelle des DEUX chemins de reranking (rag_core.E008)
# ---------------------------------------------------------------------------
# Deux mécanismes de reranking coexistent dans le dépôt, et rien n'empêchait de
# les activer ensemble :
#
#   - `RAG_RERANK_ENABLED`  — reranking HISTORIQUE, modèle chargé dans le process
#     (`services/rerank.py`, appelé par `services/query/candidate_reranker.py`) ;
#   - `RAG_RERANKER_ENABLED` — reranking AI GATEWAY (`POST /v1/rerank`), aucun
#     modèle local (`services/rerank_gateway.py`).
#
# Activés ensemble, la chaîne devient : fusion RRF -> reranker Gateway ->
# diversification -> reranker historique -> sélection. Le classement du Gateway,
# qui fait autorité par construction, est alors RÉÉCRIT en aval par un second
# modèle — et le plafond documentaire du chemin reranké (`RAG_RERANKER_MAX_PER_GROUP`)
# s'applique à une sélection que le second reranker réordonne ensuite. Aucun cas
# d'exploitation ne demande cette composition ; aucune documentation ne la décrit.
#
# Le coût de la laisser possible n'est pas une panne : c'est une MESURE FAUSSE.
# Une qualification qui croit mesurer le reranker Gateway mesurerait en réalité la
# composition des deux, sans que rien ne le signale. D'où le refus au démarrage,
# plutôt qu'un avertissement.
#
# Ce check ne DÉSACTIVE rien : il refuse une combinaison, et laisse l'exploitant
# choisir lequel des deux il veut.
ID_BOTH_RERANKERS = "rag_core.E008"


def check_rerank_paths_exclusive(app_configs, **kwargs):
    """Refuse l'activation SIMULTANÉE du reranking historique et du reranking Gateway.

    Chaque drapeau est lu par le lecteur qui fait déjà autorité pour son chemin :
    `settings.RAG_RERANK_ENABLED` pour l'historique (comme `check_rerank_provider`
    ci-dessus et `domain/policies.py`), et `rerank_gateway.reranker_enabled()` pour
    le Gateway — la fonction que l'orchestrateur interroge lui-même. Aucune
    résolution de configuration n'est réécrite ici.
    """
    # Import différé : le module client tire `ops_counters` et `diversify`, qui
    # n'ont rien à faire dans le chemin d'import de l'application.
    from apps.rag_core.services import rerank_gateway

    legacy_on = bool(getattr(settings, "RAG_RERANK_ENABLED", False))
    gateway_on = rerank_gateway.reranker_enabled()

    if not (legacy_on and gateway_on):
        return []

    return [
        Error(
            "RAG_RERANK_ENABLED et RAG_RERANKER_ENABLED sont activés en même temps : "
            "les deux mécanismes de reranking ne peuvent pas coexister.",
            hint=(
                "RAG_RERANK_ENABLED = reranking HISTORIQUE, modèle chargé dans le "
                "process (services/rerank.py). "
                "RAG_RERANKER_ENABLED = reranking via l'AI Gateway, POST /v1/rerank "
                "(services/rerank_gateway.py). "
                "Activés ensemble, le classement rendu par la Gateway est réécrit en "
                "aval par le reranker historique : une qualification croirait mesurer "
                "l'un et mesurerait la composition des deux. "
                "Désactiver l'un des deux — RAG_RERANK_ENABLED=0 pour utiliser le "
                "reranker AI Gateway, RAG_RERANKER_ENABLED=0 pour conserver le "
                "reranker historique."
            ),
            id=ID_BOTH_RERANKERS,
        )
    ]


# ---------------------------------------------------------------------------
# Reranking Gateway activé sans adresse de Gateway (rag_core.E009)
# ---------------------------------------------------------------------------
# `RAG_RERANKER_ENABLED=1` avec `RAG_RERANKER_BASE_URL` vide est une configuration
# qui « fonctionne » : sous `fail_open_rrf`, chaque requête tente l'appel, échoue, et
# rend l'ordre RRF. Rien ne casse. Le service répond. Les compteurs montent.
#
# C'est précisément ce qui la rend dangereuse pour une qualification : la campagne
# croirait mesurer un pipeline reranké et mesurerait la baseline, avec un drapeau à 1
# pour l'attester. Le repli est compté, mais un compteur ne se lit que si l'on sait
# qu'il faut le lire — et sous `fail_closed`, la même configuration ne dégrade pas :
# elle refuse toutes les réponses.
#
# L'adresse n'entre volontairement PAS dans l'identité gelée d'une campagne (c'est de
# la topologie, et une valeur privée) : la garde doit donc vivre ici. C'est la
# contrepartie exacte de ce choix de classification.
ID_RERANKER_WITHOUT_GATEWAY = "rag_core.E009"


def check_reranker_gateway_address(app_configs, **kwargs):
    """Refuse un reranking Gateway activé sans adresse de Gateway.

    Silencieux quand le drapeau est éteint : la valeur est alors inerte, et exiger une
    URL d'un environnement qui ne rerank pas n'aurait aucun sens.
    """
    from apps.rag_core.services import rerank_gateway

    if not rerank_gateway.reranker_enabled():
        return []
    if str(getattr(settings, "RAG_RERANKER_BASE_URL", "") or "").strip():
        return []

    return [
        Error(
            "RAG_RERANKER_ENABLED=1 mais RAG_RERANKER_BASE_URL est vide : aucun "
            "reranking ne peut aboutir.",
            hint=(
                "Chaque requête tenterait l'appel, échouerait, et la politique d'échec "
                "s'appliquerait — `fail_open_rrf` rendrait l'ordre RRF (une baseline "
                "servie sous une étiquette « rerankée »), `fail_closed` refuserait "
                "toutes les réponses. Poser l'adresse de l'AI Gateway (jamais celle "
                "d'un backend de reranking), ou RAG_RERANKER_ENABLED=0."
            ),
            id=ID_RERANKER_WITHOUT_GATEWAY,
        )
    ]

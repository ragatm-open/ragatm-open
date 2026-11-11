"""Défauts de configuration modèle d'un RagSpace — SOURCE DE VÉRITÉ UNIQUE.

Ce module ne contient QUE de la lecture de settings. Aucun import de modèle,
d'app Django ni de DRF : il est importable depuis `models.py` (les défauts de
champ en dépendent) sans risque de cycle ni d'effet de bord au chargement des
applications.

## Pourquoi ce module existe

La valeur `llama3.1:8b` était écrite en dur en **trois** endroits qui se
croyaient chacun source de vérité :

1. `RagSpace.llm_model` (défaut de champ Django) ;
2. `NEW_SPACE_DEFAULTS` dans le formulaire frontend ;
3. le repli `AI_GATEWAY_MODEL` du routeur.

Résultat observé en production le 27/07/2026 : `DEFAULT_RAGSPACE_CHAT_MODEL` et
`AI_GATEWAY_MODEL` valaient tous deux `qwen3:30b-instruct`, et pourtant
`rag_query_log.llm_model` portait `llama3.1:8b`. Le formulaire préremplissait son
littéral, l'envoyait explicitement, et cette valeur — devenue un « choix
utilisateur » aux yeux du backend — gagnait sur toute la configuration.

Un défaut dupliqué n'est pas un défaut : c'est une divergence en attente.

## Hiérarchie de préséance (du plus fort au plus faible)

1. **Valeur persistée du RagSpace** (`llm_model`) — un choix explicite reste un
   choix, et n'est JAMAIS réécrit par la configuration d'environnement ;
2. **Défauts de création** — ce module, alimenté par `DEFAULT_RAGSPACE_*` ;
3. **Repli Gateway** (`AI_GATEWAY_MODEL`) — n'intervient que si l'espace ne porte
   aucun modèle (champ vide).

`AI_GATEWAY_MODEL` n'écrase donc jamais le modèle stocké dans un RagSpace.
"""
from __future__ import annotations

from django.conf import settings

#: Clés du dictionnaire de défauts = noms RÉELS des champs du modèle `RagSpace`.
#: Permet un `RagSpace.objects.create(**ragspace_model_defaults(), ...)` direct.
MODEL_DEFAULT_FIELDS: tuple[str, ...] = (
    "llm_provider",
    "llm_model",
    "embedding_provider",
    "embedding_model",
)


# --- Défauts de CHAMP Django ------------------------------------------------
# Callables et non littéraux : évalués à l'instanciation, donc alignés sur la
# configuration de l'environnement qui tourne. Un littéral dans `models.py`
# survivrait à tout changement de configuration sans que rien ne le signale.
#
# Ces fonctions sont référencées par une migration (`0033_...`) : elles doivent
# rester importables sous ce chemin. Les renommer casserait la relecture de
# l'historique des migrations.

def default_ragspace_chat_provider() -> str:
    return settings.DEFAULT_RAGSPACE_CHAT_PROVIDER


def default_ragspace_chat_model() -> str:
    return settings.DEFAULT_RAGSPACE_CHAT_MODEL


def default_ragspace_embedding_provider() -> str:
    return settings.DEFAULT_RAGSPACE_EMBEDDING_PROVIDER


def default_ragspace_embedding_model() -> str:
    return settings.DEFAULT_RAGSPACE_EMBEDDING_MODEL


# --- Vue d'ensemble ---------------------------------------------------------

def ragspace_model_defaults() -> dict[str, str]:
    """Les quatre défauts effectifs, relus à CHAQUE appel.

    Relire à chaque appel (plutôt que figer à l'import) permet à
    `override_settings` de fonctionner en test et à un redéploiement de changer la
    valeur sans reconstruire d'image.
    """
    return {
        "llm_provider": default_ragspace_chat_provider(),
        "llm_model": default_ragspace_chat_model(),
        "embedding_provider": default_ragspace_embedding_provider(),
        "embedding_model": default_ragspace_embedding_model(),
    }


def fill_missing_model_defaults(kwargs: dict) -> dict:
    """Complète `kwargs` avec les défauts pour les champs modèle ABSENTS.

    Ne touche jamais une clé déjà présente — y compris si sa valeur est vide :
    un champ explicitement vidé est un choix (« utiliser le repli Gateway »), pas
    une omission.
    """
    defaults = ragspace_model_defaults()
    for field in MODEL_DEFAULT_FIELDS:
        if field not in kwargs:
            kwargs[field] = defaults[field]
    return kwargs

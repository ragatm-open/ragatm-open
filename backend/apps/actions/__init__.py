# backend/apps/actions/__init__.py
"""Centralisation du déclenchement d'actions à partir des phrases utilisateur.

Ce paquet est un **module Python pur** (pas une app Django : aucun modèle, aucune
migration, non enregistré dans INSTALLED_APPS). Il est volontairement *isolé* et
n'est PAS encore branché sur le chemin de chat live (cf. plan de migration, lot 3).

Séparation stricte de deux concepts (cf. audit) :
- ``ActionPlan``      : ce que l'utilisateur veut FAIRE (search.answer, ...).
- ``ResponsePolicy``  : COMMENT répondre après retrieval (explain, partial, ...).

Contraintes de sécurité tenues par construction dans ce lot :
- le LLM ne peut que PROPOSER une intention (classifier optionnel, désactivé par
  défaut) ; il n'exécute jamais rien ;
- aucune action mutante n'est déclenchable par une phrase
  (``natural_language_enabled=False`` + ``matchers=()``).
"""
from __future__ import annotations

from .plan import ActionContext, ActionPlan, ActionResult, MatchResult, ResponsePolicy
from .registry import ACTION_REGISTRY, ActionDefinition, PermissionSpec, catalog
from .router import ActionRouter

__all__ = [
    "ActionContext",
    "ActionPlan",
    "ActionResult",
    "MatchResult",
    "ResponsePolicy",
    "ACTION_REGISTRY",
    "ActionDefinition",
    "PermissionSpec",
    "catalog",
    "ActionRouter",
]

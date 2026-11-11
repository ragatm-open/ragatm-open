"""Lecture d'environnement robuste (source de vérité unique pour les booléens).

Motivation (audit de configuration, 2026-07) : la production portait
``RAG_HYBRID_ENABLED=True`` dans son ``.env``, mais le code testait
``os.getenv("RAG_HYBRID_ENABLED") == "1"``. La chaîne ``"True"`` n'étant pas ``"1"``,
le drapeau était résolu à **False** : le leg vectoriel était **désactivé
silencieusement** alors que des embeddings étaient configurés et facturés à
l'ingestion. Aucune alerte, aucun log.

Règle : plus aucun booléen d'environnement ne doit être parsé « à la main ». Une valeur
non reconnue **lève** (``ImproperlyConfigured``) plutôt que de retomber en silence sur
False — une faute de frappe doit être bruyante, pas invisible.
"""
from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from config.parsers import FALSE_LITERALS as _FALSE
from config.parsers import TRUE_LITERALS as _TRUE

# Les littéraux acceptés ne sont plus déclarés ici : ils viennent de
# `config/parsers.py`, source de vérité unique du parsing (registre, overrides
# Core, audit des `.env`). Ce module conserve son contrat propre : lever une
# `ImproperlyConfigured` Django, avec le message historique.


def env_bool(name: str, default: bool) -> bool:
    """Booléen d'environnement tolérant à la casse/aux espaces, strict sur le reste.

    Accepté (insensible à la casse et aux espaces) :
      - vrai  : 1, true, yes, on
      - faux  : 0, false, no, off

    Toute autre valeur lève ``ImproperlyConfigured`` : on n'échoue JAMAIS
    silencieusement vers False (cf. bug ``RAG_HYBRID_ENABLED=True`` en production).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    v = str(raw).strip().strip("\"'").lower()
    if v == "":
        return default
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"{name}={raw!r} n'est pas un booléen reconnu. "
        f"Valeurs acceptées : {sorted(_TRUE)} (vrai) / {sorted(_FALSE)} (faux)."
    )


def env_str(name: str, default: str) -> str:
    """Chaîne d'environnement normalisée (trim + guillemets retirés)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    v = str(raw).strip().strip("\"'")
    return v if v else default

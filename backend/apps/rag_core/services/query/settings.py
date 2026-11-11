"""Accès aux réglages du pipeline de recherche — par le registre, ou en constante.

Ce module portait quatre lecteurs permissifs (``_s``, ``_s_int``, ``_s_float``,
``_s_bool``) construits sur ``getattr(settings, nom, défaut)``. Ils donnaient à
n'importe quel nom l'apparence d'un réglage : il suffisait de l'écrire. Vingt-trois
noms en ont profité, dont vingt étaient posés en production sans le moindre effet,
parce qu'aucun module de settings ne les projetait.

Ils sont remplacés par ``setting(name)``, qui n'accepte **pas de défaut** : le
défaut vient du registre, il n'y en a qu'un, et un nom inconnu lève. Ce qui n'a
pas vocation à être réglé par un exploitant devient une CONSTANTE nommée ici même
— visible, testable, et impossible à croire configurable.
"""
from __future__ import annotations

from typing import Any

from config.settings.access import get_registered_setting

__all__ = [
    "setting",
    "setting_int",
    "setting_float",
    "setting_bool",
    "MIN_SOURCE_SCORE",
    "MAX_SNIPPET_CHARS_AUDIT",
    "MAX_REQUEST_ID_CHARS",
]


def setting(name: str) -> Any:
    """Valeur d'un réglage DÉCLARÉ. Pas de défaut d'appel : il est au registre."""
    return get_registered_setting(name)


def setting_int(name: str) -> int:
    return int(setting(name))


def setting_float(name: str) -> float:
    return float(setting(name))


def setting_bool(name: str) -> bool:
    value = setting(name)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# Constantes internes — délibérément NON configurables
# ---------------------------------------------------------------------------
#: Score en dessous duquel un candidat est traité comme du bruit numérique. Ce
#: n'est pas un seuil de pertinence (celui-là s'appelle `RAG_MIN_SCORE` et se
#: règle) : c'est l'epsilon qui distingue « score nul » de « score minuscule ».
#: Le rendre réglable inviterait à s'en servir comme d'un seuil métier.
MIN_SOURCE_SCORE = 1e-6

#: Longueur d'extrait conservée dans l'audit. Format de journal, pas politique de
#: recherche : l'allonger grossit les journaux sans rien changer aux réponses.
MAX_SNIPPET_CHARS_AUDIT = 200

#: Borne défensive sur l'identifiant de requête reçu de l'extérieur. Une borne de
#: sécurité qui se desserre par variable d'environnement n'est pas une borne.
MAX_REQUEST_ID_CHARS = 200

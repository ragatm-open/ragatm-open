"""Pont entre le registre canonique et les settings Django.

Objectif : que `settings/base.py` cesse de reparser l'environnement à la main
pour les variables couvertes par le registre, **sans changer la moindre valeur
effective** des déploiements existants.

Trois garanties tiennent cette promesse :

1. **Parsing identique** pour toute valeur déjà valide (`0`/`1`, entiers, URLs).
2. **Ambiguïté refusée, jamais résolue en silence** — une variable historiquement
   lue avec ``== "1"`` valait ``False`` pour ``true``/``yes``/``on``. Le registre
   la lirait ``True`` : ce serait une activation silencieuse. On refuse donc le
   démarrage avec un message explicite plutôt que de trancher (cf.
   ``LEGACY_STRICT_ONE_BOOLS``).
3. **Aucune validation nouvelle bloquante sur les types texte** — une URL
   malformée n'a jamais empêché le démarrage ; elle est désormais SIGNALÉE par un
   system check (``config.E001``) au lieu de lever à l'import.

Le module ne fait aucun accès base ni réseau.
"""
from __future__ import annotations

import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from config import parsers, registry
from config.parsers import ConfigValueError

#: Anomalies non bloquantes détectées pendant le chargement des settings,
#: rapportées ensuite par ``config.checks.check_registry_conformance``.
DEFERRED_ANOMALIES: list[tuple[str, str]] = []

#: Avertissements de dépréciation collectés au chargement (alias historiques).
DEPRECATION_WARNINGS: list[str] = []

# Types pour lesquels une valeur invalide DOIT interrompre le démarrage : une
# faute de frappe sur un booléen ou un entier a des conséquences silencieuses.
# (Pour les entiers, `int(os.getenv(...))` levait déjà : comportement inchangé.)
#
# Les énumérations en sont EXCLUES volontairement : une valeur interdite comme
# `AI_GATEWAY_FALLBACK_PROVIDER=ollama` est déjà refusée par un system check dédié
# (`llm_router.E004`), avec un message métier bien plus précis. Lever à l'import
# masquerait ce diagnostic.
_RAISE_TYPES = frozenset({parsers.TYPE_BOOL, parsers.TYPE_INT, parsers.TYPE_FLOAT})

# Booléens historiquement lus avec `os.getenv(...) == "1"` : toute valeur autre
# que "0"/"1" valait False. Accepter aujourd'hui "true" comme vrai ACTIVERAIT une
# fonctionnalité qui était éteinte. On exige la normalisation explicite.
LEGACY_STRICT_ONE_BOOLS: frozenset[str] = frozenset(
    {
        "AI_GATEWAY_ENABLED",
        "AI_GATEWAY_FALLBACK_ENABLED",
        "ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS",
        "CORE_CONFIG_EDIT_ENABLED",
        "CORE_CONFIG_RUNTIME_ENABLED",
        "RAG_ANSWER_LANGUAGE_POLICY_ENABLED",
    }
)

_STRICT_ONE_LITERALS = frozenset({"0", "1"})


def _raw(name: str, environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None:
        return None
    value = str(raw).strip()
    return value if value else None


def _alias_raw(definition_name: str, environ: dict[str, str] | None = None) -> str | None:
    """Valeur d'un alias déprécié pointant vers ``definition_name``, si posée seule."""
    env = os.environ if environ is None else environ
    for alias in registry.DEPRECATED_ALIASES:
        if alias.new_name != definition_name:
            continue
        old = _raw(alias.old_name, env)
        if old is None:
            continue
        new = _raw(alias.new_name, env)
        message = (
            f"{alias.old_name} est dépréciée ; utiliser {alias.new_name} "
            f"(suppression prévue après {alias.remove_after})."
        )
        if new is None:
            DEPRECATION_WARNINGS.append(message)
            return old
        if new == old:
            DEPRECATION_WARNINGS.append(f"{message} Les deux portent la même valeur.")
            return None
        raise ImproperlyConfigured(
            f"{alias.old_name} et {alias.new_name} sont toutes deux définies avec des "
            f"valeurs différentes. Supprimez {alias.old_name} (dépréciée)."
        )
    return None


def env_value(name: str, *, environ: dict[str, str] | None = None) -> Any:
    """Valeur effective d'une variable du registre, lue depuis l'environnement.

    Retourne le défaut déclaré si la variable est absente ou vide.
    """
    definition = registry.require(name)
    # La politique de dépréciation est évaluée MÊME quand le nom canonique est
    # posé : c'est ainsi qu'un conflit de valeurs est détecté (et refusé).
    inherited = _alias_raw(name, environ)
    raw = _raw(name, environ)
    if raw is None:
        raw = inherited
    if raw is None:
        return definition.default

    if name in LEGACY_STRICT_ONE_BOOLS and raw.strip().lower() not in _STRICT_ONE_LITERALS:
        raise ImproperlyConfigured(
            f"{name}={raw!r} est ambiguë : cette variable était historiquement lue avec "
            f'`== "1"` et TOUTE autre valeur (y compris "true") valait False. '
            f"Normalisez explicitement en `{name}=1` ou `{name}=0` avant de démarrer — "
            "RagATM refuse d'interpréter la valeur à votre place."
        )

    try:
        return definition.parse(raw)
    except ConfigValueError as exc:
        if definition.value_type in _RAISE_TYPES:
            raise ImproperlyConfigured(str(exc)) from None
        # Types texte/URL : jamais bloquant à l'import (comportement historique).
        # L'anomalie remonte par le system check `config.E001`.
        DEFERRED_ANOMALIES.append((name, exc.reason))
        return parsers.normalize_raw(raw)


def env_values(*names: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Lit plusieurs variables d'un coup (confort pour les blocs de settings)."""
    return {name: env_value(name, environ=environ) for name in names}


def reset_state() -> None:
    """Vide les anomalies/avertissements collectés (utilisé par les tests)."""
    DEFERRED_ANOMALIES.clear()
    DEPRECATION_WARNINGS.clear()

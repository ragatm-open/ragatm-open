"""Registre canonique de la configuration RagATM — source de vérité unique.

Ce paquet est **pur** : il n'importe ni Django, ni la base, ni le réseau, et ne
lit aucun fichier à l'import. Il est donc consommable par :

- ``config/settings/`` (valeurs effectives) ;
- les system checks (``config.checks``) ;
- l'API Admin Core (métadonnées, portées, redaction) ;
- ``manage.py config_audit`` (cohérence code ↔ `.env.example`) ;
- ``manage.py generate_config_reference`` (documentation déterministe) ;
- les tests.

Aucune de ces surfaces ne doit re-déclarer un type, un défaut, une borne ou une
description : elles lisent le registre.

Résolution de la valeur effective (inchangée par ce lot) ::

    ragspace > workspace > environment > global > settings/.env > défaut de code

Les quatre premiers niveaux sont les overrides RagATM_Core ; ils ne sont
appliqués au runtime que si ``CORE_CONFIG_RUNTIME_ENABLED=1``.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Iterator

from config.parsers import ConfigValueError
from config.registry.definition import (
    ALL_ENVIRONMENTS,
    DOMAIN_LABELS,
    DOMAINS,
    SCOPE_RANK,
    SCOPES,
    ConfigDefinition,
    DeprecatedAlias,
    redact_url,
)
from config.registry.domains import (
    chat,
    core,
    corpus,
    deployment,
    email,
    embeddings,
    gateway,
    generation,
    ingestion,
    observability,
    quotas,
    retrieval,
    security,
    storage,
    tabular,
    worker,
)

__all__ = [
    "ALL_ENVIRONMENTS",
    "DEFINITIONS",
    "DEPRECATED_ALIASES",
    "DOMAINS",
    "DOMAIN_LABELS",
    "IGNORED_ENV_NAMES",
    "SCOPES",
    "SCOPE_RANK",
    "ConfigDefinition",
    "ConfigValueError",
    "DeprecatedAlias",
    "all_definitions",
    "alias_for",
    "by_domain",
    "get",
    "names",
    "redact_url",
    "resolve_aliases",
    "require",
]

_MODULES = (
    core,
    gateway,
    retrieval,
    embeddings,
    generation,
    chat,
    ingestion,
    tabular,
    corpus,
    quotas,
    storage,
    security,
    observability,
    worker,
    email,
    deployment,
)


def _build() -> tuple[ConfigDefinition, ...]:
    seen: dict[str, str] = {}
    collected: list[ConfigDefinition] = []
    for module in _MODULES:
        for definition in module.DEFINITIONS:
            if definition.name in seen:
                raise ValueError(
                    f"{definition.name}: déclarée deux fois "
                    f"({seen[definition.name]} et {module.__name__})"
                )
            seen[definition.name] = module.__name__
            collected.append(definition)
    return tuple(collected)


DEFINITIONS: tuple[ConfigDefinition, ...] = _build()

_BY_NAME: dict[str, ConfigDefinition] = {d.name: d for d in DEFINITIONS}


# ----------------------------------------------------------------------------
# Alias dépréciés
# ----------------------------------------------------------------------------
# Politique : l'ancien nom reste ACCEPTÉ (aucun déploiement existant ne casse),
# il produit un avertissement explicite, et sa valeur est convertie vers le nom
# canonique. Ancien + nouveau définis avec des valeurs DIFFÉRENTES => erreur.
DEPRECATED_ALIASES: tuple[DeprecatedAlias, ...] = (
    DeprecatedAlias(
        old_name="AI_GATEWAY_URL",
        new_name="AI_GATEWAY_BASE_URL",
        since="2026-07",
        remove_after="2027-01",
        note="Nom historique issu des docker-compose « forward-looking ».",
    ),
    DeprecatedAlias(
        old_name="DEFAULT_LLM_PROVIDER",
        new_name="RAG_LLM_PROVIDER",
        since="2026-07",
        remove_after="2027-01",
        note="Repli encore lu par `base.py` quand RAG_LLM_PROVIDER est absente.",
    ),
    DeprecatedAlias(
        old_name="OLLAMA_DEFAULT_MODEL",
        new_name="RAG_LLM_MODEL",
        since="2026-07",
        remove_after="2027-01",
        note="Repli encore lu par `base.py` quand RAG_LLM_MODEL est absente.",
    ),
    DeprecatedAlias(
        old_name="DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS",
        new_name="DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY",
        since="2026-06",
        remove_after="2027-01",
        note="À 1, équivaut à la politique `all`.",
    ),
)

_ALIAS_BY_OLD: dict[str, DeprecatedAlias] = {a.old_name: a for a in DEPRECATED_ALIASES}

# Variables présentes dans l'environnement mais qui n'appartiennent PAS à la
# configuration RagATM : variables système, ou noms fabriqués par les tests.
IGNORED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "TMPDIR",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        # Noms d'exemple utilisés par les tests de parsing d'environnement.
        "X_FLAG",
        "X_ABSENT_FLAG",
        "X_S",
        # Corpus de messagerie : chemin de fixture, pas un réglage de service.
        "RAGATM_MSG_CORPUS",
    }
)


# ----------------------------------------------------------------------------
# API de consultation
# ----------------------------------------------------------------------------
def all_definitions() -> tuple[ConfigDefinition, ...]:
    """Toutes les définitions, dans l'ordre déclaratif (domaine puis déclaration)."""
    return DEFINITIONS


def names() -> frozenset[str]:
    return frozenset(_BY_NAME)


def get(name: str) -> ConfigDefinition | None:
    return _BY_NAME.get(name)


def require(name: str) -> ConfigDefinition:
    definition = _BY_NAME.get(name)
    if definition is None:
        raise KeyError(f"{name}: variable inconnue du registre de configuration")
    return definition


def by_domain(domain: str | None = None) -> Iterator[ConfigDefinition]:
    for definition in DEFINITIONS:
        if domain is None or definition.domain == domain:
            yield definition


def grouped_by_domain() -> list[tuple[str, list[ConfigDefinition]]]:
    """Définitions groupées par domaine, dans l'ordre canonique de ``DOMAINS``."""
    groups: list[tuple[str, list[ConfigDefinition]]] = []
    for domain in DOMAINS:
        items = [d for d in DEFINITIONS if d.domain == domain]
        if items:
            groups.append((domain, items))
    return groups


def secrets() -> tuple[ConfigDefinition, ...]:
    return tuple(d for d in DEFINITIONS if d.secret)


def deprecated() -> tuple[ConfigDefinition, ...]:
    return tuple(d for d in DEFINITIONS if d.deprecated)


def runtime_editable() -> tuple[ConfigDefinition, ...]:
    return tuple(d for d in DEFINITIONS if d.runtime_editable)


def alias_for(old_name: str) -> DeprecatedAlias | None:
    return _ALIAS_BY_OLD.get(old_name)


# ----------------------------------------------------------------------------
# Lecture d'environnement pilotée par le registre
# ----------------------------------------------------------------------------
def value_from_environ(
    name: str, environ: dict[str, str] | None = None
) -> tuple[Any, str]:
    """Valeur effective d'une variable et sa SOURCE (``"env"`` ou ``"default"``).

    Ne lit que l'environnement : aucun accès base, aucun override Core. C'est le
    niveau « settings/.env » de la chaîne de résolution.
    """
    definition = require(name)
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return definition.default, "default"
    return definition.parse(raw), "env"


def resolve_aliases(
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Applique la politique de dépréciation aux variables d'environnement.

    Retourne ``(valeurs_canoniques, avertissements, erreurs)`` :

    - ancien nom seul => la valeur est reportée sur le nom canonique + warning ;
    - ancien et nouveau avec la MÊME valeur => warning ;
    - ancien et nouveau avec des valeurs DIFFÉRENTES => erreur.

    Ne modifie jamais ``os.environ`` : l'appelant décide quoi en faire.
    """
    env = os.environ if environ is None else environ
    canonical: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []

    for alias in DEPRECATED_ALIASES:
        old_raw = env.get(alias.old_name)
        if old_raw is None or str(old_raw).strip() == "":
            continue
        new_raw = env.get(alias.new_name)
        base_message = (
            f"{alias.old_name} est dépréciée ; utiliser {alias.new_name} "
            f"(dépréciée depuis {alias.since}, suppression prévue après {alias.remove_after})."
        )
        if new_raw is None or str(new_raw).strip() == "":
            canonical[alias.new_name] = old_raw
            warnings.append(base_message)
            continue
        if str(new_raw).strip() == str(old_raw).strip():
            warnings.append(f"{base_message} Les deux portent la même valeur.")
            continue
        errors.append(
            f"{alias.old_name} et {alias.new_name} sont toutes deux définies avec des "
            f"valeurs différentes. Supprimer {alias.old_name}."
        )
    return canonical, warnings, errors


def iter_unregistered(observed: Iterable[str]) -> list[str]:
    """Noms observés qui ne sont ni au registre, ni ignorés, ni des alias."""
    known = names() | IGNORED_ENV_NAMES | set(_ALIAS_BY_OLD)
    return sorted({name for name in observed if name not in known})

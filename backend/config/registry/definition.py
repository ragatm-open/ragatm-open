"""Métadonnées canoniques d'un paramètre de configuration — module PUR.

Aucun import Django, aucun accès base/réseau/fichier : ce module est importable
par les tests, la commande d'audit, la génération documentaire et le résolveur
Core sans déclencher le chargement des applications.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from config import parsers
from config.parsers import ConfigValueError

# Portées d'override RagATM_Core, du moins prioritaire au plus prioritaire.
# Réplique l'ordre appliqué par `apps.rag_core.core.config_resolver` :
#   ragspace > workspace > environment > global > settings > défaut de code.
SCOPES: tuple[str, ...] = ("global", "environment", "workspace", "ragspace")
SCOPE_RANK: dict[str, int] = {scope: rank for rank, scope in enumerate(SCOPES)}

# Environnements de déploiement connus.
ENVIRONMENTS: tuple[str, ...] = ("dev", "test", "staging", "prod")
ALL_ENVIRONMENTS: tuple[str, ...] = ENVIRONMENTS

# Domaines fonctionnels. Ordre = ordre de rendu dans la documentation générée.
DOMAINS: tuple[str, ...] = (
    "core",
    "gateway",
    "retrieval",
    "embeddings",
    "generation",
    "chat",
    "ingestion",
    "tabular",
    "corpus",
    "quotas",
    "storage",
    "security",
    "observability",
    "worker",
    "email",
    "deployment",
)

DOMAIN_LABELS: dict[str, str] = {
    "core": "RagATM_Core (configuration dynamique)",
    "gateway": "AI Gateway (client)",
    "retrieval": "Retrieval (recherche et classement)",
    "embeddings": "Embeddings",
    "generation": "Génération LLM (presets et routage)",
    "chat": "Chat et mémoire de conversation",
    "ingestion": "Ingestion et extraction documentaire",
    "tabular": "Moteur tabulaire",
    "corpus": "Analyses globales de corpus",
    "quotas": "Quotas documentaires",
    "storage": "Stockage et base de données",
    "security": "Sécurité, authentification et exposition",
    "observability": "Audit, rétention et observabilité",
    "worker": "Workers Celery et ordonnancement",
    "email": "Messagerie sortante",
    "deployment": "Infrastructure et déploiement",
}


@dataclass(frozen=True)
class ConfigDefinition:
    """Définition canonique d'un paramètre de configuration.

    Une seule déclaration alimente : les settings, les system checks, l'Admin
    Core, l'audit des fichiers ``.env.example``, la documentation générée et les
    tests. Aucune de ces surfaces ne doit re-déclarer les mêmes informations.
    """

    name: str
    domain: str
    value_type: str
    default: Any
    description: str

    #: Obligatoire dans les environnements listés par ``required_in``.
    required: bool = False
    required_in: tuple[str, ...] = ()

    #: Valeur sensible : jamais rendue en clair (API, logs, audit, doc).
    secret: bool = False
    #: Valeur partiellement masquée (URL privée : schéma + port conservés).
    redact_mode: str = "full"  # "full" | "url"

    #: Modifiable via l'overlay RagATM_Core (écriture d'override).
    runtime_editable: bool = False
    #: Portée maximale d'un override ; ``None`` => non éditable.
    max_scope: str | None = None
    #: Un redémarrage est nécessaire pour que la valeur prenne effet.
    restart_required: bool = True

    environments: tuple[str, ...] = ALL_ENVIRONMENTS
    deprecated: bool = False
    replacement: str | None = None

    #: Fichier/composant qui DÉCLARE la valeur effective.
    source: str = "backend/config/settings/base.py"
    #: Principaux consommateurs (informatif, rendu dans la documentation).
    consumers: tuple[str, ...] = ()

    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    separator: str = ","

    #: Défaut calculé à partir d'un autre paramètre (ex. « = RAG_TOP_K »).
    default_expr: str | None = None
    #: Le défaut du code n'est pas comparable au registre (valeur dérivée,
    #: chemin absolu, secret) : l'audit de cohérence saute la comparaison.
    default_is_dynamic: bool = False

    #: Notes complémentaires rendues dans la documentation.
    notes: str = ""

    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    # ------------------------------------------------------------------
    # Invariants de la définition elle-même
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        if self.value_type not in parsers.VALUE_TYPES:
            raise ValueError(f"{self.name}: type inconnu {self.value_type!r}")
        if self.domain not in DOMAINS:
            raise ValueError(f"{self.name}: domaine inconnu {self.domain!r}")
        if self.max_scope is not None and self.max_scope not in SCOPES:
            raise ValueError(f"{self.name}: portée inconnue {self.max_scope!r}")
        if self.runtime_editable and self.max_scope is None:
            raise ValueError(f"{self.name}: runtime_editable exige un max_scope")
        if self.value_type == parsers.TYPE_ENUM and not self.choices:
            raise ValueError(f"{self.name}: type enum sans choices")
        if self.deprecated and not self.replacement and not self.notes:
            raise ValueError(f"{self.name}: dépréciation sans remplacement ni note")
        for env in self.environments:
            if env not in ALL_ENVIRONMENTS:
                raise ValueError(f"{self.name}: environnement inconnu {env!r}")
        for env in self.required_in:
            if env not in ALL_ENVIRONMENTS:
                raise ValueError(f"{self.name}: environnement inconnu {env!r}")
        if self.redact_mode not in ("full", "url"):
            raise ValueError(f"{self.name}: redact_mode inconnu {self.redact_mode!r}")
        if not self.description.strip():
            raise ValueError(f"{self.name}: description obligatoire")

    # ------------------------------------------------------------------
    # Parsing / validation d'une valeur
    # ------------------------------------------------------------------
    def parse(self, raw: Any) -> Any:
        """Parse une valeur brute (texte) et applique les bornes déclarées."""
        value = parsers.parse(
            self.name,
            raw,
            self.value_type,
            choices=self.choices,
            separator=self.separator,
        )
        parsers.check_bounds(self.name, value, minimum=self.minimum, maximum=self.maximum)
        return value

    def coerce(self, value: Any) -> Any:
        """Coerce une valeur déjà typée (ou textuelle) vers le type déclaré."""
        if self.value_type == parsers.TYPE_BOOL and isinstance(value, bool):
            parsers.check_bounds(self.name, value)
            return value
        if self.value_type == parsers.TYPE_LIST and isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        if value is None:
            return self.default
        return self.parse(value)

    def serialize(self, value: Any) -> str:
        """Forme texte canonique (stockage d'override, documentation)."""
        return parsers.serialize(value, self.value_type, separator=self.separator)

    def validate_scope(self, scope_type: str) -> None:
        """Refuse un override à une portée supérieure à celle autorisée."""
        if self.max_scope is None or not self.runtime_editable:
            raise ConfigValueError(self.name, "clé non éditable")
        if scope_type not in SCOPES:
            raise ConfigValueError(self.name, f"portée inconnue {scope_type!r}")
        if SCOPE_RANK[scope_type] > SCOPE_RANK[self.max_scope]:
            raise ConfigValueError(
                self.name,
                f"portée {scope_type!r} interdite",
                hint=f"portée maximale autorisée : {self.max_scope}",
            )

    # ------------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------------
    def is_required_in(self, environment: str) -> bool:
        if self.required_in:
            return environment in self.required_in
        return self.required and environment in self.environments

    def display_value(self, value: Any) -> str:
        """Représentation SÛRE d'une valeur (jamais un secret en clair)."""
        if self.secret:
            if self.redact_mode == "url":
                return redact_url(str(value or ""))
            return "••••••••" if value not in (None, "", []) else ""
        return self.serialize(value)

    def default_display(self) -> str:
        """Défaut tel qu'affiché dans la documentation (déterministe)."""
        if self.default_expr:
            return self.default_expr
        if self.secret:
            return "(non affiché)"
        if self.default in (None, ""):
            return "(vide)"
        return self.serialize(self.default)

    def as_dict(self) -> dict[str, Any]:
        """Métadonnées sérialisables (API Admin Core, `config_audit --format json`)."""
        return {
            "name": self.name,
            "domain": self.domain,
            "type": self.value_type,
            "default": None if self.secret else self.default,
            "default_display": self.default_display(),
            "required": self.required,
            "required_in": list(self.required_in),
            "secret": self.secret,
            "runtime_editable": self.runtime_editable,
            "max_scope": self.max_scope,
            "restart_required": self.restart_required,
            "environments": list(self.environments),
            "deprecated": self.deprecated,
            "replacement": self.replacement,
            "source": self.source,
            "consumers": list(self.consumers),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "description": self.description,
        }


def redact_url(value: str) -> str:
    """Masque l'hôte d'une URL en conservant schéma et port.

    Réplique le comportement historique de
    ``apps.rag_core.core.config_resolver.redact_base_url`` : la politique de
    redaction de ``AI_GATEWAY_BASE_URL`` ne change pas.
    """
    if not value:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        scheme = parsed.scheme or "http"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{scheme}://…{port}"
    except Exception:  # pragma: no cover - URL non parsable
        return "…"


@dataclass(frozen=True)
class DeprecatedAlias:
    """Ancien nom de variable encore accepté, converti vers le nom canonique.

    Politique (cf. §8 du lot « maîtrise de la configuration ») :
    - seul l'ancien nom défini => warning + conversion silencieuse de valeur ;
    - les deux définis avec la MÊME valeur => warning ;
    - les deux définis avec des valeurs DIFFÉRENTES => erreur de configuration.
    """

    old_name: str
    new_name: str
    since: str
    remove_after: str
    note: str = ""


def parse_definition_value(definition: ConfigDefinition, raw: Any) -> Any:
    """Helper de convenance (évite d'importer `parsers` chez l'appelant)."""
    return definition.parse(raw)

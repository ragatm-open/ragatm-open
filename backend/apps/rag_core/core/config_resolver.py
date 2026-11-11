"""Résolveur de configuration effective RagATM_Core — LECTURE SEULE (PR-A).

Lit les settings Django déjà chargés depuis l'environnement, plus l'overlay
d'overrides en base. Aucune écriture, aucun appel réseau, aucune influence sur le
runtime : c'est un miroir de diagnostic destiné à l'écran « Admin Core »
(superuser only).

Les métadonnées des clés (type, défaut, bornes, portée, sensibilité, description)
NE SONT PLUS déclarées ici : elles proviennent du **registre canonique**
``config.registry``, source de vérité unique partagée avec les settings, les
system checks, l'audit `.env` et la documentation générée. Ce module ne conserve
que ce qui lui est propre : la liste des clés EXPOSÉES dans l'Admin Core, leur
ordre d'affichage, et la mécanique de résolution DB > settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from django.conf import settings

from config import registry as _registry


@dataclass(frozen=True)
class KeySpec:
    """Spécification d'une clé de configuration Core.

    `editable`/`max_scope` ne servent qu'à l'écriture (PR-F) ; la lecture
    (PR-A→E) reste inchangée. `max_scope=None` => clé non éditable.
    """

    key: str
    value_type: str  # "bool" | "int" | "str"
    default: Any
    sensitive: bool
    description: str
    editable: bool = False
    max_scope: str | None = None
    min_int: int | None = None
    max_int: int | None = None
    #: Domaine fonctionnel et applicabilité runtime, repris du registre canonique.
    domain: str = ""
    restart_required: bool = True


# Clés EXPOSÉES par l'Admin Core, dans leur ORDRE d'affichage (contrat d'IHM :
# l'écran superuser les rend dans cet ordre). Toutes les clés éditables du
# registre canonique doivent y figurer — un test le verrouille — plus
# RAG_EMBEDDING_DIM, exposée en lecture seule parce qu'elle conditionne
# l'ingestion et doit rester visible à l'exploitant.
#
# Cette liste ne contient QUE des réglages de production. Les tests de l'overlay
# ont besoin d'une clé allant jusqu'au ragspace : elle est INJECTÉE au moment du
# test (`tests/tests_root/conftest.py`, fixture `synthetic_core_key`) et n'existe
# ni dans le registre canonique, ni ici, ni dans aucune réponse d'API.
EXPOSED_KEYS: tuple[str, ...] = (
    "AI_GATEWAY_ENABLED",
    "AI_GATEWAY_BASE_URL",
    "AI_GATEWAY_TIMEOUT_SECONDS",
    "AI_GATEWAY_FALLBACK_ENABLED",
    "RAG_EMBEDDING_DIM",
    "WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT",
    "WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES",
)

# Types du registre canonique projetés sur les trois types que manipule l'overlay
# Core (bool / int / str). `url` et `enum` sont du texte pour le stockage.
_TYPE_PROJECTION = {"bool": "bool", "int": "int", "float": "str"}


def _spec_from_registry(name: str) -> KeySpec:
    """Projette une définition canonique en ``KeySpec`` (API historique conservée)."""
    definition = _registry.require(name)
    minimum = None if definition.minimum is None else int(definition.minimum)
    maximum = None if definition.maximum is None else int(definition.maximum)
    return KeySpec(
        key=definition.name,
        value_type=_TYPE_PROJECTION.get(definition.value_type, "str"),
        default=definition.default,
        sensitive=definition.secret,
        description=definition.description,
        editable=definition.runtime_editable,
        max_scope=definition.max_scope,
        min_int=minimum if definition.value_type == "int" else None,
        max_int=maximum if definition.value_type == "int" else None,
        domain=definition.domain,
        restart_required=definition.restart_required,
    )


_REGISTRY: list[KeySpec] = [_spec_from_registry(name) for name in EXPOSED_KEYS]

_SPECS: dict[str, KeySpec] = {s.key: s for s in _REGISTRY}

# Ensemble des clés sensibles (à redacter à l'affichage / dans les logs d'audit).
SENSITIVE_KEYS = frozenset(s.key for s in _REGISTRY if s.sensitive)


def key_spec(key: str) -> KeySpec | None:
    return _SPECS.get(key)


def is_sensitive_key(key: str) -> bool:
    return key in SENSITIVE_KEYS


@dataclass(frozen=True)
class ConfigEntry:
    """Une clé Core telle que la voit l'Admin Core.

    Les champs historiques (``effective_value``, ``source``, ``editable``,
    ``sensitive``, ``redacted``, ``description``) sont INCHANGÉS. Les champs
    ajoutés répondent aux questions que l'exploitant se posait sans pouvoir y
    répondre : d'où vient la valeur, quel était le défaut, l'override est-il
    actif, faut-il redémarrer.
    """

    key: str
    effective_value: Any
    source: str
    editable: bool
    sensitive: bool
    redacted: bool
    description: str
    # --- Enrichissement (lot « maîtrise de la configuration ») ---------------
    domain: str = ""
    value_type: str = "str"
    default_value: Any = None
    settings_value: Any = None
    override_value: Any = None
    override_scope: str | None = None
    effective_source: str = "settings"
    runtime_editable: bool = False
    max_scope: str | None = None
    runtime_active: bool = False
    restart_required: bool = True
    display_value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "effective_value": self.effective_value,
            "source": self.source,
            "editable": self.editable,
            "sensitive": self.sensitive,
            "redacted": self.redacted,
            "description": self.description,
            "domain": self.domain,
            "value_type": self.value_type,
            "default_value": self.default_value,
            "settings_value": self.settings_value,
            "override_value": self.override_value,
            "override_scope": self.override_scope,
            "effective_source": self.effective_source,
            "runtime_editable": self.runtime_editable,
            "max_scope": self.max_scope,
            "runtime_active": self.runtime_active,
            "restart_required": self.restart_required,
            "display_value": self.display_value,
        }


def redact_base_url(value: str) -> str:
    """Masque l'hôte d'une URL en conservant schéma et port.

    "http://127.0.0.1:8100" -> "http://…:8100" ; "" -> "". Jamais d'hôte en clair.
    """
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        scheme = parsed.scheme or "http"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{scheme}://…{port}"
    except Exception:
        # En cas d'URL non parsable, ne jamais renvoyer la valeur brute.
        return "…"


def _coerce(value: Any, value_type: str) -> Any:
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        # Valeurs DB stockées en texte : parser explicitement.
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if value_type == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return "" if value is None else str(value)


def current_environment() -> str:
    return str(getattr(settings, "DJANGO_ENV", "dev") or "dev")


# Rangs de précédence des portées (DB) : ragspace > workspace > environment > global.
_SCOPE_RANK = {"ragspace": 3, "workspace": 2, "environment": 1, "global": 0}
VALID_SCOPE_TYPES = frozenset(_SCOPE_RANK.keys())


def scope_rank(scope_type: str) -> int:
    """Rang d'une portée (−1 si inconnue)."""
    return _SCOPE_RANK.get(scope_type, -1)


def is_editable_key(key: str) -> bool:
    spec = _SPECS.get(key)
    return bool(spec and spec.editable and spec.max_scope is not None)


def allowed_max_scope(key: str) -> str | None:
    spec = _SPECS.get(key)
    return spec.max_scope if spec else None


def coerce_value_for_storage(key: str, value: Any) -> str:
    """Convertit une valeur en TEXTE canonique pour stockage (écriture).

    Lève ``ValueError`` si la valeur est invalide pour le type de la clé
    (utilisé par le service d'écriture pour rejeter en 400). bool -> "true"/"false".
    """
    spec = _SPECS.get(key)
    if spec is None:
        raise ValueError("unknown_key")

    if spec.value_type == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return "true"
        if s in ("0", "false", "no", "off"):
            return "false"
        raise ValueError("invalid_bool")

    if spec.value_type == "int":
        try:
            iv = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("invalid_int")
        if spec.min_int is not None and iv < spec.min_int:
            raise ValueError("int_below_min")
        if spec.max_int is not None and iv > spec.max_int:
            raise ValueError("int_above_max")
        return str(iv)

    # str/url : trim, jamais None.
    return "" if value is None else str(value).strip()


def _load_override_map(
    *, environment: str, workspace_id=None, rag_space_id=None
) -> dict[str, tuple[str, str]]:
    """Charge les overrides DB applicables et garde, par clé, la portée la plus
    prioritaire. Retourne {key: (value_str, scope_type)}.

    Lecture seule. Si la table est vide (cas par défaut), retourne {} => le
    résolveur retombe intégralement sur les settings (aucun changement).
    """
    from django.db.models import Q

    from apps.rag_core.models import CoreConfigOverride

    q = Q(scope_type="global", scope_ref="") | Q(scope_type="environment", scope_ref=environment)
    if workspace_id is not None:
        q |= Q(scope_type="workspace", scope_ref=str(workspace_id))
    if rag_space_id:
        q |= Q(scope_type="ragspace", scope_ref=str(rag_space_id))

    best: dict[str, tuple[str, str]] = {}
    for row in CoreConfigOverride.objects.filter(q).only("key", "value", "scope_type"):
        rank = _SCOPE_RANK.get(row.scope_type, -1)
        cur = best.get(row.key)
        if cur is None or rank > _SCOPE_RANK.get(cur[1], -1):
            best[row.key] = (row.value, row.scope_type)
    return best


def _effective_map(
    *, environment: str, workspace_id=None, rag_space_id=None
) -> dict[str, tuple[Any, str]]:
    """Pour chaque clé du registre : (valeur coercée, source).

    source = "db:<scope>" si override, sinon "settings". DB > env ; fallback env.
    """
    overrides = _load_override_map(
        environment=environment, workspace_id=workspace_id, rag_space_id=rag_space_id
    )
    out: dict[str, tuple[Any, str]] = {}
    for spec in _REGISTRY:
        key, value_type, default = spec.key, spec.value_type, spec.default
        if key in overrides:
            raw, scope_type = overrides[key]
            out[key] = (_coerce(raw, value_type), f"db:{scope_type}")
        else:
            out[key] = (_coerce(getattr(settings, key, default), value_type), "settings")
    return out


def resolve_effective_config(
    *, environment: str | None = None, workspace_id=None, rag_space_id=None
) -> list[ConfigEntry]:
    """Config effective DB-over-env (overlay lecture seule, editable False).

    Sans override en base, identique au comportement env-only de PR-A.
    """
    environment = environment or current_environment()
    overrides = _load_override_map(
        environment=environment, workspace_id=workspace_id, rag_space_id=rag_space_id
    )
    emap = _effective_map(
        environment=environment, workspace_id=workspace_id, rag_space_id=rag_space_id
    )
    # Un override existe TOUJOURS en base indépendamment du runtime : il n'est
    # APPLIQUÉ que si l'overlay runtime est branché. La distinction est portée par
    # `runtime_active` pour qu'on ne confonde plus « visible » et « actif ».
    runtime_on = bool(getattr(settings, "CORE_CONFIG_RUNTIME_ENABLED", False))

    entries: list[ConfigEntry] = []
    for spec in _REGISTRY:
        key = spec.key
        value, source = emap[key]
        settings_value = _coerce(getattr(settings, key, spec.default), spec.value_type)

        override_raw, override_scope = overrides.get(key, (None, None))
        override_value = (
            _coerce(override_raw, spec.value_type) if override_raw is not None else None
        )

        redacted = False
        display_value: Any = value
        if spec.sensitive:
            redacted = bool(value)
            if key == "AI_GATEWAY_BASE_URL":
                value = redact_base_url(value)
                display_value = value
                settings_value = redact_base_url(str(settings_value or ""))
                override_value = (
                    redact_base_url(str(override_value or ""))
                    if override_value is not None
                    else None
                )
            else:
                value = None
                display_value = "••••••••" if redacted else ""
                settings_value = None
                override_value = None

        entries.append(
            ConfigEntry(
                key=key,
                effective_value=value,
                source=source,
                editable=False,  # lecture seule (l'écriture passe par /override/, PR-F)
                sensitive=spec.sensitive,
                redacted=redacted,
                description=spec.description,
                domain=spec.domain,
                value_type=spec.value_type,
                default_value=None if spec.sensitive else spec.default,
                settings_value=settings_value,
                override_value=override_value,
                override_scope=override_scope,
                effective_source=source,
                runtime_editable=spec.editable,
                max_scope=spec.max_scope,
                # RAG_EMBEDDING_DIM est exclue DUREMENT de l'overlay runtime :
                # un override ne la piloterait jamais, flag ou pas.
                runtime_active=bool(
                    runtime_on and spec.editable and key != "RAG_EMBEDDING_DIM"
                ),
                restart_required=spec.restart_required,
                display_value=display_value,
            )
        )
    return entries


def resolve_health() -> dict[str, Any]:
    """Santé Core/Gateway — SANS appel réseau (option conservatrice PR-A).

    Reflète l'overlay DB au niveau global/environnement (cohérent avec la config
    effective). Le test de génération réel du Gateway viendra plus tard.
    """
    environment = current_environment()
    emap = _effective_map(environment=environment)

    base_url = str(emap["AI_GATEWAY_BASE_URL"][0] or "")
    gateway_enabled = bool(emap["AI_GATEWAY_ENABLED"][0])
    fallback_enabled = bool(emap["AI_GATEWAY_FALLBACK_ENABLED"][0])
    embedding_dim = int(emap["RAG_EMBEDDING_DIM"][0])

    return {
        # aucune sonde n'établit la santé du « cœur » ; ``ok`` n'est plus
        # affirmé sans preuve (null = non vérifié). La santé RÉELLE des services Gateway est
        # ajoutée par la vue (sondes GET /health/*), jamais inventée ici.
        "core_health": {"ok": None, "verified": False, "embedding_dim": embedding_dim},
        "gateway_configured": bool(base_url),
        "gateway_enabled": gateway_enabled,
        "gateway_health": None,
        "fallback_enabled": fallback_enabled,
    }

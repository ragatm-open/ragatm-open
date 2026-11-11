"""Parseurs typés de configuration — module PUR (aucun import Django).

Source de vérité unique du *parsing* des valeurs de configuration, qu'elles
viennent de l'environnement (`os.environ`), d'un fichier `.env.example` ou d'un
override stocké en base par RagATM_Core.

Pourquoi un module à part :

- ``config/env.py`` (booléens robustes) ne couvrait que ``bool``/``str`` ; les
  entiers, flottants, URLs, listes et énumérations étaient reparsés à la main
  dans ``settings/base.py``, dans les services et dans le résolveur Core — avec
  des conventions divergentes (``== "1"`` ici, ``in ("1","true","yes")`` là) ;
- le registre canonique (``config.registry``) doit pouvoir valider des valeurs
  **sans** charger Django : il est consommé par les tests, la commande d'audit
  et la génération documentaire.

Règle de conception : un parseur **lève** (`ConfigValueError`) sur une valeur
invalide. On n'échoue jamais silencieusement vers le défaut — cf. l'incident
``RAG_HYBRID_ENABLED=True`` documenté dans ``config/env.py``.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

# Types de valeurs supportés par le registre.
TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_STR = "str"
TYPE_URL = "url"
TYPE_LIST = "list"
TYPE_ENUM = "enum"
TYPE_PATH = "path"

VALUE_TYPES: frozenset[str] = frozenset(
    {TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STR, TYPE_URL, TYPE_LIST, TYPE_ENUM, TYPE_PATH}
)

# Littéraux booléens acceptés — strictement les mêmes que ``config/env.py``
# (source historique) pour ne rejeter aucune valeur d'un `.env` existant.
TRUE_LITERALS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
FALSE_LITERALS: frozenset[str] = frozenset({"0", "false", "no", "off"})

# Schémas d'URL acceptés. Volontairement restreint : une URL de configuration
# RagATM est toujours un service HTTP ou un backing service (redis, postgres).
URL_SCHEMES: frozenset[str] = frozenset(
    {"http", "https", "redis", "rediss", "postgres", "postgresql", "amqp", "amqps"}
)


class ConfigValueError(ValueError):
    """Valeur de configuration invalide (type, bornes ou énumération).

    Hérite de ``ValueError`` : les appelants historiques qui interceptaient
    ``ValueError`` (service d'écriture des overrides Core) restent corrects.
    """

    def __init__(self, name: str, reason: str, *, hint: str = "") -> None:
        self.name = name
        self.reason = reason
        self.hint = hint
        message = f"{name}: {reason}"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)


def normalize_raw(raw: Any) -> str:
    """Normalise une valeur brute : trim + retrait des guillemets englobants.

    ``.env`` autorise ``KEY="valeur"`` ; sans ce retrait, ``"true"`` (guillemets
    compris) ne serait pas reconnu comme booléen.
    """
    if raw is None:
        return ""
    return str(raw).strip().strip("\"'").strip()


def parse_bool(name: str, raw: Any) -> bool:
    value = normalize_raw(raw).lower()
    if value in TRUE_LITERALS:
        return True
    if value in FALSE_LITERALS:
        return False
    raise ConfigValueError(
        name,
        f"{raw!r} n'est pas un booléen reconnu",
        hint=f"vrai: {sorted(TRUE_LITERALS)} / faux: {sorted(FALSE_LITERALS)}",
    )


def parse_int(name: str, raw: Any) -> int:
    value = normalize_raw(raw)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigValueError(name, f"{raw!r} n'est pas un entier") from None


def parse_float(name: str, raw: Any) -> float:
    value = normalize_raw(raw)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigValueError(name, f"{raw!r} n'est pas un nombre") from None


def parse_str(name: str, raw: Any) -> str:  # noqa: ARG001 — signature homogène
    return normalize_raw(raw)


def parse_path(name: str, raw: Any) -> str:  # noqa: ARG001 — signature homogène
    return normalize_raw(raw)


def parse_url(name: str, raw: Any, *, allow_empty: bool = True) -> str:
    """Valide une URL absolue sans jamais la résoudre (aucun appel réseau)."""
    value = normalize_raw(raw)
    if not value:
        if allow_empty:
            return ""
        raise ConfigValueError(name, "URL vide")
    parsed = urlparse(value)
    if not parsed.scheme:
        raise ConfigValueError(name, "URL sans schéma", hint="ex. http://hote:8100")
    if parsed.scheme not in URL_SCHEMES:
        raise ConfigValueError(
            name,
            f"schéma d'URL non supporté ({parsed.scheme!r})",
            hint=f"schémas acceptés : {sorted(URL_SCHEMES)}",
        )
    if not parsed.netloc:
        raise ConfigValueError(name, "URL sans hôte", hint="ex. http://hote:8100")
    return value


def parse_list(name: str, raw: Any, *, separator: str = ",") -> list[str]:  # noqa: ARG001
    """Liste d'éléments non vides, séparateur ``,`` par défaut. ``""`` -> ``[]``."""
    value = normalize_raw(raw)
    if not value:
        return []
    return [item.strip() for item in value.split(separator) if item.strip()]


def parse_enum(name: str, raw: Any, choices: Sequence[str], *, case_sensitive: bool = False) -> str:
    value = normalize_raw(raw)
    if case_sensitive:
        if value in choices:
            return value
    else:
        lowered = value.lower()
        for choice in choices:
            if choice.lower() == lowered:
                return choice
    raise ConfigValueError(
        name, f"{raw!r} hors des valeurs autorisées", hint=f"valeurs : {list(choices)}"
    )


def parse(
    name: str,
    raw: Any,
    value_type: str,
    *,
    choices: Sequence[str] | None = None,
    separator: str = ",",
) -> Any:
    """Parse selon le type déclaré du registre. Lève ``ConfigValueError``."""
    if value_type == TYPE_BOOL:
        return parse_bool(name, raw)
    if value_type == TYPE_INT:
        return parse_int(name, raw)
    if value_type == TYPE_FLOAT:
        return parse_float(name, raw)
    if value_type == TYPE_URL:
        return parse_url(name, raw)
    if value_type == TYPE_LIST:
        return parse_list(name, raw, separator=separator)
    if value_type == TYPE_ENUM:
        return parse_enum(name, raw, tuple(choices or ()))
    if value_type == TYPE_PATH:
        return parse_path(name, raw)
    if value_type == TYPE_STR:
        return parse_str(name, raw)
    raise ConfigValueError(name, f"type de valeur inconnu ({value_type!r})")


def serialize(value: Any, value_type: str, *, separator: str = ",") -> str:
    """Forme TEXTE canonique d'une valeur typée.

    Utilisée pour persister un override Core et pour rendre la documentation
    déterministe : ``True`` -> ``"true"``, ``["a","b"]`` -> ``"a,b"``.
    """
    if value is None:
        return ""
    if value_type == TYPE_BOOL:
        return "true" if bool(value) else "false"
    if value_type == TYPE_LIST:
        if isinstance(value, str):
            items: Iterable[str] = parse_list("<literal>", value, separator=separator)
        else:
            items = [str(item) for item in value]
        return separator.join(items)
    return str(value)


def check_bounds(
    name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    """Vérifie les bornes numériques. Ne fait rien si aucune borne n'est déclarée."""
    if minimum is None and maximum is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    if minimum is not None and value < minimum:
        raise ConfigValueError(name, f"{value} < minimum {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigValueError(name, f"{value} > maximum {maximum}")

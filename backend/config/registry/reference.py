"""Génération DÉTERMINISTE de la référence de configuration (Markdown).

Deux exécutions successives sans modification du registre produisent le même
octet : c'est ce qui permet à la CI de détecter une documentation non
régénérée (`generate_config_reference --check`).

Aucune valeur sensible n'est rendue : pour un secret, seule l'information
« configuré / obligatoire » est produite, jamais la valeur ni le défaut.
"""
from __future__ import annotations

from config import registry
from config.registry.definition import DOMAIN_LABELS

HEADER = """<!-- FICHIER GÉNÉRÉ — NE PAS MODIFIER À LA MAIN -->
<!-- Source : backend/config/registry/ · Régénérer : make config-reference -->

# Référence de configuration

*Statut : **implémenté**. Document **généré** depuis le registre canonique
[`backend/config/registry/`](../../backend/config/registry/). Toute correction se
fait dans le registre, jamais dans ce fichier — la CI compare les deux.*

## Comment lire ce document

| Colonne | Signification |
|---|---|
| **Type** | `bool`, `int`, `float`, `str`, `url`, `list`, `enum`, `path`. Un booléen accepte `1/true/yes/on` et `0/false/no/off`. |
| **Défaut** | Valeur appliquée quand la variable est absente ou vide. `= AUTRE` signale un défaut dérivé d'un autre réglage. |
| **Env.** | Environnements où la variable a un sens. **G** = obligatoire dans l'environnement indiqué. |
| **Dyn.** | Modifiable par override RagATM_Core (`✓`) ou uniquement par redéploiement (`—`). |
| **Portée** | Portée maximale d'un override : `global` < `environment` < `workspace` < `ragspace`. |
| **Secret** | `✓` = valeur jamais affichée (API, journaux, audit, documentation). |
| **Redém.** | `✓` = un redémarrage est nécessaire pour appliquer un changement. |

Ordre de résolution de la valeur effective :

```
ragspace > workspace > environment > global > settings/.env > défaut de code
```

Les quatre premiers niveaux sont les overrides RagATM_Core. Ils ne sont appliqués
au **runtime** que si `CORE_CONFIG_RUNTIME_ENABLED=1` ; sinon ils restent
visibles dans l'Admin Core sans être actifs (`runtime_active=false`).
"""

FOOTER = """
## Références d'implémentation

- Registre canonique : [`backend/config/registry/`](../../backend/config/registry/)
- Parseurs typés : [`backend/config/parsers.py`](../../backend/config/parsers.py)
- Pont settings ↔ registre : [`backend/config/settings/from_registry.py`](../../backend/config/settings/from_registry.py)
- System checks de configuration : [`backend/config/checks.py`](../../backend/config/checks.py)
- Audit `.env` : `python manage.py config_audit [--format json] [--strict]`
- Régénération de ce document : `python manage.py generate_config_reference`
"""

_ENV_SHORT = {"dev": "dev", "test": "test", "staging": "stg", "prod": "prod"}


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _environments_cell(definition) -> str:
    parts = []
    for env in definition.environments:
        label = _ENV_SHORT[env]
        if definition.is_required_in(env):
            label = f"**{label}**"
        parts.append(label)
    return " ".join(parts)


def _row(definition) -> str:
    dynamic = "✓" if definition.runtime_editable else "—"
    scope = definition.max_scope or "—"
    secret = "✓" if definition.secret else "—"
    restart = "✓" if definition.restart_required else "—"
    default = "(non affiché)" if definition.secret else definition.default_display()
    description = _escape(definition.description)
    if definition.deprecated:
        replacement = (
            f" Remplacée par `{definition.replacement}`."
            if definition.replacement
            else " Sans remplacement."
        )
        description = f"**DÉPRÉCIÉE.**{replacement} {description}"
    if definition.notes:
        description = f"{description} — {_escape(definition.notes)}"
    bounds = ""
    if definition.minimum is not None or definition.maximum is not None:
        low = "" if definition.minimum is None else _number(definition.minimum)
        high = "" if definition.maximum is None else _number(definition.maximum)
        bounds = f" *(bornes : {low or '−∞'} … {high or '+∞'})*"
    if definition.choices:
        bounds = f" *(valeurs : {', '.join(definition.choices)})*"

    return (
        f"| `{definition.name}` | {definition.value_type} | `{_escape(str(default))}` "
        f"| {_environments_cell(definition)} | {dynamic} | {scope} | {secret} "
        f"| {restart} | {description}{bounds} |"
    )


def _number(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render() -> str:
    """Rend le document complet. Fonction PURE : même registre => même texte."""
    lines: list[str] = [HEADER.rstrip(), ""]

    groups = registry.grouped_by_domain()

    lines.append("## Sommaire")
    lines.append("")
    for domain, items in groups:
        label = DOMAIN_LABELS[domain]
        anchor = _anchor(label)
        lines.append(f"- [{label}](#{anchor}) — {len(items)} variables")
    lines.append("")

    total = len(registry.all_definitions())
    secrets = len(registry.secrets())
    dynamic = len(registry.runtime_editable())
    deprecated = len(registry.deprecated())
    lines.append(
        f"**{total} variables** déclarées · {secrets} secrètes · {dynamic} "
        f"modifiables dynamiquement · {deprecated} dépréciées."
    )
    lines.append("")

    for domain, items in groups:
        lines.append(f"## {DOMAIN_LABELS[domain]}")
        lines.append("")
        lines.append(
            "| Variable | Type | Défaut | Env. | Dyn. | Portée | Secret | Redém. | Description |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for definition in items:
            lines.append(_row(definition))
        lines.append("")

    deprecated_items = registry.deprecated()
    lines.append("## Dépréciations")
    lines.append("")
    if not deprecated_items and not registry.DEPRECATED_ALIASES:
        lines.append("Aucune variable dépréciée.")
        lines.append("")
    else:
        lines.append(
            "Politique : l'ancien nom reste **accepté** (aucun déploiement ne casse), "
            "il produit un avertissement, et sa valeur est reportée sur le nom "
            "canonique. Ancien **et** nouveau définis avec des valeurs **différentes** "
            "⇒ refus de démarrer."
        )
        lines.append("")
        lines.append("| Ancien nom | Remplacement | Déprécié depuis | Suppression après | Note |")
        lines.append("|---|---|---|---|---|")
        for alias in registry.DEPRECATED_ALIASES:
            lines.append(
                f"| `{alias.old_name}` | `{alias.new_name}` | {alias.since} "
                f"| {alias.remove_after} | {_escape(alias.note)} |"
            )
        for definition in deprecated_items:
            if registry.alias_for(definition.name) is not None:
                continue
            replacement = f"`{definition.replacement}`" if definition.replacement else "—"
            lines.append(
                f"| `{definition.name}` | {replacement} | — | — | {_escape(definition.notes)} |"
            )
        lines.append("")

    lines.append(FOOTER.strip())
    lines.append("")
    return "\n".join(lines)


def _anchor(label: str) -> str:
    text = label.lower()
    keep = []
    for char in text:
        if char.isalnum():
            keep.append(char)
        elif char in " -_":
            keep.append("-")
    anchor = "".join(keep)
    while "--" in anchor:
        anchor = anchor.replace("--", "-")
    return anchor.strip("-")

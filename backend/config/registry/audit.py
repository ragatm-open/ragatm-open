"""Audit de cohérence entre le registre canonique et les fichiers `.env.example`.

Module PUR : il lit des fichiers d'EXEMPLE versionnés (jamais un `.env` réel,
jamais un secret de production) et ne dépend pas de Django.

Trois familles de constats :

- ``error``   : configuration cassée ou fuite potentielle => code de sortie non nul ;
- ``warning`` : divergence à traiter (variable non couverte, dépréciation) ;
- ``info``    : information de couverture.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from config import registry
from config.parsers import ConfigValueError

# Fichiers d'exemple audités, avec l'environnement qu'ils décrivent.
EXAMPLE_FILES: tuple[tuple[str, str], ...] = (
    (".env.example", "dev"),
    (".env.staging.example", "staging"),
    (".env.prod.example", "prod"),
)

#: Motifs qui trahissent une valeur d'exemple (donc PAS un secret réel).
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "change-me",
    "changeme",
    "change_me",
    "replace",
    "your-",
    "your_",
    "à-remplir",
    "a-remplir",
    "todo",
    "xxx",
    "dummy",
    "example",
    "généré",
    "genere",
    "placeholder",
)

_LINE_RE = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9_]*)\s*=(?P<value>.*)$")
_COMMENTED_RE = re.compile(r"^\s*#\s*(?P<name>[A-Z][A-Z0-9_]*)\s*=(?P<value>.*)$")

SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    name: str = ""
    location: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "name": self.name,
            "location": self.location,
            "message": self.message,
        }

    def render(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity.upper():7} {self.code:22} {self.message}{where}"


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def as_dict(self) -> dict[str, object]:
        return {
            "stats": dict(sorted(self.stats.items())),
            "counts": {
                severity: sum(1 for f in self.findings if f.severity == severity)
                for severity in SEVERITIES
            },
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Lecture des fichiers d'exemple
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvEntry:
    name: str
    value: str
    line: int
    commented: bool


def parse_env_file(path: Path) -> tuple[list[EnvEntry], list[str]]:
    """Analyse un fichier `.env`. Retourne (entrées, lignes non analysables).

    Les lignes commentées de la forme ``# NAME=valeur`` sont conservées : elles
    documentent une variable, et une clé inconnue y est tout aussi trompeuse.
    """
    entries: list[EnvEntry] = []
    unparsable: list[str] = []
    for number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        match = _LINE_RE.match(line)
        if match:
            entries.append(
                EnvEntry(
                    name=match.group("name"),
                    value=match.group("value").strip(),
                    line=number,
                    commented=False,
                )
            )
            continue
        commented = _COMMENTED_RE.match(line)
        if commented:
            entries.append(
                EnvEntry(
                    name=commented.group("name"),
                    value=commented.group("value").strip(),
                    line=number,
                    commented=True,
                )
            )
            continue
        if line.lstrip().startswith("#"):
            continue
        unparsable.append(f"{path.name}:{number}: {line[:60]}")
    return entries, unparsable


def looks_like_placeholder(value: str) -> bool:
    stripped = value.strip().strip("\"'")
    if not stripped:
        return True
    lowered = stripped.lower()
    if stripped.startswith("<") or stripped.startswith("${"):
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def audit_example_file(path: Path, environment: str, report: AuditReport) -> None:
    entries, unparsable = parse_env_file(path)

    for line in unparsable:
        report.add(
            Finding("error", "env.unparsable", f"Ligne non analysable : {line}", location=path.name)
        )

    seen: dict[str, int] = {}
    for entry in entries:
        location = f"{path.name}:{entry.line}"
        if not entry.commented:
            if entry.name in seen:
                report.add(
                    Finding(
                        "error",
                        "env.duplicate",
                        f"{entry.name} est définie deux fois (première : ligne {seen[entry.name]}).",
                        name=entry.name,
                        location=location,
                    )
                )
            else:
                seen[entry.name] = entry.line

        definition = registry.get(entry.name)
        alias = registry.alias_for(entry.name)

        if definition is None and alias is None:
            if entry.name in registry.IGNORED_ENV_NAMES:
                continue
            report.add(
                Finding(
                    "warning",
                    "env.unregistered",
                    f"{entry.name} n'est pas déclarée au registre de configuration.",
                    name=entry.name,
                    location=location,
                )
            )
            continue

        if alias is not None and definition is None:
            report.add(
                Finding(
                    "warning",
                    "env.deprecated",
                    f"{entry.name} est dépréciée ; utiliser {alias.new_name}.",
                    name=entry.name,
                    location=location,
                )
            )
            continue

        assert definition is not None  # pour les vérificateurs de types

        if definition.deprecated:
            replacement = (
                f" Utiliser {definition.replacement}." if definition.replacement else ""
            )
            report.add(
                Finding(
                    "warning",
                    "env.deprecated",
                    f"{entry.name} est dépréciée.{replacement}",
                    name=entry.name,
                    location=location,
                )
            )

        if environment not in definition.environments:
            report.add(
                Finding(
                    "warning",
                    "env.scope",
                    f"{entry.name} est déclarée pour {list(definition.environments)} "
                    f"mais figure dans un exemple {environment}.",
                    name=entry.name,
                    location=location,
                )
            )

        # Valeur : une valeur vide signifie « laisser le défaut » (contrat des .env).
        # Les lignes COMMENTÉES ne sont pas auditées sur la valeur : `# NAME=1 (voir
        # tel check)` documente la variable, la fin de ligne est de la prose.
        if entry.value.strip() and not entry.commented:
            _audit_value(definition, entry, location, report)

    _audit_required(path, environment, entries, report)


def _audit_value(definition, entry: EnvEntry, location: str, report: AuditReport) -> None:
    if looks_like_placeholder(entry.value):
        return

    try:
        definition.parse(entry.value)
    except ConfigValueError as exc:
        report.add(
            Finding(
                "error",
                "env.invalid_value",
                f"{entry.name} : {exc.reason}.",
                name=entry.name,
                location=location,
            )
        )
        return

    # Un secret PLEINEMENT masqué ne doit jamais porter de valeur réaliste dans un
    # fichier versionné. Les valeurs à redaction partielle (URL privée) sont
    # exclues : leur hôte est déjà documenté et n'est pas un identifiant.
    if definition.secret and definition.redact_mode == "full":
        report.add(
            Finding(
                "error",
                "env.secret_leak",
                f"{entry.name} porte une valeur réaliste dans un fichier versionné "
                "(la valeur n'est volontairement pas reproduite ici).",
                name=entry.name,
                location=location,
            )
        )


def _audit_required(
    path: Path, environment: str, entries: list[EnvEntry], report: AuditReport
) -> None:
    declared = {e.name for e in entries if not e.commented and e.value.strip()}
    for definition in registry.all_definitions():
        if not definition.is_required_in(environment):
            continue
        if definition.name in declared:
            continue
        if any(a.new_name == definition.name and a.old_name in declared for a in registry.DEPRECATED_ALIASES):
            continue
        report.add(
            Finding(
                "error",
                "env.missing_required",
                f"{definition.name} est obligatoire en {environment} mais absente.",
                name=definition.name,
                location=path.name,
            )
        )


# ---------------------------------------------------------------------------
# Scan du code : variables lues mais non déclarées
# ---------------------------------------------------------------------------
_CODE_ENV_RE = re.compile(
    # Les lecteurs du dépôt, tous nommés ici : un lecteur absent de cette liste
    # fait passer ses paramètres pour « lus par personne », ce qui transforme un
    # inventaire en bruit. `get_registered_setting`/`setting*` sont l'API du
    # registre ; `_get_setting` est le lecteur de `llm_router`, qui interroge les
    # settings PUIS l'environnement.
    r"""(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[|env_bool\(|env_str\(|_env_flag\(|reg\(|env_value\(|get_registered_setting\(|_get_setting\(|setting\(|setting_int\(|setting_float\(|setting_bool\(|_s\()\s*["']([A-Z][A-Z0-9_]{2,})["']"""
)

#: Lecture d'environnement dans les scripts shell (`${NAME}`, `$NAME`, `NAME=`).
_SHELL_ENV_RE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")

_SKIP_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".next", ".pytest_cache",
     ".mypy_cache", "staticfiles", "media", "benchmarks"}
)


def iter_code_env_names(root: Path) -> Iterator[tuple[str, str, str]]:
    """Noms de variables lus depuis l'environnement par le code du dépôt.

    Couvre le Python (settings, services, commandes) et les scripts shell
    d'exploitation : une variable n'est « consommée » que si quelqu'un la lit.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(".py"):
                pattern = _CODE_ENV_RE
            elif filename.endswith(".sh"):
                pattern = _SHELL_ENV_RE
            else:
                continue
            file_path = Path(dirpath) / filename
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover - fichier illisible
                continue
            relative = file_path.relative_to(root)
            # Les tests lisent l'environnement pour se configurer eux-mêmes (noms
            # d'exemple, échappatoires). Ils prouvent qu'une variable est
            # consommée, mais ne sont pas une source de déclaration : un nom
            # inventé dans un test n'a pas à figurer au registre.
            is_test = "tests" in relative.parts or filename.startswith("test_")
            if pattern is not _CODE_ENV_RE:
                kind = "shell"
            elif is_test:
                kind = "test"
            else:
                kind = "python"
            for match in pattern.finditer(text):
                yield match.group(1), str(file_path.relative_to(root)), kind


def audit_code_coverage(root: Path, report: AuditReport) -> None:
    observed: dict[str, str] = {}
    python_observed: dict[str, str] = {}
    for name, location, kind in iter_code_env_names(root):
        observed.setdefault(name, location)
        if kind == "python":
            python_observed.setdefault(name, location)

    # Le scan shell sert UNIQUEMENT à prouver qu'une variable est consommée : il
    # capterait sinon toutes les variables système ($HOME, $PWD…) comme
    # « non déclarées », ce qui noierait le constat utile.
    for name in registry.iter_unregistered(python_observed):
        report.add(
            Finding(
                "warning",
                "code.unregistered",
                f"{name} est lue depuis l'environnement mais absente du registre.",
                name=name,
                location=python_observed[name],
            )
        )

    # Variables déclarées mais qu'AUCUN code de ce dépôt ne lit : réglage inerte
    # (lot livré sur une autre branche, variable d'infrastructure, ou vestige).
    # Information, pas anomalie : la liste est le point de départ du nettoyage.
    unconsumed = sorted(
        d.name
        for d in registry.all_definitions()
        if d.name not in observed and d.domain != "deployment"
    )
    for name in unconsumed:
        definition = registry.require(name)
        report.add(
            Finding(
                "info",
                "registry.unconsumed",
                f"{name} est déclarée mais lue par aucun module Python du dépôt "
                f"(source déclarée : {definition.source}).",
                name=name,
            )
        )

    report.stats["code_env_names"] = len(observed)
    report.stats["unconsumed"] = len(unconsumed)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def run_audit(
    repo_root: Path,
    *,
    files: Iterable[tuple[str, str]] = EXAMPLE_FILES,
    scan_code: bool = True,
) -> AuditReport:
    report = AuditReport()
    report.stats["registered"] = len(registry.all_definitions())
    report.stats["secrets"] = len(registry.secrets())
    report.stats["deprecated"] = len(registry.deprecated())
    report.stats["runtime_editable"] = len(registry.runtime_editable())

    audited = 0
    for relative, environment in files:
        path = repo_root / relative
        if not path.exists():
            report.add(
                Finding(
                    "info",
                    "env.absent",
                    f"{relative} est absent du dépôt (aucun exemple {environment}).",
                    location=relative,
                )
            )
            continue
        audited += 1
        audit_example_file(path, environment, report)
    report.stats["example_files"] = audited

    if scan_code:
        audit_code_coverage(repo_root, report)

    return report

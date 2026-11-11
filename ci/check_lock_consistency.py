#!/usr/bin/env python3
"""ci/check_lock_consistency.py — cohérence DÉTERMINISTE entre les déclarations de dépendances et les locks d'exécution.

    ci/check_lock_consistency.py            (code 0 si cohérent, 1 sinon, 64 si prérequis manquant)

Vérifie, SANS consulter PyPI, que chaque exigence déclarée est satisfaite par un pin du lock correspondant :
  - `backend/pyproject.toml` : [project].dependencies, plus les extras couverts par chaque lock
    (requirements.lock : [tables] ; requirements-ocr.lock : [tables, ocr] ; requirements-ifc.lock : [tables, ifc],
    comme le déclare backend/lock-runtime.sh) ;
  - `backend/requirements-runtime.in` (serveur WSGI) ;
  - chaque pin d'un lock porte au moins une empreinte `--hash=` (installation `--require-hashes`).
Les marqueurs d'environnement sont évalués pour la cible des locks (Linux, CPython 3.12, x86_64).

Ce contrôle est BLOQUANT et reproductible. Il remplace, comme porte, la re-résolution complète
(`backend/lock-runtime.sh --check`) qui dépend de l'état de l'index au moment du run et reste exécutée en
mode consultatif. Il ne prouve PAS qu'un lock est la résolution « optimale » : il prouve que les locks livrés
satisfont toutes les déclarations et sont installables à empreintes vérifiées.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version
except ImportError:  # l'environnement CI installe `packaging` via ci/requirements-ci.lock ; pip l'embarque sinon
    from pip._vendor.packaging.requirements import Requirement  # type: ignore
    from pip._vendor.packaging.utils import canonicalize_name  # type: ignore
    from pip._vendor.packaging.version import Version  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LOCKS = {
    "requirements.lock": ["tables"],
    "requirements-ocr.lock": ["tables", "ocr"],
    "requirements-ifc.lock": ["tables", "ifc"],
}
TARGET_ENV = {
    "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
    "python_version": "3.12", "python_full_version": "3.12.0", "implementation_name": "cpython",
    "platform_python_implementation": "CPython", "extra": "",
}
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s\;]+)")


def parse_lock(path: Path) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    without_hash: list[str] = []
    current = None
    hashed = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = PIN.match(line)
        if m:
            if current and not hashed:
                without_hash.append(current)
            current, hashed = canonicalize_name(m.group(1)), False
            pins[current] = m.group(3)
        elif current and line.startswith("--hash="):
            hashed = True
    if current and not hashed:
        without_hash.append(current)
    return pins, without_hash


def requirements_for(extras: list[str]) -> list[Requirement]:
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = [Requirement(r) for r in project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            print(f"ERREUR : extra [{extra}] absent de pyproject.toml")
            sys.exit(64)
        reqs += [Requirement(r) for r in optional[extra]]
    for line in (BACKEND / "requirements-runtime.in").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            reqs.append(Requirement(line))
    return reqs


def main() -> int:
    problems: list[str] = []
    for lock_name, extras in LOCKS.items():
        path = BACKEND / lock_name
        if not path.is_file():
            problems.append(f"{lock_name} : fichier absent")
            continue
        pins, without_hash = parse_lock(path)
        checked = 0
        for req in requirements_for(extras):
            if req.marker is not None and not req.marker.evaluate(TARGET_ENV):
                continue
            name = canonicalize_name(req.name)
            if name not in pins:
                problems.append(f"{lock_name} : {req} — aucun pin")
                continue
            version = Version(pins[name])
            if not req.specifier.contains(version, prereleases=True):
                problems.append(f"{lock_name} : {req} — pin {version} hors contrainte")
            checked += 1
        for name in without_hash:
            problems.append(f"{lock_name} : {name} — pin sans empreinte --hash")
        print(f"[lock-consistency] {lock_name} : {len(pins)} pins, {checked} exigences vérifiées (extras {extras})")
    if problems:
        print("[lock-consistency] ÉCHEC :")
        for p in problems:
            print("   " + p)
        return 1
    print("[lock-consistency] OK — déclarations satisfaites par les locks, toutes les distributions à empreintes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

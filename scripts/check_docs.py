#!/usr/bin/env python3
"""Vérificateur de cohérence de la documentation RagATM.

Contrôle, sans dépendance externe :
  1. les liens Markdown relatifs cassés (cible inexistante) dans docs/ ;
  2. les fichiers/chemins de code référencés qui n'existent pas ;
  3. les documents « orphelins » (aucun lien entrant depuis un autre doc) ;
  4. les titres H1 en doublon entre fichiers (indice de contenu redondant).

Le script est volontairement tolérant : il ne bloque que sur des faits vérifiables
(liens et fichiers manquants). Les orphelins et doublons de titres sont des
avertissements (n'échouent pas par défaut).

Usage :
    python scripts/check_docs.py            # rapport + code de sortie
    python scripts/check_docs.py --strict   # les avertissements deviennent bloquants
    python scripts/check_docs.py --docs docs

Code de sortie : 0 si aucune erreur (bloquante), 1 sinon.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Lien Markdown [texte](cible) — on ignore images ![...] traitées pareil (cible fichier).
LINK_RE = re.compile(r"(?<!\\)\[[^\]]*\]\(([^)]+)\)")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Schémas de liens externes/non-fichiers à ignorer.
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# Sous-dossiers exclus du contrôle (documents figés/historiques, liens non maintenus).
EXCLUDED_DIRS = {"archive"}


def iter_markdown(docs_dir: Path, include_archive: bool = False):
    for p in sorted(docs_dir.rglob("*.md")):
        if not include_archive and any(part in EXCLUDED_DIRS for part in p.relative_to(docs_dir).parts):
            continue
        yield p


def resolve_target(md_file: Path, target: str) -> Path:
    """Résout une cible de lien (relative au fichier md) en chemin absolu, sans l'ancre."""
    target = target.split("#", 1)[0].strip()
    return (md_file.parent / target).resolve()


def check_links(docs_dir: Path, root: Path, include_archive: bool = False):
    """Retourne (erreurs_liens, cibles_docs_referencees)."""
    errors = []
    referenced_docs: set[Path] = set()
    for md in iter_markdown(docs_dir, include_archive):
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            raw = m.group(1).strip()
            if not raw or raw.startswith(EXTERNAL_PREFIXES):
                continue
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
            target = raw.split("#", 1)[0].strip()
            if not target:  # lien purement ancre (#...) déjà filtré
                continue
            resolved = resolve_target(md, raw)
            if resolved.exists():
                if resolved.suffix == ".md" and docs_dir in resolved.parents:
                    referenced_docs.add(resolved)
                continue
            # Cible manquante : distinguer lien intra-doc vs référence code.
            rel = _safe_rel(md, root)
            errors.append(f"{rel}: lien cassé -> {raw}")
    return errors, referenced_docs


def _safe_rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def check_orphans(docs_dir: Path, root: Path, referenced_docs: set[Path], include_archive: bool = False):
    """Documents jamais référencés par un lien (hors README/point d'entrée)."""
    warnings = []
    entry_points = {
        (docs_dir / "README.md").resolve(),
    }
    for md in iter_markdown(docs_dir, include_archive):
        rp = md.resolve()
        if rp in entry_points:
            continue
        # Les README de section sont des points d'entrée implicites (liens de dossier).
        if md.name == "README.md":
            continue
        if rp not in referenced_docs:
            warnings.append(f"{_safe_rel(md, root)}: document non référencé (orphelin)")
    return warnings


def check_duplicate_titles(docs_dir: Path, root: Path, include_archive: bool = False):
    """Titres H1 identiques dans plusieurs fichiers (indice de doublon)."""
    warnings = []
    seen: dict[str, list[str]] = {}
    for md in iter_markdown(docs_dir, include_archive):
        text = md.read_text(encoding="utf-8", errors="replace")
        m = H1_RE.search(text)
        if not m:
            continue
        title = m.group(1).strip().lower()
        seen.setdefault(title, []).append(_safe_rel(md, root))
    for title, files in seen.items():
        if len(files) > 1:
            warnings.append(f"titre H1 en doublon « {title} » : {', '.join(files)}")
    return warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Vérifie la cohérence de la doc RagATM.")
    ap.add_argument("--docs", default="docs", help="répertoire de documentation (défaut: docs)")
    ap.add_argument("--strict", action="store_true", help="échoue aussi sur les avertissements")
    ap.add_argument("--include-archive", action="store_true",
                    help="inclut docs/archive/ (exclu par défaut : documents figés)")
    args = ap.parse_args()

    root = repo_root()
    docs_dir = (root / args.docs).resolve()
    if not docs_dir.is_dir():
        print(f"ERREUR: répertoire introuvable: {docs_dir}", file=sys.stderr)
        return 2

    inc = args.include_archive
    link_errors, referenced = check_links(docs_dir, root, inc)
    orphan_warnings = check_orphans(docs_dir, root, referenced, inc)
    title_warnings = check_duplicate_titles(docs_dir, root, inc)

    n_md = sum(1 for _ in iter_markdown(docs_dir, inc))
    print(f"Documentation : {n_md} fichiers Markdown analysés dans {args.docs}/\n")

    if link_errors:
        print(f"❌ Liens/fichiers cassés ({len(link_errors)}) :")
        for e in link_errors:
            print(f"   - {e}")
    else:
        print("✅ Aucun lien cassé.")

    if orphan_warnings:
        print(f"\n⚠️  Documents orphelins ({len(orphan_warnings)}) :")
        for w in orphan_warnings:
            print(f"   - {w}")

    if title_warnings:
        print(f"\n⚠️  Titres en doublon ({len(title_warnings)}) :")
        for w in title_warnings:
            print(f"   - {w}")

    failed = bool(link_errors) or (args.strict and (orphan_warnings or title_warnings))
    print()
    print("Résultat :", "ÉCHEC" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

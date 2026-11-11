"""Compare deux instantanés de configuration selon le manifeste de parité.

Pourquoi cet outil existe
-------------------------
Une qualification menée en staging ne dit quelque chose de la production que sur
les chemins que les deux environnements empruntent de la même façon. La
cartographie du 31/07/2026 a relevé douze réglages fonctionnels divergents : le
repli de mode B, le moteur tabulaire, l'ingestion asynchrone et le repli Gateway
étaient ACTIFS en production et ÉTEINTS en staging. Quatre chemins que la
qualification ne pouvait donc pas valider — sans que personne ne le sache.

Ce que l'outil garantit
-----------------------
- il travaille sur des INSTANTANÉS (`dump_effective_config`), jamais sur une
  connexion à un environnement distant : un test ne doit pas dépendre d'un
  serveur joignable ;
- il ne compare JAMAIS une valeur secrète, seulement sa présence ;
- il distingue trois cas : divergence INTERDITE (code de sortie 1), divergence
  AUTORISÉE (nommée dans le manifeste), et divergence simplement RAPPORTÉE ;
- il rend un rapport JSON exploitable par la CI.

    python manage.py check_config_parity \\
        --reference production.json --candidate staging.json --json rapport.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

DEFAULT_MANIFEST = "deploy/config-parity.yml"

#: Sévérités du rapport.
INTERDITE = "interdite"
AUTORISEE = "autorisée"
RAPPORTEE = "rapportée"


def _repo_root() -> Path:
    # .../backend/apps/rag_core/management/commands/x.py -> racine du dépôt
    return Path(__file__).resolve().parents[5]


def _load_manifest(path: Path) -> tuple[list[str], dict[str, str]]:
    try:
        import yaml  # noqa: PLC0415 - dépendance d'outillage, pas de production
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise CommandError(
            "PyYAML est requis pour lire le manifeste de parité "
            f"({path}). Installez-le : `pip install pyyaml` (extra `dev`)."
        ) from exc

    if not path.is_file():
        raise CommandError(f"manifeste de parité introuvable : {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    required = list(data.get("required") or [])
    allowed = dict(data.get("allowed_differences") or {})
    if not required:
        raise CommandError(
            f"{path} ne déclare aucun réglage requis : un manifeste vide "
            "validerait n'importe quelle divergence."
        )

    # Un nom déclaré des DEUX côtés est une contradiction, pas une nuance : la
    # boucle ci-dessous ignore `allowed_differences` pour tout nom exigé, si bien
    # qu'une exception écrite pour autoriser une divergence de qualification
    # n'aurait AUCUN effet — et l'auteur croirait l'avoir déclarée. On refuse le
    # manifeste plutôt que de trancher en silence en faveur de `required`.
    ambigus = sorted(set(required) & set(allowed))
    if ambigus:
        raise CommandError(
            f"{path} déclare {len(ambigus)} réglage(s) à la fois en `required` et en "
            f"`allowed_differences` : {', '.join(ambigus)}. Un réglage est exigé "
            "identique, OU sa divergence est autorisée et nommée — jamais les deux. "
            "Pour autoriser une divergence de qualification, retirer le nom de "
            "`required` et l'inscrire en `allowed_differences` avec sa raison, son "
            "risque et son suivi."
        )
    return required, allowed


def _load_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CommandError(f"instantané introuvable : {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"{path} n'est pas un JSON valide : {exc}") from exc
    if "values" not in data:
        raise CommandError(
            f"{path} ne ressemble pas à un instantané de configuration "
            "(clé `values` absente). Produisez-le avec `dump_effective_config`."
        )
    return data


def _comparable(value: Any) -> str:
    """Forme canonique pour la comparaison.

    `1`/`True`/`"true"` désignent la même intention d'exploitation ; les traiter
    comme trois valeurs distinctes produirait des divergences fantômes qui
    finiraient par faire ignorer l'outil.
    """
    if value is None:
        return "<absent>"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("1", "true", "yes", "on"):
        return "true"
    if lowered in ("0", "false", "no", "off"):
        return "false"
    return text


class Command(BaseCommand):
    help = "Compare deux instantanés de configuration selon le manifeste de parité."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reference", required=True, help="Instantané de RÉFÉRENCE (production).")
        parser.add_argument("--candidate", required=True, help="Instantané CANDIDAT (staging).")
        parser.add_argument("--manifest", default="", help=f"Manifeste (défaut : {DEFAULT_MANIFEST}).")
        parser.add_argument("--json", dest="json_out", default="", help="Écrit le rapport JSON dans ce fichier.")

    def handle(self, *args, **options) -> None:
        manifest_path = Path(options["manifest"]) if options["manifest"] else _repo_root() / DEFAULT_MANIFEST
        required, allowed = _load_manifest(manifest_path)

        reference = _load_snapshot(Path(options["reference"]))
        candidate = _load_snapshot(Path(options["candidate"]))
        ref_values, cand_values = reference["values"], candidate["values"]

        ecarts: list[dict[str, Any]] = []
        manquants: list[str] = []

        for nom in required:
            if nom not in ref_values or nom not in cand_values:
                # Un réglage exigé mais absent d'un instantané est une anomalie
                # de manifeste, pas une parité : le dire plutôt que le taire.
                manquants.append(nom)
                continue
            gauche, droite = _comparable(ref_values[nom]), _comparable(cand_values[nom])
            if gauche != droite:
                ecarts.append({
                    "name": nom, "severity": INTERDITE,
                    "reference": gauche, "candidate": droite,
                    "reason": "réglage fonctionnel exigé identique par le manifeste",
                })

        exiges = set(required)
        for nom in sorted(set(ref_values) | set(cand_values)):
            if nom in exiges:
                continue
            gauche, droite = _comparable(ref_values.get(nom)), _comparable(cand_values.get(nom))
            if gauche == droite:
                continue
            if nom in allowed:
                ecarts.append({
                    "name": nom, "severity": AUTORISEE,
                    "reference": gauche, "candidate": droite,
                    "reason": allowed[nom],
                })
            else:
                ecarts.append({
                    "name": nom, "severity": RAPPORTEE,
                    "reference": gauche, "candidate": droite,
                    "reason": "ni exigé ni déclaré différent — à trancher",
                })

        interdites = [e for e in ecarts if e["severity"] == INTERDITE]
        rapport = {
            "schema_version": 1,
            "manifest": str(manifest_path),
            "reference": {"label": reference.get("label"), "app_git_sha": reference.get("app_git_sha")},
            "candidate": {"label": candidate.get("label"), "app_git_sha": candidate.get("app_git_sha")},
            "counts": {
                "required": len(required),
                "forbidden_differences": len(interdites),
                "allowed_differences": sum(1 for e in ecarts if e["severity"] == AUTORISEE),
                "reported_differences": sum(1 for e in ecarts if e["severity"] == RAPPORTEE),
                "missing_from_snapshots": len(manquants),
            },
            "missing_from_snapshots": manquants,
            "differences": ecarts,
            "verdict": "divergence" if interdites else "parité",
        }

        if options["json_out"]:
            Path(options["json_out"]).write_text(
                json.dumps(rapport, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )

        self._render(rapport, ecarts, manquants)

        if interdites:
            raise CommandError(
                f"exit 1 — {len(interdites)} divergence(s) fonctionnelle(s) interdite(s) "
                f"entre « {reference.get('label')} » et « {candidate.get('label')} »."
            )

    def _render(self, rapport: dict[str, Any], ecarts: list[dict[str, Any]], manquants: list[str]) -> None:
        w = self.stdout.write
        ref, cand = rapport["reference"]["label"], rapport["candidate"]["label"]
        w(f"parité de configuration : « {ref} » (référence) ↔ « {cand} » (candidat)")
        w(f"manifeste : {rapport['manifest']} — {rapport['counts']['required']} réglages exigés identiques")
        w("")

        for severite, titre in (
            (INTERDITE, "DIVERGENCES INTERDITES — la qualification ne couvre pas ces chemins"),
            (RAPPORTEE, "divergences rapportées — ni exigées identiques, ni déclarées différentes"),
            (AUTORISEE, "divergences autorisées"),
        ):
            lot = [e for e in ecarts if e["severity"] == severite]
            if not lot:
                continue
            w(f"{titre} ({len(lot)})")
            for e in lot:
                w(f"  {e['name']:<56} {e['reference']!s:>22}  ≠  {e['candidate']!s:<22}")
                if severite != AUTORISEE:
                    w(f"  {'':<56} {e['reason']}")
            w("")

        if manquants:
            w(f"réglages exigés ABSENTS d'un instantané ({len(manquants)}) : {', '.join(manquants)}")
            w("  (manifeste obsolète, ou instantané produit par une version antérieure)")
            w("")

        if rapport["verdict"] == "parité":
            w("VERDICT : parité — aucun réglage fonctionnel exigé ne diverge.")

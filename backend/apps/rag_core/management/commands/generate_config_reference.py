"""Génère `docs/10-reference/configuration.md` depuis le registre canonique.

    python manage.py generate_config_reference           # écrit le fichier
    python manage.py generate_config_reference --check   # échoue si périmé
    python manage.py generate_config_reference --stdout  # affiche sans écrire

La génération est déterministe : deux exécutions sans modification du registre
produisent exactement le même fichier. `--check` renvoie un code non nul quand
le fichier versionné diverge — c'est le garde-fou CI.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from config.registry import reference

DEFAULT_TARGET = "docs/10-reference/configuration.md"


class Command(BaseCommand):
    help = "Génère la référence de configuration depuis le registre canonique."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="N'écrit rien ; échoue si le fichier versionné n'est pas à jour.",
        )
        parser.add_argument(
            "--stdout",
            action="store_true",
            help="Écrit le document sur la sortie standard au lieu du fichier.",
        )
        parser.add_argument(
            "--output",
            default=None,
            help=f"Chemin du fichier généré (défaut : {DEFAULT_TARGET}).",
        )

    def handle(self, *args, **options) -> None:
        content = reference.render()

        if options["stdout"]:
            self.stdout.write(content)
            return

        target = (
            Path(options["output"]) if options["output"] else _repo_root() / DEFAULT_TARGET
        )

        if options["check"]:
            if not target.exists():
                raise CommandError(
                    f"{target} est absent. Lancez `python manage.py generate_config_reference`."
                )
            current = target.read_text(encoding="utf-8")
            if current != content:
                raise CommandError(
                    f"{target} n'est plus à jour vis-à-vis du registre de configuration. "
                    "Régénérez-le (`python manage.py generate_config_reference`) et "
                    "commitez le résultat."
                )
            self.stdout.write(self.style.SUCCESS(f"{target} est à jour."))
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"{target} écrit ({len(content)} octets)."))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]

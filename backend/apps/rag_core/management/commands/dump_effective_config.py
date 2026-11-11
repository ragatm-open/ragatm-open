"""Instantané de la configuration EFFECTIVE d'un environnement, sans secret.

Un instantané, pas une lecture de `.env` : ce qui compte est la valeur que les
processus utilisent, pas celle qu'un fichier annonce. La cartographie du
31/07/2026 a montré l'écart — vingt variables posées dans `.env.prod` n'étaient
lues par personne.

Les valeurs déclarées `secret` au registre ne sortent JAMAIS : l'instantané
n'enregistre que leur PRÉSENCE (`"<posé>"` / `"<absent>"`). C'est suffisant pour
comparer deux environnements, et c'est tout ce qu'une comparaison a besoin de
savoir d'un mot de passe.

    python manage.py dump_effective_config --output production.json
    python manage.py dump_effective_config --label staging | jq .
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from config import registry

#: Marqueurs substitués aux valeurs sensibles. Comparables entre eux, jamais
#: réversibles vers la valeur réelle.
PRESENT = "<posé>"
ABSENT = "<absent>"


class Command(BaseCommand):
    help = "Instantané JSON de la configuration effective (sans valeur secrète)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--output",
            help="Fichier de destination. Sans lui, l'instantané part sur la sortie standard.",
        )
        parser.add_argument(
            "--label",
            default="",
            help="Étiquette libre de l'instantané (par défaut : DJANGO_ENV).",
        )

    def handle(self, *args, **options) -> None:
        label = (options.get("label") or "").strip() or str(
            getattr(settings, "DJANGO_ENV", "") or "inconnu"
        )
        snapshot = {
            "schema_version": 1,
            "label": label,
            "django_env": str(getattr(settings, "DJANGO_ENV", "") or ""),
            "app_git_sha": str(getattr(settings, "APP_GIT_SHA", "") or ""),
            "values": self._values(),
        }
        payload = json.dumps(snapshot, indent=1, sort_keys=True, default=str)

        destination = options.get("output")
        if destination:
            path = Path(destination)
            path.write_text(payload + "\n", encoding="utf-8")
            self.stdout.write(
                f"instantané « {label} » écrit : {path} "
                f"({len(snapshot['values'])} réglages, aucune valeur secrète)"
            )
        else:
            self.stdout.write(payload)

    def _values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for definition in registry.all_definitions():
            if not hasattr(settings, definition.name):
                # Non projeté : l'instantané le dit plutôt que de l'omettre. Une
                # absence des DEUX côtés est une parité ; une absence d'un seul
                # côté est précisément ce qu'on cherche à voir.
                out[definition.name] = None
                continue
            value = getattr(settings, definition.name)
            if definition.secret:
                out[definition.name] = PRESENT if str(value or "").strip() else ABSENT
                continue
            out[definition.name] = value
        return out

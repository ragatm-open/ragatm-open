"""Fixtures des tests de configuration.

Charger les settings dans un processus à environnement VIDE est la seule façon
d'observer les VRAIS défauts du code : dans le processus de test, `.env` est
chargé et masquerait toute divergence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"

_DUMP_SCRIPT = """
import json, django
from django.conf import settings

django.setup()
payload = dict()
for name in json.loads(__NAMES__):
    if hasattr(settings, name):
        value = getattr(settings, name)
        if isinstance(value, (str, int, float, bool, list, type(None))):
            payload[name] = value
        else:
            payload[name] = str(value)
print("<<<JSON>>>" + json.dumps(payload))
"""


def load_settings_with_environment(overrides: dict[str, str]) -> dict:
    """Charge les settings dans un sous-processus à environnement contrôlé.

    Ne conserve que le strict nécessaire à l'exécution de Python et Django : tout
    le reste (dont `.env`, non chargé puisque DJANGO_ENV != dev) est écarté.
    """
    from config import registry

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(BACKEND),
        "DJANGO_SETTINGS_MODULE": "config.settings.dev",
        # `test` évite le chargement de .env par base.py tout en restant un
        # environnement non prod-like (pas de garde ALLOWED_HOSTS).
        "DJANGO_ENV": "test",
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    env.update(overrides)

    # `CACHES` n'est pas une variable d'environnement : c'est un setting DÉRIVÉ de
    # DJANGO_CACHE_URL. On le dumpe quand même pour vérifier que le choix du
    # backend de cache reste identique après consolidation.
    names = sorted(registry.names()) + ["CACHES", "DATA_UPLOAD_MAX_MEMORY_SIZE"]
    script = _DUMP_SCRIPT.replace("__NAMES__", repr(json.dumps(names)))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Chargement des settings en échec :\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    marker = "<<<JSON>>>"
    line = next(
        (l for l in completed.stdout.splitlines() if l.startswith(marker)), None
    )
    assert line is not None, completed.stdout
    return json.loads(line[len(marker):])


def try_load_settings_with_environment(
    overrides: dict[str, str],
) -> tuple[bool, str]:
    """Variante tolérante : retourne (succès, sortie d'erreur)."""
    try:
        load_settings_with_environment(overrides)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


@pytest.fixture(scope="session")
def settings_without_environment() -> dict:
    """Settings chargés SANS aucune variable RagATM dans l'environnement."""
    return load_settings_with_environment({})

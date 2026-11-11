"""Garde : la découverte pytest ne parcourt jamais les données runtime.

Incident du 11/09/2026 (le poste de développement) : `python -m pytest -n 8` lancé depuis la racine
SANS chemin a collecté tout le dépôt, dont `backend/media` (~373 000 fichiers,
~688 000 dossiers). Mesuré : 330 s et 1,76 Gio de RSS retenus par processus ; chaque
worker xdist refaisant la collecte, 8 workers ont saturé RAM et swap (OOM global).

Trois protections, verrouillées ici :

1. `testpaths = ["tests/tests_root"]` — racine par défaut d'une invocation sans chemin ;
2. `backend/media` dans `norecursedirs` — aucune descente dans ce dossier, même depuis
   un chemin large (`pytest .`) ou sans chemin depuis `backend/` ;
3. `PYTEST_WORKERS ?= 2` dans le Makefile, et `PYTEST_WORKERS=1` retire toujours `-n`.

La configuration est vérifiée telle qu'écrite, telle que chargée par la session en
cours, puis par son EFFET réel sur une réplique miniature du dépôt : les valeurs de
`pyproject.toml` y sont recopiées et pytest y est lancé en sous-processus. Le vrai
`backend/media` n'est jamais parcouru.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Liste par défaut de pytest 8 : poser `norecursedirs` la REMPLACE, elle doit donc
#: être recopiée — sinon `.git`, `.venv` ou `node_modules` redeviendraient parcourus.
PYTEST_DEFAULT_NORECURSEDIRS = (
    "*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}",
)


def _ini_options() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


# --------------------------------------------------------------------------- #
# Configuration écrite
# --------------------------------------------------------------------------- #
def test_testpaths_vise_uniquement_tests_root():
    assert _ini_options().get("testpaths") == ["tests/tests_root"], (
        "`testpaths` doit valoir exactement [\"tests/tests_root\"] : sans lui, "
        "`python -m pytest` à la racine parcourt tout le dépôt (incident OOM)."
    )


def test_norecursedirs_exclut_backend_media_et_garde_les_defauts():
    norecurse = _ini_options().get("norecursedirs", [])
    assert "backend/media" in norecurse, "exclusion `backend/media` retirée (incident OOM)"
    manquants = [p for p in PYTEST_DEFAULT_NORECURSEDIRS if p not in norecurse]
    assert not manquants, f"motifs par défaut de pytest perdus : {manquants}"


def test_la_session_courante_charge_ces_protections(pytestconfig):
    """La config lue plus haut est bien celle que pytest applique (fichier unique)."""
    assert pytestconfig.inipath == PYPROJECT, pytestconfig.inipath
    assert pytestconfig.getini("testpaths") == ["tests/tests_root"]
    assert "backend/media" in pytestconfig.getini("norecursedirs")


# --------------------------------------------------------------------------- #
# Effet réel, sur une réplique miniature
# --------------------------------------------------------------------------- #
_TRIVIAL = "def test_ok():\n    pass\n"


@pytest.fixture
def replique(tmp_path: Path) -> Path:
    """Dépôt miniature portant les valeurs RÉELLES de `testpaths`/`norecursedirs`."""
    opts = _ini_options()
    # Seules les clés présentes sont recopiées : une protection retirée du vrai
    # fichier disparaît aussi de la réplique, et l'échec montre la fuite elle-même.
    lignes = [f"{cle} = {json.dumps(opts[cle])}\n"
              for cle in ("testpaths", "norecursedirs") if cle in opts]
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n" + "".join(lignes), encoding="utf-8")
    fichiers = {
        "tests/tests_root/test_attendu.py",
        # Un dossier nommé `media` AILLEURS reste collecté : l'exclusion est ciblée.
        "tests/tests_root/media/test_media_homonyme.py",
        "autre/test_hors_testpaths.py",
        "backend/media/rag_docs/a/b/test_fuite.py",
        "backend/media/test_fuite_racine.py",
    }
    for rel in fichiers:
        chemin = tmp_path / rel
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(_TRIVIAL, encoding="utf-8")
    return tmp_path


def _collecte(cwd: Path, *args: str) -> set[str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_ADDOPTS", "DJANGO_SETTINGS_MODULE", "PYTEST_XDIST_WORKER")}
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:django", "-p", "no:xdist", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode in (0, 5), out.stdout + out.stderr   # 5 = rien collecté
    return {ligne.split("::", 1)[0] for ligne in out.stdout.splitlines() if "::" in ligne}


def test_sans_chemin_depuis_la_racine_seul_tests_root_est_collecte(replique):
    assert _collecte(replique) == {
        "tests/tests_root/test_attendu.py",
        "tests/tests_root/media/test_media_homonyme.py",
    }


def test_chemin_large_explicite_ne_descend_pas_dans_backend_media(replique):
    collectes = _collecte(replique, ".")
    assert not [c for c in collectes if c.startswith("backend/media")], collectes
    # La récursion fonctionne ailleurs : l'exclusion ne masque que `backend/media`.
    assert "autre/test_hors_testpaths.py" in collectes, collectes


def test_sans_chemin_depuis_backend_ne_descend_pas_dans_media(replique):
    """Hors racine, `testpaths` ne s'applique pas : seul `norecursedirs` protège."""
    collectes = _collecte(replique / "backend")
    assert not [c for c in collectes if "media" in c], collectes


# --------------------------------------------------------------------------- #
# Parallélisme du Makefile
# --------------------------------------------------------------------------- #
def _recette_standard(**env_extra: str) -> str:
    if shutil.which("make") is None:
        pytest.skip("`make` absent")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_WORKERS", "PYTEST_JUNIT_PATH", "MAKEFLAGS", "MAKELEVEL")}
    env.update(env_extra)
    out = subprocess.run(["make", "-n", "-s", "test-backend-root"], cwd=str(REPO_ROOT),
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_le_defaut_local_est_deux_workers():
    recette = _recette_standard()
    assert "-n 2 --dist worksteal" in recette, recette


def test_pytest_workers_1_retire_xdist():
    recette = _recette_standard(PYTEST_WORKERS="1")
    assert " -n " not in recette and "worksteal" not in recette, recette
    assert "../tests/tests_root" in recette, recette

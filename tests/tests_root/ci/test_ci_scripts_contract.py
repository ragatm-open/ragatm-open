"""Contrat des points d'entrée CI portables (`ci/*.sh`).

Ces tests sont **hermétiques** : aucun PostgreSQL, aucun Docker, aucun réseau,
aucune campagne réelle. Les commandes coûteuses (`python manage.py …`, `make`) sont
remplacées par des *shims* qui enregistrent leur `argv` et leur environnement, et
rendent un code de sortie pilotable.

Ils remplacent l'ancienne garde `test_workflow_present_and_invariants`, qui lisait
le TEXTE du fichier de workflow du benchmark et y exigeait des noms d'actions.
Cette lecture couplait la suite backend à un orchestrateur précis : toute
réécriture du workflow cassait le test censé la valider. Le contrat
porte désormais sur ce qui compte vraiment — les paramètres transmis à la commande
métier et les codes de sortie propagés — et non sur la mise en forme d'un fichier.

Ce que ces tests NE vérifient pas : la sémantique du benchmark elle-même. Elle est
couverte, sans shell, par `rag_core/test_benchmark_ci_baseline.py`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CI = _REPO / "ci"

SCRIPTS = (
    "lib.sh",
    "check-docs.sh",
    "check-config.sh",
    "test-backend-standard.sh",
    "test-frontend.sh",
    "benchmark-retrieval.sh",
    "test-tabular-heavy.sh",
    # Outils de maintenance de l'environnement reproductible : ils vivent sous
    # `ci/` et doivent donc respecter les mêmes invariants que les points
    # d'entrée.
    "lock-python.sh",
    "audit-build-context.sh",
)
# `lib.sh` est SOURCÉ, pas exécuté : il n'a pas à porter le bit exécutable.
EXECUTABLE_SCRIPTS = tuple(s for s in SCRIPTS if s != "lib.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash requis pour exercer les scripts CI"
)


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------
def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_python(tmp_path: Path) -> tuple[Path, Path]:
    """Shim d'interpréteur : journalise `argv`, écrit les fichiers demandés.

    Retourne (chemin du shim, chemin du journal). Le code de sortie de chaque
    commande de gestion se pilote par `FAKE_RC_<commande>`.
    """
    log = tmp_path / "argv.log"
    shim = _write_exec(
        tmp_path / "fake-python",
        r"""#!/usr/bin/env bash
# argv complet, une ligne par appel.
printf '%s\n' "$*" >> "$FAKE_LOG"

sub="$2"   # $1 = manage.py

# Produit les fichiers que la commande réelle produirait, pour que l'appelant
# puisse enchaîner (et pour vérifier les chemins d'artefacts documentés).
prev=""
for arg in "$@"; do
    case "$prev" in
        --canonical-out|--output|--markdown) printf '{}' > "$arg" ;;
    esac
    prev="$arg"
done

# `$2` n'est une sous-commande que pour manage.py ; pour un script Python
# appelé directement, ce peut être une option. On ne construit un nom de
# variable que s'il est valide.
case "$sub" in
    [A-Za-z_]*) var="FAKE_RC_${sub}" ;;
    *)          var="FAKE_RC_UNNAMED" ;;
esac
exit "${!var:-0}"
""",
    )
    return shim, log


@pytest.fixture
def fake_make(tmp_path: Path) -> tuple[Path, Path]:
    """Shim `make` placé en tête de PATH : journalise argv, cible et environnement."""
    log = tmp_path / "make.log"
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    _write_exec(
        bin_dir / "make",
        r"""#!/usr/bin/env bash
printf 'ARGV=%s\n' "$*" >> "$FAKE_MAKE_LOG"
printf 'TABULAR_VOLUME_FULL=%s\n' "${TABULAR_VOLUME_FULL-<unset>}" >> "$FAKE_MAKE_LOG"
printf 'RAG_RUN_EXTERNAL_TESTS=%s\n' "${RAG_RUN_EXTERNAL_TESTS-<unset>}" >> "$FAKE_MAKE_LOG"
printf 'PYTEST_JUNIT_PATH=%s\n' "${PYTEST_JUNIT_PATH-<unset>}" >> "$FAKE_MAKE_LOG"
printf 'PYTEST_WORKERS=%s\n' "${PYTEST_WORKERS-<unset>}" >> "$FAKE_MAKE_LOG"
exit "${FAKE_MAKE_RC:-0}"
""",
    )
    return bin_dir, log


def run_script(name: str, *args: str, env: dict | None = None, cwd: Path | None = None):
    """Exécute un script `ci/` dans un environnement neutralisé."""
    full = os.environ.copy()
    # Neutralise la configuration héritée du poste : les tests doivent décrire
    # eux-mêmes l'environnement qu'ils exercent.
    for key in list(full):
        if key.startswith(("RAGATM_", "FAKE_", "PYTEST_", "TABULAR_", "FULL_VOLUME")):
            del full[key]
    full.pop("RAG_RUN_EXTERNAL_TESTS", None)
    full.update(env or {})
    return subprocess.run(
        ["bash", str(_CI / name), *args],
        env=full,
        cwd=str(cwd or _REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )


def bench_env(fake_python, tmp_path: Path, **extra) -> dict:
    shim, log = fake_python
    env = {
        "RAGATM_PYTHON": str(shim),
        "FAKE_LOG": str(log),
        "RAGATM_CI_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
    }
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Existence, permissions, syntaxe
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exists(name):
    assert (_CI / name).is_file(), f"point d'entrée portable manquant : ci/{name}"


@pytest.mark.parametrize("name", EXECUTABLE_SCRIPTS)
def test_script_is_executable(name):
    mode = (_CI / name).stat().st_mode
    assert mode & stat.S_IXUSR, f"ci/{name} n'est pas exécutable"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_shell_syntax_is_valid(name):
    r = subprocess.run(
        ["bash", "-n", str(_CI / name)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"ci/{name} : syntaxe invalide\n{r.stderr}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_has_no_orchestrator_dependency(name):
    """Invariant d'ARCHITECTURE : `ci/` ne connaît aucun orchestrateur.

    C'est une assertion d'ABSENCE — elle ne dépend d'aucune mise en forme et ne
    reproduit donc pas le défaut de l'ancienne lecture de YAML. Un script qui
    lirait le résumé de job d'un orchestrateur, ou un chemin de son répertoire de
    configuration, ne serait plus portable.
    """
    txt = (_CI / name).read_text(encoding="utf-8")
    # Les commentaires nomment volontairement les orchestrateurs pour expliquer la
    # frontière ; seul le CODE est inspecté.
    code = "\n".join(
        line for line in txt.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    for forbidden in ("github", "forgejo", "woodpecker"):
        assert forbidden not in code, f"ci/{name} dépend de l'orchestrateur : {forbidden}"


# ---------------------------------------------------------------------------
# Contrat du benchmark retrieval
# ---------------------------------------------------------------------------
def test_benchmark_runs_the_five_management_commands_in_order(fake_python, tmp_path):
    _, log = fake_python
    r = run_script("benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path))
    assert r.returncode == 0, r.stderr

    calls = log.read_text(encoding="utf-8").splitlines()
    subcommands = [c.split()[1] for c in calls]
    assert subcommands == [
        "check",
        "migrate",
        "seed_demo_benchmark",
        "rag_benchmark",
        "rag_benchmark_compare",
    ]


def test_benchmark_forces_the_deterministic_profile(fake_python, tmp_path):
    _, log = fake_python
    run_script("benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path))
    run = next(c for c in log.read_text(encoding="utf-8").splitlines()
               if c.split()[1] == "rag_benchmark")

    assert "--embeddings-provider stub" in run, "profil non déterministe"
    assert "--fail-under" in run, "garde CI absente : un seuil violé passerait"
    assert "--format json" in run
    assert "--canonical-out" in run


def test_benchmark_never_weakens_validation_nor_calls_a_model(fake_python, tmp_path):
    """`--skip-validation` désactiverait la cohérence documentaire ;
    `--with-answers` déclencherait une génération LLM. Ni l'un ni l'autre."""
    _, log = fake_python
    run_script("benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path))
    calls = log.read_text(encoding="utf-8")

    assert "--skip-validation" not in calls
    assert "--with-answers" not in calls
    assert "--allow-unresolved-diagnostic" not in calls


def test_benchmark_decision_k_matches_the_domain_default(fake_python, tmp_path):
    """Le seuil du script est lié à la constante du domaine, pas à un littéral libre."""
    from apps.rag_core.benchmark import compare as cmp

    _, log = fake_python
    run_script("benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path))
    compare = next(c for c in log.read_text(encoding="utf-8").splitlines()
                   if c.split()[1] == "rag_benchmark_compare")

    assert f"--decision-k {cmp.DEFAULT_DECISION_K}" in compare
    assert cmp.DEFAULT_DECISION_K == 5


@pytest.mark.parametrize("code", [0, 1, 2])
def test_benchmark_propagates_the_comparison_exit_code(fake_python, tmp_path, code):
    """0 = conforme, 1 = régression, 2 = incompatible. Jamais neutralisés."""
    r = run_script(
        "benchmark-retrieval.sh",
        env=bench_env(fake_python, tmp_path, FAKE_RC_rag_benchmark_compare=str(code)),
    )
    assert r.returncode == code


def test_benchmark_writes_the_documented_artifacts(fake_python, tmp_path):
    artifacts = tmp_path / "artifacts"
    r = run_script("benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path))
    assert r.returncode == 0

    for produced in (
        "demo-fr-candidate.json",
        "demo-fr-comparison.json",
        "demo-fr-comparison.md",
    ):
        assert (artifacts / produced).is_file(), f"artefact manquant : {produced}"
    assert (artifacts / "bench").is_dir()


def test_benchmark_failure_stops_before_the_comparison(fake_python, tmp_path):
    """Un run candidat en échec ne doit pas être comparé : on ne compare pas un
    rapport qu'on sait faux. Le code du run candidat est propagé."""
    _, log = fake_python
    r = run_script(
        "benchmark-retrieval.sh",
        env=bench_env(fake_python, tmp_path, FAKE_RC_rag_benchmark="1"),
    )
    assert r.returncode == 1
    assert "rag_benchmark_compare" not in log.read_text(encoding="utf-8")


def test_benchmark_runs_from_any_working_directory(fake_python, tmp_path):
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    r = run_script(
        "benchmark-retrieval.sh", env=bench_env(fake_python, tmp_path), cwd=elsewhere
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Contrat de la suite backend standard
# ---------------------------------------------------------------------------
def test_backend_standard_delegates_to_make_without_enabling_external_tests(fake_make):
    bin_dir, log = fake_make
    r = run_script(
        "test-backend-standard.sh",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_MAKE_LOG": str(log),
        },
    )
    assert r.returncode == 0, r.stderr

    recorded = log.read_text(encoding="utf-8")
    assert "ARGV=test-backend-all" in recorded
    # La famille `external` doit rester inerte : ce point d'entrée ne l'allume jamais.
    assert "RAG_RUN_EXTERNAL_TESTS=<unset>" in recorded


def test_backend_standard_forwards_the_ci_settings_to_make(fake_make):
    """`PYTEST_WORKERS` et `PYTEST_JUNIT_PATH` restent décidés par l'appelant.

    Le script ne les fixe pas et ne les filtre pas : c'est le Makefile qui les
    lit. Cette garde interdit qu'un point d'entrée s'arroge le parallélisme ou
    fasse disparaître le rapport JUnit de la CI.
    """
    bin_dir, log = fake_make
    r = run_script(
        "test-backend-standard.sh",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_MAKE_LOG": str(log),
            "PYTEST_WORKERS": "2",
            "PYTEST_JUNIT_PATH": "artifacts/pytest-standard.xml",
        },
    )
    assert r.returncode == 0, r.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "PYTEST_WORKERS=2" in recorded
    assert "PYTEST_JUNIT_PATH=artifacts/pytest-standard.xml" in recorded


def test_backend_standard_propagates_the_pytest_exit_code(fake_make):
    bin_dir, log = fake_make
    r = run_script(
        "test-backend-standard.sh",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_MAKE_LOG": str(log),
            "FAKE_MAKE_RC": "1",
        },
    )
    assert r.returncode == 1


# ---------------------------------------------------------------------------
# Contrat de la campagne lourde
# ---------------------------------------------------------------------------
def test_heavy_defaults_to_partial_volume_and_launches_nothing_in_dry_run(fake_make):
    bin_dir, log = fake_make
    r = run_script(
        "test-tabular-heavy.sh",
        "--dry-run",
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_MAKE_LOG": str(log)},
    )
    assert r.returncode == 0
    assert "TABULAR_VOLUME_FULL = 0" in r.stdout
    assert not log.exists(), "la campagne a été lancée malgré --dry-run"


def test_heavy_full_volume_requires_an_explicit_flag(fake_make):
    bin_dir, log = fake_make
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_MAKE_LOG": str(log)}
    r = run_script("test-tabular-heavy.sh", "--full-volume", "--dry-run", env=env)
    assert "TABULAR_VOLUME_FULL = 1" in r.stdout


def test_heavy_passes_the_volume_scope_to_make(fake_make):
    bin_dir, log = fake_make
    r = run_script(
        "test-tabular-heavy.sh",
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_MAKE_LOG": str(log)},
    )
    assert r.returncode == 0
    recorded = log.read_text(encoding="utf-8")
    assert "ARGV=test-backend-heavy" in recorded
    assert "TABULAR_VOLUME_FULL=0" in recorded


@pytest.mark.parametrize(
    "args,env",
    [
        (("--bogus",), {}),
        ((), {"FULL_VOLUME": "peut-etre"}),
    ],
)
def test_heavy_refuses_invalid_input_rather_than_guessing(fake_make, args, env):
    """Une campagne de plusieurs heures ne part pas sur une valeur interprétée."""
    bin_dir, log = fake_make
    full = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_MAKE_LOG": str(log)}
    full.update(env)
    r = run_script("test-tabular-heavy.sh", *args, env=full)
    assert r.returncode == 64, r.stdout
    assert not log.exists(), "la campagne a été lancée malgré une entrée invalide"


# ---------------------------------------------------------------------------
# Contrat du contrôle documentaire
# ---------------------------------------------------------------------------
def test_check_docs_delegates_to_the_repository_checker(fake_python, tmp_path):
    shim, log = fake_python
    elsewhere = tmp_path / "ailleurs"
    elsewhere.mkdir()
    r = run_script(
        "check-docs.sh",
        "--strict",
        env={"RAGATM_PYTHON": str(shim), "FAKE_LOG": str(log)},
        cwd=elsewhere,
    )
    assert r.returncode == 0, r.stderr
    call = log.read_text(encoding="utf-8").strip()
    assert call.startswith("scripts/check_docs.py")
    assert "--strict" in call, "les options ne sont pas transmises au contrôleur"


def test_check_docs_propagates_the_checker_exit_code(tmp_path):
    shim = _write_exec(
        tmp_path / "failing-python", "#!/usr/bin/env bash\nexit 1\n"
    )
    r = run_script("check-docs.sh", env={"RAGATM_PYTHON": str(shim)})
    assert r.returncode == 1

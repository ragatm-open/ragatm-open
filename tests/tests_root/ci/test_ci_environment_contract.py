"""Contrat de l'ENVIRONNEMENT CI reproductible (`ci/requirements-ci.lock`,
`ci/Dockerfile.ci`).

Ces tests sont **statiques et hermétiques** : aucun Docker, aucun réseau, aucune
construction d'image. Ils vérifient les invariants que la reproductibilité exige
et qu'une relecture humaine laisserait passer — une base non épinglée, une
empreinte manquante, un `torch` réintroduit par mégarde.

Ce qu'ils NE font pas : reconstruire l'image, ni vérifier que le lock est à jour
vis-à-vis de `backend/pyproject.toml`. Cette dérive se détecte par
`ci/lock-python.sh --check`, qui exige Docker et vit donc dans le pipeline, pas
dans la suite standard.

Ils ne lisent aucun fichier de workflow d'orchestrateur : le couplage supprimé
avec le contrat portable ne doit pas être réintroduit par une autre porte.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
LOCK = _REPO / "ci" / "requirements-ci.lock"
DOCKERFILE = _REPO / "ci" / "Dockerfile.ci"
LOCK_SCRIPT = _REPO / "ci" / "lock-python.sh"
DOCKERIGNORE = _REPO / "ci" / ".dockerignore"
AUDIT_SCRIPT = _REPO / "ci" / "audit-build-context.sh"

# Le contexte de construction est `ci/`, et le seul fichier qu'il doit porter.
BUILD_CONTEXT = "ci"
CONTEXT_ALLOWLIST = ("requirements-ci.lock",)

# Cible unique et assumée : le lock et l'image sont qualifiés pour Python 3.12
# sur linux/amd64. Toute autre cible exigerait d'autres empreintes.
EXPECTED_PYTHON = "3.12"
EXPECTED_NODE_MAJOR = "20"

# Extras couverts par le lock, et celui qui doit en rester dehors.
INCLUDED_EXTRAS = ("dev", "ocr", "tables", "ifc")
EXCLUDED_EXTRA = "rerank"

# Plancher glibc de la wheel `ifcopenshell` ; la cible générique n'y suffit pas.
EXPECTED_PLATFORM = "x86_64-manylinux_2_31"

_PINNED = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)")


def _lock_text() -> str:
    return LOCK.read_text(encoding="utf-8")


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _dockerfile_code() -> str:
    """Le Dockerfile SANS ses commentaires.

    Les commentaires y expliquent volontairement ce qui est proscrit (`latest`,
    `curl | sh`, publication). Les inspecter reviendrait à interdire d'expliquer
    la règle qu'on applique.
    """
    return "\n".join(
        line for line in _dockerfile_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _locked_distributions() -> list[tuple[str, str, int]]:
    """(nom, version, nombre d'empreintes) pour chaque distribution du lock."""
    out: list[list] = []
    for line in _lock_text().splitlines():
        m = _PINNED.match(line)
        if m:
            out.append([m.group(1), m.group(2), 0])
        elif out and "--hash=sha256:" in line:
            out[-1][2] += line.count("--hash=sha256:")
    return [tuple(x) for x in out]


# ---------------------------------------------------------------------------
# Existence et régénérabilité
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [LOCK, DOCKERFILE, LOCK_SCRIPT])
def test_environment_artifact_exists(path):
    assert path.is_file(), f"fichier d'environnement CI manquant : {path}"


def test_lock_header_names_the_regeneration_command():
    """Un lock qu'on ne sait pas régénérer n'est pas reproductible."""
    header = "\n".join(_lock_text().splitlines()[:5])
    assert "ci/lock-python.sh" in header, (
        "l'en-tête du lock doit nommer sa commande de régénération"
    )


def test_lock_script_pins_the_resolution_target():
    """Les options de résolution vivent dans le script, pas dans une mémoire."""
    txt = LOCK_SCRIPT.read_text(encoding="utf-8")
    assert f'PYTHON_VERSION="{EXPECTED_PYTHON}"' in txt
    assert f'PYTHON_PLATFORM="{EXPECTED_PLATFORM}"' in txt
    assert "--generate-hashes" in txt
    for extra in INCLUDED_EXTRAS:
        assert f"--extra {extra}" in txt, f"extra {extra} absent de la résolution"
    assert f"--extra {EXCLUDED_EXTRA}" not in txt, (
        f"l'extra {EXCLUDED_EXTRA} ne doit jamais entrer dans le lock"
    )


def test_lock_script_bounds_resolution_in_time():
    """Sans borne fixe, `--check` dérive dès que PyPI publie une version."""
    txt = LOCK_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^EXCLUDE_NEWER="([^"]*)"$', txt, re.MULTILINE)
    assert m, "EXCLUDE_NEWER doit être une constante du script"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", m.group(1)), (
        f"EXCLUDE_NEWER doit être un horodatage UTC littéral, pas {m.group(1)!r}"
    )
    assert '--exclude-newer "${EXCLUDE_NEWER}"' in txt


# ---------------------------------------------------------------------------
# Empreintes : la propriété centrale du lock
# ---------------------------------------------------------------------------
def test_lock_is_not_empty():
    assert len(_locked_distributions()) > 50, "lock anormalement court"


def test_every_locked_distribution_has_at_least_one_hash():
    missing = [n for n, _v, h in _locked_distributions() if h == 0]
    assert not missing, f"distributions sans empreinte : {missing}"


def test_every_locked_version_is_exact():
    """`==` partout : aucune plage ne doit subsister dans un lock."""
    loose = [
        n for n, v, _h in _locked_distributions()
        if not re.fullmatch(r"[0-9][0-9A-Za-z.\-+!]*", v)
    ]
    assert not loose, f"versions non figées : {loose}"


def test_lock_declares_no_private_index_nor_credential():
    txt = _lock_text()
    for forbidden in ("--index-url", "--extra-index-url", "--find-links", "@git+", "://"):
        assert forbidden not in txt, f"le lock référence une source externe : {forbidden}"


# ---------------------------------------------------------------------------
# Périmètre : l'extra `rerank` reste dehors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("banned", ["torch", "sentence-transformers", "nvidia", "triton"])
def test_heavy_inference_stack_is_absent_from_the_lock(banned):
    """`rerank` tirerait torch et la pile CUDA : des gigaoctets inutiles à la CI."""
    present = [n for n, _v, _h in _locked_distributions() if banned in n.lower()]
    assert not present, f"paquet interdit dans le lock : {present}"


# ---------------------------------------------------------------------------
# Image : épinglage
# ---------------------------------------------------------------------------
def test_every_from_is_pinned_by_digest():
    froms = re.findall(r"^FROM\s+(\S+)", _dockerfile_code(), re.MULTILINE)
    assert froms, "aucune instruction FROM trouvée"
    # Un FROM peut référencer un ÉTAGE LOCAL (ex. `toolchain`) : sans registre
    # ni tag, il n'y a rien à épingler. Seules les références externes comptent.
    external = [f for f in froms if "/" in f or ":" in f]
    unpinned = [f for f in external if "@sha256:" not in f]
    assert not unpinned, f"FROM non épinglé par digest : {unpinned}"


def test_every_copy_from_image_is_pinned_by_digest():
    copies = re.findall(r"COPY\s+--from=(\S+)", _dockerfile_code())
    external = [c for c in copies if "/" in c or ":" in c]
    unpinned = [c for c in external if "@sha256:" not in c]
    assert not unpinned, f"COPY --from non épinglé par digest : {unpinned}"


def test_no_floating_tag_anywhere_in_the_image_definition():
    code = _dockerfile_code()
    assert ":latest" not in code, "tag flottant `latest` dans ci/Dockerfile.ci"
    assert "apt-get upgrade" not in code, "aucun `apt upgrade` global n'est autorisé"
    assert "apt-get dist-upgrade" not in code


def test_downloaded_archives_are_checksum_verified():
    """Toute archive téléchargée doit être vérifiée avant d'être ouverte."""
    code = _dockerfile_code()
    if "curl" in code and "http" in code:
        assert "sha256sum -c" in code, (
            "un téléchargement sans vérification d'empreinte n'est pas reproductible"
        )
    assert not re.search(r"curl[^\n|]*\|\s*(ba)?sh", code), (
        "un script distant exécuté directement est proscrit"
    )


def test_image_declares_the_expected_language_versions():
    code = _dockerfile_code()
    assert f"python:{EXPECTED_PYTHON}-slim@sha256:" in code, (
        f"l'image doit partir de python:{EXPECTED_PYTHON}-slim épinglée"
    )
    node = re.search(r"ARG NODE_VERSION=(\S+)", code)
    assert node, "NODE_VERSION doit être un ARG explicite"
    assert node.group(1).startswith(f"{EXPECTED_NODE_MAJOR}."), (
        f"parité Node {EXPECTED_NODE_MAJOR} attendue, trouvé {node.group(1)}"
    )
    sha = re.search(r"ARG NODE_SHA256=([0-9a-f]{64})", code)
    assert sha, "NODE_SHA256 doit être une empreinte SHA-256 complète"


def test_image_installs_python_dependencies_with_mandatory_hashes():
    code = _dockerfile_code()
    assert "--require-hashes" in code, (
        "l'installation Python doit refuser toute distribution sans empreinte"
    )
    # Le chemin est relatif au contexte `ci/` — d'où `requirements-ci.lock` et
    # non `ci/requirements-ci.lock` : voir test_the_dockerfile_copies_only_from
    # _the_bounded_context.
    assert "requirements-ci.lock" in code, "l'image doit installer depuis le lock"


# ---------------------------------------------------------------------------
# Non-publication : le lot définit l'image, il ne la publie pas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    ["docker push", "docker login", "buildx build --push", "--push", "type=gha",
     "docker save", "docker export"],
)
def test_the_image_definition_publishes_nothing(forbidden):
    assert forbidden not in _dockerfile_code(), (
        f"opération de publication dans ci/Dockerfile.ci : {forbidden}"
    )


def test_the_lock_script_publishes_nothing():
    code = "\n".join(
        l for l in LOCK_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not l.lstrip().startswith("#")
    )
    for banned in ("docker push", "docker login", "--push", "type=gha"):
        assert banned not in code, f"publication dans ci/lock-python.sh : {banned}"


# ---------------------------------------------------------------------------
# Portabilité : aucun chemin de machine, aucun orchestrateur
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [DOCKERFILE, LOCK_SCRIPT, LOCK])
def test_no_machine_specific_path(path):
    txt = path.read_text(encoding="utf-8")
    for marker in ("/home/", "le serveur applicatif", "le serveur IA", "le poste de développement"):
        assert marker not in txt, f"chemin propre à une machine dans {path.name} : {marker}"


@pytest.mark.parametrize("path", [DOCKERFILE, LOCK_SCRIPT])
def test_no_orchestrator_dependency(path):
    code = "\n".join(
        l for l in path.read_text(encoding="utf-8").splitlines()
        if not l.lstrip().startswith("#")
    ).lower()
    for forbidden in ("github", "forgejo", "woodpecker"):
        assert forbidden not in code, (
            f"{path.name} dépend d'un orchestrateur : {forbidden}"
        )


# ---------------------------------------------------------------------------
# Contexte de construction : ce qui NE DOIT PAS pouvoir y entrer
#
# Ces tests sont statiques et portables. La vérification FONCTIONNELLE — déposer
# des sentinelles et prouver qu'aucune ne franchit le contexte — exige un démon
# Docker et vit donc dans `ci/audit-build-context.sh`, appelé par le pipeline.
#
# Aucun de ces tests ne lit un fichier de workflow : le découplage obtenu au lot
# précédent ne doit pas être réintroduit par une autre porte.
# ---------------------------------------------------------------------------
def test_the_build_context_is_bounded_by_a_dockerignore():
    assert DOCKERIGNORE.is_file(), (
        "ci/.dockerignore absent : le contexte de construction ne serait plus borné"
    )


def test_the_dockerignore_is_an_allowlist_not_a_blocklist():
    """`*` d'abord : rien n'entre sauf ce qui est explicitement réadmis.

    Une liste noire énumère ce qu'on sait devoir exclure ; elle laisse passer le
    prochain fichier auquel personne n'aura pensé.
    """
    lines = [
        l.strip() for l in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.lstrip().startswith("#")
    ]
    assert lines, "ci/.dockerignore ne contient aucune règle"
    assert lines[0] == "*", (
        f"la première règle doit tout exclure, trouvé : {lines[0]!r}"
    )
    readmitted = sorted(l[1:] for l in lines if l.startswith("!"))
    assert readmitted == sorted(CONTEXT_ALLOWLIST), (
        f"seul {CONTEXT_ALLOWLIST} doit être réadmis, trouvé : {readmitted}"
    )


def test_the_dockerfile_copies_only_from_the_bounded_context():
    """Les chemins des `COPY` sont relatifs au contexte `ci/`.

    Un `COPY ci/...` trahirait une construction depuis la racine du dépôt.
    """
    code = _dockerfile_code()
    copies = [
        c.split()[-2:] for c in re.findall(r"^COPY\s+(?!--from=)(.+)$", code, re.MULTILINE)
    ]
    sources = [parts[0] for parts in copies]
    assert sources, "aucun COPY depuis le contexte"
    for src in sources:
        assert not src.startswith(f"{BUILD_CONTEXT}/"), (
            f"COPY {src} : chemin relatif à la RACINE, pas au contexte {BUILD_CONTEXT}/"
        )
        assert src.lstrip("./") in CONTEXT_ALLOWLIST, (
            f"COPY {src} : ce fichier n'est pas réadmis par ci/.dockerignore"
        )


def test_every_documented_build_uses_the_bounded_context():
    """Aucune commande versionnée ne doit construire depuis la racine.

    Seuls les fichiers de `ci/` sont inspectés : les adaptateurs d'orchestrateur
    relèvent de la revue, pas de la suite de tests.
    """
    offenders = []
    for path in sorted((_REPO / "ci").iterdir()):
        if not path.is_file() or path.name == ".dockerignore":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("#") or "docker build" not in line:
                continue
            if re.search(r"docker build .*(\s\.\s*$|\s\.\s)", line):
                offenders.append(f"{path.name}:{n}")
    assert not offenders, (
        f"construction depuis la racine du dépôt : {offenders}"
    )


def test_the_negative_context_audit_exists_and_reads_no_real_secret():
    """Le test négatif ne doit jamais copier un `.env` réel, même temporairement."""
    assert AUDIT_SCRIPT.is_file()
    code = "\n".join(
        l for l in AUDIT_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not l.lstrip().startswith("#")
    )
    assert "SENTINEL" in code.upper(), "l'audit doit s'appuyer sur des sentinelles"
    for forbidden in ('cp "${CI_REPO_ROOT}/.env', "cat ${CI_REPO_ROOT}/.env", '/.env"', "/.env'"):
        assert forbidden not in code, (
            f"l'audit lit ou copie un .env réel : {forbidden}"
        )

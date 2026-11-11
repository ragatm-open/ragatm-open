#!/usr/bin/env bash
# ci/lock-python.sh — régénère (ou vérifie) le lock Python de la CI.
#
#     ci/lock-python.sh            régénère ci/requirements-ci.lock
#     ci/lock-python.sh --check    vérifie qu'il est à jour, n'écrit rien
#
# POURQUOI PASSER PAR UN CONTENEUR. Le lock doit dépendre des seules sources
# versionnées : `backend/pyproject.toml`, la version d'`uv` et la cible Python.
# Le générer avec un `uv` installé sur le poste ferait dépendre le résultat de
# ce poste. On utilise donc l'étage `toolchain` de `ci/Dockerfile.ci`, où `uv`
# est épinglé par version ET digest — le même `uv` qui installera ensuite le
# lock dans l'image. Générateur et installateur ne peuvent pas diverger.
#
# PÉRIMÈTRE DU LOCK :
#   extras INCLUS  : dev, ocr, tables, ifc
#   extra  EXCLU   : rerank — il entraînerait `torch` et
#                    `sentence-transformers`, inutiles à la CI et très lourds
#   cible          : Python 3.12, x86_64-manylinux_2_31 — `ifcopenshell` n'est
#                    publié qu'en wheel manylinux_2_31 ; la cible générique
#                    x86_64-unknown-linux-gnu (glibc 2.17) ne la résout pas.
#                    L'image CI (glibc 2.41), le runner ubuntu-24.04 (2.39) et
#                    l'image runtime (2.36) satisfont tous ce plancher.
#
# POLITIQUE TEMPORELLE. Sans borne, `--check` rerésout contre l'état COURANT de
# PyPI : le lock « dérive » dès qu'une dépendance transitive publie une version,
# sans qu'aucun fichier du dépôt ait changé. `EXCLUDE_NEWER` fige l'univers de
# résolution. Règle : l'horodatage UTC du dernier commit qui a modifié
# `backend/pyproject.toml` ou `ci/requirements-ci.in`. Toute modification de
# ces fichiers doit donc avancer `EXCLUDE_NEWER` dans le même commit, puis
# régénérer le lock. Une date calculée à l'exécution réintroduirait la dérive.
#
# ENTRÉES : `backend/pyproject.toml` (dépendances d'exécution) ET
# `ci/requirements-ci.in` (backend de construction déclaré par le pyproject —
# `uv pip compile` ne le résout pas seul, et sans lui l'installation du paquet
# local exigerait un téléchargement à l'exécution).
#
# Le lock ne couvre QUE des distributions tierces. Le paquet local du dépôt n'a
# pas d'empreinte de distribution comparable : il s'installe séparément, avec
# `--no-deps --no-build-isolation`, sans jamais relâcher la vérification des
# empreintes des autres.
#
# Codes de sortie : 0 succès · 1 dérive détectée (--check) · 64 prérequis/usage.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

LOCK_REL="ci/requirements-ci.lock"
LOCK="${CI_REPO_ROOT}/${LOCK_REL}"
DOCKERFILE_REL="ci/Dockerfile.ci"
TOOLCHAIN_TAG="ragatm-ci-toolchain:lot2-local"

# Les options de résolution sont ici, et NULLE PART ailleurs : c'est ce qui
# rend la régénération reproductible par quiconque lit ce fichier.
PYTHON_VERSION="3.12"
PYTHON_PLATFORM="x86_64-manylinux_2_31"
EXTRAS=(--extra dev --extra ocr --extra tables --extra ifc)
# Horodatage (2026-11-09T13:00:46Z) : dernier changement des dépendances déclarées (plages DRF, pypdf,
# python-dotenv, Pillow — correctifs de vulnérabilités) ; précédent : 9f68a833 (2026-10-30T21:44:50Z)
# (django-otp, django-axes — lot AUTH-MFA-01).
EXCLUDE_NEWER="2026-11-09T13:00:46Z"

check_only=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)   check_only=1 ;;
        -h|--help) sed -n '2,12p' -- "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) ci_die "argument inconnu : $1 (voir --help)" ;;
    esac
    shift
done

ci_require_cmd docker
cd -- "${CI_REPO_ROOT}"
[ -f "${DOCKERFILE_REL}" ] || ci_die "introuvable : ${DOCKERFILE_REL}"

ci_step "Construction de l'étage toolchain (uv épinglé)"
# Contexte `ci/` (cf. ci/.dockerignore) : le démon ne voit pas la racine.
docker build --target toolchain --tag "${TOOLCHAIN_TAG}" --file "${DOCKERFILE_REL}" ci >/dev/null

workdir="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf -- '${workdir}'" EXIT

ci_step "Résolution (uv pip compile, empreintes obligatoires)"
docker run --rm \
    --user "$(id -u):$(id -g)" \
    --env HOME=/out \
    --env UV_CACHE_DIR=/out/.uv-cache \
    --volume "${CI_REPO_ROOT}:/w:ro" \
    --volume "${workdir}:/out" \
    --workdir /w \
    "${TOOLCHAIN_TAG}" \
    uv pip compile backend/pyproject.toml ci/requirements-ci.in \
        "${EXTRAS[@]}" \
        --generate-hashes \
        --python-version "${PYTHON_VERSION}" \
        --python-platform "${PYTHON_PLATFORM}" \
        --exclude-newer "${EXCLUDE_NEWER}" \
        --custom-compile-command "${LOCK_REL##*/} : régénérer avec  ci/lock-python.sh" \
        --output-file /out/lock

[ -s "${workdir}/lock" ] || ci_die "la résolution n'a produit aucun lock."

if [ "${check_only}" -eq 1 ]; then
    if [ ! -f "${LOCK}" ]; then
        printf 'ERREUR: %s absent — lancez ci/lock-python.sh\n' "${LOCK_REL}" >&2
        exit 1
    fi
    if diff -u "${LOCK}" "${workdir}/lock" > "${workdir}/diff"; then
        ci_info "${LOCK_REL} est à jour."
        exit 0
    fi
    printf 'ERREUR: %s a DÉRIVÉ de backend/pyproject.toml.\n' "${LOCK_REL}" >&2
    printf '       Régénérez-le avec : ci/lock-python.sh\n\n' >&2
    head -40 "${workdir}/diff" >&2
    exit 1
fi

cp -- "${workdir}/lock" "${LOCK}"
ci_info "écrit : ${LOCK_REL}  ($(wc -l < "${LOCK}") lignes)"

#!/usr/bin/env bash
# backend/lock-runtime.sh — régénère (ou vérifie) les locks d'exécution de l'image backend.
#
#     backend/lock-runtime.sh            régénère requirements.lock, requirements-ocr.lock, requirements-ifc.lock
#     backend/lock-runtime.sh --check    vérifie qu'ils sont à jour, n'écrit rien (code 1 si dérive)
#
# POURQUOI. Le Dockerfile installe les dépendances avec `pip install --require-hashes`
# depuis ces locks : versions ET empreintes figées, directes et transitives. Sans lock, une
# publication transitive (ex. kombu 5.7.0 incompatible avec redis-py < 6.1, constaté)
# casse l'image sans qu'aucun fichier du dépôt ait changé.
#
# COMMENT. Même méthode que `ci/lock-python.sh` : l'étage `toolchain` de `ci/Dockerfile.ci`
# (uv épinglé par version et digest), cible Python 3.12 / x86_64-manylinux_2_31, univers de
# résolution borné par EXCLUDE_NEWER (horodatage UTC du dernier changement de dépendances :
# avancer la date ET régénérer dans le même changement).
#
# ENTRÉES : backend/pyproject.toml (dépendances d'exécution + extras) et
# backend/requirements-runtime.in (serveur WSGI). SORTIES :
#   requirements.lock      cœur + extra [tables]            (image par défaut)
#   requirements-ocr.lock  cœur + [tables] + [ocr]          (WITH_OCR=1)
#   requirements-ifc.lock  cœur + [tables] + [ifc]          (WITH_IFC=1)
# L'extra [rerank] (torch) n'est jamais verrouillé ici : hors profil Compact.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
TOOLCHAIN_TAG="ragatm-ci-toolchain:lot2-local"
PYTHON_VERSION="3.12"; PYTHON_PLATFORM="x86_64-manylinux_2_31"
# 2026-11-09 : qualification interne (premier verrouillage de l'image backend).
EXCLUDE_NEWER="2026-11-09T12:57:00Z"
check_only=0; [[ "${1:-}" == "--check" ]] && check_only=1
command -v docker >/dev/null || { echo "docker requis" >&2; exit 64; }
docker build --target toolchain --tag "$TOOLCHAIN_TAG" --file ci/Dockerfile.ci ci >/dev/null
work="$(mktemp -d)"; trap 'rm -rf -- "$work"' EXIT
compile() { # $1 = fichier de sortie, reste = extras
  local out="$1"; shift; local args=(); for e in "$@"; do args+=(--extra "$e"); done
  docker run --rm --user "$(id -u):$(id -g)" --env HOME=/out --env UV_CACHE_DIR=/out/.uv-cache \
    --volume "$HERE:/w:ro" --volume "$work:/out" --workdir /w --entrypoint uv "$TOOLCHAIN_TAG" \
    pip compile backend/pyproject.toml backend/requirements-runtime.in "${args[@]}" \
      --generate-hashes --python-version "$PYTHON_VERSION" --python-platform "$PYTHON_PLATFORM" \
      --exclude-newer "$EXCLUDE_NEWER" \
      --custom-compile-command "backend/$out : régénérer avec  backend/lock-runtime.sh" \
      --output-file "/out/$out" >/dev/null
}
compile requirements.lock tables
compile requirements-ocr.lock tables ocr
compile requirements-ifc.lock tables ifc
rc=0
for f in requirements.lock requirements-ocr.lock requirements-ifc.lock; do
  if [[ $check_only -eq 1 ]]; then
    if diff -q "backend/$f" "$work/$f" >/dev/null; then echo "[lock] backend/$f à jour"; else echo "[lock] backend/$f a DÉRIVÉ — régénérer avec backend/lock-runtime.sh" >&2; diff "backend/$f" "$work/$f" | grep -E '^[<>] [A-Za-z0-9_.-]+==' | head -20 >&2; rc=1; fi
  else
    cp -- "$work/$f" "backend/$f"; echo "[lock] écrit backend/$f ($(grep -c '^[a-z]' "backend/$f") distributions)"
  fi
done
exit $rc

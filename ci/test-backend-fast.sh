#!/usr/bin/env bash
# ci/test-backend-fast.sh — profil FAST du backend : AUCUN service requis.
#
#     ci/test-backend-fast.sh
#
# Délègue à `make test-fast`, qui reste la SOURCE DE VÉRITÉ : compileall puis
# `pytest --no-db -m "not slow and not tabular_volume and not tabular_concurrency
# and not external"`. L'option `--no-db` (tests/tests_root/conftest.py) désélectionne
# tout test qui demande PostgreSQL : ce profil tourne sans base ni Redis, c'est sa
# raison d'être (première porte de CI, boucle de développement).
#
# Ce que ce script ne fait PAS : installer, démarrer un service, toucher au
# parallélisme, poser RAG_RUN_EXTERNAL_TESTS. Variables lues par le Makefile :
# PYTEST_WORKERS, PYTEST_JUNIT_PATH (vide = aucun fichier).
#
# Codes de sortie : celui de pytest ; 64 si prérequis manquant.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

ci_require_cmd make
cd -- "${CI_REPO_ROOT}"

ci_step "Backend — profil FAST (make test-fast, sans base de données)"
if [ -n "${PYTEST_JUNIT_PATH:-}" ]; then
    ci_info "rapport JUnit : ${PYTEST_JUNIT_PATH}"
fi

exec make test-fast

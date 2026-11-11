#!/usr/bin/env bash
# ci/test-backend-full.sh — profil FULL du backend : suite COMPLÈTE, aucun filtre.
#
#     ci/test-backend-full.sh
#
# Délègue à `make test-full` (= `make test-backend-full`) : compileall puis `pytest`
# sans filtre de marqueur. Inclut la campagne `tabular_volume` (longue) ; la famille
# `external` est collectée mais SAUTÉE sans RAG_RUN_EXTERNAL_TESTS=1, que ce script
# ne pose jamais. Voie de qualification exhaustive (release), pas de PR.
#
# Prérequis : dépendances backend installées, PostgreSQL et Redis joignables.
# Variables lues par le Makefile : PYTEST_WORKERS, PYTEST_JUNIT_PATH.
# Codes de sortie : celui de pytest ; 64 si prérequis manquant.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

ci_require_cmd make
cd -- "${CI_REPO_ROOT}"

ci_step "Backend — profil FULL (make test-full, aucun filtre)"
if [ -n "${PYTEST_JUNIT_PATH:-}" ]; then
    ci_info "rapport JUnit : ${PYTEST_JUNIT_PATH}"
fi

exec make test-full

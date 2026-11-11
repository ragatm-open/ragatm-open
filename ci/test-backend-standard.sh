#!/usr/bin/env bash
# ci/test-backend-standard.sh — suite backend STANDARD (la voie bloquante).
#
#     ci/test-backend-standard.sh
#
# Délègue à `make test-backend-all`, qui reste la SOURCE DE VÉRITÉ : compileall
# puis `pytest -m "not tabular_volume and not external"`. Le Makefile décide seul
# du nombre de workers (`PYTEST_WORKERS`, `--dist worksteal`) et du chemin JUnit
# (`PYTEST_JUNIT_PATH`) ; ce script n'en change aucun.
#
# Ce que ce script ne fait PAS, volontairement :
#   - il n'installe aucune dépendance (rôle de l'orchestrateur) ;
#   - il ne démarre ni PostgreSQL, ni conteneur, ni service ;
#   - il ne touche pas au parallélisme ;
#   - il ne définit JAMAIS `RAG_RUN_EXTERNAL_TESTS` : la famille `external` est de
#     toute façon exclue par le marqueur, et rien ici ne doit pouvoir la rallumer ;
#   - il ne rejoue aucune sous-suite déjà couverte.
#
# Variables lues par le Makefile (posées par l'appelant, pas par ce script) :
#   PYTEST_WORKERS      défaut 4 en local ; la CI pose 2
#   PYTEST_JUNIT_PATH   vide = aucun fichier écrit ; la CI pose
#                       artifacts/pytest-standard.xml (relatif à la racine)
#
# Le rapport JUnit est écrit par pytest lui-même : il existe donc AUSSI quand la
# suite échoue — c'est précisément le cas où on veut le lire.
#
# Prérequis : dépendances backend installées, PostgreSQL joignable via DB_*.
# Codes de sortie : celui de pytest, propagé à l'identique ; 64 si prérequis manquant.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

ci_require_cmd make
cd -- "${CI_REPO_ROOT}"

ci_step "Backend — suite standard (make test-backend-all)"
if [ -n "${PYTEST_JUNIT_PATH:-}" ]; then
    ci_info "rapport JUnit : ${PYTEST_JUNIT_PATH}"
fi

exec make test-backend-all

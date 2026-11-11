#!/usr/bin/env bash
# ci/test-frontend.sh — contrôles frontend, dans l'ordre actuel de `ci.yml`.
#
#     ci/test-frontend.sh
#
#   1. npm test          vitest (le hook `pretest` du package.json lie les
#                        dépendances de test — il reste déclenché par npm)
#   2. npx tsc --noEmit  typecheck
#   3. npm run build     `next build` : seul contrôle qui voit la validité des
#                        fichiers de route. Un export nommé dans un `page.tsx` y
#                        est resté six jours, rendant l'image inconstructible sans
#                        que ni vitest ni tsc ne le signalent.
#
# FRONTIÈRE ASSUMÉE — l'installation reste dans l'orchestrateur.
# `actions/setup-node`, le cache npm et `npm ci` demeurent dans le YAML : ils
# relèvent de la préparation d'ENVIRONNEMENT, que le Lot 2 remplacera par une image
# CI. Ce script ne porte que les contrôles MÉTIER. Il n'installe rien et échoue
# explicitement si les dépendances manquent, plutôt que de les télécharger.
#
# Codes de sortie : celui de la première étape en échec ; 64 si prérequis manquant.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

FRONTEND_DIR="${CI_REPO_ROOT}/frontend"
[ -d "${FRONTEND_DIR}" ] || ci_die "répertoire frontend introuvable : ${FRONTEND_DIR}"

ci_require_cmd npm npx
cd -- "${FRONTEND_DIR}"

[ -d node_modules ] || ci_die \
    "frontend/node_modules absent — lancez 'npm ci' dans frontend/ (ce script n'installe rien)."

# Valeur par défaut identique à celle que `ci.yml` posait sur l'étape de build.
export BACKEND_BASE_URL="${BACKEND_BASE_URL:-http://127.0.0.1:8000}"

ci_step "Frontend — vitest"
npm test

ci_step "Frontend — typecheck (tsc --noEmit)"
npx tsc --noEmit

ci_step "Frontend — build de production (next build)"
npm run build

ci_info "Contrôles frontend : OK."

#!/usr/bin/env bash
# ci/audit-build-context.sh — prouve que le contexte de construction de l'image
# CI n'emporte QUE le fichier attendu.
#
#     ci/audit-build-context.sh
#
# POURQUOI UN SCRIPT ET PAS UN TEST DE LA SUITE. La vérification exige un démon
# Docker : la placer dans la suite backend obligerait à la sauter partout où
# Docker manque, c'est-à-dire à ne rien garantir. Les invariants qui se
# vérifient SANS Docker (contexte `ci/`, forme du `.dockerignore`) sont eux dans
# tests/tests_root/ci/test_ci_environment_contract.py.
#
# MÉTHODE — test négatif, sans aucun secret réel.
# Des SENTINELLES factices portant des noms de fichiers sensibles sont déposées
# dans une COPIE du contexte, en dehors du dépôt. On construit une cible d'audit
# qui recopie tout le contexte, puis on liste ce qui est réellement arrivé.
# Aucun `.env` réel n'est lu, copié ni monté : les sentinelles ne contiennent
# qu'une chaîne inerte.
#
# Codes de sortie : 0 contexte propre · 1 fuite détectée · 64 prérequis/usage.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

CONTEXT_DIR="${CI_REPO_ROOT}/ci"
EXPECTED="requirements-ci.lock"

# Noms de sentinelles : ce qu'un poste de développement peut réellement porter.
SENTINELS=(
    ".env.lot2-sentinel"
    ".env"
    "id_rsa_lot2_sentinel"
    ".credentials_lot2_sentinel"
    ".netrc"
    "secret_lot2_sentinel.pem"
    "nested/node_modules/lot2-sentinel"
    "artifacts/lot2-sentinel"
    "__pycache__/lot2-sentinel.pyc"
)

ci_require_cmd docker
[ -f "${CONTEXT_DIR}/.dockerignore" ] || ci_die "ci/.dockerignore introuvable — le contexte ne serait pas borné."
[ -f "${CONTEXT_DIR}/${EXPECTED}" ]   || ci_die "ci/${EXPECTED} introuvable."

# L'image de base est LUE dans le Dockerfile : l'audit ne peut pas dériver de ce
# qui est réellement construit.
BASE="$(grep -oE '^FROM python:[^ ]+' "${CONTEXT_DIR}/Dockerfile.ci" | head -1 | cut -d' ' -f2)"
[ -n "${BASE}" ] || ci_die "image de base introuvable dans ci/Dockerfile.ci"

work="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf -- '${work}'" EXIT

ci_step "Préparation d'un contexte de test (copie hors dépôt)"
cp -a -- "${CONTEXT_DIR}/." "${work}/ctx/" 2>/dev/null || { mkdir -p "${work}/ctx"; cp -a -- "${CONTEXT_DIR}/." "${work}/ctx/"; }
for s in "${SENTINELS[@]}"; do
    mkdir -p -- "${work}/ctx/$(dirname -- "$s")"
    printf 'SENTINELLE-INERTE-LOT2\n' > "${work}/ctx/${s}"
done
ci_info "sentinelles déposées : ${#SENTINELS[@]}"

cat > "${work}/Audit.dockerfile" <<DOCKERFILE
FROM ${BASE}
COPY . /audit-context
DOCKERFILE

ci_step "Construction de la cible d'audit (contexte = copie de ci/)"
tag="ragatm-lot2-review-audit-$$"
docker build --quiet --file "${work}/Audit.dockerfile" \
    --tag "${tag}" --label io.ragatm.lot2-review=1 "${work}/ctx" >/dev/null

# shellcheck disable=SC2064
trap "docker image rm -f '${tag}' >/dev/null 2>&1 || true; rm -rf -- '${work}'" EXIT

ci_step "Inventaire de ce qui est RÉELLEMENT arrivé dans le contexte"
listing="$(docker run --rm "${tag}" find /audit-context -type f -printf '%P\n' | sort)"
printf '%s\n' "${listing}" | sed 's/^/     /'

rc=0
leaked=""
for s in "${SENTINELS[@]}"; do
    if printf '%s\n' "${listing}" | grep -qxF -- "$s"; then
        leaked="${leaked} ${s}"
        rc=1
    fi
done

if [ -n "${leaked}" ]; then
    printf '\nERREUR: sentinelle(s) présente(s) dans le contexte :%s\n' "${leaked}" >&2
    printf '       le contexte de construction n%st pas borné.\n' "'es" >&2
    exit 1
fi

if ! printf '%s\n' "${listing}" | grep -qxF -- "${EXPECTED}"; then
    printf '\nERREUR: %s ABSENT du contexte — la liste blanche est trop stricte,\n' "${EXPECTED}" >&2
    printf '       la construction de l%simage échouerait.\n' "'" >&2
    exit 1
fi

if [ "$(printf '%s\n' "${listing}" | grep -c .)" -ne 1 ]; then
    printf '\nERREUR: le contexte contient autre chose que %s.\n' "${EXPECTED}" >&2
    exit 1
fi

ci_info "aucune sentinelle n'a franchi le contexte ; seul ${EXPECTED} est présent."
exit "${rc}"

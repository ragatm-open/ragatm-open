#!/usr/bin/env bash
# ci/test-tabular-heavy.sh — campagne LOURDE du moteur tabulaire.
#
#     ci/test-tabular-heavy.sh [--full-volume | --no-full-volume] [--dry-run]
#
# MANUELLE EXCLUSIVEMENT — c'est une décision, pas un oubli. La campagne coûte
# ~21 min sans `--full-volume`, et plusieurs heures avec. Rien ici ne doit pouvoir
# la déclencher tout seul : aucun défaut ne vaut « volume complet », et l'appelant
# doit demander explicitement les tailles extrêmes.
#
# Délègue à `make test-backend-heavy` (`pytest -m "tabular_volume"`), qui reste la
# source de vérité de la sélection.
#
# Périmètre :
#   défaut            TABULAR_VOLUME_FULL=0 — les tailles 100k/500k/1M sont ignorées
#   --full-volume     TABULAR_VOLUME_FULL=1 — les ajoute (plusieurs heures)
#   --no-full-volume  force explicitement le défaut
#   --dry-run         affiche le périmètre retenu puis SORT — ne lance rien
#
# La variable d'environnement `FULL_VOLUME` est aussi acceptée (`0|1|true|false`),
# pour les orchestrateurs qui transmettent un booléen d'entrée. Un drapeau explicite
# l'emporte sur elle. Toute autre valeur est REFUSÉE plutôt qu'interprétée : une
# campagne de plusieurs heures ne doit pas partir sur une coquille.
#
# ARTEFACTS : aucun aujourd'hui. `make test-backend-heavy` n'écrit pas de rapport
# JUnit (contrairement à la voie standard, qui l'écrit via PYTEST_JUNIT_PATH). En
# ajouter un serait un changement de politique CI, hors périmètre de ce lot.
#
# Codes de sortie : celui de pytest, propagé à l'identique ; 64 si usage invalide
# ou prérequis manquant.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    sed -n '2,10p' -- "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- Périmètre depuis l'environnement, puis depuis les drapeaux ---------------
full_volume=0
if [ -n "${FULL_VOLUME:-}" ]; then
    case "$(printf '%s' "${FULL_VOLUME}" | tr '[:upper:]' '[:lower:]')" in
        1|true)  full_volume=1 ;;
        0|false) full_volume=0 ;;
        *) ci_die "FULL_VOLUME invalide : valeur attendue 0, 1, true ou false." ;;
    esac
fi

dry_run=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --full-volume)    full_volume=1 ;;
        --no-full-volume) full_volume=0 ;;
        --dry-run)        dry_run=1 ;;
        -h|--help)        usage; exit 0 ;;
        *) ci_die "argument inconnu : $1 (voir --help)" ;;
    esac
    shift
done

ci_require_cmd make
cd -- "${CI_REPO_ROOT}"

export TABULAR_VOLUME_FULL="${full_volume}"

ci_step "Campagne tabulaire lourde — périmètre retenu"
ci_info "full_volume         = ${full_volume}"
ci_info "TABULAR_VOLUME_FULL = ${TABULAR_VOLUME_FULL}"
if [ "${full_volume}" -eq 1 ]; then
    ci_info "ATTENTION : tailles 100k/500k/1M incluses — plusieurs heures."
fi

if [ "${dry_run}" -eq 1 ]; then
    ci_info "--dry-run : la campagne n'est PAS lancée."
    exit 0
fi

exec make test-backend-heavy

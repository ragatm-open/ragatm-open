#!/usr/bin/env bash
# ci/check-docs.sh — contrôle de cohérence de la documentation.
#
#     ci/check-docs.sh [options passées telles quelles à scripts/check_docs.py]
#
# Encapsule EXACTEMENT le contrôle documentaire actuel : `scripts/check_docs.py`
# reste la source de vérité, ce script ne fait que le rendre appelable de la même
# façon depuis un poste, depuis GitHub Actions et, plus tard, depuis un autre
# orchestrateur.
#
# Prérequis : un interpréteur Python. AUCUNE installation, AUCUN service, AUCUN
# réseau — le script vérifié n'importe que la bibliothèque standard.
#
# Codes de sortie : ceux de `scripts/check_docs.py`, propagés à l'identique
#   0  documentation cohérente
#   1  liens cassés (ou avertissements avec --strict)
#   2  répertoire de documentation introuvable
#  64  prérequis manquant (cf. ci/README.md)
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

cd -- "${CI_REPO_ROOT}"

CHECKER="scripts/check_docs.py"
[ -f "${CHECKER}" ] || ci_die "contrôleur documentaire introuvable : ${CHECKER}"

ci_step "Documentation — liens et cohérence (${CHECKER})"

# exec : le code de sortie du contrôleur devient celui de ce script, sans
# interprétation ni reformulation.
exec "$(ci_python)" "${CHECKER}" "$@"

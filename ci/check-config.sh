#!/usr/bin/env bash
# ci/check-config.sh — gardes de CONFIGURATION du backend, avant la suite de tests.
#
#     ci/check-config.sh
#
# Regroupe les trois contrôles que `ci.yml` exécutait en trois étapes distinctes.
# Ils sont rapides (quelques secondes), n'appellent aucun service externe et aucun
# modèle : ils échouent TÔT quand le registre de configuration, les `.env.example`
# ou la référence générée divergent du code.
#
#   1. manage.py check                          system checks Django (dont llm_router.*)
#   2. manage.py config_audit                   registre <-> code <-> .env.example
#   3. manage.py generate_config_reference      --check UNIQUEMENT
#
# L'étape 3 est toujours lancée en mode VÉRIFICATION : ce script ne régénère jamais
# `docs/10-reference/configuration.md`. La régénération reste un geste délibéré
# (`make config-reference`), parce qu'une CI qui réécrit la documentation qu'elle
# est censée contrôler ne contrôle plus rien.
#
# Prérequis : dépendances backend installées (l'orchestrateur s'en charge) et les
# variables de connexion habituelles (DB_HOST, DB_PORT, DB_NAME, DB_USER,
# DB_PASSWORD, DJANGO_SECRET_KEY). Ce script ne charge AUCUN `.env`.
#
# Codes de sortie : celui du PREMIER contrôle en échec ; 64 si un prérequis manque.
set -Eeuo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

BACKEND_DIR="${CI_REPO_ROOT}/backend"
[ -f "${BACKEND_DIR}/manage.py" ] || ci_die "manage.py introuvable sous ${BACKEND_DIR}"

PYTHON="$(ci_python)"
cd -- "${BACKEND_DIR}"

ci_step "System checks Django"
"${PYTHON}" manage.py check

ci_step "Audit de configuration (registre <-> code <-> .env.example)"
"${PYTHON}" manage.py config_audit

ci_step "Référence de configuration à jour (vérification seule)"
"${PYTHON}" manage.py generate_config_reference --check

ci_info "Gardes de configuration : OK."

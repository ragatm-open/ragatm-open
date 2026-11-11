#!/usr/bin/env bash
# Arrête la pile. `--purge` supprime AUSSI les volumes (base, documents, modèles) — irréversible.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
if [[ "${1:-}" == "--purge" ]]; then read -r -p "Supprimer TOUTES les données de ${COMPOSE_PROJECT_NAME:-ragatm-compact} ? (oui/NON) " a; [[ "$a" == "oui" ]] || exit 1; docker compose down -v --remove-orphans; else docker compose down --remove-orphans; fi

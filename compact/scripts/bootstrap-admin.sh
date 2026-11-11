#!/usr/bin/env bash
# Crée (idempotent) le premier superutilisateur et son espace documentaire initial.
# Variables : RAGATM_ADMIN_EMAIL (défaut admin@localhost), RAGATM_ADMIN_PASSWORD (OBLIGATOIRE,
# jamais affiché ni passé en argument), RAGATM_WORKSPACE (défaut "Compact"), RAGATM_RAGSPACE (défaut "Initial RAG").
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
: "${RAGATM_ADMIN_PASSWORD:?RAGATM_ADMIN_PASSWORD requis (variable d environnement)}"
EMAIL="${RAGATM_ADMIN_EMAIL:-admin@localhost}"; WS="${RAGATM_WORKSPACE:-Compact}"; RS="${RAGATM_RAGSPACE:-Initial RAG}"
case "${RAGATM_ADMIN_PASSWORD,,}" in admin|password|change-me|changeme|123456*|test*) echo "[bootstrap] mot de passe de démonstration refusé" >&2; exit 3;; esac
[[ ${#RAGATM_ADMIN_PASSWORD} -ge 12 ]] || { echo "[bootstrap] mot de passe trop court (12 caractères minimum)" >&2; exit 3; }
docker compose exec -T -e DJANGO_SUPERUSER_USERNAME="${EMAIL%@*}" -e DJANGO_SUPERUSER_EMAIL="$EMAIL" -e DJANGO_SUPERUSER_PASSWORD="$RAGATM_ADMIN_PASSWORD" backend python manage.py ensure_superuser
docker compose exec -T backend python manage.py bootstrap_workspace --email "$EMAIL" --workspace "$WS" --ragspace "$RS"
echo "[bootstrap] compte $EMAIL prêt ; connexion : http://localhost:$(grep -E '^COMPACT_FRONTEND_PORT=' .env | cut -d= -f2)/login (second facteur TOTP enrôlé à la première connexion)"

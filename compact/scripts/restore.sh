#!/usr/bin/env bash
# Restauration dans une instance ISOLÉE (projet compose distinct, volumes neufs, autre port).
# Usage : COMPOSE_PROJECT_NAME=ragatm-compact-restore COMPACT_FRONTEND_PORT=18081 scripts/restore.sh <dossier_sauvegarde>
# Prérequis : le `.env` de l'instance cible (gen-secrets.sh dans un répertoire de projet dédié, ou
# RESTORE_ENV_FILE pointant vers un .env propre à l'instance restaurée) ; les modèles doivent être
# approvisionnés dans l'instance cible (ou les volumes de modèles partagés en lecture).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
SRC="${1:?dossier de sauvegarde requis}"; ENVF="${RESTORE_ENV_FILE:-$HERE/.env}"
: "${COMPOSE_PROJECT_NAME:?COMPOSE_PROJECT_NAME requis (instance isolée)}"
( cd "$SRC" && sha256sum -c SHA256SUMS --quiet ) && echo "[restore] sommes de contrôle OK"
export COMPACT_ENV_FILE="$ENVF"   # secrets de l'instance restaurée pour backend/worker (compose.yml)
DC=(docker compose --env-file "$ENVF" -p "$COMPOSE_PROJECT_NAME")
set -a; . "$ENVF"; set +a
echo "[restore] démarrage db/redis de l'instance $COMPOSE_PROJECT_NAME"
"${DC[@]}" up -d db redis
for i in $(seq 1 30); do "${DC[@]}" exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1 && break; sleep 2; done
echo "[restore] base"
"${DC[@]}" exec -T db pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner --no-privileges < "$SRC/db.dump" || echo "[restore] pg_restore a signalé des avertissements (objets absents : normal sur base vierge)"
echo "[restore] documents"
"${DC[@]}" run --rm --no-deps --entrypoint sh backend -c "mkdir -p /data/media && cd /data/media && tar xzf -" < "$SRC/media.tar.gz"
echo "[restore] démarrage (${RESTORE_SERVICES:-pile complète})"
# RESTORE_SERVICES="db redis backend worker" : vérification sans réapprovisionner les modèles (--no-deps).
if [[ -n "${RESTORE_SERVICES:-}" ]]; then "${DC[@]}" up -d --no-deps ${RESTORE_SERVICES}; else "${DC[@]}" up -d; fi
echo "[restore] instance $COMPOSE_PROJECT_NAME démarrée ; vérifier comptes/espaces/documents puis réindexer les documents non READY (inventory.json de la sauvegarde)."
cat "$SRC/inventory.json"

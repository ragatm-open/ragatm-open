#!/usr/bin/env bash
# Construit et démarre la pile Compact (exécution NOMINALE : réseaux internes, aucune sortie).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
[[ -f .env ]] || { echo "[up] .env absent : scripts/gen-secrets.sh d'abord" >&2; exit 1; }
if [[ "${1:-}" == "--build" ]]; then docker compose build; fi
docker compose up -d
echo "[up] attente de la santé des services…"
for i in $(seq 1 60); do
  unhealthy=$(docker compose ps --format '{{.Name}} {{.Health}}' | awk '$2!="healthy" && $2!=""' | wc -l)
  [[ $unhealthy -eq 0 ]] && break; sleep 5
done
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

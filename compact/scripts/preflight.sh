#!/usr/bin/env bash
# Préflight d'installation Compact : outils, ressources, ports, secrets.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0; ok(){ echo "  [OK]   $*"; }; ko(){ echo "  [KO]   $*"; fail=1; }; warn(){ echo "  [WARN] $*"; }
echo "== Outils"
command -v docker >/dev/null && ok "docker $(docker --version | cut -d' ' -f3)" || ko "docker absent"
docker compose version >/dev/null 2>&1 && ok "docker compose $(docker compose version --short)" || ko "docker compose v2 absent"
docker info >/dev/null 2>&1 && ok "démon Docker joignable" || ko "démon Docker injoignable"
echo "== Ressources"
mem_avail_mb=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo); [[ $mem_avail_mb -ge 9000 ]] && ok "RAM disponible ${mem_avail_mb} Mo" || warn "RAM disponible ${mem_avail_mb} Mo (< 9 Go : TEI ~3,8 Go + Ollama ~2 Go + services ~2 Go)"
disk_avail_gb=$(df -BG --output=avail "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" | tail -1 | tr -dc '0-9'); [[ ${disk_avail_gb:-0} -ge 12 ]] && ok "disque Docker ${disk_avail_gb} Go" || warn "disque Docker ${disk_avail_gb} Go (images ~3 Go + modèles ~4 Go + données)"
nproc_n=$(nproc); [[ $nproc_n -ge 4 ]] && ok "${nproc_n} cœurs" || warn "${nproc_n} cœurs (génération CPU lente)"
if command -v nvidia-smi >/dev/null 2>&1; then warn "GPU NVIDIA détecté : profil 12 Go possible APRÈS qualification (compose.gpu-12gb.yml)"; else ok "aucun GPU : profil CPU compact-generation-8"; fi
echo "== Ports"
port="${COMPACT_FRONTEND_PORT:-$(grep -E '^COMPACT_FRONTEND_PORT=' "$HERE/.env" 2>/dev/null | cut -d= -f2)}"; port="${port:-18080}"
if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}$"; then ko "port 127.0.0.1:${port} déjà occupé"; else ok "port 127.0.0.1:${port} libre"; fi
echo "== Secrets"
if [[ -f "$HERE/.env" ]]; then
  perm=$(stat -c %a "$HERE/.env"); [[ "$perm" == "600" ]] && ok ".env présent (0600)" || ko ".env permissions $perm (attendu 600)"
  grep -q "__GENERATE__\|change-me" "$HERE/.env" && ko ".env contient des valeurs non générées" || ok ".env sans valeur de démonstration"
else warn ".env absent : exécuter scripts/gen-secrets.sh"; fi
echo "== Conteneurs du projet"
proj="$(grep -E '^COMPOSE_PROJECT_NAME=' "$HERE/.env" 2>/dev/null | cut -d= -f2)"; proj="${proj:-ragatm-compact}"
docker ps -a --filter "label=com.docker.compose.project=${proj}" --format '  {{.Names}}\t{{.Status}}' | sed 's/^/  /' || true
[[ $fail -eq 0 ]] && echo "PREFLIGHT=PASS" || { echo "PREFLIGHT=FAIL"; exit 1; }

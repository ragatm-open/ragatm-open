#!/usr/bin/env bash
set -Eeuo pipefail
# scripts/dev/stop_dev.sh
# -----------------------------------------------------------------------------
# Arrête proprement la stack DEV RAG-ATM lancée par up.sh / up_rebuild.sh :
#   - backend Django   (127.0.0.1:6811, legacy 0.0.0.0:8010)
#   - frontend Next.js (127.0.0.1:6300, legacy :3000 UNIQUEMENT si RagATM)
#   - Postgres dev      (docker compose dev — down sans -v, données conservées)
#
# Sûr et idempotent :
#   - ne tue JAMAIS un process tiers incertain (port 3000 peut être un autre
#     projet → on ne tue que si le process appartient clairement à ce dépôt) ;
#   - SIGTERM puis SIGKILL seulement si encore vivant ;
#   - réussit même si rien n'est lancé / compose déjà arrêté ;
#   - n'utilise pas sudo et ne supprime ni données, ni .env/.venv/node_modules.
#
# Détection (par ordre de préférence) :
#   1. fichiers PID écrits par up.sh/up_rebuild.sh (.dev-logs/{backend,frontend}.pid)
#   2. pgrep avec patterns stricts (commande + port dédié RagATM)
#   3. process à l'écoute du port dédié (6811/6300 réservés RagATM)
#   4. ports legacy (8010/3000) : tué seulement si appartenance au dépôt confirmée
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOG_DIR="${REPO_ROOT}/.dev-logs"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/dev/docker-compose.yml"

# Ports affichés dans le résumé final (alignés sur la politique dev RagATM).
PORTS=(6300 6811 65432 66379 6900 6901)

log()  { printf "\033[1;36m[stop-dev] %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m[stop-dev] %s\033[0m\n" "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

# ----------------------------------------------------------------- helpers ports
# PID(s) à l'écoute (LISTEN) d'un port, via ss (présent par défaut) puis lsof.
pids_on_port() {
  local port="$1"
  if have ss; then
    ss -ltnp "( sport = :${port} )" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true
  elif have lsof; then
    lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | sort -u || true
  fi
}

# Vrai si un port est occupé (LISTEN), sans rien tuer.
port_occupied() {
  local port="$1"
  if have ss; then
    ss -ltn "( sport = :${port} )" 2>/dev/null | tail -n +2 | grep -q .
  elif have lsof; then
    lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && { exec 3>&- || true; return 0; } || return 1
  fi
}

# Vrai si le process PID appartient clairement à CE dépôt (cmdline ou cwd).
pid_belongs_to_project() {
  local pid="$1" cmdline cwd
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
  [[ "${cmdline}" == *"${REPO_ROOT}"* ]] && return 0
  cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
  [[ -n "${cwd}" && "${cwd}" == "${REPO_ROOT}"* ]] && return 0
  return 1
}

# --------------------------------------------------------------- arrêt process
# SIGTERM, petite attente, puis SIGKILL uniquement si encore vivant.
term_then_kill() {
  local pid="$1" label="$2" i
  kill -0 "${pid}" 2>/dev/null || return 0
  log "SIGTERM → ${label} (PID ${pid})"
  kill -TERM "${pid}" 2>/dev/null || true
  for i in $(seq 1 15); do
    kill -0 "${pid}" 2>/dev/null || { log "  arrêté : ${label} (PID ${pid})"; return 0; }
    sleep 0.2
  done
  warn "encore vivant après SIGTERM → SIGKILL : ${label} (PID ${pid})"
  kill -KILL "${pid}" 2>/dev/null || true
}

# Émet des lignes "pid|label" pour tous les candidats SÛRS à arrêter.
gather_pids() {
  local uid; uid="$(id -u)"
  local pid f raw

  # 1) Fichiers PID écrits par up.sh / up_rebuild.sh
  for f in backend frontend; do
    [[ -f "${LOG_DIR}/${f}.pid" ]] || continue
    raw="$(tr -dc '0-9' < "${LOG_DIR}/${f}.pid" 2>/dev/null || true)"
    [[ -n "${raw}" ]] && kill -0 "${raw}" 2>/dev/null && printf '%s|%s (pidfile)\n' "${raw}" "${f}"
  done

  # 2) Backend Django sur port dédié 6811 (127.0.0.1 / localhost) → RagATM-sûr
  pgrep -u "${uid}" -f 'manage\.py runserver (127\.0\.0\.1|localhost):6811' 2>/dev/null \
    | while read -r pid; do printf '%s|backend runserver:6811\n' "${pid}"; done || true

  # 3) Listener du port dédié 6811 (réservé RagATM → sûr ; capte le worker autoreload)
  pids_on_port 6811 | while read -r pid; do
    [[ -n "${pid}" ]] && printf '%s|backend listener:6811\n' "${pid}"
  done || true

  # 4) Backend legacy 0.0.0.0:8010 — seulement si appartenance au dépôt confirmée
  pgrep -u "${uid}" -f 'manage\.py runserver 0\.0\.0\.0:8010' 2>/dev/null \
    | while read -r pid; do
        pid_belongs_to_project "${pid}" && printf '%s|backend legacy:8010\n' "${pid}"
      done || true

  # 5) Frontend Next.js sur port dédié 6300 (process appartenant au dépôt)
  pgrep -u "${uid}" -f 'next.*(-p 6300|:6300)' 2>/dev/null \
    | while read -r pid; do
        pid_belongs_to_project "${pid}" && printf '%s|frontend next:6300\n' "${pid}"
      done || true

  # 6) Listener du port dédié 6300 (réservé RagATM → sûr ; capte le worker next-server)
  pids_on_port 6300 | while read -r pid; do
    [[ -n "${pid}" ]] && printf '%s|frontend listener:6300\n' "${pid}"
  done || true

  # 7) Frontend legacy :3000 — JAMAIS tué à l'aveugle. Uniquement si le process
  #    qui écoute appartient CLAIREMENT à ce dépôt (sinon = autre projet).
  pids_on_port 3000 | while read -r pid; do
    [[ -n "${pid}" ]] && pid_belongs_to_project "${pid}" \
      && printf '%s|frontend legacy:3000 (RagATM)\n' "${pid}"
  done || true
}

kill_matching_processes() {
  local self=" $$ ${PPID:-0} " seen=" " found=0 pid label
  while IFS='|' read -r pid label; do
    [[ "${pid:-}" =~ ^[0-9]+$ ]] || continue
    case "${self}" in *" ${pid} "*) continue ;; esac   # jamais le shell courant / parent
    case "${seen}" in *" ${pid} "*) continue ;; esac   # dédoublonnage
    seen="${seen}${pid} "
    found=1
    term_then_kill "${pid}" "${label}"
  done < <(gather_pids)

  if [[ "${found}" -eq 0 ]]; then
    log "Aucun process backend/frontend RagATM dev à arrêter."
  fi

  # :3000 occupé mais NON RagATM → on prévient sans jamais y toucher.
  if port_occupied 3000; then
    local owned=0 p
    while read -r p; do
      [[ -n "${p}" ]] && pid_belongs_to_project "${p}" && owned=1
    done < <(pids_on_port 3000)
    [[ "${owned}" -eq 0 ]] \
      && warn ":3000 occupé par un process NON RagATM → laissé intact (autre projet ?)."
  fi

  rm -f "${LOG_DIR}/backend.pid" "${LOG_DIR}/frontend.pid" 2>/dev/null || true
}

# ---------------------------------------------------------- arrêt docker compose
stop_docker_compose() {
  if ! have docker; then
    warn "docker introuvable — compose dev non géré (rien à faire)."
    return 0
  fi
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    warn "compose dev absent (${COMPOSE_FILE}) — ignoré."
    return 0
  fi
  log "Arrêt docker compose dev (down, sans -v → données Postgres conservées)…"
  # --remove-orphans : nettoie d'éventuels services optionnels retirés du compose.
  if docker compose -f "${COMPOSE_FILE}" down --remove-orphans 2>&1 \
       | sed 's/^/[stop-dev]   /'; then
    log "docker compose dev arrêté."
  else
    warn "docker compose down a renvoyé une erreur (peut-être déjà arrêté) — ignoré."
  fi
}

# ------------------------------------------------------------------ résumé ports
print_port_status() {
  local p any=0
  log "État des ports RagATM dev :"
  for p in "${PORTS[@]}"; do
    if port_occupied "${p}"; then
      any=1
      warn "  :${p} OCCUPÉ → diag : ss -ltnp | grep ':${p}'   ou   lsof -i :${p}"
    else
      log "  :${p} libre"
    fi
  done
  if [[ "${any}" -eq 0 ]]; then
    log "Tous les ports RagATM dev sont libres."
  else
    warn "Des ports RagATM dev restent occupés (voir ci-dessus)."
  fi
}

# ----------------------------------------------------------------------- main
main() {
  log "Racine projet : ${REPO_ROOT}"
  kill_matching_processes
  stop_docker_compose
  print_port_status
  log "Terminé."
}

main "$@"

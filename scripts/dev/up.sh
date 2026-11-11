#!/usr/bin/env bash
# scripts/dev/up.sh
# -----------------------------------------------------------------------------
# Lance toute la stack de DEV RAG-ATM en une commande :
#   - Postgres + pgvector (docker compose dev, loopback 127.0.0.1:${DEV_POSTGRES_PORT})
#   - Backend Django   sur ${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}   (def 127.0.0.1:6811)
#   - Frontend Next.js sur ${DEV_FRONTEND_HOST}:${DEV_FRONTEND_PORT} (def 127.0.0.1:6300)
#
# Politique ports dev RAGATM (évite les collisions multi-projets sur
# le poste de développement ; tout service dev écoute UNIQUEMENT en loopback) :
#   frontend  127.0.0.1:6300     API backend 127.0.0.1:6811
#   postgres  127.0.0.1:65432    redis dbg   127.0.0.1:63790
#   → on n'utilise JAMAIS les ports génériques 3000/5432/6379/8000/8010/11434
#     comme ports hôte partagés, et rien n'écoute en 0.0.0.0.
#
# Bootstrap automatique si manquant :
#   - venv .venv/ + deps backend (editable + extras dev)
#   - deps frontend (npm install)
#   - migrations Django (workaround bug migration rag_core.0011 dupliquée)
#   - user de test + workspace + ragspace si la DB est vide
#
# Ctrl-C arrête backend + frontend proprement. Postgres docker reste up.
# Pour l'arrêter aussi :
#   docker compose -f deploy/docker/dev/docker-compose.yml down
#
# Variables surchargeables (valeurs par défaut conformes à la politique) :
#   DEV_BACKEND_HOST   (def 127.0.0.1)   DEV_BACKEND_PORT   (def 6811)
#   DEV_FRONTEND_HOST  (def 127.0.0.1)   DEV_FRONTEND_PORT  (def 6300)
#   DEV_POSTGRES_HOST  (def 127.0.0.1)   DEV_POSTGRES_PORT  (def 65432)
#   DEV_REDIS_HOST     (def 127.0.0.1)   DEV_REDIS_PORT     (def 63790)
#   DEV_CELERY_CONCURRENCY (def 3)  — slots prefork du worker Celery. Volontairement
#     < nb de cœurs : l'ingestion est CPU-bound ET multi-thread par tâche (OCR
#     Tesseract + embeddings Ollama locaux). Trop de slots => sur-souscription CPU,
#     chaque extraction s'affame et dépasse le délai (faux "timeout"). Monter au
#     besoin sur une machine puissante / provider d'embeddings distant.
#   TEST_EMAIL    (def admin@example.com)
#   TEST_PASSWORD (def Test1234Test1234)
# -----------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
LOG_DIR="${REPO_ROOT}/.dev-logs"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/dev/docker-compose.yml"

# --- Ports dev (loopback only). Compat : anciens BACKEND_PORT/FRONTEND_PORT
#     restent acceptés comme surcharge, mais les défauts suivent la politique.
DEV_BACKEND_HOST="${DEV_BACKEND_HOST:-127.0.0.1}"
DEV_BACKEND_PORT="${DEV_BACKEND_PORT:-${BACKEND_PORT:-6811}}"
DEV_FRONTEND_HOST="${DEV_FRONTEND_HOST:-127.0.0.1}"
DEV_FRONTEND_PORT="${DEV_FRONTEND_PORT:-${FRONTEND_PORT:-6300}}"
DEV_POSTGRES_HOST="${DEV_POSTGRES_HOST:-127.0.0.1}"
DEV_POSTGRES_PORT="${DEV_POSTGRES_PORT:-65432}"
DEV_REDIS_HOST="${DEV_REDIS_HOST:-127.0.0.1}"
DEV_REDIS_PORT="${DEV_REDIS_PORT:-63790}"
# Concurrence du worker Celery. Défaut 3 (< nb de cœurs) : voir en-tête.
DEV_CELERY_CONCURRENCY="${DEV_CELERY_CONCURRENCY:-3}"
export DEV_POSTGRES_HOST DEV_POSTGRES_PORT

TEST_EMAIL="${TEST_EMAIL:-admin@example.com}"
TEST_USERNAME="${TEST_USERNAME:-admin}"
TEST_PASSWORD="${TEST_PASSWORD:-Test1234Test1234}"

mkdir -p "${LOG_DIR}"

log()  { printf "\033[1;36m[dev] %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m[dev] %s\033[0m\n" "$*" >&2; }
die()  { printf "\033[1;31m[dev] %s\033[0m\n" "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 manquant. Installe-le avant."; }

# Refuse de démarrer si un port nécessaire est déjà occupé (message actionnable).
port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :$1 )" 2>/dev/null | tail -n +2 | grep -q .
  else
    (echo > "/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
  fi
}
ensure_port_free() {
  local port="$1" label="$2"
  if port_busy "${port}"; then
    die "Port ${port} (${label}) déjà occupé. Identifier le processus :
       ss -ltnp | grep ':${port}'
   ou  lsof -i :${port}
   puis libérez le port (ou surchargez la variable DEV_* correspondante)."
  fi
}

# --------------------------------------------------- neutralisation venv hérité
# Si le shell appelant a un venv ACTIF (potentiellement obsolète ou cassé), on le
# retire du PATH pour repartir sur le python3 système — évite un python3 fantôme
# si <venv>/bin a été supprimé entre-temps.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  log "Neutralisation du venv actif hérité (${VIRTUAL_ENV})"
  PATH="$(printf '%s' "${PATH}" | tr ':' '\n' | grep -vxF "${VIRTUAL_ENV}/bin" | paste -sd ':' -)"
  export PATH
  unset VIRTUAL_ENV
fi
hash -r 2>/dev/null || true

# ------------------------------------------------------------------ pré-requis
need python3
need docker
need node
need npm
python3 -c "import venv" 2>/dev/null \
  || die "python3-venv manquant. sudo apt install python3.12-venv python3-pip"

# ------------------------------------------------------- contrôle des ports
log "Vérification des ports dev (loopback)…"
ensure_port_free "${DEV_BACKEND_PORT}"  "backend Django"
ensure_port_free "${DEV_FRONTEND_PORT}" "frontend Next.js"

# ----------------------------------------------------------- venv + deps py
# (Re)crée le venv s'il manque OU s'il est cassé (bin/activate absent, p.ex.
# après un up_rebuild.sh interrompu).
if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
  if [[ -d "${VENV_DIR}" ]]; then
    warn "venv incomplet (${VENV_DIR}/bin/activate absent) → recréation"
    rm -rf "${VENV_DIR}"
    hash -r 2>/dev/null || true
  fi
  log "Création du venv ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

if ! python -c "import django" 2>/dev/null; then
  log "Installation deps backend (editable + extras dev,ocr,tables)"
  pip install -U pip
  # extra ocr = pytesseract/pypdfium2/Pillow (OCR à la demande). Binaire système
  # tesseract requis séparément (apt install tesseract-ocr tesseract-ocr-fra).
  pip install -e "${REPO_ROOT}/backend[dev,ocr,tables]"
fi

# -------------------------------------------------------------- postgres dev
log "Démarrage Postgres (docker compose dev, ${DEV_POSTGRES_HOST}:${DEV_POSTGRES_PORT})"
docker compose -f "${COMPOSE_FILE}" up -d >/dev/null

log "Attente Postgres sur ${DEV_POSTGRES_HOST}:${DEV_POSTGRES_PORT} ..."
for _ in {1..30}; do
  (echo > "/dev/tcp/${DEV_POSTGRES_HOST}/${DEV_POSTGRES_PORT}") >/dev/null 2>&1 && break
  sleep 1
done
(echo > "/dev/tcp/${DEV_POSTGRES_HOST}/${DEV_POSTGRES_PORT}") >/dev/null 2>&1 \
  || die "Postgres injoignable après 30s — vérifier docker compose"

# --------------------------------------------------------------- migrations
export DJANGO_ENV=dev
export DJANGO_SETTINGS_MODULE=config.settings.dev
# Aligne la connexion DB sur le port publié par le compose dev (prioritaire sur .env).
export DB_HOST="${DEV_POSTGRES_HOST}"
export DB_PORT="${DEV_POSTGRES_PORT}"
# Origines front dev (CSRF + liens email) cohérentes avec le nouveau port front.
export FRONTEND_APP_ORIGIN="http://${DEV_FRONTEND_HOST}:${DEV_FRONTEND_PORT}"
export DJANGO_CSRF_TRUSTED_ORIGINS="http://${DEV_FRONTEND_HOST}:${DEV_FRONTEND_PORT},http://${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}"

cd "${REPO_ROOT}/backend"
log "Application des migrations Django"
if ! python manage.py migrate --noinput; then
  warn "migrate a échoué — fake-apply rag_core.0011 (contrainte déjà créée par 0010)"
  python manage.py migrate rag_core 0011_unique_doc_space_sha256_not_failed --fake --noinput
  python manage.py migrate --noinput
fi

# -------------------------------------------------------- bootstrap si vide
USER_COUNT="$(
  python manage.py shell -c \
    'from django.contrib.auth import get_user_model; print(get_user_model().objects.count())' \
    2>/dev/null | tail -1 | tr -d '[:space:]'
)"
if [[ "${USER_COUNT}" == "0" ]]; then
  log "DB vide → création user de test + workspace + ragspace"
  DJANGO_SUPERUSER_PASSWORD="${TEST_PASSWORD}" \
    python manage.py createsuperuser --noinput \
      --email "${TEST_EMAIL}" \
      --username "${TEST_USERNAME}"
  python manage.py bootstrap_workspace \
    --email "${TEST_EMAIL}" \
    --workspace "WorkspaceTest" \
    --ragspace "Default"
else
  log "DB déjà peuplée (${USER_COUNT} user(s)) — pas de bootstrap"
fi

# ----------------------------------------------------------- frontend deps
cd "${REPO_ROOT}/frontend"
if [[ ! -d node_modules ]]; then
  log "Installation deps frontend (npm install)"
  npm install
fi

# -------------------------------------------------------- start back + front
cd "${REPO_ROOT}/backend"
log "Démarrage backend Django sur ${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}"
python manage.py runserver "${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}" \
  >"${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!

# Worker Celery (queues celery/reindex/analytics) — requis pour la réindexation
# durable (POST 202 + traitement async). Non lancé si EAGER=1 (tâches inline).
#   --concurrency : borné (défaut 3) pour ne pas sur-souscrire le CPU sur des
#     tâches CPU-bound + multi-thread (OCR + embeddings) => extraction plus rapide,
#     moins de faux "timeout". --max-tasks-per-child recycle les procs (fuite
#     mémoire Pillow/OCR sur les gros lots).
WORKER_PID=""
if [[ "${CELERY_TASK_ALWAYS_EAGER:-0}" != "1" ]]; then
  log "Démarrage worker Celery (queues celery,reindex,analytics ; concurrency=${DEV_CELERY_CONCURRENCY})"
  celery -A config worker -l info -Q celery,reindex,analytics -n devworker@%h \
    --concurrency="${DEV_CELERY_CONCURRENCY}" --max-tasks-per-child=50 \
    >"${LOG_DIR}/worker.log" 2>&1 &
  WORKER_PID=$!
  echo "${WORKER_PID}" >"${LOG_DIR}/worker.pid"
else
  log "CELERY_TASK_ALWAYS_EAGER=1 → pas de worker (tâches inline ; gros reindex refusés)"
fi

cd "${REPO_ROOT}/frontend"
log "Démarrage frontend Next.js sur ${DEV_FRONTEND_HOST}:${DEV_FRONTEND_PORT}"
# -H/-p explicites : Next bind en loopback sur le port dédié (jamais 0.0.0.0:3000).
BACKEND_BASE_URL="http://${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}" \
  npm run dev -- -H "${DEV_FRONTEND_HOST}" -p "${DEV_FRONTEND_PORT}" \
  >"${LOG_DIR}/frontend.log" 2>&1 &
FRONTEND_PID=$!

# Fichiers PID pour un arrêt fiable via scripts/dev/stop_dev.sh.
echo "${BACKEND_PID}"  >"${LOG_DIR}/backend.pid"
echo "${FRONTEND_PID}" >"${LOG_DIR}/frontend.pid"

# ------------------------------------------------------------------ cleanup
cleanup() {
  echo
  log "Arrêt backend (PID ${BACKEND_PID}) + frontend (PID ${FRONTEND_PID})${WORKER_PID:+ + worker (PID ${WORKER_PID})}"
  kill "${BACKEND_PID}" "${FRONTEND_PID}" ${WORKER_PID} 2>/dev/null || true
  wait "${BACKEND_PID}" "${FRONTEND_PID}" ${WORKER_PID} 2>/dev/null || true
  rm -f "${LOG_DIR}/backend.pid" "${LOG_DIR}/frontend.pid" "${LOG_DIR}/worker.pid" 2>/dev/null || true
  log "Postgres docker reste up. Pour l'arrêter :"
  log "  docker compose -f ${COMPOSE_FILE} down"
}
trap cleanup EXIT INT TERM

# Petit délai pour que les premiers logs s'écrivent
sleep 2

log "Stack prête :"
log "  Backend  : http://${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}/api/v1/"
log "  Admin    : http://${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}/admin/"
log "  Frontend : http://${DEV_FRONTEND_HOST}:${DEV_FRONTEND_PORT}/"
log "  Login    : ${TEST_EMAIL} / ${TEST_PASSWORD}"
log "  Logs     : ${LOG_DIR}/{backend,frontend}.log"
log "Ctrl-C pour tout arrêter (les logs sont streamés ci-dessous)"
echo

# Stream des logs jusqu'au Ctrl-C
tail -n +1 -F "${LOG_DIR}/backend.log" "${LOG_DIR}/frontend.log"

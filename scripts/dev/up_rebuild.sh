#!/usr/bin/env bash
# scripts/dev/up_rebuild.sh
# -----------------------------------------------------------------------------
# Variante "full rebuild" de scripts/dev/up.sh.
#
# Identique à up.sh pour le démarrage de la stack, mais force une reconstruction
# complète pour éviter les états incohérents et les messages du type :
#
#   Your models in app(s): 'chat', 'rag_core' have changes that are not yet
#   reflected in a migration ... Run 'manage.py makemigrations' ...
#
# Différences vs up.sh :
#   - recréation du venv + réinstallation deps backend (REBUILD_VENV=1 par défaut)
#   - réinstallation deps frontend (npm ci / install) + purge node_modules
#   - docker compose : --build + recreate (REBUILD_DB=0 garde les données ;
#     REBUILD_DB=1 fait un down -v → DB neuve + re-bootstrap)
#   - makemigrations AVANT migrate pour générer les migrations manquantes
#
# Ctrl-C arrête backend + frontend proprement. Postgres docker reste up.
# Pour l'arrêter aussi :
#   docker compose -f deploy/docker/dev/docker-compose.yml down
#
# Ports dev (loopback only) — politique RAGATM, cf. scripts/dev/up.sh.
#
# Variables surchargeables :
#   DEV_BACKEND_HOST  (def 127.0.0.1)  DEV_BACKEND_PORT  (def 6811)
#   DEV_FRONTEND_HOST (def 127.0.0.1)  DEV_FRONTEND_PORT (def 6300)
#   DEV_POSTGRES_HOST (def 127.0.0.1)  DEV_POSTGRES_PORT (def 65432)
#   TEST_EMAIL   (def admin@example.com)
#   TEST_PASSWORD (def Test1234Test1234)
#   REBUILD_VENV  (def 1)  → 0 pour réutiliser le venv existant
#   REBUILD_NODE  (def 1)  → 0 pour réutiliser node_modules existant
#   REBUILD_DB    (def 0)  → 1 pour repartir d'une DB vide (down -v)
# -----------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
LOG_DIR="${REPO_ROOT}/.dev-logs"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/dev/docker-compose.yml"

# Ports dev (loopback only). Compat : anciens BACKEND_PORT/FRONTEND_PORT acceptés
# comme surcharge, mais les défauts suivent la politique RAGATM.
DEV_BACKEND_HOST="${DEV_BACKEND_HOST:-127.0.0.1}"
DEV_BACKEND_PORT="${DEV_BACKEND_PORT:-${BACKEND_PORT:-6811}}"
DEV_FRONTEND_HOST="${DEV_FRONTEND_HOST:-127.0.0.1}"
DEV_FRONTEND_PORT="${DEV_FRONTEND_PORT:-${FRONTEND_PORT:-6300}}"
DEV_POSTGRES_HOST="${DEV_POSTGRES_HOST:-127.0.0.1}"
DEV_POSTGRES_PORT="${DEV_POSTGRES_PORT:-65432}"
export DEV_POSTGRES_HOST DEV_POSTGRES_PORT

TEST_EMAIL="${TEST_EMAIL:-admin@example.com}"
TEST_USERNAME="${TEST_USERNAME:-admin}"
TEST_PASSWORD="${TEST_PASSWORD:-Test1234Test1234}"

REBUILD_VENV="${REBUILD_VENV:-1}"
REBUILD_NODE="${REBUILD_NODE:-1}"
REBUILD_DB="${REBUILD_DB:-0}"

mkdir -p "${LOG_DIR}"

log()  { printf "\033[1;36m[rebuild] %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m[rebuild] %s\033[0m\n" "$*" >&2; }
die()  { printf "\033[1;31m[rebuild] %s\033[0m\n" "$*" >&2; exit 1; }

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
# Si le shell appelant a un venv ACTIF (typiquement ce .venv qu'on s'apprête à
# supprimer), il faut le retirer du PATH AVANT le rm -rf : sinon, après
# suppression, `python3` (au PATH/hashé sur <venv>/bin) pointe sur un binaire
# fantôme → "No such file or directory" au moment de recréer le venv.
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
if [[ "${REBUILD_VENV}" == "1" && -d "${VENV_DIR}" ]]; then
  log "Suppression du venv existant (REBUILD_VENV=1)"
  rm -rf "${VENV_DIR}"
  hash -r 2>/dev/null || true
fi
# (Re)crée le venv s'il manque OU s'il est cassé (bin/activate absent).
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

log "(Ré)installation deps backend (editable + extras dev,ocr,tables)"
pip install -U pip
# extra ocr = pytesseract/pypdfium2/Pillow (OCR à la demande en dev). Le binaire
# système tesseract reste requis (apt install tesseract-ocr tesseract-ocr-fra).
pip install -e "${REPO_ROOT}/backend[dev,ocr,tables]"

# -------------------------------------------------------------- postgres dev
if [[ "${REBUILD_DB}" == "1" ]]; then
  log "Réinitialisation Postgres (down -v → DB neuve)"
  docker compose -f "${COMPOSE_FILE}" down -v >/dev/null 2>&1 || true
fi

log "Démarrage Postgres (docker compose dev, --build + recreate)"
docker compose -f "${COMPOSE_FILE}" up -d --build --force-recreate >/dev/null

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

log "Génération des migrations manquantes (makemigrations)"
python manage.py makemigrations --noinput

log "Application des migrations Django"
# NB : plus de workaround --fake ici. Le doublon de contrainte 0010/0011 qui
# imposait le fake-apply est corrigé (0011 est désormais un no-op). Un échec de
# migrate est donc une vraie erreur — on s'arrête plutôt que de bricoler le
# state des migrations (l'ancien fake-unapply pouvait corrompre la DB).
python manage.py migrate --noinput \
  || die "migrate a échoué — vérifier les migrations (ne PAS fake-apply à l'aveugle)"

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
if [[ "${REBUILD_NODE}" == "1" ]]; then
  log "Purge + réinstallation deps frontend"
  rm -rf node_modules
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
elif [[ ! -d node_modules ]]; then
  log "Installation deps frontend (npm install)"
  npm install
fi

# -------------------------------------------------------- start back + front
cd "${REPO_ROOT}/backend"
log "Démarrage backend Django sur ${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}"
python manage.py runserver "${DEV_BACKEND_HOST}:${DEV_BACKEND_PORT}" \
  >"${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!

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
  log "Arrêt backend (PID ${BACKEND_PID}) + frontend (PID ${FRONTEND_PID})"
  kill "${BACKEND_PID}" "${FRONTEND_PID}" 2>/dev/null || true
  wait "${BACKEND_PID}" "${FRONTEND_PID}" 2>/dev/null || true
  rm -f "${LOG_DIR}/backend.pid" "${LOG_DIR}/frontend.pid" 2>/dev/null || true
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
log "  - Login    : ${TEST_EMAIL} / ${TEST_PASSWORD}"
log "  - Logs     : ${LOG_DIR}/{backend,frontend}.log"
log "Ctrl-C pour tout arrêter (les logs sont streamés ci-dessous)"
echo

# Stream des logs jusqu'au Ctrl-C
tail -n +1 -F "${LOG_DIR}/backend.log" "${LOG_DIR}/frontend.log"

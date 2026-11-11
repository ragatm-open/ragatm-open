#!/usr/bin/env bash
# scripts/dev/reset_db.sh
# =============================================================================
# Réinitialisation COMPLÈTE et SÛRE de la base de données de DÉVELOPPEMENT RagATM.
#
# Architecture dev (cf. scripts/dev/up.sh) :
#   - PostgreSQL + pgvector tourne dans Docker Compose
#       fichier : deploy/docker/dev/docker-compose.yml
#       projet  : "dev" (aucun -p → nom = dossier du compose)
#       service : db      volume : dev_pgdata      publié : 127.0.0.1:65432
#   - Backend Django / frontend Next.js tournent sur l'HÔTE (pas de celery en dev).
#   - L'extension `vector` est recréée par infra/postgres/initdb/001_pgvector.sql
#     au premier démarrage d'un volume neuf.
#
# Stratégie de reset (mode Docker, détecté par défaut) :
#   arrêt des services dev → suppression du SEUL volume Postgres du projet dev
#   (docker compose down -v, borné au projet — JAMAIS `docker volume prune`) →
#   recréation du service db → attente pg_isready → migrations → check.
#
# Mode LOCAL (fallback si Postgres est hébergé sur l'hôte, non dockerisé) :
#   terminate connexions → DROP/CREATE de la base → extension vector → migrations.
#
# Garde-fous : refuse tout signal prod/staging AVANT toute opération destructive,
# exige un signal dev EXPLICITE, n'agit que sur une base locale au nom validé, et
# demande une confirmation « RESET » (sauf --yes).
# =============================================================================

set -Eeuo pipefail
IFS=$'\n\t'

# ------------------------------------------------------------------ apparence
if [[ -t 2 ]]; then
  C_RESET=$'\033[0m'; C_INFO=$'\033[1;36m'; C_WARN=$'\033[1;33m'
  C_ERR=$'\033[1;31m'; C_OK=$'\033[1;32m'
else
  C_RESET=""; C_INFO=""; C_WARN=""; C_ERR=""; C_OK=""
fi

info()  { printf '%s[INFO]%s  %s\n'  "${C_INFO}" "${C_RESET}" "$*"; }
warn()  { printf '%s[WARN]%s  %s\n'  "${C_WARN}" "${C_RESET}" "$*" >&2; }
error() { printf '%s[ERROR]%s %s\n'  "${C_ERR}"  "${C_RESET}" "$*" >&2; }
ok()    { printf '%s[OK]%s    %s\n'  "${C_OK}"   "${C_RESET}" "$*"; }
die()   { error "$*"; exit 1; }

# Trap d'erreur : indique la ligne et la commande fautive.
on_err() {
  local rc=$? line=$1 cmd=$2
  error "Échec ligne ${line} (code ${rc}) : ${cmd}"
  exit "${rc}"
}
trap 'on_err "${LINENO}" "${BASH_COMMAND}"' ERR

lc() { printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'; }

need() { command -v "$1" >/dev/null 2>&1 || die "Dépendance manquante : $1"; }

# --------------------------------------------------------------------- chemins
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# Repli : si la structure attendue n'est pas là, tente la racine git.
if [[ ! -f "${REPO_ROOT}/backend/manage.py" ]]; then
  if git_root="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null)"; then
    REPO_ROOT="${git_root}"
  fi
fi
DEV_COMPOSE="${REPO_ROOT}/deploy/docker/dev/docker-compose.yml"
STOP_DEV="${SCRIPT_DIR}/stop_dev.sh"

# ------------------------------------------------------------------- options
ASSUME_YES=0
WITH_FIXTURES=0
SKIP_SUPERUSER=0

usage() {
  cat <<'EOF'
reset_db.sh — Réinitialise la base de DÉVELOPPEMENT RagATM (destructif, dev only).

Usage :
  scripts/dev/reset_db.sh [options]

Options :
  --yes             Ne pas demander la confirmation « RESET » (dangereux, CI).
  --with-fixtures   Charge les fixtures de dev réellement présentes (échoue si aucune).
  --skip-superuser  Ne crée aucun superutilisateur, même si les variables sont définies.
  -h, --help        Affiche cette aide.

Superutilisateur (créé uniquement si les TROIS sont définies, sauf --skip-superuser) :
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_EMAIL
  DJANGO_SUPERUSER_PASSWORD   (jamais affiché ; consommé par manage.py)

Sécurité : refuse tout signal prod/staging (DJANGO_ENV/ENVIRONMENT/APP_ENV,
DEBUG=0/false, chemin ou nom de base « prod »/« staging », hôte DB non local).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)         ASSUME_YES=1 ;;
    --with-fixtures)  WITH_FIXTURES=1 ;;
    --skip-superuser) SKIP_SUPERUSER=1 ;;
    -h|--help)        usage; exit 0 ;;
    *) die "Option inconnue : $1 (voir --help)" ;;
  esac
  shift
done

# --------------------------------------------------- lecture .env SANS exécution
# On NE source PAS les fichiers .env (pas d'eval, pas d'exécution de contenu
# arbitraire, pas de fuite de secret). On lit uniquement les clés nécessaires.
# Précédence : variable d'environnement du process > .env.local > .env.dev > .env
ENV_FILES=("${REPO_ROOT}/.env.local" "${REPO_ROOT}/.env.dev" "${REPO_ROOT}/.env")

read_env_key() {
  local key="$1" file line val
  if [[ -n "${!key+x}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  for file in "${ENV_FILES[@]}"; do
    [[ -f "${file}" ]] || continue
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "${file}" 2>/dev/null | tail -n1 || true)"
    [[ -n "${line}" ]] || continue
    val="${line#*"${key}"=}"
    val="${val%%$'\r'}"
    # retire d'éventuels guillemets encadrants
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    printf '%s' "${val}"
    return 0
  done
  return 0
}

# Valeurs effectives (avec défauts alignés sur settings/base.py + compose dev).
DB_NAME="$(read_env_key DB_NAME)";         DB_NAME="${DB_NAME:-ragatm}"
DB_USER="$(read_env_key DB_USER)";         DB_USER="${DB_USER:-ragatm}"
DB_PASSWORD="$(read_env_key DB_PASSWORD)"; DB_PASSWORD="${DB_PASSWORD:-ragatm}"
DB_HOST="$(read_env_key DB_HOST)";         DB_HOST="${DB_HOST:-127.0.0.1}"
DEV_POSTGRES_PORT="$(read_env_key DEV_POSTGRES_PORT)"
DEV_POSTGRES_PORT="${DEV_POSTGRES_PORT:-$(read_env_key DB_PORT)}"
DEV_POSTGRES_PORT="${DEV_POSTGRES_PORT:-65432}"
DJANGO_ENV_EFF="$(read_env_key DJANGO_ENV)"
SETTINGS_EFF="$(read_env_key DJANGO_SETTINGS_MODULE)"

# -------------------------------------------------------------- garde-fous env
is_prod_like() {
  case "$(lc "$1")" in prod|production|staging) return 0 ;; *) return 1 ;; esac
}

guard_environment() {
  local var v
  # 1) Marqueurs d'environnement explicitement prod/staging → refus immédiat.
  for var in DJANGO_ENV ENVIRONMENT APP_ENV; do
    v="$(read_env_key "${var}")"
    if [[ -n "${v}" ]] && is_prod_like "${v}"; then
      die "Refus : ${var}=${v} → environnement production/staging. Aucune opération destructive."
    fi
  done
  # 2) DEBUG désactivé = signal production.
  case "$(lc "$(read_env_key DEBUG)")" in
    false|0) die "Refus : DEBUG désactivé → signal production." ;;
  esac
  # 3) Exiger un signal dev EXPLICITE (positif) — plus strict que la simple absence de prod.
  if [[ "$(lc "${DJANGO_ENV_EFF}")" != "dev" && "${SETTINGS_EFF}" != *.dev ]]; then
    die "Refus : aucun signal dev explicite (DJANGO_ENV='${DJANGO_ENV_EFF:-unset}', settings='${SETTINGS_EFF:-unset}'). Exigé : DJANGO_ENV=dev ou settings *.dev."
  fi
  # 4) Chemin dépôt / répertoire courant contenant prod|staging.
  case "$(lc "${REPO_ROOT}")" in *prod*|*staging*) die "Refus : chemin dépôt contient 'prod'/'staging' (${REPO_ROOT})." ;; esac
  case "$(lc "${PWD}")"        in *prod*|*staging*) die "Refus : répertoire courant contient 'prod'/'staging' (${PWD})." ;; esac
  # 5) Nom de base contenant prod|production|staging.
  case "$(lc "${DB_NAME}")" in
    *prod*|*production*|*staging*) die "Refus : nom de base '${DB_NAME}' évoque production/staging." ;;
  esac
  # 6) Hôte DB : uniquement local/connu (jamais de reset distant ou inconnu).
  case "${DB_HOST}" in
    127.0.0.1|localhost|::1|db) : ;;
    *) die "Refus : hôte DB '${DB_HOST}' non local/inconnu — jamais de reset sur une base distante." ;;
  esac
  # 7) .env.prod : présent dans ce dépôt comme TEMPLATE (jamais chargé par ce script).
  #    On avertit sans bloquer, puisque les signaux dev ci-dessus sont explicites.
  if [[ -f "${REPO_ROOT}/.env.prod" ]]; then
    warn ".env.prod présent (template prod, NON chargé). Poursuite : signaux dev explicites validés."
  fi
}

# ----------------------------------------------------------- détection du mode
detect_mode() {
  if command -v docker >/dev/null 2>&1 && [[ -f "${DEV_COMPOSE}" ]]; then
    MODE="docker"
  elif command -v psql >/dev/null 2>&1; then
    MODE="local"
  else
    die "Ni Docker+compose dev (${DEV_COMPOSE}) ni psql disponibles — mode de reset indéterminable."
  fi
}

# ----------------------------------------------------------- python (venv)
resolve_python() {
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PY_BIN="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
    warn "venv absent (${REPO_ROOT}/.venv) — utilisation de python3 système."
  else
    die "Aucun interpréteur Python (.venv ou python3) disponible."
  fi
}

# -------------------------------------------------------------------- résumé
print_summary() {
  local ops
  if [[ "${MODE}" == "docker" ]]; then
    ops="down -v (volume dev_pgdata uniquement) → up -d db → migrate → check"
  else
    ops="terminate connexions → DROP/CREATE ${DB_NAME} → extension vector → migrate → check"
  fi
  cat >&2 <<EOF

${C_INFO}──────────────────────────────────────────────────────────────${C_RESET}
${C_INFO} RÉINITIALISATION DE LA BASE DE DÉVELOPPEMENT RagATM${C_RESET}
${C_INFO}──────────────────────────────────────────────────────────────${C_RESET}
  Environnement     : dev (DJANGO_ENV='${DJANGO_ENV_EFF:-unset}', settings='${SETTINGS_EFF:-config.settings.dev}')
  Mode              : ${MODE}
  Hôte PostgreSQL   : ${DB_HOST}
  Port              : ${DEV_POSTGRES_PORT}
  Base de données   : ${DB_NAME}   (user: ${DB_USER})
  Racine dépôt      : ${REPO_ROOT}
  Python            : ${PY_BIN}
  Fixtures          : $([[ ${WITH_FIXTURES} -eq 1 ]] && echo 'oui (--with-fixtures)' || echo 'non')
  Superutilisateur  : $([[ ${SKIP_SUPERUSER} -eq 1 ]] && echo 'ignoré (--skip-superuser)' || echo 'si DJANGO_SUPERUSER_* définies')
  Opérations        : ${ops}
${C_WARN} ⚠  DESTRUCTIF : toutes les données de la base '${DB_NAME}' seront perdues.${C_RESET}
${C_INFO}──────────────────────────────────────────────────────────────${C_RESET}
EOF
}

confirm() {
  if [[ ${ASSUME_YES} -eq 1 ]]; then
    warn "Confirmation ignorée (--yes)."
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "Entrée non interactive et --yes absent — abandon (aucune destruction)."
  fi
  local ans=""
  printf '%sTapez exactement RESET pour confirmer : %s' "${C_WARN}" "${C_RESET}" >&2
  read -r ans || true
  [[ "${ans}" == "RESET" ]] || die "Confirmation invalide ('${ans}') — abandon."
}

# ------------------------------------------------------------- arrêt services
stop_services() {
  info "Arrêt des services dev pouvant détenir des connexions DB…"
  if [[ -x "${STOP_DEV}" ]]; then
    # stop_dev.sh est idempotent et sûr (backend/frontend hôte + compose down).
    "${STOP_DEV}" >&2 || warn "stop_dev.sh a signalé une erreur (ignoré)."
  else
    warn "stop_dev.sh introuvable — arrêt best-effort du backend runserver:6811."
    if command -v pgrep >/dev/null 2>&1; then
      pgrep -u "$(id -u)" -f 'manage\.py runserver .*:6811' 2>/dev/null \
        | while read -r pid; do [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true; done || true
    fi
  fi
}

# ---------------------------------------------------------- reset mode DOCKER
compose() { docker compose -f "${DEV_COMPOSE}" "$@"; }

reset_docker() {
  need docker
  info "Suppression du volume Postgres du projet dev (borné au projet, jamais de prune)…"
  # down -v est SCOPÉ au projet 'dev' → ne touche que dev_pgdata. --remove-orphans
  # nettoie d'éventuels services retirés du compose. JAMAIS `docker volume prune`.
  compose down -v --remove-orphans >&2 || warn "compose down a renvoyé une erreur (déjà arrêté ?) — poursuite."

  info "Recréation du service db (volume neuf → initdb recrée l'extension vector)…"
  compose up -d db >&2

  info "Attente de PostgreSQL (pg_isready dans le conteneur db)…"
  local i
  for i in $(seq 1 60); do
    if compose exec -T db pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
      ok "PostgreSQL prêt (conteneur db)."
      break
    fi
    [[ "${i}" -eq 60 ]] && die "PostgreSQL non prêt après 60s (conteneur db)."
    sleep 1
  done

  # Idempotent : l'extension est déjà créée par initdb sur volume neuf.
  info "Vérification de l'extension vector…"
  compose exec -T db psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
  ok "Extension vector présente."
}

# ----------------------------------------------------------- reset mode LOCAL
reset_local() {
  need psql
  # Validation stricte du nom AVANT tout DROP (défense en profondeur).
  [[ "${DB_NAME}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
    || die "Nom de base non sûr pour DROP/CREATE : '${DB_NAME}'."
  case "$(lc "${DB_NAME}")" in
    *prod*|*production*|*staging*) die "Refus DROP : nom de base '${DB_NAME}' évoque production/staging." ;;
  esac
  local host="${DB_HOST}" port="${DEV_POSTGRES_PORT}"
  export PGPASSWORD="${DB_PASSWORD}"
  local admin=(psql -v ON_ERROR_STOP=1 -h "${host}" -p "${port}" -U "${DB_USER}" -d postgres)

  info "Fermeture des connexions à ${DB_NAME}…"
  "${admin[@]}" -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" \
    >/dev/null || warn "Terminate connexions : avertissement (base peut-être absente)."

  info "Suppression puis recréation de la base ${DB_NAME}…"
  "${admin[@]}" -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" >/dev/null
  "${admin[@]}" -c "CREATE DATABASE \"${DB_NAME}\" WITH OWNER \"${DB_USER}\" ENCODING 'UTF8';" >/dev/null

  info "Activation de l'extension vector…"
  psql -v ON_ERROR_STOP=1 -h "${host}" -p "${port}" -U "${DB_USER}" -d "${DB_NAME}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
  unset PGPASSWORD
  ok "Base ${DB_NAME} recréée (extension vector activée)."
}

# ----------------------------------------------------- environnement Django
export_django_env() {
  export DJANGO_ENV="dev"
  export DJANGO_SETTINGS_MODULE="${SETTINGS_EFF:-config.settings.dev}"
  [[ "${DJANGO_SETTINGS_MODULE}" == *.dev ]] || export DJANGO_SETTINGS_MODULE="config.settings.dev"
  # En mode docker, on se connecte au port publié en loopback.
  export DB_HOST="127.0.0.1"
  export DB_PORT="${DEV_POSTGRES_PORT}"
  export DB_NAME DB_USER DB_PASSWORD
}

# ---------------------------------------------------------------- migrations
run_migrations() {
  info "Application des migrations Django…"
  (
    cd "${REPO_ROOT}/backend"
    if ! "${PY_BIN}" manage.py migrate --noinput; then
      warn "migrate a échoué — fake-apply rag_core.0011 (contrainte déjà créée par 0010), puis reprise."
      "${PY_BIN}" manage.py migrate rag_core 0011_unique_doc_space_sha256_not_failed --fake --noinput || true
      "${PY_BIN}" manage.py migrate --noinput
    fi
  )
  ok "Migrations appliquées."
}

# ------------------------------------------------------------------ fixtures
load_fixtures() {
  [[ ${WITH_FIXTURES} -eq 1 ]] || return 0
  info "Détection des fixtures de dev…"
  local found=()
  local f
  while IFS= read -r f; do
    [[ -n "${f}" ]] && found+=("${f}")
  done < <(find "${REPO_ROOT}/backend" -path '*/fixtures/*' \
             \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) 2>/dev/null | sort)
  if [[ ${#found[@]} -eq 0 ]]; then
    die "--with-fixtures demandé mais aucune fixture trouvée (backend/**/fixtures/*.json|yaml)."
  fi
  info "Fixtures détectées :"
  for f in "${found[@]}"; do info "  - ${f#"${REPO_ROOT}"/}"; done
  ( cd "${REPO_ROOT}/backend" && "${PY_BIN}" manage.py loaddata "${found[@]}" )
  ok "Fixtures chargées (${#found[@]})."
}

# --------------------------------------------------------------- superuser
create_superuser() {
  if [[ ${SKIP_SUPERUSER} -eq 1 ]]; then
    info "Superutilisateur : ignoré (--skip-superuser)."
    return 0
  fi
  if [[ -z "${DJANGO_SUPERUSER_USERNAME:-}" || -z "${DJANGO_SUPERUSER_EMAIL:-}" || -z "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
    info "Superutilisateur : non créé (DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD non toutes définies)."
    return 0
  fi
  info "Création du superutilisateur de dev (${DJANGO_SUPERUSER_USERNAME})…"
  # Le mot de passe est consommé par manage.py via DJANGO_SUPERUSER_PASSWORD (jamais affiché).
  if ( cd "${REPO_ROOT}/backend" && "${PY_BIN}" manage.py createsuperuser --noinput \
         --username "${DJANGO_SUPERUSER_USERNAME}" --email "${DJANGO_SUPERUSER_EMAIL}" ); then
    ok "Superutilisateur créé."
  else
    warn "createsuperuser a échoué (déjà existant ?) — ignoré."
  fi
}

# ------------------------------------------------------- vérifications finales
final_checks() {
  info "Vérifications finales…"
  (
    cd "${REPO_ROOT}/backend"
    "${PY_BIN}" manage.py check
    if ! "${PY_BIN}" manage.py makemigrations --check --dry-run >/dev/null; then
      warn "makemigrations --check : des migrations semblent manquantes (dérive de modèles ?)."
    else
      ok "makemigrations --check : aucune migration manquante."
    fi
  )
  ok "manage.py check : OK."
}

# --------------------------------------------------------------------- main
main() {
  [[ -f "${REPO_ROOT}/backend/manage.py" ]] \
    || die "manage.py introuvable sous ${REPO_ROOT}/backend — hors du dépôt RagATM ?"

  guard_environment
  detect_mode
  resolve_python
  print_summary
  confirm

  stop_services

  case "${MODE}" in
    docker) reset_docker ;;
    local)  reset_local ;;
    *)      die "Mode inconnu : ${MODE}" ;;
  esac

  export_django_env
  run_migrations
  load_fixtures
  create_superuser
  final_checks

  cat >&2 <<EOF

${C_OK}──────────────────────────────────────────────────────────────${C_RESET}
${C_OK} ✔ Base de développement '${DB_NAME}' réinitialisée avec succès.${C_RESET}
${C_OK}──────────────────────────────────────────────────────────────${C_RESET}
  Mode              : ${MODE}
  PostgreSQL        : ${DB_HOST}:${DEV_POSTGRES_PORT}  (prêt)
  Migrations        : appliquées
  manage.py check   : OK
  Pour relancer la stack dev :  scripts/dev/up.sh
EOF
}

main "$@"

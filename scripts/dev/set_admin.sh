#!/usr/bin/env bash
# scripts/dev/set_admin.sh
# =============================================================================
# Crée OU réinitialise (idempotent) le superutilisateur admin de DÉVELOPPEMENT.
#
# Identifiants par défaut (dev only, surchargeables par variables d'env) :
#   username = admin
#   email    = admin@example.com
#   password = AdmAndr0maqueCh3ngE
#
# - Si l'utilisateur existe : email/mot de passe réinitialisés, flags
#   is_staff/is_superuser/is_active forcés à True.
# - Sinon : création du superutilisateur.
#
# Se connecte à la base dev (Postgres du compose dev, 127.0.0.1:65432 par défaut)
# via le venv du dépôt. Refuse tout signal prod/staging AVANT toute écriture.
# Le mot de passe n'est jamais affiché ni passé en argument (uniquement via env).
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

on_err() { local rc=$? line=$1 cmd=$2; error "Échec ligne ${line} (code ${rc}) : ${cmd}"; exit "${rc}"; }
trap 'on_err "${LINENO}" "${BASH_COMMAND}"' ERR

lc() { printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'; }

# --------------------------------------------------------------------- chemins
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
if [[ ! -f "${REPO_ROOT}/backend/manage.py" ]]; then
  if git_root="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null)"; then
    REPO_ROOT="${git_root}"
  fi
fi

# ------------------------------------------------------------------- options
usage() {
  cat <<'EOF'
set_admin.sh — Crée ou réinitialise le superutilisateur admin de DEV (idempotent).

Usage :
  scripts/dev/set_admin.sh [-h|--help]

Identifiants par défaut (dev only, surchargeables) :
  SET_ADMIN_USERNAME  (def admin)
  SET_ADMIN_EMAIL     (def admin@example.com)
  SET_ADMIN_PASSWORD  (def AdmAndr0maqueCh3ngE ; jamais affiché)

Sécurité : refuse tout signal prod/staging (DJANGO_ENV/ENVIRONMENT/APP_ENV,
DEBUG=0/false, chemin ou nom de base « prod »/« staging », hôte DB non local).
EOF
}
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") : ;;
  *) die "Option inconnue : $1 (voir --help)" ;;
esac

# --------------------------------------------------- lecture .env SANS exécution
ENV_FILES=("${REPO_ROOT}/.env.local" "${REPO_ROOT}/.env.dev" "${REPO_ROOT}/.env")
read_env_key() {
  local key="$1" file line val
  if [[ -n "${!key+x}" ]]; then printf '%s' "${!key}"; return 0; fi
  for file in "${ENV_FILES[@]}"; do
    [[ -f "${file}" ]] || continue
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "${file}" 2>/dev/null | tail -n1 || true)"
    [[ -n "${line}" ]] || continue
    val="${line#*"${key}"=}"; val="${val%%$'\r'}"
    val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    printf '%s' "${val}"; return 0
  done
  return 0
}

DB_NAME="$(read_env_key DB_NAME)";         DB_NAME="${DB_NAME:-ragatm}"
DB_USER="$(read_env_key DB_USER)";         DB_USER="${DB_USER:-ragatm}"
DB_PASSWORD="$(read_env_key DB_PASSWORD)"; DB_PASSWORD="${DB_PASSWORD:-ragatm}"
DB_HOST="$(read_env_key DB_HOST)";         DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT_EFF="$(read_env_key DEV_POSTGRES_PORT)"
DB_PORT_EFF="${DB_PORT_EFF:-$(read_env_key DB_PORT)}"
DB_PORT_EFF="${DB_PORT_EFF:-65432}"
DJANGO_ENV_EFF="$(read_env_key DJANGO_ENV)"
SETTINGS_EFF="$(read_env_key DJANGO_SETTINGS_MODULE)"

# -------------------------------------------------------------- garde-fous env
is_prod_like() { case "$(lc "$1")" in prod|production|staging) return 0 ;; *) return 1 ;; esac; }
guard_environment() {
  local var v
  for var in DJANGO_ENV ENVIRONMENT APP_ENV; do
    v="$(read_env_key "${var}")"
    if [[ -n "${v}" ]] && is_prod_like "${v}"; then
      die "Refus : ${var}=${v} → environnement production/staging. Aucune écriture."
    fi
  done
  case "$(lc "$(read_env_key DEBUG)")" in false|0) die "Refus : DEBUG désactivé → signal production." ;; esac
  if [[ "$(lc "${DJANGO_ENV_EFF}")" != "dev" && "${SETTINGS_EFF}" != *.dev ]]; then
    die "Refus : aucun signal dev explicite (DJANGO_ENV='${DJANGO_ENV_EFF:-unset}', settings='${SETTINGS_EFF:-unset}'). Exigé : DJANGO_ENV=dev ou settings *.dev."
  fi
  case "$(lc "${REPO_ROOT}")" in *prod*|*staging*) die "Refus : chemin dépôt contient 'prod'/'staging' (${REPO_ROOT})." ;; esac
  case "$(lc "${PWD}")"        in *prod*|*staging*) die "Refus : répertoire courant contient 'prod'/'staging' (${PWD})." ;; esac
  case "$(lc "${DB_NAME}")" in *prod*|*production*|*staging*) die "Refus : nom de base '${DB_NAME}' évoque production/staging." ;; esac
  case "${DB_HOST}" in
    127.0.0.1|localhost|::1|db) : ;;
    *) die "Refus : hôte DB '${DB_HOST}' non local/inconnu — jamais d'écriture sur une base distante." ;;
  esac
}

# ----------------------------------------------------------- python (venv)
resolve_python() {
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PY_BIN="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"; warn "venv absent — utilisation de python3 système."
  else
    die "Aucun interpréteur Python (.venv ou python3) disponible."
  fi
}

# ------------------------------------------------------- identifiants admin
# Défauts dev (surchargeables). Le mot de passe ne transite QUE par l'env, jamais
# par argv (invisible dans `ps`), et n'est jamais affiché.
ADMIN_USERNAME="${SET_ADMIN_USERNAME:-${DJANGO_SUPERUSER_USERNAME:-admin}}"
ADMIN_EMAIL="${SET_ADMIN_EMAIL:-${DJANGO_SUPERUSER_EMAIL:-admin@example.com}}"
ADMIN_PASSWORD="${SET_ADMIN_PASSWORD:-${DJANGO_SUPERUSER_PASSWORD:-AdmAndr0maqueCh3ngE}}"

# ---------------------------------------------------------- application Django
apply_admin() {
  info "Cible : ${DB_HOST}:${DB_PORT_EFF}/${DB_NAME} — admin '${ADMIN_USERNAME}' <${ADMIN_EMAIL}>"
  local py_code
  read -r -d '' py_code <<'PY' || true
import os
from django.contrib.auth import get_user_model
U = get_user_model()
username = os.environ["SET_ADMIN_USERNAME"]
email = os.environ["SET_ADMIN_EMAIL"]
password = os.environ["SET_ADMIN_PASSWORD"]
user, created = U.objects.get_or_create(username=username, defaults={"email": email})
user.email = email
user.is_staff = True
user.is_superuser = True
user.is_active = True
user.set_password(password)
user.save()
print("CREATED" if created else "UPDATED", user.pk, user.username, user.email)
PY
  (
    cd "${REPO_ROOT}/backend"
    export DJANGO_ENV="dev"
    export DJANGO_SETTINGS_MODULE="config.settings.dev"
    [[ "${SETTINGS_EFF}" == *.dev ]] && export DJANGO_SETTINGS_MODULE="${SETTINGS_EFF}"
    export DB_HOST="${DB_HOST}" DB_PORT="${DB_PORT_EFF}"
    export DB_NAME DB_USER DB_PASSWORD
    # Identifiants passés uniquement par l'environnement (pas dans argv).
    export SET_ADMIN_USERNAME="${ADMIN_USERNAME}"
    export SET_ADMIN_EMAIL="${ADMIN_EMAIL}"
    export SET_ADMIN_PASSWORD="${ADMIN_PASSWORD}"
    "${PY_BIN}" manage.py shell -c "${py_code}"
  )
}

main() {
  [[ -f "${REPO_ROOT}/backend/manage.py" ]] \
    || die "manage.py introuvable sous ${REPO_ROOT}/backend — hors du dépôt RagATM ?"
  guard_environment
  resolve_python
  local result
  result="$(apply_admin | tail -n1)"
  case "${result}" in
    CREATED*) ok "Superutilisateur créé : ${ADMIN_USERNAME} <${ADMIN_EMAIL}>" ;;
    UPDATED*) ok "Superutilisateur réinitialisé : ${ADMIN_USERNAME} <${ADMIN_EMAIL}>" ;;
    *)        die "Résultat inattendu du management Django : ${result:-<vide>}" ;;
  esac
  info "Mot de passe mis à jour (non affiché). Login admin : /admin/"
}

main "$@"

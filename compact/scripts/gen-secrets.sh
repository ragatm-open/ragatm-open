#!/usr/bin/env bash
# Génère `.env` depuis `.env.example` avec des secrets aléatoires.
# - refuse d'écraser un `.env` existant (sauf --force) ;
# - aucun secret n'est affiché ; le fichier est créé en 0600 ;
# - refuse les valeurs de démonstration (change-me, admin, password, …).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$HERE/.env.example"; TARGET="$HERE/.env"
FORCE=0
for a in "$@"; do case "$a" in --force) FORCE=1;; *) TARGET="$a";; esac; done   # cible alternative : instance isolée (restauration)
[[ -f "$EXAMPLE" ]] || { echo "[gen-secrets] $EXAMPLE introuvable" >&2; exit 1; }
if [[ -e "$TARGET" && $FORCE -eq 0 ]]; then
  echo "[gen-secrets] $TARGET existe déjà ; utilisez --force pour le régénérer (les services devront être recréés)." >&2; exit 2
fi
command -v openssl >/dev/null || { echo "[gen-secrets] openssl requis" >&2; exit 1; }
rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"$1"; }
SECRET_KEY="$(rand 64)"; DB_PW="$(rand 32)"; REDIS_PW="$(rand 32)"; OPS_TOKEN="$(rand 40)"; FB_SALT="$(rand 48)"
for v in "$SECRET_KEY" "$DB_PW" "$REDIS_PW" "$OPS_TOKEN" "$FB_SALT"; do
  case "${v,,}" in *change-me*|*password*|*admin*|*secret*) echo "[gen-secrets] valeur générée inacceptable, relancer" >&2; exit 3;; esac
  [[ ${#v} -ge 32 ]] || { echo "[gen-secrets] longueur insuffisante" >&2; exit 3; }
done
umask 077
tmp="$(mktemp "$(dirname "$TARGET")/.env.XXXXXX")"
sed -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=${SECRET_KEY}|" \
    -e "s|^DB_PASSWORD=.*|DB_PASSWORD=${DB_PW}|" \
    -e "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PW}|" \
    -e "s|^RAG_OPS_METRICS_TOKEN=.*|RAG_OPS_METRICS_TOKEN=${OPS_TOKEN}|" \
    -e "s|^FEEDBACK_PSEUDONYM_SALT=.*|FEEDBACK_PSEUDONYM_SALT=${FB_SALT}|" \
    -e "s|^DJANGO_CACHE_URL=.*|DJANGO_CACHE_URL=redis://:${REDIS_PW}@redis:6379/1|" \
    -e "s|^CELERY_BROKER_URL=.*|CELERY_BROKER_URL=redis://:${REDIS_PW}@redis:6379/0|" \
    -e "s|^CELERY_RESULT_BACKEND=.*|CELERY_RESULT_BACKEND=redis://:${REDIS_PW}@redis:6379/0|" \
    "$EXAMPLE" > "$tmp"
if grep -v "^#" "$tmp" | grep -q "__GENERATE__"; then echo "[gen-secrets] champ non généré restant" >&2; rm -f "$tmp"; exit 4; fi
chmod 600 "$tmp"; mv "$tmp" "$TARGET"
echo "[gen-secrets] $TARGET généré (0600). Secrets : DJANGO_SECRET_KEY, DB_PASSWORD, REDIS_PASSWORD, RAG_OPS_METRICS_TOKEN, FEEDBACK_PSEUDONYM_SALT — jamais affichés."

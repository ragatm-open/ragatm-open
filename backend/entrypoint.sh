#!/usr/bin/env bash
set -euo pipefail

# Valeurs par défaut (peuvent être overridées via env)
: "${DJANGO_SETTINGS_MODULE:=config.settings.prod}"
: "${DJANGO_ENV:=prod}"
: "${BACKEND_PORT:=8010}"
: "${GUNICORN_BIND:=0.0.0.0:${BACKEND_PORT}}"
: "${GUNICORN_WORKERS:=2}"
: "${GUNICORN_THREADS:=4}"
: "${GUNICORN_TIMEOUT:=120}"
: "${DJANGO_MIGRATE:=1}"
: "${DJANGO_COLLECTSTATIC:=0}"

echo "[backend] DJANGO_ENV=${DJANGO_ENV}"
echo "[backend] DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"

# Attente DB si DB_HOST défini
if [[ -n "${DB_HOST:-}" ]]; then
  echo "[backend] Waiting for DB ${DB_HOST}:${DB_PORT:-5432} ..."
  python - <<'PY'
import os, time, socket
host = os.environ.get("DB_HOST")
port = int(os.environ.get("DB_PORT","5432"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            print("[backend] DB reachable")
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit("[backend] DB not reachable after 60s")
PY
fi

if [[ "${DJANGO_MIGRATE}" == "1" ]]; then
  echo "[backend] Running migrations..."
  python manage.py migrate --noinput
fi

if [[ "${DJANGO_COLLECTSTATIC}" == "1" ]]; then
  echo "[backend] Collecting static..."
  python manage.py collectstatic --noinput
fi

echo "[backend] Starting gunicorn..."
exec gunicorn config.wsgi:application \
  --bind "${GUNICORN_BIND}" \
  --workers "${GUNICORN_WORKERS}" \
  --threads "${GUNICORN_THREADS}" \
  --timeout "${GUNICORN_TIMEOUT}" \
  --access-logfile "-" \
  --error-logfile "-"

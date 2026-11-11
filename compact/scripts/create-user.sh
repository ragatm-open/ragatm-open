#!/usr/bin/env bash
# Crée (idempotent) un utilisateur applicatif SANS droits (ni superuser, ni membre d'aucun workspace),
# pour les tests d'isolation. Variables : RAGATM_USER_EMAIL, RAGATM_USER_PASSWORD (jamais en argument).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
: "${RAGATM_USER_EMAIL:?}"; : "${RAGATM_USER_PASSWORD:?}"
docker compose exec -T -e U_EMAIL="$RAGATM_USER_EMAIL" -e U_PW="$RAGATM_USER_PASSWORD" backend python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
U = get_user_model(); email = os.environ['U_EMAIL']
u, created = U.objects.get_or_create(email=email, defaults={'username': email.split('@')[0]})
u.set_password(os.environ['U_PW']); u.is_active = True; u.is_staff = False; u.is_superuser = False; u.save()
print('created' if created else 'updated', u.pk)"

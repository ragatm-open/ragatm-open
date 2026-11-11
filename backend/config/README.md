# config (Django)

## Emplacement
- Chemin (relatif racine repo) : `backend/config`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Configuration Django (settings, URLs, WSGI/ASGI, etc.).

## Contenu
- `settings/` : settings par environnement (dev/prod)

## Conventions
- secrets via variables d’environnement (pas en dur)
- config DB Postgres + pgvector

## commandes
cd ~/ragATM/backend
source ../.venv/bin/activate
pytest -q

# Développement local

Objectif : monter la stack RagATM en local pour développer. Public : développeuses et
développeurs qui découvrent le dépôt. La procédure décrite est celle réellement
implémentée par `scripts/dev/up.sh` (le code fait foi).

En dev, seuls **Postgres + pgvector** tournent dans Docker ; le **backend Django** et le
**frontend Next.js** s'exécutent en processus locaux (pas de conteneur). Aucun Redis ni
Ollama n'est lancé par le compose dev.

## Prérequis

| Outil | Version attendue | Rôle |
|---|---|---|
| Python | 3.12 (venv) | Backend Django + outils. `python3-venv` requis (`apt install python3.12-venv python3-pip`). |
| Node.js | 20 | Frontend Next.js (`npm`). |
| Docker | récent, avec `docker compose` | Héberge Postgres + pgvector en loopback. |

`up.sh` vérifie la présence de `python3`, `docker`, `node`, `npm` et échoue tôt avec un
message actionnable si l'un manque.

## Chemin en une commande (recommandé)

Depuis la racine du dépôt :

```bash
./scripts/dev/up.sh
```

Ce que le script fait, dans l'ordre :

1. **Contrôle des ports** dev (loopback) : refuse de démarrer si 6811 (backend) ou 6300
   (frontend) est déjà occupé, avec la commande de diagnostic à lancer.
2. **Postgres dockerisé** : `docker compose -f deploy/docker/dev/docker-compose.yml up -d`
   (image `pgvector/pgvector:pg16`, publié sur `127.0.0.1:65432`). L'extension `vector`
   est créée au premier démarrage par `infra/postgres/initdb/001_pgvector.sql`.
3. **Environnement Python** : crée `.venv/` si absent (ou le recrée s'il est cassé), puis
   installe le backend en editable avec les extras : `pip install -e backend[dev,ocr,tables]`.
4. **Migrations** : `python manage.py migrate --noinput` (avec un contournement `--fake`
   documenté pour la migration `rag_core.0011` si la contrainte existe déjà).
5. **Bootstrap si la DB est vide** : crée un superutilisateur de test puis un workspace et
   un RagSpace via `python manage.py bootstrap_workspace` (workspace « WorkspaceTest »,
   RagSpace « Default »). Identifiants par défaut surchargeables via `TEST_EMAIL` /
   `TEST_USERNAME` / `TEST_PASSWORD` (valeurs de dev jetables, à ne jamais réutiliser
   ailleurs).
6. **Dépendances frontend** : `npm install` dans `frontend/` si `node_modules/` est absent.
7. **Lancement** : `python manage.py runserver 127.0.0.1:6811` et
   `npm run dev -- -H 127.0.0.1 -p 6300` (avec `BACKEND_BASE_URL` pointant vers le
   backend). Les logs sont streamés depuis `.dev-logs/{backend,frontend}.log`.

`Ctrl-C` arrête proprement backend + frontend. **Postgres reste up** ; pour l'arrêter :

```bash
docker compose -f deploy/docker/dev/docker-compose.yml down
```

### URLs résultantes

| Service | URL |
|---|---|
| API backend | `http://127.0.0.1:6811/api/v1/` |
| Admin Django | `http://127.0.0.1:6811/admin/` |
| Frontend | `http://127.0.0.1:6300/` |
| Healthcheck | `http://127.0.0.1:6811/api/health/` |

## Politique des ports dev

Tous les services dev écoutent **uniquement en loopback** (`127.0.0.1`) sur des ports
dédiés RagATM, pour éviter les collisions multi-projets :

| Service | Port dev | Variable de surcharge |
|---|---|---|
| Frontend | `6300` | `DEV_FRONTEND_PORT` / `DEV_FRONTEND_HOST` |
| API backend | `6811` | `DEV_BACKEND_PORT` / `DEV_BACKEND_HOST` |
| Postgres | `65432` | `DEV_POSTGRES_PORT` / `DEV_POSTGRES_HOST` |
| Redis (debug) | `66379` | `DEV_REDIS_PORT` |

Le compose dev (`deploy/docker/dev/docker-compose.yml`) ne lance **que Postgres** : backend et
frontend tournent en processus locaux, aucun autre conteneur n'est démarré.

On **n'utilise jamais** les ports génériques `3000` / `5432` / `6379` / `8010` /
`11434` comme ports hôte partagés, et **rien n'écoute en `0.0.0.0`**. Voir la table
complète dans [../10-reference/ports.md](../10-reference/ports.md).

## Alternative manuelle (pas à pas)

Utile pour comprendre chaque étape ou déboguer un blocage. Depuis la racine :

```bash
# 1. Postgres dev (loopback 65432)
docker compose -f deploy/docker/dev/docker-compose.yml up -d

# 2. venv + dépendances backend
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e "backend[dev,ocr,tables]"

# 3. Migrations (le compose dev publie Postgres sur 65432)
cd backend
export DJANGO_ENV=dev
export DJANGO_SETTINGS_MODULE=config.settings.dev
export DB_HOST=127.0.0.1 DB_PORT=65432
python manage.py migrate

# 4. Superutilisateur + workspace/RagSpace de départ (cf. premier-lancement.md)
../scripts/dev/set_admin.sh
python manage.py bootstrap_workspace --email <email> --workspace WorkspaceTest --ragspace Default

# 5. Backend
python manage.py runserver 127.0.0.1:6811
```

Dans un second terminal :

```bash
cd frontend
npm install
BACKEND_BASE_URL=http://127.0.0.1:6811 npm run dev -- -H 127.0.0.1 -p 6300
```

## Arrêt et remise à zéro

| Action | Commande |
|---|---|
| Arrêt propre backend + frontend + Postgres dev | `./scripts/dev/stop_dev.sh` |
| Arrêt Postgres seul (données conservées) | `docker compose -f deploy/docker/dev/docker-compose.yml down` |
| Reset complet de la DB dev (destructif, confirmation `RESET`) | `./scripts/dev/reset_db.sh` |
| Recréer le venv + relancer | `./scripts/dev/up_rebuild.sh` |

`stop_dev.sh` est idempotent et prudent : il ne tue jamais un process tiers incertain
(un `:3000` appartenant à un autre projet est laissé intact). `reset_db.sh` refuse tout
signal prod/staging avant toute opération destructive.

Pour aller plus loin : liste complète des commandes utiles dans
[../10-reference/commandes.md](../10-reference/commandes.md), configuration minimale dans
[configuration.md](configuration.md), et première prise en main dans
[premier-lancement.md](premier-lancement.md). En cas de blocage, voir
[depannage.md](depannage.md).

## Références d'implémentation

- `scripts/dev/up.sh`
- `scripts/dev/stop_dev.sh`
- `scripts/dev/reset_db.sh`
- `scripts/dev/set_admin.sh`
- `deploy/docker/dev/docker-compose.yml`
- `infra/postgres/initdb/001_pgvector.sql`
- `backend/pyproject.toml`
- `backend/manage.py`

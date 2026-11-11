# Ports par environnement

Référence des ports réseau de RagATM. **Source unique** : les autres docs pointent ici.
Public : exploitants et développeurs. Le CODE/compose fait foi (`backend/config/settings/base.py`,
`deploy/docker/`, `scripts/dev/`).

**Politique de ports**  : en développement, tout écoute en
**loopback `127.0.0.1`** sur des **ports dédiés**. On n'utilise **jamais** `3000`, `5432`,
`6379`, `8010`, `11434`, ni un bind `0.0.0.0`. Lors d'un déploiement, **aucun service ne publie
de port public** : seul le frontend est exposé en loopback, derrière un reverse-proxy.

---

## Développement (loopback)

Stack lancée par `scripts/dev/up.sh` : Postgres conteneurisé, backend Django et frontend Next
en processus locaux. Tous les ports sont surchargeables (`DEV_BACKEND_PORT/HOST`, `DEV_FRONTEND_*`,
`DEV_POSTGRES_*`…).

| Service | Hôte:Port | Variable | Note |
|---|---|---|---|
| Frontend (Next) | `127.0.0.1:6300` | `DEV_FRONTEND_PORT` | Écran principal. |
| API backend (runserver) | `127.0.0.1:6811` | `DEV_BACKEND_PORT` | Django dev. |
| PostgreSQL (conteneur) | `127.0.0.1:65432` | `DEV_POSTGRES_PORT` | `pgvector/pgvector:pg16`. |
| Redis (debug) | `127.0.0.1:66379` | — | Optionnel. |
| AI Gateway | `ai-gateway:8100` | `AI_GATEWAY_BASE_URL` | Hors stack ; service Compose `ai-gateway` ou `http://127.0.0.1:8100` en local. Via AI Gateway Pool : `http://ai-gateway-pool:8080`. |

Le compose dev (`deploy/docker/dev/docker-compose.yml`) ne lance **que Postgres** : aucun autre
service conteneurisé, donc aucun autre port hôte.

---

## Overrides de debug (opt-in, jamais sur une instance publique)

Bindings loopback activables via `docker-compose.debug.yml` :

| Service | Hôte:Port (debug) |
|---|---|
| PostgreSQL | `127.0.0.1:55432` |
| Redis | `127.0.0.1:6379` |
| Ollama | `127.0.0.1:11434` |
| Backend | `127.0.0.1:8010` |

---

## Ports internes conteneurs (référence)

Ces valeurs sont les ports **dans** les conteneurs (non publiés tels quels) : Postgres `5432`,
Redis `6379`, Ollama `11434`, backend gunicorn `8010`, AI Gateway `8100`.

RagATM n'embarque **aucun service de stockage objet** : les documents vivent sur le volume local
`media`, donc aucun port associé. Voir
[../01-overview/decisions/ADR-001-stockage-documentaire.md](../01-overview/decisions/ADR-001-stockage-documentaire.md).

---

## Références d'implémentation

- `scripts/dev/up.sh`, `deploy/docker/dev/docker-compose.yml`
- `deploy/docker/docker-compose.yml` + `docker-compose.prod.yml` + `docker-compose.debug.yml`

# Configuration

Trois fichiers, trois rôles :

| Fichier | Rôle | Versionnable |
|---|---|---|
| `.env` | secrets générés, exposition (port, origine), budgets mémoire, digest TEI | **non** (0600) ; modèle : `.env.example` |
| `config/backend.env` | configuration non secrète de RAGatm (backend, worker, beat) | oui |
| `config/gateway.env` | configuration non secrète d'AI Gateway Open | oui |

## `.env`

| Variable | Rôle |
|---|---|
| `COMPOSE_PROJECT_NAME` | nom du projet compose (préfixe des conteneurs/volumes) |
| `COMPACT_FRONTEND_PORT` | seul port publié (127.0.0.1) |
| `FRONTEND_APP_ORIGIN`, `DJANGO_CSRF_TRUSTED_ORIGINS` | origine vue par le navigateur |
| `DJANGO_SECRET_KEY`, `DB_*`, `REDIS_PASSWORD`, `RAG_OPS_METRICS_TOKEN`, `DJANGO_CACHE_URL`, `CELERY_*` | secrets et URL dérivées (gen-secrets.sh) |
| `AI_GATEWAY_RERANK_BACKEND_IMAGE_DIGEST` | digest de l'image TEI (source unique : compose + Gateway) |
| `TEI_HF_HUB_OFFLINE` | 1 en nominal (aucun appel au Hub) |
| `COMPACT_MEM_*`, `COMPACT_WORKER_CONCURRENCY`, `OLLAMA_KEEP_ALIVE` | budgets |
| `APP_GIT_SHA`, `AI_GATEWAY_BUILD_SHA` | provenance de build (vides = `not_reported`) |

## `config/backend.env` — points clés

- `AI_GATEWAY_*` : Gateway obligatoire, URL de service `http://ai-gateway:8100` pour les trois
  usages (génération, embeddings, rerank), `AI_GATEWAY_POOL_HOSTS` vide (accès direct, pas de Pool),
  `AI_GATEWAY_FALLBACK_PROVIDER=none`.
- Modèles par défaut : `DEFAULT_RAGSPACE_CHAT_MODEL=compact-generation-8`,
  `DEFAULT_RAGSPACE_EMBEDDING_MODEL=fr-en-retrieval`, `RAG_EMBEDDING_DIM=768`.
- Reranker CPU : `RAG_RERANKER_POOL_K=20`, `RAG_RERANKER_TIMEOUT_SECONDS=20`, `fail_open_rrf`.
- Délais CPU : `AI_GATEWAY_TIMEOUT_SECONDS=300`, `GUNICORN_TIMEOUT=600`.
- `RAG_INGEST_ASYNC_DEFAULT=1` : ingestion par le worker Celery.
- Capacités OFF : `DOCUMENT_OCR_ENABLED=0`, `DOCUMENT_VISUAL_ANALYSIS_ENABLED=0`,
  `DOCUMENT_AUDIO_INGEST_ENABLED=0`, `DOCUMENT_AUDIO_GLOBAL_ENABLED=0`.

Référence complète des variables RAGatm : `../docs/10-reference/configuration.md`.
Toute variable `OPENAI_*`, `ANTHROPIC_*`, `MISTRAL_*`… fait échouer le démarrage (`config.E009`).

## `config/gateway.env` — points clés

Registres stricts (`*_STRICT_REGISTRY=1`), `OLLAMA_GENERATE_MODEL=compact-generation-8`,
`OLLAMA_EMBEDDING_MODEL=fr-en-retrieval`, dimension 768, concurrence 2/2/2, corps JSON ≤ 2 Mio,
reranker en boucle locale `http://127.0.0.1:8300`, ASR/Vision OFF.
Référence : `doc/03-configuration.md` du dépôt ai-gateway-open (`../../ai-gateway-open/doc/03-configuration.md` dans la disposition par défaut).

## Réseau

| Réseau | Type | Membres |
|---|---|---|
| `edge` | pont standard | frontend (publication du port) |
| `app` | **interne** | frontend, backend |
| `data` | **interne** | backend, worker, beat, db, redis |
| `ai` | **interne** | backend, worker, ai-gateway (+ reranker via l'espace réseau de la Gateway) |
| `inference` | **interne** | ai-gateway, ollama — depuis une version antérieure, Ollama n'est joignable **que** par la Gateway (le backend et le worker ne peuvent pas contourner le registre strict de modèles) |

Un réseau interne n'a pas de route de sortie : aucun des services d'inférence ou de données ne
peut joindre Internet en exécution nominale.

## Disposition des sources (`RAGATM_OPEN_DIR`, `AI_GATEWAY_OPEN_DIR`)

Les contextes de build `backend` et `frontend` sont résolus depuis `RAGATM_OPEN_DIR` (défaut `..`, la racine du dépôt
`ragatm-open` qui contient ce kit) ; le contexte `ai-gateway` depuis `AI_GATEWAY_OPEN_DIR` (défaut `../../ai-gateway-open`,
un clone du dépôt `ai-gateway-open` placé à côté de `ragatm-open/`). Pour une autre disposition, poser ces variables dans `.env`.

## Accès depuis l'hôte Docker

`internal: true` supprime la route de sortie d'un réseau, mais l'hôte Docker lui-même route vers ses
ponts : un processus local peut atteindre les services internes par leur IP de pont (équivalent aux
droits `docker exec`). Ce n'est pas une exposition LAN. Pour l'interdire, l'exploitant ajoute des
règles de pare-feu hôte (`DOCKER-USER`/`INPUT`), voir le dossier de qualification interne ; aucune règle
n'est posée par cette distribution.

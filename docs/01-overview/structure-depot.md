# Structure du dépôt

Monorepo RagATM. Vue d'ensemble des répertoires et de leur rôle réel.

```
ragatm/
├── backend/                 Backend Django + DRF
│   ├── api/v1/              Couche HTTP (views/urls/serializers) exposée sous /api/v1/
│   ├── apps/                Apps Django (voir ci-dessous)
│   ├── benchmarks/          Datasets, baselines, profils, seuils du benchmark RAG
│   ├── config/              settings/ (base·dev·prod), urls, celery, middleware
│   ├── media/               Originaux des documents (rag_docs/, corpus_docs/) — volume local
│   ├── tests/               Arbre de tests « legacy » (pytest)
│   ├── Dockerfile · entrypoint.sh · manage.py · pyproject.toml
├── frontend/                Frontend Next.js 14 (App Router)
│   ├── src/app/             Routes + reverse-proxy /api/[...path]
│   ├── src/components/      Shell, design system (ui/), contexte workspace
│   ├── src/lib/             Client API, auth, corpus, thème
│   ├── vitest.config.ts · package.json · Dockerfile
├── services/                Services annexes
│   ├── ai_gateway/          Service AI Gateway (FastAPI v0.1.0 : /health + /v1/generate)
│   ├── llm_local/           Scaffold planifié (Dockerfile vide — non implémenté)
│   └── ocr/                 Scaffold (OCR réel intégré au backend)
├── deploy/                  Déploiement
│   ├── docker/              docker-compose.yml (base) + overrides (prod/staging/…)
│   ├── nginx/               nginx.conf + conf.d/ (hôte)
├── infra/                   Postgres (initdb) + Redis pour dev/prod
├── scripts/                 dev/ · prod/ · staging/ (bootstrap, up, deploy, reembed…)
├── shared/                  Contrats : OpenAPI (contracts/openapi/), events, action_intents
├── tests/                   Arbre de tests « racine » : tests/tests_root/ + tests/frontend/
├── tools/                   Outillage divers
├── docs/                    Cette documentation
├── .env.example · .env.staging.example   Modèles d'environnement (valeurs d'exemple)
├── Makefile · redocly.yaml · README.md
```

## Apps Django (`backend/apps/`)

| App | Rôle | Note |
|---|---|---|
| `accounts` | authentification (cookie-token) + permission propriétaire global | pas de modèle |
| `workspaces` | workspaces + memberships (RBAC) | racine de tenancy |
| `rag_core` | moteur RAG : RagSpaces, documents, chunks, retrieval, ingestion, résumés, analyses, benchmark, Core façade | app centrale |
| `chat` | threads/messages, orchestration chat (sync + SSE), mémoire | |
| `corpus` | corpus métier partagé (`domain_corpus_*`) | admin superuser |
| `audit` | `RagQueryLog`, `ActionAuditEvent`, coûts LLM, purge de rétention | |
| `llm_router` | routage génération/embeddings, capabilities, checks IA | |
| `actions` | routeur phrase→action | **plain package** (hors INSTALLED_APPS), flag `ACTIONS_ROUTER_ENABLED` |
| `help` | catalogue d'aide `/help` déterministe | **plain package** |
| `documents` | — | app **vide** (couche HTTP dans `api/v1/documents/`) |
| `billing` | — | app **vide** (placeholder) |

## Deux arbres de tests backend

Le backend a une **suite de tests unique**, `tests/tests_root/`, lancée via
`make test-backend-all`. (Deux arbres coexistaient historiquement ; ils ont été fusionnés.)
Détails : [../08-testing/strategie.md](../08-testing/strategie.md).

## Références d'implémentation

- `backend/apps/` — apps Django
- `frontend/src/` — code frontend
- `deploy/docker/` — orchestration
- `shared/contracts/openapi/` — spécification API

# tests/ — suite de tests racine RAG-ATM

Suite de tests transverse couvrant **backend** (Django/DRF, pytest) et
**frontend** (Next.js/React, Vitest + Testing Library).

```
tests/
├── backend/            # pytest (Django) — 176 tests
│   ├── pytest.ini      # rootdir + DJANGO_SETTINGS_MODULE
│   ├── conftest.py     # sys.path -> ../../backend, embeddings stub, fixtures
│   ├── unit/           # fonctions pures (policies, chunking, rerank, merge, dates…)
│   ├── integration/    # services + DB (ingest, query_rag, chat, mémoire, SSE, news)
│   └── http_api/       # endpoints DRF (auth, chat, documents, prompts, ACL…)
└── frontend/           # Vitest + RTL — 74 tests
    ├── setup.ts
    ├── lib/            # auth, storage, api, sse, utils admin (logique pure)
    └── components/     # SourceList, ChatComposer, ChatHeader, Document*…
```

## Backend (pytest)

Prérequis : PostgreSQL **avec l'extension pgvector** accessible (les tests
d'intégration créent une vraie base de test). Les embeddings sont forcés en mode
**stub déterministe** (`EMBEDDINGS_PROVIDER=stub`) par le conftest — aucun
serveur Ollama/OpenAI requis.

```bash
cd tests/backend
source ../../.venv/bin/activate
python -m pytest -q                 # toute la suite
python -m pytest unit/ -q           # uniquement les tests purs (sans DB, rapides)
python -m pytest integration/ -q    # services + DB
python -m pytest http_api/ -q       # endpoints HTTP
```

Le conftest ajoute `../../backend` au `sys.path` et fixe
`DJANGO_SETTINGS_MODULE=config.settings.dev` (via `pytest.ini`).

## Frontend (Vitest)

```bash
cd frontend
npm install        # installe vitest + @testing-library/* (déjà en devDependencies)
npm test           # lance toute la suite (../tests/frontend/**)
npm run test:watch # mode watch
```

> La suite frontend vit hors du package `frontend/`. Le script `pretest`
> (`scripts/link-test-deps.mjs`) recrée un lien `tests/frontend/node_modules ->
> frontend/node_modules` (gitignoré) afin que la résolution des imports
> (`vitest`, `@testing-library/*`) fonctionne. L'alias `@/ -> frontend/src` est
> configuré dans `frontend/vitest.config.ts`.

## Bugs corrigés (verrouillés par ces tests)

Trois anomalies détectées pendant l'écriture des tests ont été corrigées et
sont désormais couvertes :

- **Ingest HTTP synchrone** (`DocumentIngestView`/`DocumentUploadView`) :
  appelaient la pipeline avec de mauvais kwargs → 500. Corrigé — la vue texte
  délègue à `ingest_document_from_text`, la vue upload à
  `reindex_existing_document` (storage_file déjà persisté), et la branche async
  texte persiste l'original pour la réindexation Celery. (cf.
  `http_api/test_documents_api.py::test_ingest_sync_*`)
- **`RagSpaceStatsView`** comptait par statut en minuscules alors que
  `DocumentStatus` est en MAJUSCULES (`TextChoices`) → compteurs toujours à 0.
  Corrigé en utilisant l'enum. (cf.
  `http_api/test_ragspaces_api.py::test_stats_counts_ready_documents`)
- **Drift d'import** : `from apps.rag_core.services.ingest import
  ingest_plain_text` cassé (la fonction vivait dans `ingest_facade`). Corrigé
  par une ré-exposition unique côté package `ingest` (sans import circulaire).

## Notes

- **URLs avec slash final** : `APPEND_SLASH=False` côté backend → toute URL doit
  finir par `/`. Tous les tests ici utilisent le slash final.
- **Embeddings stub** : la recherche vectorielle est testée sur son contrat
  technique, pas sa pertinence sémantique (vecteurs déterministes mais non
  sémantiques). Les cas `no_answer` reposent donc sur des espaces vides /
  l'isolation, pas sur des questions « hors sujet ».

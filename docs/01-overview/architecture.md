# Architecture

Architecture réelle de RagATM, fondée sur les imports, services et flux du code. Les
diagrammes reflètent le chemin réellement exécuté (pas d'intention non implémentée).

## Architecture logique

```mermaid
flowchart TB
    subgraph Client
        FE[Frontend Next.js<br/>App Router]
    end
    subgraph NextServer[Serveur Next.js]
        PROXY[Reverse-proxy /api/*<br/>route.ts]
    end
    subgraph Backend[Backend Django + DRF]
        API[api/v1 — DRF views]
        CHAT[apps.chat]
        RAG[apps.rag_core<br/>ingestion / retrieval]
        CORPUS[apps.corpus<br/>corpus métier]
        LLM[apps.llm_router]
        AUDIT[apps.audit]
    end
    subgraph Data
        PG[(PostgreSQL + pgvector)]
        REDIS[(Redis)]
        MEDIA[Volume media<br/>originaux]
    end
    subgraph Async
        WORKER[Celery worker<br/>celery + analytics]
    end
    subgraph IA[IA — hors stack]
        GW[AI Gateway<br/>ai-gateway:8100]
        OLL[Ollama GPU]
    end

    FE --> PROXY --> API
    API --> CHAT --> RAG
    RAG --> CORPUS
    CHAT --> LLM
    RAG --> LLM
    RAG --> PG
    RAG --> MEDIA
    API --> AUDIT --> PG
    API -. enfile .-> REDIS
    REDIS --> WORKER --> RAG
    LLM -->|génération / embeddings| GW
    GW --> OLL
```

Points de vérité :

- Le **navigateur ne parle qu'à `/api/*` en same-origin** ; un reverse-proxy Next
  (`src/app/api/[...path]/route.ts`) relaie vers le backend (`BACKEND_BASE_URL`, variable
  serveur uniquement — aucune variable `NEXT_PUBLIC_*`).
- La **génération IA passe par l'AI Gateway** quand elle est activée ; RagATM n'appelle
  jamais Ollama en direct en environnement officiel (system check `llm_router.E003` dès que la
  Gateway est éteinte). Il
  n'existe aucun provider externe ni repli.
- Les **originaux** des documents sont stockés sur un **volume local `media`** (Django
  FileField, `MEDIA_ROOT`). RagATM n'utilise **aucun stockage objet** : décision explicite,
  voir [decisions/ADR-001-stockage-documentaire.md](decisions/ADR-001-stockage-documentaire.md).

## Architecture d'exécution (conteneurs)

```mermaid
flowchart LR
    NGINX[Nginx hôte<br/>80/443] --> FEC[frontend<br/>127.0.0.1:6100/6200]
    FEC --> BE[backend<br/>gunicorn]
    BE --> DB[(db<br/>pgvector/pg16)]
    BE --> RD[(redis)]
    W1[worker celery] --> RD
    W2[worker_analytics] --> RD
    W1 --> DB
    W2 --> DB
    BE -.-> GW[AI Gateway]
```

- **Nginx** (hôte) est le seul point d'entrée public ; il proxifie vers le frontend en
  loopback. Aucun autre service ne publie de port public (db/redis/ollama/workers).
- Deux workers Celery : queue `celery` (ingestion, résumés, reindex) et queue `analytics`
  (analyses corpus globales). Voir [../03-backend/taches-asynchrones.md](../03-backend/taches-asynchrones.md).
- Détails d'orchestration : la documentation d'exploitation (hors périmètre de cette édition).

## Flux d'ingestion

```mermaid
flowchart LR
    UP[Upload] --> VAL[Validation<br/>taille / MIME / format]
    VAL --> STO[Stockage original<br/>media]
    STO --> DEC{sync ou async ?}
    DEC -->|OCR/Excel| CEL[Celery]
    DEC -->|défaut sync| EXT[Extraction]
    CEL --> EXT
    EXT --> CHK[Chunking<br/>2500/250]
    CHK --> EMB[Embeddings<br/>dim 768]
    EMB --> IDX[(pgvector)]
    IDX --> ST[READY / FAILED]
```

Détail : [../03-backend/ingestion-extraction.md](../03-backend/ingestion-extraction.md).

## Flux de retrieval

```mermaid
flowchart TB
    Q[Question] --> RW[Réécriture<br/>mode explicatif]
    RW --> FTS[FTS ts_rank]
    RW --> VEC[Vectoriel pgvector<br/>cosine]
    FTS --> RRF[Fusion RRF K=60]
    VEC --> RRF
    RRF --> BI[Fusion bi-source<br/>privé + corpus métier]
    BI --> DIV[Diversité<br/>max 2/groupe]
    DIV --> POL{should_answer ?}
    POL -->|pool vide| NA[no-answer]
    POL -->|sinon| PR[Prompt + sources]
    PR --> GEN[Génération LLM<br/>en aval, via Gateway]
```

Détail : [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md) et
[../06-ai/answerability.md](../06-ai/answerability.md).

## Flux chat

Le chemin complet (interceptions déterministes, mémoire, retrieval, génération, streaming,
persistance) est décrit dans [../03-backend/chat-generation.md](../03-backend/chat-generation.md).

## Frontières de permissions

Isolation à trois niveaux : propriétaire global (superuser) ⊃ workspace (RBAC) ⊃ RagSpace
(filtrage par requête). Voir [../03-backend/permissions-isolation.md](../03-backend/permissions-isolation.md)
et la documentation d'exploitation (hors périmètre de cette édition).

## Dépendances critiques

| Dépendance | Rôle | Câblée |
|---|---|---|
| PostgreSQL + pgvector | données + index vectoriel/FTS | oui |
| Redis | broker Celery + cache optionnel | oui |
| Celery | ingestion/résumés/analyses asynchrones | oui (pas de conteneur `beat`) |
| AI Gateway | génération et embeddings | hors stack (service `ai-gateway`) |
| Ollama | moteur LLM local derrière la Gateway | hôte GPU uniquement |
| Volume local `media` | stockage des originaux (`MEDIA_ROOT`) | oui — seul stockage documentaire ([ADR-001](decisions/ADR-001-stockage-documentaire.md)) |

## Références d'implémentation

- `frontend/src/app/api/[...path]/route.ts` — reverse-proxy
- `backend/config/urls.py`, `backend/api/v1/` — routage
- `backend/apps/rag_core/services/query/orchestrator.py` — retrieval
- `backend/apps/chat/services/chat_service.py` — chat
- `backend/apps/llm_router/router.py` — routage LLM
- `deploy/docker/docker-compose.yml` + overrides — exécution

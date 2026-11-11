# Tâches asynchrones (Celery)

Objectif : décrire l'exécution asynchrone de RagATM (Celery + Redis) : file d'attente, tâches,
politique de retry, planification. Public : développeurs backend, opérateurs.

## Infrastructure

- Application Celery `ragATM` (`config/celery.py`), config lue depuis les settings Django sous le
  namespace `CELERY_`. Autodécouverte des `tasks.py`.
- **Broker & backend de résultats** : Redis (`CELERY_BROKER_URL`, défaut de code
  `redis://redis:6379/0` ; `CELERY_RESULT_BACKEND` = même URL par défaut). Les
  environnements déployés forcent la DB selon la convention — **DB 0 = prod**, **DB 1 =
  staging**, **DB 2 = cache** : voir
  [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md#convention-de-numérotation-des-db-redis).
- **Mode tests/CI** : `CELERY_TASK_ALWAYS_EAGER=1` exécute les tâches **inline** (pas de worker).
- **Limites de temps** (globales) : soft `540 s` / hard `600 s`
  (`CELERY_TASK_SOFT_TIME_LIMIT` / `CELERY_TASK_TIME_LIMIT`). La file `analytics` relève ces
  bornes à `3300 s` / `3600 s`.

## Files d'attente

| File | Tâches | Notes |
|---|---|---|
| `celery` (défaut) | ingestion, réindexation par document, résumés, reap, benchmark, reformulation | limites 540/600 s |
| `analytics` (dédiée) | `run_corpus_analysis_job`, `purge_expired_analysis_jobs_task` | routées via `CELERY_TASK_ROUTES` ; limites 3300/3600 s |
| `reindex` (dédiée) | `run_reindex_job_task`, `reindex_job_item_task`, `reap_stale_reindex_items_task` | réindexation **globale** durable d'un RagSpace ; voir [reindexation-jobs.md](reindexation-jobs.md) |
| `ingest_ifc` (dédiée, **par document**) | `ingest_document_task`, `reindex_document_task`, `reindex_job_item_task` quand le document est une maquette `.ifc` | routage `apply_async(queue=)` via `services/ingest/queues.py` (invisible de `CELERY_TASK_ROUTES`) ; consommée par `worker_ifc` (profil `ifc`, extra `[ifc]`, concurrence 1, prefetch 1 — qualification interne) ; n'activer `DOCUMENT_IFC_ENABLED` qu'une fois ce worker démarré ; les relances `self.retry` restent sur la file ; voir docker-compose.md |
| `ingest_dwg` (dédiée, **par document**) | `ingest_document_task`, `reindex_document_task`, `reindex_job_item_task` quand le document est un dessin `.dwg` | même routage `apply_async(queue=)` (`services/ingest/queues.py`) ; consommée par `worker_dwg` (profil `dwg`, image `WITH_DWG=1` portant GNU LibreDWG 0.14, concurrence 1, prefetch 1 — qualification interne) ; le worker convertit le DWG en DXF temporaire (sous-processus) puis applique le DxfExtractor ; n'activer `DOCUMENT_DWG_ENABLED` qu'une fois ce worker démarré ; les relances `self.retry` restent sur la file ; voir docker-compose.md |
| `ingest_audio` (dédiée, **par document**) | `ingest_document_task` d'un audio quand `DOCUMENT_AUDIO_INGEST_ENABLED=1`, **et toujours** `audio_transcription_step` (étapes courtes re-planifiées : préparation ffmpeg, dépôt/relevé/suppression des jobs ASR Gateway) | `worker_audio`, concurrence 1 (≤ 1 job ASR en vol). Chien de garde `audio-transcription-watchdog` (beat 300 s, file `maintenance`). Le document reste PENDING pendant l'ASR (jamais tué par le reaper). Voir [ingestion-audio.md](ingestion-audio.md) |
| `ingest_visual` (dédiée, **par document**) | `ingest_document_task` pour une image autonome ou une reprise primaire `visual_primary=True` ; `visual_enrichment_task` (VISUAL-ENRICHMENT-DEFERRED) | consommée par `worker_visual` (staging, concurrence 1). Un PDF/DOCX/PPTX n'y va PLUS pour sa passe primaire : READY sur la file par défaut, puis enrichissement différé (références seulement, relance au niveau du visuel, no-op sur génération périmée) ; voir [ingestion-extraction](ingestion-extraction.md) |

## Tâches

| Tâche | Module | Rôle | Retry |
|---|---|---|---|
| `ingest_document_task` | `rag_core/tasks.py` | Ingestion async d'un `Document` (extraction + chunking + embeddings). `PENDING→INGESTING→READY/FAILED`. Émet des événements `action.*`. Param `ocr` force l'OCR local pour ce run. | max 3, backoff exp, erreurs transitoires |
| `reindex_document_task` | `rag_core/tasks.py` | Réindexation d'un document existant (`reindex_existing_document`). | max 3, backoff exp |
| `run_reindex_job_task` | `rag_core/tasks.py` | Orchestre un `RagSpaceReindexJob` : `pending→running`, fan-out 1 item/document. File `reindex`. Voir [reindexation-jobs.md](reindexation-jobs.md). | aucun retry |
| `reindex_job_item_task` | `rag_core/tasks.py` | Réindexe **un** document d'un job (idempotent, lease, atomique par doc). File `reindex`. | aucun retry (échec → item `FAILED`) |
| `reap_stale_reindex_items_task` | `rag_core/tasks.py` | Items `running` au lease expiré → `interrupted` ; documents `INGESTING` orphelins → `FAILED`. Beat `*/5 min`. N'efface aucun chunk. | — |
| `generate_document_summary_task` | `rag_core/tasks.py` | Résumé long-document (map-reduce). `PENDING→READY/FAILED`. | max 3, erreurs provider transitoires |
| `reap_stale_summaries_task` | `rag_core/tasks.py` | Passe les résumés `PENDING` dépassant `SUMMARY_PENDING_TIMEOUT_S` en `FAILED`. | — |
| `run_corpus_analysis_job` | `rag_core/tasks.py` | Exécute un `CorpusAnalysisJob` (`analysis.runner.execute_job`). Ne lève jamais : tous les issues sont terminaux. File `analytics`. | aucun retry |
| `purge_expired_analysis_jobs_task` | `rag_core/tasks.py` | Purge les jobs terminaux expirés. File `analytics`. | aucun retry |
| `run_benchmark` | `rag_core/tasks_benchmark.py` | Benchmark de fumée nocturne sur les `RagBenchmarkQuestion` actives. | — |
| `generate_rephrasing_suggestions_task` | `chat/tasks.py` | Suggestions de reformulation (relit la question en base). `pending→ready/failed`. Sous `RAG_REPHRASING_SUGGESTIONS_ENABLED` (off). Limites soft/hard 5/8 s. | aucun retry |

Notes :
- L'ingestion **corpus métier est synchrone** (pas de Celery) — voir
  [corpus-metier.md](corpus-metier.md).
- La réindexation d'un RagSpace (`POST rag-spaces/<id>/reindex/`) s'exécute inline (eager) ou
  s'enfile ; `503` si le broker est indisponible, `202` quand la tâche est mise en file.

## Politique de retry

- Retry uniquement sur **erreurs transitoires** (indisponibilité provider/réseau), `max_retries=3`,
  **backoff exponentiel**.
- Les erreurs déterministes (format non supporté, contrainte violée) échouent immédiatement
  (`FAILED`) sans retry.
- L'émission d'audit est **fail-open** : un échec de journalisation ne casse jamais la tâche.

## Planification (beat)

Deux tâches planifiées, définies dans `config/celery.py` :

| Clé | Tâche | Cadence |
|---|---|---|
| `rag_benchmark_nightly` | `apps.rag_core.tasks_benchmark.run_benchmark` | tous les jours à **02:00** |
| `reap_stale_summaries` | `apps.rag_core.tasks.reap_stale_summaries_task` | toutes les **10 min** |

> **Limite connue** : aucun conteneur `beat` n'est défini dans les fichiers Compose. Le scheduler
> Celery Beat doit être lancé séparément pour que ces tâches planifiées s'exécutent. Voir
> [../10-reference/limites-connues.md](../10-reference/limites-connues.md).

Voir aussi [../10-reference/feature-flags.md](../10-reference/feature-flags.md) pour les flags
associés (`RAG_INGEST_ASYNC_DEFAULT`, `SUMMARY_ASYNC_ENABLED`,
`RAG_REPHRASING_SUGGESTIONS_ENABLED`, `CORPUS_ANALYTICS_*`).

## Références d'implémentation

- `backend/config/celery.py`, `backend/config/settings/base.py` (bloc `CELERY_*`)
- `backend/apps/rag_core/tasks.py`, `backend/apps/rag_core/tasks_benchmark.py`
- `backend/apps/chat/tasks.py`
- `backend/apps/rag_core/analysis/runner.py`

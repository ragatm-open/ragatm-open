# Roadmap & dette technique

Feuille de route produit et dette technique de RagATM. Public : mainteneurs. Le CODE fait foi ;
les items résolus/faux de l'ancienne roadmap ont été retirés. Les limites *déjà constatées et
stables* sont décrites dans [../10-reference/limites-connues.md](../10-reference/limites-connues.md) ;
cette page liste le **travail à faire** et son séquencement.

---

## Dette technique (TODO constatés dans le code)

Régressions/écarts à corriger. Voir [../10-reference/limites-connues.md](../10-reference/limites-connues.md)
pour la description détaillée de chacun.

| # | Sujet | Localisation | Sévérité |
|---|---|---|---|
| ~~D1~~ | **Provider de rerank silencieusement dégradé**. **REQUALIFIÉE puis RÉSOLUE côté code** (2026-07-21). La prémisse initiale (« `.env.prod` pose `local` ») n'a **pas été confirmée** : le `.env.prod` local pose `cross_encoder`. `.env.prod` étant gitignoré, **la valeur du serveur de production reste à vérifier hors dépôt**. Le défaut de code, lui, était réel et prouvé : seul le littéral `cross_encoder` était reconnu, toute autre valeur faisait tourner le stub avec `fallback_used=False` (dégradation invisible). Durci : validation explicite `{stub, cross_encoder}`, repli signalé, log traçant `requested_provider` **et** `effective_provider`, et **system check `rag_core.E004`** bloquant au démarrage si `RAG_RERANK_ENABLED=1`. | `services/rerank.py`, `checks_rerank.py` | — |
| ~~D2~~ | **Aucun filtre `status` dans le SQL de retrieval**. **RÉSOLU** (Lot 2 — levée du NO-GO P0-A). La sévérité affichée (« Moyenne ») **sous-estimait le risque** : combiné au repli LLM externe, le défaut permettait l'envoi du contenu d'un document `FAILED` à un tiers. Corrigé par un prédicat canonique (`domain/eligibility.py`) appliqué en SQL avant `LIMIT` sur tous les chemins, une commande d'audit et la matrice de tests D1..D8. | `domain/eligibility.py` | — |
| D3 | **~25 réglages `RAG_*` lus mais jamais définis** (`RAG_MAX_PROMPT_CHARS`, `RAG_RERANK_POOL_K`, `RAG_EXPLAIN_*`, `RAG_INVENTORY_*`, `RAG_NEWS_*`, `RAG_ENABLE_NEWS_HEURISTIC`) → no-op silencieux ; `news_mode` inerte. | `services/query/orchestrator.py`, `settings/base.py` | Moyenne |
| D5 | **Backup/restore non implémentés** (stubs vides) ; pas de backup chiffré des `storage_file`. | `scripts/prod/backup.sh`, `restore.sh` | Élevée (ops) |
| ~~D6~~ | **Pas de conteneur Celery `beat`**. **RÉSOLU CÔTÉ CODE** (Lot 4) : `celery_beat` + `worker_maintenance` dans les 5 stacks officielles, schedule centralisé en settings, verrous distribués, heartbeat observable, purge de rétention enfin ordonnancée. Reste à DÉPLOYER sur les hôtes. | `settings/base.py`, composes | — |
| D7 | **Answerability P1.8 non branchée au live** (outil offline) ; no-answer plafonne via `min_score`. | `benchmark/answerability.py` | Moyenne (qualité) |
| D8 | **`RagQueryLog.request_id` = `CharField(64)`** mais `normalize_request_id` tronque à 200 → `X-Request-ID` long ⇒ `DataError` possible à l'insertion. | `audit/models.py`, `query/settings.py` | Faible |
| D9 | **Migration `rag_core.0011`** : workaround `--fake` dans le bootstrap dev. | `scripts/dev/up.sh` | Faible |
| D10 | **`RAG_HYBRID_ENABLED` déclaré 2×** dans `base.py` (même défaut, redondant). | `config/settings/base.py` | Cosmétique |
| D11 | **Pas d'`unaccent` sur le FTS** → recherche FR limitée. | `infra/postgres/initdb/002_fts.sql` | Faible |
| D12 | **Apps vides** `documents`/`billing` + fichier vide `services/feedback.py` (vestiges). | `apps/documents/`, `apps/billing/`, `services/feedback.py` | Faible |
| D13 | **Pas de scan antivirus des uploads.** | `api/v1/documents/views_write.py` | Sécurité |
| D14 | **`intents/news.llm_route_news_or_none` appelle un LLM sans budget.** | `apps/chat/services/intents/news.py` | Coût |
| D15 | **Tests `ai_gateway` hors CI** ; pas de garde lint/type Python en CI (ni mypy). | `services/ai_gateway/tests`, `.github/workflows/ci.yml` | Moyenne (qualité) |
| ~~D16~~ | **Deux arbres de tests backend** à fusionner. **RÉSOLU** (2026-07-20) : consolidés sous `tests/tests_root`, `tests_legacy` supprimé, config pytest unique au repo root. | `tests/tests_root` | — |
| D17 | **Nom de nœud Celery non unique en dev** : `up.sh` lance le worker avec `-n devworker@%h`, valeur fixe. Deux workers simultanés (ex. `up.sh` relancé sans arrêt du précédent) s'annoncent alors sous le **même nom** ; Celery émet `DuplicateNodenameWarning`, `inspect ping` reçoit deux réponses mais conclut « 1 node online ». La concurrence devient invisible aux outils de supervision, et une commande de contrôle peut atteindre l'un ou l'autre worker indifféremment. Observé le 20/07/2026 : un worker orphelin (PPID 1) tournait depuis 26 h à côté du worker légitime. Piste : suffixer le nom (`-n devworker-$$@%h` ou identifiant de session). | `scripts/dev/up.sh` | Moyenne (exploitation) |
| D18 | **Les cibles `make test-backend-*` présument l'environnement `up.sh`** : `test-backend-root/-all` n'exportent que `EMBEDDINGS_PROVIDER`. Elles dépendent donc de `DB_HOST`/`DB_PORT`/`DJANGO_SECRET_KEY` déjà présents dans l'environnement — fournis par `up.sh` en dev et par le bloc `env:` en CI. Dans un **worktree frais sans `.env`** (gitignoré, non copié), `make test-backend-all` échoue massivement : connexions PostgreSQL sur le port par défaut `5432` (≠ `65432` dev) et gardes prod `config.settings.prod` sans `DJANGO_SECRET_KEY`. Constaté le 20/07/2026 pendant l'harmonisation pytest (1215 erreurs + 4 échecs, **tous environnementaux, aucune régression de code**). Piste : faire lire `.env` par les cibles, ou documenter le prérequis `up.sh`. | `Makefile`, `scripts/dev/up.sh` | Moyenne (robustesse) |
| D19 | **Le mode inventaire ne reconnaît que quatre racines de vocabulaire.** `is_inventory_question` exige un mot de liste **et** l'une de `startup`, `pitch`, `deck`, `pitchdeck`. Toute la mécanique d'inventaire — surcharge de budget, diversité forcée par document (`diversify_by_document`), contournement de la diversification standard — est donc réservée aux questions sur des startups ou des pitch decks. Mesuré le 31/07/2026 : « liste les documents disponibles sur le capital-risque » et « quelles sont les sociétés analysées ? » **n'activent pas** le mode, alors que ce sont des inventaires documentaires légitimes ; le témoin `IN01` de la qualification production non plus. Conséquence : sur les corpus actuels (AEC - Built World, références VC), le chemin est quasi inatteignable, et les réglages `RAG_INVENTORY_*` pilotent une branche presque jamais empruntée. Le vocabulaire date de la période où le produit servait un fonds de capital-risque et n'a pas été élargi depuis. | `services/query/heuristics.py:58-64` | Moyenne (qualité) |
| D20 | **Les échecs et timeouts du résumé de conversation sont silencieux.** `maybe_update_thread_summary` est encadrée par `except Exception: return`, son appelant par `except Exception: pass`, et le module `chat_threads.py` ne contient **aucun logger** (ni `import logging`, ni appel). Aucune des huit métriques attendues n'existe : `memory_summary_triggered`, `conversation_messages_at_trigger`, `memory_summary_input_chars`, `memory_summary_output_chars`, `memory_summary_latency_ms`, `memory_summary_timeout`, `memory_summary_failure`, ratio de compression. Un timeout de résumé est donc indiscernable d'un fil trop court, d'un transcript vide ou d'un modèle indisponible — et un résumé perdu l'est **définitivement**, le checkpoint n'avançant pas. Bloquant pour toute qualification de `CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS` ou d'un timeout dédié (décision du 31/07/2026). Aggravant : le résumé est **synchrone dans la requête HTTP**, donc sa latence s'ajoute à celle de la réponse utilisateur, sans être mesurée. | `apps/chat/services/chat_threads.py` | Élevée (observabilité) |
| D21 | **`RAG_HNSW_EF_SEARCH=40` face à un pool demandé de 120.** En production, la limite envoyée à pgvector vaut `max(vector_top_k, candidate_k)` = 120 (`RAG_TOP_K=40` × multiplicateur 3), alors que `ef_search` vaut 40. Un `ef_search` inférieur à la limite borne ce que l'index HNSW peut restituer, indépendamment de la limite SQL. Relevé le 31/07/2026 pendant l'analyse d'impact des paramètres d'inventaire ; **non évalué** dans ce lot, à traiter séparément (mesure du rappel réel à `ef_search` 40 / 80 / 120 sur corpus représentatif). | `repositories/chunk_repository.py`, `settings/base.py` | À évaluer |

> Les items historiques suivants sont **résolus** et retirés : coût LLM matérialisé
> (`services/pricing.py` + `RagQueryLog.llm_cost_estimated` + agrégations), CI versionnée
> (4 workflows), token DRF en cookie HttpOnly, CSP frontend, index ANN pgvector (HNSW),
> `EMBEDDINGS_PROVIDER=stub` refusé en prod, `FRONTEND_APP_ORIGIN` en env, audit des actions
> (`ActionAuditEvent`), benchmark qualité versionné, routage de la queue Celery `analytics`.
>
> **D4 (stockage objet) est tranchée, pas en dette** : le volume local persistant est le seul
> stockage documentaire ; MinIO/S3 est écarté et retiré des configurations actives. Voir
> [../01-overview/decisions/ADR-001-stockage-documentaire.md](../01-overview/decisions/ADR-001-stockage-documentaire.md)
> (conditions de réévaluation incluses).

---

## Roadmap produit — Analyses globales de corpus (séquencement L0–L8)

Le pipeline nominal répond à des **questions ciblées** (4 à 8 extraits max) : il n'agrège pas
sur tout un corpus et ne mesure pas la part réellement lue. Vocabulaire :
[../10-reference/coverage-semantics.md](../10-reference/coverage-semantics.md).

| Lot | Objectif | État |
|---|---|---|
| **L0** | Vocabulaire d'exhaustivité + objet `coverage`. | ✅ Fait (`domain/coverage.py`). |
| **L1** | Statistiques génériques du RagSpace (SQL). | ✅ Fait (`services/coverage_service.py`, endpoint `rag-spaces/<id>/stats/`). |
| **L2** | `CorpusAnalysisJob` + queue Celery `analytics` + progression + annulation + idempotence. | ✅ Fait (modèle + `run_corpus_analysis_job`, queue routée). |
| **L3** | Détection des demandes globales + **dialogue « rapide / exhaustif »** dans le chat. | ✅ Fait (`chat/services/corpus_mode_service.py`). |
| **L4** | Recherche exhaustive par préfiltre FTS large. | 🚧 En cours / partiel (`analysis/global_search.py`). |
| **L5** | Remplissage des `DocumentSummary`. | 🚧 Partiel (modèle + `generate_document_summary_task` existants). |
| **L6** | Synthèse globale (map-reduce hiérarchique, citations propagées). | ⬜ Planifié. |
| **L7** | Exports CSV / Markdown (réutiliser l'échappement anti-injection). | ⬜ Planifié. |
| **L8+** | Inventaire KPI, entités, chronologie, comparaison, contradictions. | ⬜ Planifié — exige une extraction structurée neuve. |

Deux points structurants (audit corpus) :

- **L'extraction structurée existante est inexploitable en l'état** : le schéma d'entités
  (extracteurs regex domaine construction) est vide sur les autres corpus, et `document_date`
  n'est renseigné sur aucun document. Les lots L8+ repartent de zéro ; toute extraction
  persistée doit porter une `extraction_version` pour être ré-extractible **en delta**.
- **L3 est prioritaire sur L8** : tant que le chat ne distingue pas une réponse top-k d'une
  analyse de corpus, enrichir les réponses les rend plus riches sans les rendre plus vraies
  (L3 est fait ; la valeur est désormais dans L4–L6).

---

## Roadmap produit — Autres pistes

Priorisées par valeur/effort ; non engageantes.

- **Answerability en production** : brancher (avec réserves) le module déterministe P1.8 au
  chemin live une fois les cibles de promotion atteintes (FP ≤ 0.20 et FN ≤ 0.05). → D7.
- **Reranking cross-encoder réellement actif** en prod (corriger D1 d'abord).
- **Backup chiffré** des originaux (D5). Le stockage objet n'est **pas** une piste : décision
  ADR-001 (volume local) — à rouvrir seulement si l'une de ses conditions de réévaluation
  survient.
- **Streaming via Gateway** (aujourd'hui pseudo-stream quand la Gateway est active).
- **Quotas LLM par workspace** (table d'usage + garde) — cohérent avec la contrainte de coût.
- **Codegen TypeScript depuis OpenAPI** + garde CI de divergence de schéma (D15 associé).
- **Observabilité** : dashboards latence / taux no-answer / ingestion FAILED / queues / tokens.
- **Tests e2e** (Playwright) sur les flux principaux ; garbage collector des `storage_file` orphelins.
- **P3-c — Vision descriptive des plans** : différée (cadrage seul, aucun code) ; arbitrage
  confidentialité requis (cloud interdit par défaut). Cf.
  [../03-backend/ingestion-extraction.md](../03-backend/ingestion-extraction.md).

> Backlog corpus métier (tickets non bloquants) : voir [backlog-corpus.md](backlog-corpus.md).

---

## Références d'implémentation

- `backend/apps/rag_core/services/rerank.py`, `backend/apps/rag_core/repositories/chunk_repository.py`
- `backend/apps/rag_core/domain/coverage.py`, `backend/apps/rag_core/services/coverage_service.py`
- `backend/apps/rag_core/analysis/`, `backend/apps/chat/services/corpus_mode_service.py`
- `backend/apps/rag_core/benchmark/answerability.py`
- `backend/config/celery.py`, `.github/workflows/`

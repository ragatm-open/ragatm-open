# Backlog — Corpus documentaire métier

Backlog consolidé des tickets de suivi issus de l'audit du lot « corpus métier »
(`domain_corpus_*`). Public : mainteneurs. **Tous ces points sont non bloquants** : aucun
n'affecte la sécurité multi-tenant ni l'isolation privé/corpus (validées à l'audit).

Contexte du lot : voir [../03-backend/corpus-metier.md](../03-backend/corpus-metier.md). Le CODE
fait foi ; les chemins de fichiers ci-dessous sont repo-relatifs.

> ⚠️ Rappel d'état : **l'ingestion des documents de corpus est encore synchrone**
> (`apps/corpus/services/ingest.py`, pas de Celery ; Excel refusé pour le corpus). Le ticket B2
> ci-dessous porte précisément cette limite.

| # | Ticket | Priorité | Domaine | État |
|---|---|---|---|---|
| B1 | Test API de sérialisation des sources corpus | P2 | Tests backend | Backlog |
| B2 | Ingestion async Celery des documents de corpus | P2 | Ingestion | Backlog |
| B3 | Tests RTL des pages `/corpora` et `/ragatm-admin` | P2 | Tests frontend | Backlog |
| B4 | Documenter les logs restant en `sources_schema_version=1` | P2 | Documentation | Backlog |
| B5 | Optimiser `_enrich_sources_with_document_date` (sources mixtes) | P2 | Performance | Backlog |

---

## B1 — Test API de sérialisation des sources corpus

Le typage des sources de la réponse chat repose sur `sources_for_api()` →
`normalize_source_for_api()` (`apps/chat/services/chat_sources.py`), enrichi de `source_type` +
métadonnées corpus. Ce chemin n'est couvert que **transitivement** (tests bi-source sur les
chunks bruts, tests d'audit sur `normalize_sources_for_audit`, helpers frontend purs) : il
manque un test backend asservissant directement la **sérialisation API**.

**Périmètre** : ajouter un test unitaire (`tests/tests_root/unit/test_chat_sources.py`) vérifiant,
sur une source corpus et une source workspace : `source_type == "domain_corpus"` /
`"workspace_document"` ; présence de `corpus_id`, `corpus_name`, `business_domain_id`,
`business_domain_name` pour le corpus ; `schema_version == 2` ; fallback `workspace_document`
si `source_type` absent (ancien format).

**Réf.** : `apps/chat/services/chat_sources.py`, `apps/chat/services/chat_service.py`.

## B2 — Ingestion async Celery des documents de corpus

L'ingestion corpus est **synchrone** (`apps/corpus/services/ingest.py`), contrairement au
pipeline privé (`apps.rag_core.tasks.ingest_document_task`/`reindex_document_task` +
`ingest.async_policy`). Conséquence : upload et surtout **réindexation d'un corpus volumineux**
sont bloquants côté requête admin.

**Périmètre** : introduire des tâches Celery miroir (`ingest_corpus_document_task`,
`reindex_corpus_task`), réutiliser `async_policy.resolve_async` et l'invariant « OCR ⇒ async »,
exposer un mode `queued` (202) cohérent avec `RagSpaceReindexView`. Le doc corpus conserve déjà
`storage_file` (réindex sans re-upload). Aucune écriture sur `rag_document`/`rag_chunk` ; mode
EAGER conservé pour les tests.

**Réf.** : `apps/corpus/services/ingest.py`, `apps/rag_core/tasks.py`,
`apps/rag_core/services/ingest/async_policy.py`, `api/v1/ragspaces/views.py::RagSpaceReindexView`.

## B3 — Tests RTL des pages `/corpora` et `/ragatm-admin`

Les nouvelles pages React ne sont couvertes que par `tsc --noEmit` et les helpers purs
(`tests/frontend/lib/sources.test.ts`). Aucun test React Testing Library ne valide le rendu :

- `/corpora` : liste des corpus actifs, état vide, libellé « Lecture seule · administré par
  ragATM », absence de bouton d'écriture.
- `/ragatm-admin` : `AdminGate` masque l'UI si `is_ragatm_owner=false` ; flux domaine → corpus
  → documents → associations pour un global owner (mocker `useWorkspace` et `@/lib/corpus`).
- Sidebar : entrée « Admin corpus » visible seulement si `isGlobalOwner`.

**Réf.** : `frontend/src/app/corpora/page.tsx`, `frontend/src/app/ragatm-admin/page.tsx`,
`frontend/src/components/shell/Sidebar.tsx`, `tests/frontend/`.

## B4 — Documenter les logs en `sources_schema_version=1`

Le chemin RAG principal écrit désormais `sources_schema_version=2` (sources typées). Mais
certains chemins **indépendants** écrivent légitimement encore en **v1** :

- **news** : `apps/chat/services/intents/news.py::update_log_sources_best_effort` force `v1` ;
- **résumés** : `apps/rag_core/services/summary_service.py` écrit `v1`.

Ces chemins n'impliquent pas le corpus, mais un consommateur d'audit doit savoir que
`sources_schema_version` peut valoir **1 ou 2** selon le chemin (v2 = RAG principal privé +
corpus ; v1 = news, résumés) et traiter `source_type` comme **optionnel** (fallback
`workspace_document`). Documentation seule ; l'harmonisation news/summary vers v2 est hors
périmètre. À intégrer dans la documentation d'exploitation (hors périmètre de cette édition) et/ou
[../03-backend/corpus-metier.md](../03-backend/corpus-metier.md).

**Réf.** : `apps/chat/services/intents/news.py`, `apps/rag_core/services/summary_service.py`,
`apps/rag_core/services/query/audit.py` (`SOURCES_SCHEMA_VERSION=2`).

## B5 — Optimiser `_enrich_sources_with_document_date`

Avec le retrieval bi-source, la liste passée à `_enrich_sources_with_document_date()`
(`apps/rag_core/services/query/orchestrator.py`) peut contenir des sources corpus dont le
`document_id` est un `DomainCorpusDocument`. La requête
`Document.objects.filter(rag_space_id=…, id__in=document_ids)` inclut donc ces ids dans une
requête filtrée sur les documents **privés** : **sans fuite ni erreur** (le filtre `rag_space_id`
exclut tout, aucun match), mais inutilement élargie, et la date des documents corpus n'est pas
enrichie depuis cette source.

**Périmètre** : ne transmettre à `_fetch_document_dates_map` que les `document_id` des sources
`source_type == "workspace_document"` ; optionnellement enrichir la date des sources corpus
depuis `DomainCorpusDocument`. Aucun changement observable côté isolation/typage.

**Réf.** : `apps/rag_core/services/query/orchestrator.py`.

---

## Références d'implémentation

- `backend/apps/corpus/services/{ingest,retrieval}.py`
- `backend/apps/chat/services/chat_sources.py`
- `backend/apps/rag_core/services/query/orchestrator.py`
- `frontend/src/app/corpora/`, `frontend/src/app/ragatm-admin/`
- Tickets d'origine : `docs/follow-ups/corpus/`

# API — Contrats d'erreurs

Objectif : décrire la forme des erreurs de l'API v1, les codes HTTP utilisés, les
motifs d'anti-énumération, les rejets d'upload et le contrat des sources. Public :
intégrateurs qui gèrent les cas d'échec.

Le CODE prévaut. Comportements vérifiés dans les vues `backend/api/v1/`, les
services `apps/rag_core/services/` et les primitives d'isolation
`apps/workspaces/services/access.py`.

## Forme des erreurs (DRF)

Les erreurs suivent le format standard Django REST Framework :

- Erreur simple : `{"detail": "message"}`.
- Erreur de validation par champ : `{"champ": ["message", ...]}`.
- Erreur de validation non liée à un champ : `{"non_field_errors": ["message", ...]}`.
- Erreur interne : `{"detail": "internal_error", "request_id": "..."}`.

L'en-tête `X-Request-ID` est renvoyé dans **toutes** les réponses (voir
[vue-ensemble.md](vue-ensemble.md)) et permet de corréler une erreur avec les logs.

## Codes HTTP utilisés

| Code | Sens dans RagATM |
|---|---|
| 400 | validation invalide, upload rejeté (format/contenu illisible), limites d'analyse |
| 401 | authentification manquante ou invalide (token absent/expiré côté header) |
| 403 | accès refusé en écriture ; endpoint superuser sans droit ; override de config en prod |
| 404 | ressource introuvable **ou** sans droit de lecture (anti-énumération) |
| 409 | conflit d'état : delete d'un document `PENDING`/`INGESTING` ; job d'analyse déjà actif ; override sans `CORE_CONFIG_EDIT_ENABLED` |
| 413 | upload trop volumineux (> `UPLOAD_MAX_FILE_SIZE_MB`, 25 Mo) |
| 415 | format d'upload non supporté |
| 503 | broker Celery indisponible lors d'un reindex asynchrone |

Codes de succès notables : `202` (traitement enfilé — upload/reindex async,
création de job d'analyse, résumé long), `200` (job réutilisé/idempotent), `204`
(logout, purge d'un RagSpace).

## Anti-énumération (404 plutôt que 403)

Pour ne pas révéler l'existence de ressources d'autres tenants, les accès en
**lecture** à une ressource inaccessible renvoient **404** (et non 403), avec le
même message que « introuvable ». Ce motif s'applique aux documents, threads,
analyses et corpus. Le **refus d'écriture** sur une ressource par ailleurs
lisible renvoie **403**. Les primitives `require_workspace_access` /
`require_rag_space_access` produisent un message identique pour « introuvable » et
« pas d'accès ».

## Rejets d'upload (`POST /documents/upload/`)

La validation a lieu **avant** la création du `Document` :

- **413** — fichier plus lourd que `UPLOAD_MAX_FILE_SIZE_MB` (25 Mo par défaut).
- **415** — extension/type MIME non supporté (hors TXT, Markdown, PDF, DOCX, CSV,
  HTML, images, XLSX).
- **400** — fichier du bon type mais **illisible/rejeté** à l'extraction (ex. PDF
  au-delà de `UPLOAD_PDF_MAX_PAGES` = 500, contenu vide, garde de format). Les
  fichiers Excel et l'OCR forcent l'ingestion **asynchrone** (réponse `202`).

Le succès renvoie `{"ok": true, "queued": <bool>, "document_id": "...", "deduped": <bool>}`.
Un doublon d'un document dont l'ingestion est déjà planifiée ou en cours ajoute
`"status": "PENDING"|"INGESTING"` et `"detail": "ingestion_already_in_progress"` : aucune
nouvelle tâche n'est créée.
La déduplication repose sur `sha256` au sein du RagSpace (contrainte unique
partielle, statut ≠ `FAILED`). Voir
[ingestion-extraction](../03-backend/ingestion-extraction.md).

## Conflit 409

- **Suppression de document** : `DELETE /documents/<uuid>/delete/` refuse (409) tant
  que le document est `PENDING` ou `INGESTING` (une ingestion est en cours).
- **Réindexation sans source** : `POST /documents/<uuid>/reindex/` en mode synchrone
  renvoie 409 avec `error_code` = `source_file_missing` (source déclarée, fichier
  introuvable) ou `no_storage_file_for_reindex` (aucune source conservée), et
  `retryability: replace_source_recommended`. Ces cas répondaient auparavant 500
  `reindex_failed` : l'appelant ne pouvait pas distinguer une panne de service d'un
  constat définitif sur ce document. Voir
  [réindexation](../03-backend/reindexation-jobs.md).
- **Analyse de corpus** : la création d'un job (`POST /rag-spaces/<uuid>/analyses/`)
  renvoie 409 lorsqu'un job actif existe déjà pour la même combinaison
  (RagSpace, type d'analyse, hash de paramètres, empreinte de corpus). Un job
  identique déjà terminé et réutilisable renvoie 200 ; un nouveau renvoie 202.
- **Override de config** : `PUT /admin/core-config/override/` renvoie 409 hors
  staging avec `CORE_CONFIG_EDIT_ENABLED` (et 403 en prod).

## Indisponibilité 503

Un reindex asynchrone (`POST /rag-spaces/<uuid>/reindex/` ou
`POST /documents/<uuid>/reindex/`) qui doit enfiler une tâche Celery renvoie **503**
si le broker Redis est injoignable. En mode inline (`CELERY_TASK_ALWAYS_EAGER`) le
traitement est synchrone et ce cas ne se produit pas.

## Contrat des sources

Les sources renvoyées par le chat (`sources` dans la réponse et l'événement SSE
`sources`) sont typées. Le champ `schema_version` porte la version courante,
**`3`** dans le code (`SOURCES_SCHEMA_VERSION`), qui ajoute la citation Excel
(feuille + plage dans `meta`) au-dessus du contrat v2. La distinction structurante
introduite en v2 — le champ **`source_type`** — reste la clé du contrat :

Forme d'une source (par élément) :

```json
{
  "schema_version": 3,
  "source_type": "workspace_document",
  "rank": 1,
  "document_id": "<uuid>",
  "chunk_id": "<uuid>",
  "score": 0.73,
  "filename": "politique-rh.pdf",
  "page_number": 4,
  "document_date": "2025-01-12",
  "snippet": "extrait de texte…",
  "meta": {}
}
```

- `source_type` ∈ `workspace_document` (document privé du RagSpace, défaut) et
  `domain_corpus` (corpus métier partagé).
- Quand `source_type == "domain_corpus"`, quatre champs supplémentaires sont
  ajoutés : `corpus_id`, `corpus_name`, `business_domain_id`,
  `business_domain_name` (badges d'affichage).
- `meta` est projeté sur une **allowlist** (citation : feuille/plage/titre) ;
  l'audit applique exactement la même projection. Le `Chunk.meta` brut (entités
  regex, offsets…) n'est jamais exposé.

Coexistence : des `RagQueryLog` historiques portent `sources_schema_version=1`
(défaut de colonne). Le contrat courant émis par le runtime est celui décrit
ci-dessus. Voir [pipeline retrieval](../03-backend/pipeline-retrieval.md) et
[corpus métier](../03-backend/corpus-metier.md).

## Voir aussi

- [Vue d'ensemble de l'API](vue-ensemble.md)
- [Authentification](authentification.md)
- [Référence des endpoints](reference-endpoints.md)
- [Permissions et isolation](../03-backend/permissions-isolation.md)
- [Limites connues](../10-reference/limites-connues.md)

## Références d'implémentation

- `backend/apps/rag_core/services/query/audit.py` (`SOURCES_SCHEMA_VERSION`,
  normalisation des sources)
- `backend/apps/chat/services/chat_sources.py` (projection, `source_type`, badges corpus)
- `backend/apps/workspaces/services/access.py` (anti-énumération, refus write)
- `backend/api/v1/documents/` (rejets 413/415/400, conflit 409)
- `backend/apps/rag_core/services/ingest/` (limites d'upload, dédup sha256)
- `backend/apps/rag_core/analysis/` (conflit de job, transitions)
- `backend/config/middleware/request_id.py`

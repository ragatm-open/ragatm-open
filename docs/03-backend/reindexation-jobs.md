# Réindexation d'un RagSpace — jobs durables

Objectif : décrire la réindexation **globale** d'un RagSpace comme un **job durable,
asynchrone et reprenable**. Public : développeurs backend, opérateurs.

> Statut : **implémenté / activé par défaut**. Réindexation globale (jobs durables),
> reprise ciblée des documents en erreur, et **stratégies de reprise** (`auto`,
> `force_ocr`, `extended_timeout`). La réindexation réelle des espaces de benchmark
> reste **gated** (feu vert explicite).

## Frontend (suivi)

Deux écrans, **un seul composant** de progression
(`components/reindex/ReindexJobProgress`) et **un seul client** (`lib/reindex.ts`) :

- **Administration d'espace** (`workspace-admin`) : bouton « Réindexer » par espace ;
- **Documents** (`/documents`) : bouton « Réindexer » de l'espace courant **et** reprise en
  masse depuis l'onglet Erreurs (cases par ligne, case de page, extension « les N en
  erreur », barre d'action, modale de confirmation).

Dans les deux cas : le POST renvoie **202** → le `job_id` est stocké, la modale se ferme, et
la barre **poll** `GET /reindex-jobs/<id>/` toutes les 3 s (arrêt à l'état terminal). Au
rechargement de la page, `GET active-reindex-job/` **reprend** le suivi — le job vit côté
serveur, quitter l'onglet ne perd rien. Le bouton est désactivé tant qu'un job est actif. Un
job terminal avec échecs affiche « Relancer les N échec(s) » (→ `retry-failed`).

Un **409 `already_running`** n'est pas une erreur : les deux écrans adoptent le job renvoyé
et affichent son suivi.

**Vérification au chargement de l'onglet Espaces (`workspace-admin`) — bornée.** Il n'existe
pas d'endpoint agrégé : l'onglet émet un `GET active-reindex-job/` **par espace**, en séquence.
Règles (`SpacesTab.tsx`) :

- un espace déjà vérifié pendant la vie de l'onglet n'est **pas** réinterrogé quand la liste
  est rechargée (création, renommage, suppression) : après une création, seul le nouvel
  espace l'est ;
- une nouvelle liste ou le démontage **annulent** le cycle en cours (`AbortController` sur la
  requête en vol, arrêt de la boucle) ; les espaces non vérifiés passent au cycle suivant ;
- le **premier 429 arrête le cycle** ; aucun cycle ne repart avant le `Retry-After`, et un
  bandeau signale les espaces dont le statut n'a pas pu être vérifié (jamais un faux
  « aucune réindexation ») ;
- les autres erreurs (401/403/404/5xx/réseau) restent ignorées, comme avant : l'espace sera
  réinterrogé au cycle suivant ;
- revérification complète : remontage de l'onglet (changement d'onglet ou de page). Un job
  lancé ou relancé depuis l'onglet est connu par la réponse du POST et suivi par polling.

Origine : incident staging du 08/10/2026 (99 espaces) — deux cycles complets superposés et
98 × 429 ignorés ont épuisé le throttle utilisateur et provoqué un renvoi à tort vers `/login`
(cf. [frontend §5.1](../04-frontend/frontend.md#51-contrat-derreur-de-la-garde-dauthentification--implémenté)).
Mesures sur fixture de 100 espaces : création 100 → **1** requête de statut ; requêtes après
annulation 94 → **0** ; requêtes après le premier 429 98 → **0**.

> Correction (lot 2) : l'écran Documents lisait encore l'ancien contrat synchrone
> (`{queued, total, reindexed, failed}`). Toutes ces clés étant absentes de la réponse 202,
> il annonçait « Réindexation terminée : 0 réussis, 0 en échec » **à l'instant où le job
> démarrait**, et présentait le 409 idempotent comme un échec.

## Pourquoi

L'ancien endpoint `POST /rag-spaces/<id>/reindex/` traitait **tous** les documents
**dans la requête HTTP** (mode `CELERY_TASK_ALWAYS_EAGER=1`, sans worker). Pour un gros
corpus (~319 documents, ~28 min), le proxy Next.js coupait à **300 s**
(`UND_ERR_HEADERS_TIMEOUT`) tandis que le backend poursuivait le travail — laissant un
**état partiel** (documents `INGESTING` bloqués). Le nouveau contrat sépare le lancement
(réponse **202** immédiate) du traitement (asynchrone, document par document, repris après
crash).

## Architecture

```
POST /rag-spaces/<id>/reindex/
  → create_reindex_job()      (transaction : job + 1 item/document, on_commit → enqueue)
  → 202 { job_id, total }     (aucune extraction dans la requête HTTP)
      run_reindex_job_task     (orchestrateur, queue `reindex`) : job → RUNNING, fan-out
        └─ reindex_job_item_task × N  (1 par document, idempotent, atomique par doc)
             └─ refresh_and_maybe_finalize()  (compteurs agrégés → COMPLETED[_WITH_ERRORS])
  GET /reindex-jobs/<job_id>/  (polling de progression)
```

- **1 tâche orchestratrice légère + 1 tâche par document** (jamais une seule tâche opaque
  de 30 min) : échecs isolés, progression observable, reprise fine.
- Queue **dédiée `reindex`** (routage `CELERY_TASK_ROUTES`) pour ne pas affamer
  ingestion/chat.

## Worker `reindex` — sans lui, rien ne démarre

Trois tâches sont routées vers la queue `reindex` : `run_reindex_job_task`,
`reindex_job_item_task` et `reap_stale_reindex_items_task`. **Un service worker doit
la consommer**, sinon les jobs restent indéfiniment en `pending`.

| Compose | Service | Queue | Concurrence | Nom de nœud |
|---|---|---|---|---|
| `docker-compose.yml` | `worker_reindex` | `reindex` | 1 | `reindex@%h` |

**Pourquoi un worker dédié** et non `--queues=celery,reindex` sur le worker principal :
un item peut occuper un slot jusqu'à **480 s** (stratégie « délai étendu ») et l'OCR est
gourmand en CPU. Partager les slots de l'ingestion et du chat rendrait ces derniers
irréguliers — même raison que pour `analytics` et `maintenance`.

**Concurrence 1** : valeur d'observation, pas une limite définitive. Un item est long et
coûteux ; on mesure avant d'envisager le parallélisme.

### Observer

```bash
# Quelles queues chaque worker consomme réellement
docker compose exec worker_reindex \
  celery -A config inspect active_queues

# Longueur de la queue (attention à la BASE Redis utilisée par l'environnement)
docker compose exec redis redis-cli -n 0 LLEN reindex
```

Un `LLEN reindex` qui croît sans fin signifie qu'aucun worker ne consomme : c'est
exactement la panne du 01/08/2026.

### Le défaut du 01/08/2026

La queue `reindex` a été introduite le 17/07 avec les jobs durables, mais **aucun
service Compose ne la consommait**. Deux semaines plus tard : 2 323 messages en attente
en staging, 1 218 en production, et le bouton « Réindexer » comme la reprise en masse
étaient inopérants — sans erreur, sans alerte, sans log. *Un job créé mais jamais démarré
ressemble à un job lent.*

Cette divergence vit entre deux fichiers qui ne se connaissent pas : les settings Django
d'un côté, les Compose de l'autre. Un test statique la rattrape désormais :
`tests/tests_root/deploy/test_celery_queue_consumers.py` échoue si une queue routée (ou
ciblée par Beat) n'a pas de consommateur dans l'un des trois composes autonomes.

### Backlog : la queue n'est pas la source de vérité

Les jobs et leurs items sont **persistants en base**. Une queue accumulée n'a donc pas à
être drainée : elle peut être purgée, et le travail encore pertinent réenfilé depuis la
base. La tâche périodique `reap_stale_reindex_items_task` porte déjà un
`expires` égal à son intervalle (`CELERY_BEAT_REAP_REINDEX_SECONDS`, 300 s par défaut) :
les exemplaires périmés sont **écartés à la consommation**, pas exécutés — un backlog de
plusieurs milliers de reaps se résorbe donc de lui-même sans rien exécuter d'inutile.

Purge — **toujours avec `-Q reindex` explicite**, jamais globale :

```bash
./scripts/staging/compose_staging.sh run --rm backend \
  celery -A config purge -f -Q reindex
```

Avant toute purge : relever les jobs actifs (`status IN ('pending','queuing','running')`)
et décider s'ils doivent être réenfilés ou marqués terminaux.

## Modèle de données

`apps/rag_core/models/reindex_job.py` :

- **`RagSpaceReindexJob`** (`rag_space_reindex_job`) : `status`, `kind`, `selection_meta`,
  compteurs (`total/pending/queued/running/succeeded/failed/cancelled/interrupted`),
  `parent_job` (lignée de relance), `error_code/message` (sûrs), horodatages.
  - **`kind`** — portée du job : `full_reindex` (défaut, tous les documents éligibles de
    l'espace) ou `failed_documents_retry` (reprise ciblée des documents en **erreur**).
    Le `kind` ne change **rien** au traitement d'un item : même tâche, mêmes invariants,
    même suivi. Seule diffère la façon dont le snapshot de départ a été constitué. Un
    `kind` qui ferait diverger le traitement devrait être une tâche distincte.
  - **`selection_meta`** (JSON) — trace descriptive de la sélection (`mode`, `search`,
    `requested_count`, `eligible_count`). **Jamais** la source de vérité de ce qui est
    traité : les items matérialisés au lancement le sont. Un job qui réévaluerait un
    filtre en cours de route verrait son total bouger sous ses pieds.
  - Statuts : `pending → queuing → running → {completed, completed_with_errors, failed,
    cancelled, interrupted}` (transitions **verrouillées**, terminal = définitif).
  - **Contrainte partielle** `uniq_active_reindex_job_per_space` : **au plus un job actif
    par espace** (la course est impossible, pas via un `SELECT` non verrouillé).
  - Invariant : `total = pending+queued+running+succeeded+failed+cancelled+interrupted`.
    Compteurs recalculés par **agrégation SQL** (jamais d'incrément naïf).
  - `progress_percent = 100 × (succeeded+failed+cancelled+interrupted) / total`.
- **`RagSpaceReindexJobItem`** (`rag_space_reindex_job_item`) : `status`, `attempt_count`,
  `celery_task_id`, **`heartbeat_at` / `lease_expires_at`** (détection stale), horodatages,
  `error_code/message`, **`effective_strategy`**, **`strategy_meta`**, **`skip_reason`**.
  Unicité `(job, document)`.
  - Statuts : `pending → queued → running → {succeeded, failed, interrupted}` ;
    `pending/queued → cancelled` ; **`skipped`** posé dès le snapshot pour un document
    volontairement non traité (distinct de `failed` : rien n'a été tenté, et rien ne
    devait l'être — les confondre ferait passer une décision pour une panne).
  - `effective_strategy` est FIGÉE au lancement. Un job en vol ne change donc pas de
    méthode parce que le code d'erreur du document aurait bougé entretemps.

## API

| Méthode | URL | Perm. | Réponse |
|---|---|---|---|
| POST | `/api/v1/rag-spaces/<id>/reindex/` | owner/admin | **202** `{status:accepted, job_id, total_documents, already_running:false}` ; **409** si un job actif existe (renvoie le job) ; **503** `reindex_eager_too_large` (EAGER sans worker au-delà du seuil) |
| GET | `/api/v1/reindex-jobs/<job_id>/` | owner/admin | **200** état complet + `failed_items[]` (document_id, filename, error_code, attempt_count) |
| GET | `/api/v1/rag-spaces/<id>/active-reindex-job/` | owner/admin | **200** `{active, job}` — reprise du suivi après refresh |
| POST | `/api/v1/rag-spaces/<id>/retry-failed-documents/preview/` | owner/admin | **200** `{strategy_breakdown, skip_reasons, actionable_documents, skipped_documents, extended_timeout_seconds}` — **ne crée rien** |
| POST | `/api/v1/rag-spaces/<id>/retry-failed-documents/` | owner/admin | corps `{selection, strategy?}` → **202** `{status:accepted, job_id, kind, requested_strategy, strategy_breakdown, skip_reasons, total_documents, skipped_documents}` ; **409** `already_running` (renvoie le job actif) / `no_eligible_documents` / `no_retryable_documents` / `selection_too_large` ; **400** `invalid_selection` |
| POST | `/api/v1/reindex-jobs/<job_id>/retry-failed/` | owner/admin | corps optionnel `{strategy?}` → **202** nouveau job (`parent_job`, `kind` **hérité**) sur les items `failed`/`interrupted` ; sans `strategy`, la méthode du parent est **conservée** ; **409** si un job actif existe |

`GET /reindex-jobs/<id>/` expose `kind`, `selection`, `requested_strategy`,
`strategy_breakdown` (avancement par méthode), `skipped` et `skip_reasons` : **un seul
contrat de suivi** pour
la réindexation globale comme pour la reprise ciblée. Le frontend n'a qu'un composant de
progression (`components/reindex/ReindexJobProgress`), partagé par l'écran Documents et
l'administration d'espace.

Aucune réponse n'expose de contenu documentaire ni de stacktrace : `error_code` appartient
à un ensemble fermé, `error_message` est un message sûr. Un refus (403) ne révèle ni
compteur, ni identifiant, ni nom de fichier.

### Admission d'une réindexation unitaire explicite (REINDEX-GUARD-1)

`POST /api/v1/documents/<id>/reindex/` est **gardé par défaut** (*implémenté*) :

| Statut du document | Sans `force` | Avec `{"force": true}` |
|---|---|---|
| `READY` | admis | admis |
| `FAILED` | règles de retryabilité existantes (**400** `document_not_eligible` si définitif) | idem — `force` ne contourne **pas** l'éligibilité |
| `PENDING` | **409** `reindex_already_in_progress` | admis |
| `INGESTING` | **409** `reindex_already_in_progress` | admis |

L'admission est **atomique** : verrou de la ligne document (`SELECT … FOR UPDATE`),
relecture du statut, puis transition `PENDING` (async) ou `INGESTING` (sync) **avant**
l'enqueue ou l'exécution inline, dans une transaction courte sans verrou d'espace. Deux
POST simultanés sur un document `READY` donnent exactement un **200** et un **409** — une
seule opération démarre (double clic, retry HTTP, page obsolète). Même stratégie ou
stratégie différente : refus identique. Cette transition n'est qu'un loquet d'admission
HTTP, jamais une précondition de publication ni un jeton de génération (INGEST-1F).

Le corps 409 porte `ok:false`, `error_code`, `message`, `document_id`, `status`,
`force_allowed:true` et, si un item de job de réindexation est réellement actif (RUNNING,
lease fraîche), `active_job_id` / `active_item_id` — jamais d'identifiant Celery. Une demande
refusée ne produit **aucun** événement d'audit (rien n'a démarré) ; une demande forcée est
journalisée avec `metadata.forced=true`.

`force=true` est l'échappatoire explicite (état `PENDING`/`INGESTING` qui semble bloqué,
volonté d'une seconde opération) : l'utilisateur assume une concurrence que INGEST-1F rend
techniquement sûre (dernier publieur gagne, index et métadonnées ensemble). Le paramètre
est un booléen JSON strict (**400** `invalid_force` sinon). Aucun ordonnancement des
requêtes n'est ajouté.

Les chemins **internes** ne passent pas par cette garde et restent libres : retry Celery,
redélivrance broker, items de job (`reindex_job_item_task`, lease), reprise `retry-failed`,
reaper `recover_stale_items`, commande `reembed_documents`. Un échec d'enqueue après
admission suit le contrat existant (document `FAILED`, **503**) : aucun `PENDING` bloqué
n'est introduit.

### Reprise ciblée des documents en erreur

Corps de `retry-failed-documents/`, deux formes exclusives :

```jsonc
// Les lignes cochées.
{"selection": {"mode": "explicit", "document_ids": ["<uuid>", "…"]}}

// « Tous les résultats du filtre », moins d'éventuelles exclusions.
{"selection": {"mode": "filter", "search": "rapport",
               "excluded_document_ids": ["<uuid>"]}}
```

Le mode `filter` évite de faire transiter des centaines d'identifiants pour dire « tout » :
le serveur reconstruit le périmètre avec **la sémantique de recherche et de statut de la
liste documentaire** (`api/v1/documents/listing.py`), sans pagination ni tri — une sélection
« tous les résultats » ne dépend donc pas de la page affichée. `status` est facultatif et ne
peut valoir qu'`error` : cet endpoint ne reprend que des documents en échec.

**Éligibilité**, évaluée en SQL au lancement (aucune requête par document) : appartenir au
RagSpace, être `FAILED` (ce qui exclut de fait `PENDING`/`INGESTING`), avoir un fichier
source, et n'être porté par **aucun item actif d'un job actif** — deux items concurrents sur
le même document sont exactement le scénario où une tâche ancienne écrase le résultat d'une
plus récente. Les identifiants hors périmètre ne matchent simplement pas : ils sont comptés
dans `skipped_documents`, jamais nommés dans la réponse.

**Snapshot figé** : les items sont créés dans la même transaction que le job. Un document
qui tombe en erreur *après* le lancement n'entre pas dans le job en cours.

**Exclusivité** : elle porte sur le **RagSpace**, pas sur le `kind`. Une réindexation globale
active bloque une reprise ciblée, et réciproquement (409 `already_running` avec le job à
suivre). Une reprise ne crée aucun `Document`, ne renvoie aucun fichier et ne consomme aucun
quota : les documents conservent identifiant et fichier source.

### Stratégies de reprise (lot 3)

Rejouer le pipeline qui vient d'échouer ne pouvait rien changer sur les deux causes
les plus fréquentes — un PDF scanné sans texte natif, un délai d'extraction dépassé.
Une reprise porte donc désormais une **méthode** :

| Stratégie | Ce qu'elle change | Applicable à |
|---|---|---|
| `standard` | rien (comportement historique) | tout |
| `force_ocr` | force l'OCR local pour cette exécution | PDF et images uniquement |
| `extended_timeout` | budget d'extraction `DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS` | tout |
| `auto` | résout la méthode **par document**, depuis la cause d'échec | tout |

`auto` n'est jamais une stratégie *effective* : c'est une demande de résolution,
tranchée au lancement et figée dans `item.effective_strategy`.

**Table de décision** — `apps/rag_core/services/ingest/retry_strategy.py`, source
unique. Le frontend affiche la recommandation qu'il reçoit ; il n'en calcule aucune.

| `error_code` | Retryabilité | Stratégie |
|---|---|---|
| `pdf_scanned_requires_ocr`, `pdf_text_unrecoverable` | `retryable_with_strategy` | `force_ocr` |
| `extraction_timeout`, `pdf_extraction_timeout` | `retryable_with_strategy` | `extended_timeout` |
| `transient_ingest_error`, `embedding_*`, `ocr_engine_missing`, `*_disabled`… | `retryable` | `standard` |
| `ocr_no_text`, `empty_document`, `empty_text`, `no_valid_chunks`, `source_file_missing` | `replace_source_recommended` | **aucune** |
| formats refusés, macros, chiffrement, corruption, limites de taille, `no_storage_file_for_reindex` | `non_retryable` | **aucune** |
| code absent ou inconnu | `retryable` | `standard` + `auto_fallback: true` |

`ocr_no_text` est le cas qui motive tout le lot : l'OCR a déjà tourné et n'a rien
produit. Le reproposer serait promettre exactement ce qui vient d'échouer — ni l'API
ni l'écran ne le font, y compris si `force_ocr` est demandé explicitement.

**`source_file_missing` — source déclarée mais absente** *(implémenté)*. À distinguer
de `no_storage_file_for_reindex`, qui dit « aucune source n'a jamais été conservée » :
ici le document a bien eu un fichier, il n'est plus dans le stockage. Avant, ce cas
n'était classé nulle part — `storage_file.open()` levait un `FileNotFoundError` qui se
dégradait en `internal_error`, rangé parmi les causes *environnementales*, donc
présenté comme « réessayer ». Un conseil qui ne pouvait jamais aboutir : le fichier ne
revient pas tout seul. La détection se fait dans
`services/ingest/reindex.py`, **avant** l'ouverture du fichier, via
`documents/source_availability`. Le contrôle est ponctuel : il n'existe **aucun**
`exists()` dans le chemin de lecture RAG (`query_rag`, retrieval, chat, embedding de
requête).

Le code d'erreur est lu dans `meta.extraction.error_code`. Les chemins qui n'en
écrivaient pas (document vide, erreur d'embeddings, erreur technique) le persistent
désormais ; un repli borné sur le **préfixe** de `error_message` couvre les documents
tombés en échec avant ce lot. Aucune décision ne dépend d'un message en prose.
**Le code appartient à la tentative courante** (*implémenté*, lot STALE-ERROR-METADATA) :
il est retiré au début de chaque tentative, et un code connu porté par le préfixe de
`error_message` prime sur un code `meta` différent (ligne antérieure au reset) — une
erreur de la tentative N ne décide jamais de la reprise de la tentative N+1 (détail dans
[ingestion-extraction.md](ingestion-extraction.md), « Code d'erreur structuré »).

**Documents non traitables** : ils entrent dans le snapshot comme items `skipped`
porteurs d'un `skip_reason` (`non_retryable`, `replace_source_recommended`,
`strategy_not_supported`) — le job rend compte de tout ce qui était sélectionné, y
compris de ce qu'il a décidé de ne pas tenter. Un lot dont *aucun* document n'est
traitable est refusé (`409 no_retryable_documents`) plutôt que créé vide.

**Ce que `force_ocr` ne fait PAS** : il ne relâche aucune limite. Taille maximale,
nombre de pages, validation MIME, formats refusés, permissions — tout reste appliqué
en amont par `validate_and_classify`. La stratégie ne change que la **méthode**
d'extraction.

**Prévisualisation** — `POST /rag-spaces/<id>/retry-failed-documents/preview/` :
mêmes permissions, même sélection, même fonction de résolution (`plan_retry`) que la
création, mais n'écrit rien, ne lit aucun fichier et ne met aucune tâche en file.
Renvoie `strategy_breakdown`, `skip_reasons` et `extended_timeout_seconds`.

**Reprise unitaire** — `POST /documents/<id>/reindex/` accepte
`{"strategy": "auto|standard|force_ocr|extended_timeout"}`. Une stratégie inconnue
est refusée (**400** `invalid_strategy`) ; un document non reprenable l'est aussi
(**400** `document_not_eligible` / `strategy_not_supported`, avec `retryability` et
`error_code`). Le paramètre historique `?ocr=true` reste accepté et traduit en
`force_ocr` — **déprécié** ; l'écran ne l'émet plus.

**Relance des échecs** — `POST /reindex-jobs/<id>/retry-failed/` : sans corps, la
stratégie du parent est **conservée** ; `{"strategy": "auto"}` la fait recalculer sur
les codes d'erreur que la tentative précédente vient d'écrire.

**Ce que ce lot ne fait toujours pas** : un seul moteur OCR (pas de moteur de
secours), aucun remplacement du fichier source depuis l'interface, aucune annulation
de job. Un `ocr_no_text` reste irrécupérable automatiquement.

## Atomicité & idempotence

- **Par document** : `reindex_existing_document` remplace les chunks dans une
  transaction de publication **courte** — les anciens ne sont supprimés qu'en phase de
  publication, après extraction, embeddings, identité de pipeline stable et revalidation
  de l'espace (*implémenté*, INGEST-1C). Extraction, chunking et embeddings s'exécutent
  **hors** de toute transaction et sans verrou (*implémenté*, INGEST-1F1, cf.
  [ingestion-extraction.md](ingestion-extraction.md) « Frontières transactionnelles »). En cas d'échec (extraction, résultat vide, embeddings,
  changement de pipeline, écriture) : **anciens chunks conservés à l'identique**, le
  document **revient à `READY`** (son index précédent reste servi), l'échec est consigné
  dans `error_message` et l'item est `FAILED`. Une première ingestion échouée reste
  `FAILED` sans chunk ; entre deux tentatives d'une erreur transitoire, le document est
  `PENDING` avec ses anciens chunks intacts. Règle générale (INGEST-1C-R2) : le statut
  du document exprime la **disponibilité du dernier index publié**, pas le résultat de
  la dernière tentative — quel que soit le point d'entrée (vue synchrone, job, worker).
- **Résultat de la tentative ≠ statut du document** (*implémenté*, INGEST-1D) : le
  service rend un `IngestResult(status=FAILED)` sans lever pour un échec métier. Le
  worker (`ingest_document_task` / `reindex_document_task`) renvoie alors
  `{"ok": false, "status": "FAILED", "doc_status": <READY|FAILED>, "error": …}` et émet
  `action.failed` (`error_code=ingest_failed`, `metadata.doc_status`) ; `action.completed`
  n'est émis que sur `IngestResult` READY. La vue synchrone
  `POST /documents/<id>/reindex/` répond `200 {"ok": false, "error_code":
  "reindex_attempt_failed", "message": …, "document": …}` et émet `action.failed` (même
  code HTTP et mêmes clés que le succès ; l'échec est porté par `ok`). Un item de job
  échoué reste `FAILED` même si le document est `READY`.
- **Retries du worker** (*implémenté*, INGEST-1D-R2) : une erreur transitoire avec
  retries restants => `PENDING`, `self.retry`, **aucun événement terminal** (seul
  `action.started`, émis à chaque tentative). Retries épuisés, `IngestError` définitive,
  source absente ou exception inattendue => sortie `ok=false` et **exactement un**
  `action.failed` (`error_code` = préfixe stable de l'erreur, `metadata.doc_status` =
  disponibilité du document : `READY` si l'ancien index reste actif, `FAILED` sinon),
  jamais d'`action.completed`. `request_id` et `task_id` sont ceux de la chaîne.
- **Item idempotent** : transition verrouillée `pending/queued → running` sous
  `select_for_update`. Une **double livraison** est absorbée (item terminal ou `running`
  au lease encore frais → ignoré), donc pas de double remplacement ni de compteur doublé.
- **Échec isolé** : une erreur document ne stoppe pas le job → finalisation
  `completed_with_errors`.

## Reprise après interruption

- Chaque item `running` porte un **lease** (`lease_expires_at = now + REINDEX_ITEM_LEASE_SECONDS`,
  défaut 900 s). Un worker mort laisse le lease **expirer**.
- **Reap** périodique (`reap_stale_reindex_items_task`, Celery beat toutes les 5 min) et
  commande manuelle :

  ```bash
  python manage.py recover_stale_reindex_items            # DRY-RUN (n'écrit rien)
  python manage.py recover_stale_reindex_items --apply    # remédiation
  ```

  → items `running` au lease expiré → **`interrupted`** ; documents restés `INGESTING`
  sans item actif **et dont la tentative est plus ancienne que le seuil** (voir
  ci-dessous) → `error_message=reindex_interrupted` et statut selon le dernier index
  publié : **`READY`** si des chunks publiés existent (l'ancien index reste servi ; la
  tentative interrompue reste portée par l'item `INTERRUPTED`), **`FAILED`** sinon
  (première ingestion interrompue). **Aucun chunk n'est supprimé** (*implémenté*,
  INGEST-1C-R2). Même règle à l'épuisement des retries du worker : index publié →
  `READY`, sinon `FAILED`. Conséquence : un document READY dont la dernière tentative a
  échoué n'entre pas dans le pool `retry-failed` (qui ne cible que `FAILED`) ; la reprise
  passe par l'item/job ou une réindexation explicite.
  Les items `interrupted` sont relançables via `retry-failed`.

### Ingestions actives : seuil d'âge et comparaison-échange *(implémenté)*

Le reaper ne sait pas si un processus traite encore un document : un document
`INGESTING` n'est **réputé mort** que si sa tentative est assez ancienne.

- **`Document.ingest_started_at`** : début de la tentative courante, écrit dans le **même
  `UPDATE`** que `status=INGESTING`, à l'heure de la base
  (`services/ingest/attempts.py`). Chaque exécution d'une tâche d'ingestion ou de
  réindexation (relance comprise) et chaque réindexation synchrone admise ouvre une
  tentative ; les écritures internes (réindexation, TX1 de l'indexation) conservent
  l'horodatage d'une tentative déjà ouverte.
- **Prédicat** : `INGESTING` **et** aucun item `running` au lease frais sur ce document
  **et** `COALESCE(ingest_started_at, created_at)` antérieur à
  `now − REINDEX_ITEM_LEASE_SECONDS` (900 s par défaut). `created_at` ne sert qu'aux
  lignes antérieures au champ (`NULL`).
- **Invariant** : ce seuil doit rester **strictement supérieur** à toute limite dure
  d'une exécution sans heartbeat — `CELERY_TASK_TIME_LIMIT` (600 s),
  `REINDEX_ITEM_TIME_LIMIT` (660 s), timeout d'extraction, timeout gunicorn (120 s).
  Un test le verrouille ; sans lui, une ingestion vivante (OCR, analyse visuelle…)
  redeviendrait récupérable et passerait transitoirement à `FAILED`/`READY`, ouvrant
  la déduplication, l'admission de réindexation, la suppression et la reprise des
  échecs à un état faux.
- **Comparaison-échange** : la mise à jour ne s'applique que si la ligne porte encore
  le statut `INGESTING` **et** l'horodatage lu (`IS NULL` pour une ligne historique).
  Une tentative redémarrée ou une publication survenue entre la sélection et l'écriture
  laisse la ligne intacte.
- **Journal** : une ligne `stale_ingestion_recovered` par document réellement récupéré
  (`document_id`, âge, horodatage, statut écrit, présence d'un index publié) ; le
  compteur `documents=` de `periodic.completed` ne compte que ces documents.

## Worker Celery en développement

Le contrat 202 exige un **worker** (sinon EAGER retomberait sur un traitement synchrone).

**Automatique** : `./scripts/dev/up.sh` démarre désormais Postgres **+ Redis** (compose dev,
loopback `127.0.0.1:${DEV_REDIS_PORT:-63790}`) **+ backend + worker Celery + frontend**. Le
worker n'est PAS lancé si `CELERY_TASK_ALWAYS_EAGER=1`.

**Manuel** (broker Redis joignable, depuis `backend/`) :

```bash
celery -A config worker -l info -Q celery,reindex,analytics
```

**Capability** : `GET /api/v1/capabilities/` expose
`reindex: { async_mode, celery_worker_available }` — le frontend sait si un worker répond
(ping caché ~15 s). Voir `apps/rag_core/services/ingest/celery_health.py`.

**Garde EAGER** (`REINDEX_EAGER_MAX_SYNC_DOCUMENTS`, défaut 25) : si
`CELERY_TASK_ALWAYS_EAGER=1` et le corpus dépasse le seuil, l'endpoint **refuse** (`503`)
au lieu de bloquer la requête — un développeur sans worker ne déclenche jamais une requête
de 30 min. En dessous du seuil (tests), l'exécution inline reste possible.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `REINDEX_ITEM_LEASE_SECONDS` | `900` | Durée du lease d'un item (détection stale) |
| `REINDEX_ITEM_SOFT_TIME_LIMIT` / `REINDEX_ITEM_TIME_LIMIT` | `600` / `660` | Limites de temps d'un item |
| `REINDEX_EAGER_MAX_SYNC_DOCUMENTS` | `25` | Seuil de refus en mode EAGER |

## Procédure opérationnelle

1. **Lancer** : `POST /rag-spaces/<id>/reindex/` → noter `job_id`.
2. **Suivre** : `GET /reindex-jobs/<job_id>/` (polling 2–5 s) jusqu'à un statut terminal.
3. **Diagnostiquer** : `failed_items[]` liste document_id/filename/error_code/attempt_count.
4. **Reprendre** : après crash, le reap récupère les items stale (ou
   `recover_stale_reindex_items --apply`).
5. **Relancer les échecs** : `POST /reindex-jobs/<job_id>/retry-failed/` → nouveau job lié.

### Réindexation des espaces de benchmark

**Gated.** Ne pas lancer sans feu vert. Procédure : récupérer les items stale
(`recover_stale_reindex_items --apply`), lancer un job, suivre jusqu'au terminal, vérifier
qu'aucun chunk Excel ne dépasse 4000 caractères
([ingestion-extraction.md](ingestion-extraction.md#plafond-strict-des-chunks-excel-lignes--colonnes--narratif--cellule)),
refaire les smoke tests retrieval.

## Références d'implémentation

- Modèles : `apps/rag_core/models/reindex_job.py`
- Service : `apps/rag_core/services/ingest/reindex_jobs.py`, `apps/rag_core/services/ingest/{retry_strategy,attempts}.py`
- Tâches : `apps/rag_core/tasks.py` (`run_reindex_job_task`, `reindex_job_item_task`,
  `reap_stale_reindex_items_task`)
- API : `api/v1/ragspaces/views.py`, `api/v1/ragspaces/urls.py`, `api/v1/urls.py`
- Commande : `apps/rag_core/management/commands/recover_stale_reindex_items.py`
- Frontend : `frontend/src/app/workspace-admin/_components/SpacesTab.tsx`, `frontend/src/lib/reindex.ts`
- Voir aussi : [taches-asynchrones.md](taches-asynchrones.md)

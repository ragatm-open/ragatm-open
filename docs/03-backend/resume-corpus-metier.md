# Résumé des documents de corpus métier

*Statut : **implémenté** ; **activé par défaut** dans le code
(`DOMAIN_CORPUS_SUMMARY_ENABLED`, défaut `1`) ; **testé** (backend + frontend) ;
**déployé** (backend **et** frontend) ; **non qualifié** — aucun parcours manuel n'a
encore été exécuté sur un environnement réel.*

> **Défaut de code ≠ état déployé.** « Activé par défaut » signifie qu'un environnement qui
> ne pose aucun override obtient la fonctionnalité. Cela ne dit rien de ce qui tourne
> réellement sur un environnement donné, ni de sa qualification. Voir
> [Activation et rollback](#activation-et-rollback).

Résumer un document — ou une page — appartenant au [corpus métier partagé](corpus-metier.md),
depuis les actions du panneau de sources du chat.

## Le problème résolu

Le panneau de sources affiche « Résumer le document » et « Résumer la page N » pour toute
source portant un `document_id`, **y compris** celles du corpus métier. Le frontend envoyait
cet identifiant vers `POST /api/v1/documents/<uuid>/summary/`, dont le résolveur
(`load_accessible_document`) ne cherche que dans la table privée `rag_document`. Un
`DomainCorpusDocument` n'y figure jamais : la réponse était `404 not_found`, affichée
« Document introuvable ou inaccessible ».

Le défaut était plus profond qu'un mauvais résolveur : il n'existait **ni endpoint, ni service,
ni table de cache** pour cette famille de documents, et `DocumentSummary` porte deux clés
étrangères dures (`Document`, `RagSpace`) qu'un document de corpus ne peut pas satisfaire.

## Contrat de source discriminé

Une citation transporte sa provenance depuis la v2 du contrat de sources
(`source_type` ∈ `workspace_document` | `domain_corpus`, cf.
[pipeline-retrieval.md](pipeline-retrieval.md)). Toute action déclenchée depuis une citation
consomme désormais une **référence discriminée**, jamais un identifiant nu :

```jsonc
{ "source_type": "domain_corpus", "document_id": "<uuid>", "page_number": 13 }
```

C'est la provenance — jamais la forme de l'UUID — qui détermine l'API appelée. Les deux
espaces d'identifiants sont **disjoints** : aucune résolution par essais successifs n'est
tolérée, dans aucun sens.

| Provenance | Endpoint | Motif |
|---|---|---|
| `workspace_document` | `/api/v1/documents/<uuid>/summary/` | inchangé |
| `domain_corpus` | `/api/v1/domain-corpus-documents/<uuid>/summary/` | **nouveau** |
| `unknown` (absent) | `/api/v1/documents/<uuid>/summary/` | **compatibilité historique** — voir ci-dessous |

### Compatibilité historique

Les sources des conversations antérieures à la v2 du contrat n'ont pas de `source_type`. Le
repli vers l'endpoint privé n'est pas une supposition : à cette date, seuls les documents
privés existaient, et seul le résumé privé était implémenté. Leurs actions sont **conservées**
— les retirer punirait l'historique sans rien sécuriser, l'API privée vérifiant elle-même
l'accès. Même règle que le lien viewer (`srcViewerHref`), qui conserve déjà ce repli.

Côté fil de discussion, `ChatMessage.meta` porte la provenance des messages de résumé. Un
message écrit avant ce champ vaut `workspace_document` à la lecture (`source_ref_of`) : aucun
backfill n'est nécessaire.

## Endpoint

```
POST /api/v1/domain-corpus-documents/<uuid:document_id>/summary/
GET  /api/v1/domain-corpus-documents/<uuid:document_id>/summary/
```

Corps du POST — compatible avec l'endpoint privé, plus `rag_space_id` :

```jsonc
{
  "mode": "bullets",            // short | detailed | bullets | executive
  "scope": "page",              // document | page
  "page": 13,                   // requis si scope=page (sinon repli sur `document`)
  "language": "auto",           // auto | fr | en
  "refresh": false,
  "rag_space_id": "<uuid>",     // espace CONSOMMATEUR (voir « Politique LLM »)
  "thread_id": "<uuid>",        // facultatif ; persistance dans le fil
  "request_id": "<uuid>"        // obligatoire dès que thread_id est fourni
}
```

Réponse : même enveloppe que le résumé privé, augmentée de `source_type`, `corpus_id` et
`corpus_name`.

Les règles de validation sont celles du chemin privé, appliquées par la **même** fonction
(`summary_service.normalize_summary_request`) : mode inconnu → `bullets`, scope inconnu →
`document`, `scope=page` sans `page` → `document`. Deux normalisations distinctes
produiraient deux clés de variante pour une même demande.

## Modèle de permissions

Un corpus métier n'appartient à aucun workspace. L'unique mécanisme d'accès reste
`WorkspaceDomainCorpusAccess` (association **existante**, `enabled=True`, corpus `is_active`),
exprimé par `allowed_corpus_ids_for_workspace` — **la même fonction que le retrieval**. Elle
n'est pas réécrite : si retrieval et résumé divergeaient, un utilisateur pourrait résumer un
corpus qu'il n'a pas le droit d'interroger.

Le résolveur dédié est `apps.corpus.services.access.load_accessible_domain_corpus_document`.
`load_accessible_document` (chemin privé) **reste mono-modèle** : le rendre polymorphe ferait
accepter un identifiant de corpus par les sept vues privées, dont celles qui suppriment et
réindexent.

### Détermination du workspace consommateur

Le workspace n'est **jamais** accepté depuis le client. Il est dérivé d'un `RagSpace` dont
l'accès est vérifié par la garde canonique `require_ragspace_access` :

1. `thread_id` fourni → le fil appartient déjà à l'appelant ; son `rag_space` fait foi ;
2. `rag_space_id` fourni → confronté à `require_ragspace_access` (existence, soft-delete,
   membership, périmètre owner) ;
3. les deux fournis → ils doivent désigner **le même** espace, sinon `404`. Présenter le fil
   d'un workspace tout en exécutant sous la politique d'un autre est exactement l'abus que ce
   contrôle empêche ;
4. aucun des deux → `404` : sans espace consommateur, aucune politique LLM n'est résolvable.

### Anti-énumération

Une **seule** réponse publique pour cinq causes distinctes : `404 {"detail": "not_found"}`.

| Cause interne | Réponse publique |
|---|---|
| document inexistant | `404 not_found` |
| corpus inactif | `404 not_found` |
| corpus non associé au workspace | `404 not_found` |
| association désactivée | `404 not_found` |
| fonctionnalité désactivée (flag) | `404 not_found` |

La cause réelle part dans un [événement structuré](#observabilité), jamais dans le corps de la
réponse. Le **statut documentaire**, lui, est révélé (`409 document_not_ready`) : un document
accessible mais non prêt est une information que l'utilisateur a le droit de recevoir.

## Politique LLM et egress

Un `DomainCorpusDocument` n'a pas de `RagSpace`. La décision retenue :

> **Le corpus fournit le contenu ; le RagSpace consommateur fournit la politique d'exécution**
> (provider, modèle, AI Gateway, politique d'egress, budgets, langue par défaut).

C'est la seule lecture qui préserve les deux invariants du projet : un workspace ne peut pas
se voir imposer un provider qu'il n'a pas choisi, et le contenu du corpus ne peut pas sortir
vers un fournisseur que la politique d'egress de cet espace interdit. Dériver la politique du
corpus lui-même — partagé, administré par le propriétaire global — laisserait une
configuration hors du contrôle du workspace décider de ses egress.

Corollaire assumé : **sans espace consommateur valide, on refuse**. Il n'existe aucun contexte
global de repli, qui serait par construction plus permissif que la politique qu'il remplace.
Voir [../06-ai/politique-egress.md](../06-ai/politique-egress.md).

## Moteur de résumé partagé

Les deux familles de documents partagent `apps.rag_core.services.summary_engine` : découpage
en blocs, budgets de contexte, prompts (FR/EN), boucle map-reduce, comptage des tokens,
décision sync/async. Les prompts sont donc identiques **à l'octet près** — condition pour
qu'un résumé de corpus soit comparable à un résumé privé, et pour qu'une correction de prompt
n'ait pas à être recopiée.

Ce qui reste propre à chaque service : la lecture des chunks (modèle concret), la résolution
du provider et de l'egress, le cache, l'audit et la persistance dans le fil.

### Récupération des chunks

`fetch_domain_corpus_summary_chunks(document, scope=, page=)` :

- modèle `DomainCorpusChunk`, filtré sur `document_id` ;
- ordre `chunk_index` puis `id` (départage les index dupliqués d'anciennes ingestions, pour
  que deux exécutions produisent la même empreinte, donc le même cache) ;
- `scope="page"` : **uniquement** `page_number=page`. Aucun repli silencieux vers une autre
  page ni vers le document entier — une page vide est une erreur métier explicite ;
- chunks vides écartés (ils fausseraient l'empreinte sans contribuer au contenu) ;
- lecture bornée : trois colonnes, par lots de 500 (`.only()` + `.iterator()`) ;
- embeddings jamais lus : un résumé se fait sur le texte.

## Persistance et cache

### Table sœur

`DomainCorpusDocumentSummary` (`domain_corpus_document_summary`) est une table **sœur** de
`rag_document_summary`, pas une extension. `DocumentSummary` porte deux FK dures ; les rendre
nullables et polymorphes ferait perdre l'intégrité référentielle sur le chemin privé — celui
qui est en production — pour accommoder un second cas. Un `GenericForeignKey` coûterait la
même intégrité plus une jointure `ContentType` par lecture de cache, pour un domaine qui ne
connaît que deux familles de documents.

Champs volontairement **absents** : `thread_id` et `request_id`. La ligne est un cache
partagé (plusieurs fils, plusieurs utilisateurs peuvent attendre le même résumé) ; y stocker
un fil d'origine n'en servirait qu'un seul. Le lien fil ↔ résumé est porté par `ChatMessage`
(`summary_id` + `request_id`), et la finalisation procède par fan-out.

### Clé de cache

Unique sur **`(rag_space, document, variant_key)`**, où `variant_key = "{mode}:{scope}:{page}:{language}"`.

`rag_space` entre dans la clé par **décision explicite** : la politique d'exécution venant de
l'espace consommateur, deux workspaces autorisés sur le même corpus peuvent résumer le même
document avec deux providers différents. Servir à l'un le résumé produit pour l'autre
contournerait sa configuration, et potentiellement sa politique d'egress.

### Invalidation

`source_fingerprint = sha256(version_de_prompt + [(chunk_id, longueur)…])`. Le cache est ignoré
et régénéré après : réingestion, réindexation modifiant le texte, remplacement du document,
modification des chunks, incrément de `SUMMARY_PROMPT_VERSION`. `refresh=true` passe outre et
incrémente `refresh_revision`.

> **L'autorisation est revérifiée avant toute restitution du cache**, y compris sur le GET de
> polling et au démarrage de la tâche Celery. Un accès révoqué bloque la livraison même si un
> résumé existe déjà en base.

## Persistance dans le fil de discussion

Identique au chemin privé (message utilisateur + message assistant `pending` écrits à la date
de la demande), avec en plus un contrat de métadonnées sur `ChatMessage.meta` :

```jsonc
{
  "message_type": "document_summary",
  "source": {
    "source_type": "domain_corpus",
    "document_id": "…", "corpus_id": "…", "corpus_name": "…",
    "filename": "…", "page_number": 13
  },
  "summary": { "scope": "page", "mode": "bullets", "language": "fr" }
}
```

Le chemin **privé** pose désormais le même contrat (`source_type: "workspace_document"`) :
sans cela, « pas de provenance » aurait deux sens — message ancien, ou message de corpus mal
écrit.

### Idempotence

Clé logique : `(thread, request_id, role)` **plus** `(source_type, document_id)`. Les deux
espaces d'UUID étant disjoints, rien n'interdit à un client de réutiliser un `request_id` pour
deux cibles différentes ; réutiliser l'échange existant répondrait alors le résumé d'un autre
document. Ce cas renvoie `409 request_id_conflict` plutôt que la mauvaise réponse.

## Observabilité

Événements structurés (`apps.corpus.services.summary_events`), **sans aucun contenu
documentaire** :

`domain_corpus_summary_requested` · `…_access_denied` · `…_not_found` · `…_started` ·
`…_completed` · `…_failed` · `…_cache_hit`

Les champs journalisables sont soumis à une **allowlist** : identifiants, énumérations et
compteurs. Tout champ non déclaré est **écarté**, pas tronqué — un extrait de document, un
prompt ou une clé d'API passés par erreur n'atteignent jamais le journal, et le test qui
l'affirme n'a qu'un seul endroit à surveiller. Toute structure (`dict`/`list`) est écartée :
c'est par là qu'un payload de source entrerait.

Causes internes différenciables derrière le `404` unique : `document_not_found`,
`corpus_not_authorized`, `workspace_missing`, `feature_disabled`,
`corpus_access_revoked_before_generation`.

## Asynchrone

Même critère que le privé (map-reduce **ou** estimation de tokens au-delà du seuil, `SUMMARY_ASYNC_ENABLED`,
hors mode eager) → `202 status="pending"`, polling par le GET.

- tâche : `apps.corpus.tasks.generate_domain_corpus_summary_task` (retry exponentiel sur
  erreur provider transitoire uniquement) ;
- reaper : `apps.corpus.tasks.reap_stale_domain_corpus_summaries_task`, planifié dans
  `CELERY_BEAT_SCHEDULE` (queue `maintenance`). Planifié inconditionnellement : no-op tant
  qu'aucune ligne n'existe, et il doit rester actif après une désactivation du flag pour
  rattraper les tâches déjà enfilées.

## Routeur d'actions déterministes

`apps.actions` (flag `ACTIONS_ROUTER_ENABLED`, **non activé par cette évolution**) distingue
désormais les deux provenances : une citation de corpus est résolue par le résolveur du
corpus et exécutée par le service du corpus. La résolution **par nom** reste bornée aux
documents privés de l'espace : un nom de fichier ne porte aucune provenance, et chercher dans
les deux familles rendrait le résultat dépendant d'une collision de noms. Une source de corpus
se résume depuis sa citation, où la provenance est explicite.

## Limites connues

- Le résumé de corpus n'est pas proposé depuis la modale Documents (elle ne liste que le privé).
- Pas de `RagQueryLog` pour ces résumés (`log_id` toujours `null`) : ce journal est indexé par
  `rag_space` **et** `workspace` du document, notions absentes d'un document de corpus. Les
  coûts et tokens sont tracés sur la ligne de cache.
- La commande en langage naturel « résume le document X.pdf » ne cible que le privé (voir
  ci-dessus).

## Activation et rollback

Voir [../10-reference/feature-flags.md](../10-reference/feature-flags.md) et
la documentation d'exploitation (hors périmètre de cette édition).

- **Défaut de code** : `DOMAIN_CORPUS_SUMMARY_ENABLED=1`. Un environnement qui ne pose
  aucun override active donc la fonctionnalité au redémarrage.
- **Priorité** : la variable d'environnement l'emporte toujours sur le défaut de code
  (`_env_flag("DOMAIN_CORPUS_SUMMARY_ENABLED", "1")`). Le registre canonique ne pilote pas le
  runtime : il déclare, documente et audite.
- **Source de vérité** : le BACKEND. Le frontend n'a aucune connaissance du flag et affiche
  toujours l'action ; c'est l'API qui refuse (`404`) quand il vaut `0`.
- **Rollback** : `DOMAIN_CORPUS_SUMMARY_ENABLED=0`, puis redémarrage des processus concernés
  (API et workers). Les résumés déjà produits sont **conservés** (aucune suppression
  automatique), les résumés privés ne sont pas affectés, et aucun rollback de migration n'est
  nécessaire en urgence. La migration `corpus.0005` reste réversible.

Quatre états à ne pas confondre :

| État | Signification | Aujourd'hui |
|---|---|---|
| Activé par défaut dans le code | absence d'override ⇒ fonctionnalité active | ✅ |
| Activé sur un environnement | l'environnement ne pose pas `=0` | ✅ aucun override posé, valeur effective `True` |
| Réellement déployé | la révision porteuse tourne sur l'environnement | ✅ `b1f87b5`, backend **et** frontend, 31/07/2026 |
| Qualifié | parcours manuel exécuté et validé | ❌ **non réalisé** |

> **Le déploiement porte sur DEUX images.** Le 31/07/2026, un déploiement n'a rebâti que
> le backend : la route et le flag étaient en place, mais le bundle frontend restait celui
> d'une révision antérieure et continuait d'envoyer les identifiants de corpus vers
> `/api/v1/documents/<uuid>/summary/` — donc `404 not_found`, symptôme **identique** à
> celui d'avant le correctif. Vérification qui tranche, sur l'hôte :
>
> ```bash
> docker exec <projet>-frontend-1 sh -lc \
>   'grep -rho "api/v1/domain-corpus-documents" /app/.next/server /app/.next/static | wc -l'
> # 0 = bundle antérieur au correctif ; ≥1 = dispatch par provenance présent
> ```

## Références d'implémentation

- Moteur partagé : `backend/apps/rag_core/services/summary_engine.py`
- Service corpus : `backend/apps/corpus/services/summary.py`
- Résolveur d'accès : `backend/apps/corpus/services/access.py`
- Événements : `backend/apps/corpus/services/summary_events.py`
- Modèle de cache : `backend/apps/corpus/models/summary.py`
- Tâches : `backend/apps/corpus/tasks.py`
- Vue et route : `backend/api/v1/corpus_documents/views.py`, `backend/api/v1/corpus_documents/urls.py`
- Persistance fil : `backend/apps/chat/services/summary_messages.py`
- Frontend — référence discriminée : `frontend/src/app/chat/_lib/sources.ts`
- Frontend — dispatch : `frontend/src/lib/api.ts`
- Frontend — actions : `frontend/src/app/chat/_components/SourcesPanel.tsx`
- Tests : `tests/tests_root/http_api/test_domain_corpus_summary_api.py`,
  `tests/tests_root/http_api/test_domain_corpus_summary_chat_persistence.py`,
  `tests/tests_root/corpus/test_summary_service.py`,
  `tests/tests_root/integration/test_domain_corpus_summary_flow.py`,
  `tests/frontend/lib/summarySourceDispatch.test.ts`,
  `tests/frontend/components/SourcesPanelSummaryActions.test.tsx`

# Liste des documents — pagination, compteurs, recherche et tri

Objectif : décrire le contrat de `GET /api/v1/documents/`, qui alimente l'écran
Documents. Public : développeurs backend et frontend.

> Statut : **implémenté / activé par défaut** (lot 1). Aucun flag, aucune migration.

## Pourquoi

L'écran chargeait `?limit=200` en une fois, puis calculait tout **localement** :
`Tous = items.length`, filtres et recherche appliqués au tableau déjà chargé. Sur un
espace réel de **1 094 documents**, l'écran affichait donc « Tous 200 » et
« Erreurs 51 » — 51 erreurs *parmi les 200 plus récents*, pas les erreurs de
l'espace. **894 documents n'étaient atteignables par aucun chemin de l'interface** :
ni pagination, ni recherche (locale), ni filtre (local). Le plafond de 200 est
imposé côté serveur, donc augmenter `limit` côté client n'y changeait rien.

Deux chiffres coexistaient par ailleurs sans être distingués : le bandeau
« Fichiers : N sur M » compte les documents de **tout le workspace** (tous les
espaces, tous les statuts, y compris les espaces *soft-deleted* non purgés — cf.
[`quota_consuming_documents`](../../backend/apps/workspaces/services/quota.py)),
là où la liste ne montre qu'**un** RagSpace. L'écart paraissait inexpliqué.

## Contrat

```
GET /api/v1/documents/
    ?rag_space_id=<uuid>          (obligatoire)
    &limit=50&offset=0            (défaut 50, maximum 200)
    &status=all|ready|analyzing|queued|error
    &search=<texte>               (≤ 200 caractères, tronqué au-delà)
    &ordering=filename|file_type|status|created_at   (préfixe `-` = décroissant)
```

```json
{
  "items": [ { "...": "...", "file_type": "PDF", "chunks_count": 12 } ],
  "total": 237,
  "limit": 50,
  "offset": 0,
  "has_next": true,
  "has_previous": false,
  "ordering": "-created_at",
  "counts": { "all": 237, "ready": 220, "analyzing": 3, "queued": 4, "error": 10 }
}
```

L'enveloppe historique `{items, total, limit, offset}` est **inchangée** ; les autres
clés sont additives. Convention `limit`/`offset` identique à celle déjà en service
côté console plateforme (mêmes bornes : défaut 50, maximum 200).

### Sémantique des compteurs

Le pipeline est fixé, et l'ordre compte :

```
documents du RagSpace → recherche → AGRÉGATS (counts) → filtre de statut → tri → pagination
```

- `counts` est calculé **après la recherche** et **avant le filtre de statut**.
  Sans recherche, il décrit tout l'espace ; avec recherche, il décrit ce que
  l'utilisateur peut effectivement trouver. Un onglet actif ne met donc **jamais**
  les autres compteurs à zéro.
- `total` suit **la recherche ET le filtre** : c'est le dénominateur de la plage
  affichée (« 51–100 sur 237 »). Sans filtre, `total == counts.all`.
- `counts.queued` est **déduit** (`all - ready - analyzing - error`) : un statut
  inattendu en base compte comme « en attente », exactement comme le repli de
  `toDocStatus()` côté frontend. `somme(counts) == counts.all` est donc toujours
  vrai.

### Correspondance statut d'ingestion ↔ onglet

| Onglet | Statut stocké |
|---|---|
| `ready` | `READY` |
| `analyzing` | `INGESTING` |
| `queued` | `PENDING` **et tout statut inconnu** |
| `error` | `FAILED` |

Source unique : `UI_STATUS_TO_DB` dans
[`backend/api/v1/documents/listing.py`](../../backend/api/v1/documents/listing.py).
Ne pas la redéclarer ailleurs.

### Ordre métier des statuts (`?ordering=status`)

`PENDING` → `INGESTING` → `READY` → `FAILED` (`STATUS_SORT_ORDER`), c'est-à-dire le
**cycle d'ingestion** : ce qui attend, ce qui travaille, ce qui est prêt, ce qui a
échoué. `-status` inverse cet ordre. L'ordre alphabétique des valeurs stockées
(`FAILED < INGESTING < PENDING < READY`) ne veut rien dire pour un utilisateur et
n'est jamais employé.

### Type de fichier (`file_type`, `?ordering=file_type`)

Champ **calculé**, jamais stocké : `content_type` (détecté par CONTENU à
l'ingestion) consulté en premier via `FILE_TYPE_BY_MIME`, repli sur l'extension du
nom (ASCII, ≤ 8 caractères), chaîne vide si ni l'un ni l'autre ne renseigne. Même
doctrine que [`preview_kind`](../../backend/api/v1/documents/preview_kind.py) :
le backend décide, l'extension ne contredit jamais un `content_type` connu.

Le libellé sérialisé (`file_type_label`) et l'expression SQL de tri
(`file_type_expression`) sont bâtis depuis **la même table** : l'ordre affiché ne
peut pas contredire la colonne affichée. Un test le vérifie sur des cas limites
(`photo.bin` en `image/jpeg`, `archive.tar.gz`, `application/pdf; charset=binary`).

### Date et heure

`Document.created_at` est le **seul** horodatage du modèle : il n'existe ni
`ingested_at`, ni `updated_at`, ni date de passage à `READY`. Le tri et l'affichage
portent donc sur la **date d'import**, et l'interface l'appelle « Importé le » —
pas « Ingestion terminée le », qui promettrait une information que le modèle ne
porte pas. L'heure est affichée : deux documents importés le même jour ne se
distinguent pas sans elle.

### Validation des paramètres

| Paramètre | Invalide | Comportement |
|---|---|---|
| `limit` | non numérique | défaut 50 |
| `limit` | `> 200` / `< 1` | borné à 200 / 1 |
| `offset` | négatif ou non numérique | 0 |
| `offset` | au-delà du total | page vide, `has_next: false` (jamais d'erreur) |
| `status` | inconnu | **400** `invalid_status` + liste des valeurs admises |
| `ordering` | hors allowlist | **400** `invalid_ordering` + liste des champs admis |
| `search` | > 200 caractères | tronqué, aucune erreur |

Aucune chaîne venue du client n'atteint `order_by()` : l'allowlist
`ORDERING_FIELDS` est vérifiée avant toute construction de requête.

### Ordre stable

Tout tri est complété par un départage déterministe
(`… , -created_at, id`). Sans lui, deux documents importés dans la même seconde
peuvent changer de place entre deux pages — et un même document apparaîtrait deux
fois, ou disparaîtrait.

## Coût

Deux requêtes SQL par page, quel que soit le nombre de lignes :

1. une agrégation `COUNT(*) FILTER (WHERE status = …)` pour les compteurs ;
2. la page elle-même, avec `chunks_count` en **annotation** (`Count("chunks")`).

Le nombre d'extraits était auparavant compté **document par document** — une requête
par ligne, soit ~200 requêtes par affichage. Un test borne le budget pour interdire
le retour de ce N+1.

Index déjà présents et suffisants au volume gouverné par le quota (1 000 documents
par workspace par défaut) : `(rag_space, created_at)` sert le tri par défaut,
`(rag_space, status)` sert les agrégats et le filtre. Les tris par nom et par type
trient un jeu déjà restreint à un espace ; aucun index n'a été ajouté, et la
recherche reste un `ILIKE` sur `filename` — l'indexer ne se justifiera qu'au-delà
de ce volume.

## Frontend

`frontend/src/app/documents/page.tsx` n'effectue **plus aucun filtrage local** : la
page rendue est le résultat du serveur. Voir
[../04-frontend/frontend.md](../04-frontend/frontend.md).

- Recherche **temporisée** (350 ms) — pas une requête par frappe.
- Tout changement de critère (espace, onglet, recherche, tri, taille de page)
  **revient à la première page**.
- Une réponse dont la requête n'est plus la dernière émise est **jetée** : une
  recherche lente ne peut pas écraser un résultat plus récent arrivé avant elle.
- Le tri est exposé deux fois sur **le même état** : en-têtes de colonnes cliquables
  (avec `aria-sort`, actionnables au clavier) et sélecteur « Trier par », qui reste
  disponible quand la table est réduite.
- Le bandeau distingue explicitement « **Quota workspace** » (tous les espaces) de
  « **RagSpace : N documents** » (l'espace courant).

## Ce que ce lot ne fait pas

- Pas de synchronisation de l'état dans l'URL : le RagSpace courant vit dans le
  contexte applicatif et non dans la route, une URL portant `status`/`search`/`page`
  sans lui serait ambiguë au partage.

> **Suite (lot 2)** : la sélection multiple et la reprise en masse des documents en
> erreur s'appuient directement sur ce contrat — `?status=error`, `?search=` et les
> compteurs exacts en étaient le prérequis. Le mode de sélection « par filtre »
> réutilise littéralement `listing.search_filter` et `listing.status_filter`, de
> sorte que la sélection ne peut pas désigner autre chose que ce que l'écran
> affiche. Voir [reindexation-jobs.md](reindexation-jobs.md).

## Références d'implémentation

- [`backend/api/v1/documents/listing.py`](../../backend/api/v1/documents/listing.py) —
  statuts, ordre métier, table des types, allowlist de tri, agrégats, bornes.
- [`backend/api/v1/documents/views_read.py`](../../backend/api/v1/documents/views_read.py) —
  `DocumentListView` (validation, pipeline, enveloppe).
- [`backend/api/v1/documents/views_common.py`](../../backend/api/v1/documents/views_common.py) —
  `_serialize_document` (champ `file_type`).
- [`frontend/src/app/documents/_lib/documentsQuery.ts`](../../frontend/src/app/documents/_lib/documentsQuery.ts) —
  construction de la requête, allowlist miroir, bascule de tri, `aria-sort`.
- [`frontend/src/app/documents/_components/DocumentsPagination.tsx`](../../frontend/src/app/documents/_components/DocumentsPagination.tsx).
- Tests : `tests/tests_root/http_api/test_documents_list_api.py`,
  `tests/frontend/components/DocumentsListServerState.test.tsx`.

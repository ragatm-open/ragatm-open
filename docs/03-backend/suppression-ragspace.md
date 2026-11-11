# Suppression d'un RagSpace

Comportement de `DELETE /api/v1/rag-spaces/<id>/`. Public : développeurs, ops, sécurité.

> **`DELETE` d'un RagSpace = suppression DÉFINITIVE (purge irréversible).**
> Ce n'est **pas** un soft delete. Les données **et les fichiers** sont supprimés.

Statut : **implémenté**, couvert par des tests d'intégration réels (base PostgreSQL +
fichiers sur disque).

## Ce qui est supprimé

| Entité | Relation au RagSpace | Exclusive | Politique |
|---|---|---|---|
| `RagSpace` | — | — | **supprimé** (ligne détruite) |
| `Document` | FK `CASCADE` | oui | **supprimé** |
| `Chunk` (+ embeddings pgvector) | FK `CASCADE` | oui | **supprimé** (en lot) |
| `DocumentVersion` / `Page` (CV-3B) | FK `CASCADE` via `Document` | non (empreintes, géométrie) | **supprimés** (cascade ORM) |
| `PageRaster` / `Crop` (CV-3C) + fichiers `rag_docs/…/vision/…/crops/*.png` | FK `CASCADE` via `Page` | oui — un crop est une image extraite du document | lignes **supprimées** (cascade ORM), fichiers **supprimés** avant la base (arrêt au premier échec) |
| `DocumentSummary` | FK `CASCADE` | oui | **supprimé** |
| `CorpusAnalysisJob` | FK `CASCADE` | oui | **supprimé** |
| `ChatThread` / `ChatMessage` | FK `CASCADE` | oui | **supprimé** |
| `RagQueryLog` | FK `CASCADE` | oui — contient `question`/`answer`/`sources` | **supprimé** : c'est le sens d'une purge (le contenu utilisateur de l'espace disparaît) |
| `LlmSettingsChangeLog` | FK `CASCADE` | oui | **supprimé** |
| **Fichiers physiques** (`rag_docs/<space>/<doc>/…`) | `FileField` | oui | **supprimés** via l'API de stockage |
| **`ActionAuditEvent`** | `rag_space_id` = **UUID nu, pas de FK** | non | **CONSERVÉ** — l'audit survit à la purge |
| Corpus métier (`domain_corpus_*`) | aucune relation | non | **intact** |

**Aucun fichier n'est partagé** : le chemin est scopé `rag_docs/<rag_space_id>/<document_id>/<nom>`.
Deux documents — même à contenu identique — ont toujours des clés distinctes. Supprimer un
espace ne peut donc jamais détruire le fichier d'un autre. Propriété **vérifiée par un test**
(`test_two_documents_never_share_a_storage_key`).

> La déduplication par `sha256` est **scopée au RagSpace** : le même contenu ré-uploadé dans
> le *même* espace réutilise le document existant ; dans un *autre* espace, il crée un
> document et un fichier distincts.

## Ordre d'exécution (et pourquoi)

```
verrou (select_for_update)
   ↓
inventaire figé (documents, clés de fichiers, compteurs)
   ↓
suppression des FICHIERS physiques        ← d'abord
   ↓   (échec ⇒ on s'arrête : la base reste intacte)
suppression de la BASE (transaction)      ← ensuite
   ↓
audit `completed` (hors transaction)
```

**Pourquoi les fichiers d'abord.** Le pire scénario est « lignes supprimées + fichiers
présents » : les fichiers deviennent des orphelins **définitivement introuvables**, car plus
aucune référence ne permet de les retrouver. En supprimant les fichiers en premier, un échec
bascule vers le cas inverse — « fichiers supprimés + base intacte » — qui est **détectable**
(`manage.py audit_media_storage` le remonte en `missing_file`) et **réparable** (restauration).
On échange un risque silencieux contre un risque visible.

Base et stockage ne partagent aucune transaction distribuée : ce compromis est assumé, pas
contourné. L'opération est gardée courte et sous verrou pour réduire la fenêtre.

## Permissions

Deux gardes **cumulatives**, dans cet ordre :

1. **Périmètre** — `require_ragspace_access(permission="manage")`, la garde canonique,
   la même que celle du `PATCH`. Sous `OWNER_SCOPE_ENABLED`, elle refuse un espace hors
   du périmètre owner de l'appelant.
2. **Niveau** — rôle workspace `owner` ou `admin`.

| Acteur | Suppression |
|---|---|
| Owner du workspace, **dans** son périmètre | ✅ autorisée |
| Admin du workspace, **dans** son périmètre | ✅ autorisée |
| Owner / admin **hors** de son périmètre | ❌ refusée (`404` anti-énumération) |
| Member / contributeur / viewer | ❌ refusée (`403`) |
| Non-membre | ❌ refusée |
| **Superuser plateforme non membre** | ❌ refusée — **aucun bypass implicite** |
| Superuser également owner/admin du périmètre | ✅ autorisée — par son **rôle**, pas par son statut |

> **Pourquoi cette garde a été ajoutée (P0-1).** Le `DELETE` ne l'appliquait pas : sous
> enforcement, un owner pouvait **détruire** un espace d'un autre périmètre alors que le
> **renommer** lui était refusé. La protection manquait exactement là où l'action est
> irréversible. L'endpoint déclarait déjà `permission="manage"` dans son propre audit ;
> l'enforcement ne fait que rejoindre cette déclaration.

Flags `OFF` (défaut) : comportement legacy strictement inchangé — owner/admin du
workspace, sans notion de périmètre. Cf. [owner-scope.md](owner-scope.md).

## Codes HTTP

| Code | Signification |
|---|---|
| `204` | Purge terminée — base **et** fichiers supprimés |
| `401` | Non authentifié |
| `403` | Authentifié mais non autorisé ; `code: rag_space_delete_forbidden` |
| `404` | Identifiant inexistant, déjà purgé (2ᵉ appel), **ou hors périmètre owner** |
| `500` | Échec — **rien n'a été supprimé** ; `code: rag_space_deletion_failed` |

Un échec n'est **jamais** présenté comme un succès. Aucun chemin de stockage n'est exposé
dans la réponse. Un refus ne révèle ni le nom du workspace, ni celui de l'espace, ni
l'identité de l'owner responsable.

## Concurrence

- **Verrou** `select_for_update()` sur la ligne : deux purges simultanées sont sérialisées ;
  la seconde constate l'absence et renvoie `404`.
- **Upload** sur un espace purgé → `403`/`404` (la ligne n'existe plus).
- **Réindexation** : `reindex_existing_document` refuse un espace supprimé
  (`IngestError("rag_space_deleted")`) et, l'espace purgé, le document n'existe plus
  (`not_found`). **Aucun chunk ne peut être recréé** après la purge — la contrainte FK
  `rag_chunk.rag_space_id` l'interdit de toute façon au niveau base.
- Une tâche Celery retardée échoue proprement sur ces gardes.
- **Provenance (CV-3B)** : `ensure_document_version` / `ensure_pdf_pages` prennent le
  verrou RagSpace puis Document (ordre canonique) : une écriture de provenance concurrente
  attend la fin de la purge et ne trouve plus le document, ou la précède et sa ligne est
  emportée par la cascade. La FK `rag_document_version → rag_document` n'a pas d'`ON
  DELETE CASCADE` en base (cascade ORM, comme `rag_chunk`).

## Audit

Chaque purge écrit un `ActionAuditEvent` (`action_name="ragspace.delete"`), **hors de la
transaction** de suppression — sinon un rollback l'effacerait aussi.

| Statut | Quand |
|---|---|
| `started` | avant le verrou |
| `completed` | purge réussie |
| `failed` | échec (avec `error_code`) |

Contenu : acteur, `workspace_id`, `rag_space_id` **historique**, `rag_space_name` historique,
compteurs, durée. **Aucun contenu documentaire, aucun nom ni chemin de fichier, aucun
secret.** L'événement **survit** à la purge (`rag_space_id` est un UUID nu, pas une FK).

### Compteurs — identiques en succès **et en échec**

| Compteur | Sens |
|---|---|
| `documents_total` | Documents inventoriés sous verrou |
| `chunks_total` | Extraits indexés inventoriés |
| `files_total` | Documents portant réellement un fichier (un document sans fichier ne compte pas) |
| `files_deleted` | Fichiers **effectivement supprimés** du stockage |
| `files_missing` | Fichiers déjà absents (n'échoue jamais la purge) |
| `files_failed` | Fichiers dont la suppression a échoué |
| `vision_files_total` / `_deleted` / `_missing` / `_failed` | Mêmes compteurs pour les artefacts de crop (CV-3C), **séparés** : `files_*` reste « un fichier source par document » |

Un événement `failed` porte l'état **réellement atteint** au moment de l'échec :

```json
{
  "documents_total": 3, "chunks_total": 120, "files_total": 3,
  "files_deleted": 1, "files_missing": 0, "files_failed": 1,
  "error_code": "storage_delete_failed"
}
```

> **Pourquoi c'est indispensable (P0-4).** « La purge a échoué » ne dit pas si zéro ou
> dix-sept fichiers ont déjà été détruits — donc pas quoi restaurer. `files_deleted` est
> la liste de courses de la reprise. C'est aussi pourquoi la suppression des fichiers
> **s'arrête au premier échec** : chaque fichier détruit au-delà est une restauration de
> plus, pour une purge de toute façon condamnée.

Les clés historiques `documents_deleted` / `chunks_deleted` restent présentes sur les
événements `completed` : la nomenclature a été **ajoutée**, aucun consommateur n'est cassé.

### Supervision

Un `failed` n'atteignait personne : il fallait une requête SQL pour le découvrir. La
tâche `monitor_ragspace_deletion_failures_task` (read-only, désactivée par défaut) le
remonte désormais en journal `ERROR` structuré et dans `ops_health`. Activation,
déduplication et conduite à tenir :
la documentation d'exploitation (hors périmètre de cette édition).

## Limites connues

- **Espaces soft-deleted résiduels.** Avant le 2026-07-16, `DELETE` posait `deleted_at` sans
  rien supprimer. Les espaces marqués ainsi **existent encore** en base avec leurs documents,
  chunks et fichiers, et **restent invisibles dans l'interface** (les listes filtrent
  `deleted_at__isnull=True`). Ils ne sont plus produits, mais ne sont pas purgés
  automatiquement. **La voie de purge est la commande d'exploitation dédiée** :
  la documentation d'exploitation (hors périmètre de cette édition)
  (`manage.py purge_soft_deleted_ragspaces`, dry-run par défaut), qui appelle le service
  directement. Depuis l'ajout de la garde canonique (P0-1), l'API `DELETE` **refuse** un
  espace `deleted_at` (`403`), comme le `PATCH` et les stats : la garde ne fait aucune
  exception pour un espace marqué supprimé. Ce n'est pas une perte de capacité — ces
  espaces sont invisibles dans l'interface, seule la commande les atteignait en pratique.
- **Suppression synchrone.** La purge s'exécute dans la requête HTTP. Sur un espace très
  volumineux, elle peut approcher le timeout applicatif (mesuré : ~0,4 s pour 3 documents ;
  la suppression des chunks est faite en lot, sans charger les objets). Un basculement en job
  asynchrone n'a **pas** été implémenté : il ne le sera que sur un besoin mesuré.
- **Pas de cohérence transactionnelle base ↔ stockage** (cf. §Ordre) — compromis assumé,
  documenté, et désormais **outillé** : la détection (`audit_media_storage`) est
  planifiable et la supervision des échecs existe (cf.
  la documentation d'exploitation (hors périmètre de cette édition)).
  Les deux contrôles sont **désactivés par défaut** : les activer est un geste
  d'exploitation explicite, environnement par environnement.
- **Ingestion active non bloquante côté API.** La commande d'exploitation refuse un espace
  dont une ingestion ou un job est en cours ; l'endpoint `DELETE`, lui, purge quand même.
  Hors périmètre de ce lot (identifié comme P1).
- **Fenêtre d'upload concurrent.** L'inventaire des fichiers est figé sous verrou, la
  cascade ne l'est pas : un document créé pendant la purge voit sa ligne supprimée et son
  fichier subsister (orphelin détectable). Hors périmètre de ce lot (P1).
- Le champ `deleted_at` / `soft_delete()` **subsiste sur le modèle** mais n'est plus utilisé
  par ce parcours. Aucune fonction d'archivage n'est exposée : si elle devient un besoin,
  elle devra être un parcours **distinct** de la purge. Le retrait de ces champs reste
  impossible tant que des résidus historiques existent : la commande de purge s'en sert
  pour les inventorier.

## Sauvegarde — ce que la purge ne rend pas réversible

> **Une sauvegarde n'est pas une corbeille.** Récupérer un RagSpace purgé par erreur impose
> une restauration **globale** (dump PostgreSQL + archive média) dans un environnement
> **séparé**, puis une extraction manuelle. C'est une opération d'ingénierie, pas un geste
> utilisateur — et appliquée en place, elle écraserait tout ce qui a été écrit depuis.

Le dispositif de sauvegarde couvre base et fichiers de façon coordonnée :
la documentation d'exploitation (hors périmètre de cette édition).

## Références d'implémentation

- `backend/apps/rag_core/services/rag_space_deletion.py` — le service (point d'entrée unique)
- `backend/api/v1/ragspaces/views.py` — vue mince (permissions + mapping HTTP)
- `backend/apps/workspaces/services/access.py` — `require_ragspace_access` (garde canonique)
- `backend/apps/rag_core/services/ingest/reindex.py` — garde `rag_space_deleted`
- `backend/apps/rag_core/services/media_storage_audit.py` — cohérence base ↔ fichiers (read-only)
- `backend/apps/rag_core/services/rag_space_deletion_monitoring.py` — supervision des échecs
- `frontend/src/app/workspace-admin/page.tsx` — `SpacesTab` (modale, anti double-clic)
- `tests/tests_root/integration/test_ragspace_hard_delete.py` — base **et** fichiers réels
- `tests/tests_root/integration/test_ragspace_delete_owner_scope.py` — périmètre owner
- `tests/tests_root/integration/test_ragspace_delete_counters.py` — compteurs (succès et échec)
- `tests/frontend/components/SpacesTabDelete.test.tsx` — UI
- `backend/apps/rag_core/management/commands/audit_media_storage.py` — détection d'orphelins
- `backend/apps/rag_core/management/commands/monitor_ragspace_deletions.py` — purges échouées

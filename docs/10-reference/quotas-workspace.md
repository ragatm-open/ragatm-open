# Quotas documentaires par workspace

Statut : *implémenté / enforcement activé par défaut (flag `WORKSPACE_QUOTAS_ENFORCED`)*.

Chaque **Workspace** porte deux quotas documentaires explicites :

- `max_document_count` — nombre maximal de fichiers ;
- `max_document_bytes` — taille cumulée maximale des **fichiers sources** (octets).

Les valeurs sont **copiées** depuis les défauts de plateforme à la création du workspace ;
une modification ultérieure des défauts n'a **aucun effet rétroactif**. Pas de mode
« illimité » (jamais de `NULL` ambigu) ; les deux limites sont strictement positives
(contrainte SQL).

## Définition de l'usage

**Usage = toutes les lignes `Document` du workspace** (`Document → rag_space → workspace`),
agrégées à la demande : `Count(id)` et `Sum(size_bytes)`. Aucune table compteur, aucun
recalcul de la taille par lecture du stockage objet (`size_bytes` est la métadonnée
canonique persistée à l'upload).

- Politique centralisée en un point unique : `quota_consuming_documents(workspace)`
  (`backend/apps/workspaces/services/quota.py`).
- Un document en **échec d'ingestion** (`status=FAILED`) **compte** : son fichier source
  reste physiquement stocké (conservé pour réindexation). La suppression d'un document est
  **dure et immédiate** dans ce dépôt (pas de corbeille documentaire) : les octets sont
  libérés à la suppression, aucun état « corbeille » à réconcilier.
- La **réindexation** ne modifie jamais l'usage (même fichier source). Un **doublon de
  contenu** (dédup par `sha256`) n'ajoute ni fichier ni octet : il ne consomme pas de quota.
- Le périmètre est le **workspace entier** : tous ses RagSpaces, y compris ceux marqués
  supprimés mais non encore purgés. Ce chiffre ne doit donc **jamais** être lu comme le
  nombre de documents de l'espace affiché à l'écran — l'interface les présente séparément
  (« Quota workspace » vs « RagSpace : N documents »), cf.
  [../03-backend/liste-documents.md](../03-backend/liste-documents.md).

## Unités

Base de données et API : **octets** (entiers). L'interface propose Mo / Go / To (binaire,
1024ⁿ) et convertit en octets avant l'envoi — la valeur canonique reste toujours entière en
octets, jamais un flottant.

## Défauts de plateforme

Deux défauts, résolus dans l'ordre **override CoreConfig `global` > settings (env) > défaut
de code** :

- `WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT` (**5000** ; 1000 jusqu'à une version antérieure) ;
- `WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES` (10 Gio = 10737418240).

Éditables par un **superuser** via la console `admin-core` (clés CoreConfig
`WORKSPACE_DEFAULT_MAX_DOCUMENT_*`, scope `global`). L'écriture reste soumise au gate serveur
existant : **interdite en prod**, conditionnée à `CORE_CONFIG_EDIT_ENABLED`. En production,
les défauts se changent donc par configuration serveur (env / déploiement). Les variables
d'environnement ne remplacent pas l'administration superuser : elles servent de fallback /
initialisation. Cf. [variables-environnement.md](variables-environnement.md).

## Plafonds absolus (garde-fous serveur)

- `WORKSPACE_HARD_MAX_DOCUMENT_COUNT` (100000) ;
- `WORKSPACE_HARD_MAX_DOCUMENT_BYTES` (1 Tio = 1099511627776).

Settings only (non éditables à chaud). Un superuser ne peut fixer aucun quota au-delà sans
changer la configuration serveur.

## Bornes d'un import de dossier (frontend)

*Implémenté.* Garde-fous **ergonomiques** de la modale d'import, indépendants du quota :

| Borne | Valeur | Constante (`frontend/src/app/documents/_lib/folderImport.ts`) |
|---|---|---|
| Fichiers compatibles par import | **1 000** (500 avant) | `MAX_FOLDER_FILES` |
| Taille totale du lot | 1 024 Mio (1 Gio) | `MAX_FOLDER_TOTAL_MB` |
| Envois simultanés | 4 | `FOLDER_UPLOAD_CONCURRENCY` |

Au-delà de l'une des deux premières bornes, l'import est **bloqué** avec un message
explicite (`folderTooManyFiles`, `folderTooLarge`). Un import reste une suite d'**uploads
unitaires** cadencés (voir [import-cadence-et-reprise.md](../04-frontend/import-cadence-et-reprise.md)),
jamais une requête de 1 000 fichiers. La sélection multiple et le glisser-déposer n'ont pas
de borne de nombre.

**Interaction avec le quota** : un import de 1 000 fichiers passe **entièrement** tant que
le workspace dispose de 1 000 places (par exemple 4 000 documents sur 5 000). Sinon, le serveur
refuse fichier par fichier (`workspace_quota_exceeded`) une fois la capacité atteinte, et les
fichiers déjà acceptés restent acquis. La borne d'import (1 000) et le quota (5 000 par défaut)
sont indépendants : relever l'un ne change pas l'autre.

## Comportement en dépassement (réduction sous l'usage)

Un superuser peut réduire un quota **sous la consommation actuelle**. Aucun document n'est
supprimé ; téléchargement, consultation, chat/retrieval et **suppression** restent
autorisés ; seuls les **nouveaux uploads** sont bloqués tant que l'usage ne repasse pas sous
la limite. La réponse de modification signale l'état (`is_exceeded`) et un `warning`.
L'interface demande une **confirmation explicite** avant d'appliquer une telle réduction.

## Concurrence

L'upload de fichier vérifie le quota **sous verrou** : `select_for_update()` sur la ligne
`WorkspaceQuota` sérialise les uploads concurrents d'un même workspace, puis crée la ligne
`Document` dans la **même transaction** (la ligne créée sert de réservation). Deux requêtes
simultanées ne peuvent donc pas être acceptées sur le même usage initial — garantie valable
**multi-processus / multi-workers** (verrou de ligne PostgreSQL). L'ingestion **texte**
(synchrone et asynchrone) et les primitives `pipeline.ingest_document_from_text` /
`ingest_document_from_upload` (façade Core) appliquent la **même réservation** à la
création de la ligne (*implémenté*, INGEST-1E3 : « reserve where you create »,
`create_document_with_quota_reservation` → `check_and_reserve`) ; la pré-vérification
`check_upload_allowed` de l'endpoint texte n'est qu'un échec rapide, la décision
autoritative est prise sous verrou. Le verrou ne couvre que dédup, relecture de l'usage et
création : jamais le stockage, l'extraction ni les embeddings. Aucune réservation
orpheline : un échec d'ingestion conserve la ligne `Document` (qui continue de compter, cf.
usage). Deux requêtes simultanées portant le **même contenu** produisent une seule ligne
(index unique partiel) et un seul quota.

## Endpoints

| Méthode & chemin | Rôle | Permission |
|---|---|---|
| `POST /api/v1/platform/workspaces/` | Création : accepte `quota:{max_document_count?, max_document_bytes?}` (optionnel → défauts). Transactionnel (workspace + owner + quota). | superuser |
| `PATCH /api/v1/platform/workspaces/<id>/quota/` | Modifie les quotas (**PATCH partiel** : une limite sans écraser l'autre) `{max_document_count?, max_document_bytes?, reason?}`. | superuser |
| `GET /api/v1/platform/workspace-quota-defaults/` | Défauts + plafonds (préremplissage). | superuser |
| `GET /api/v1/platform/workspaces/<id>/` | Fiche workspace, inclut `quota` (usage). | superuser |
| `GET /api/v1/workspaces/<id>/quota/` | Quota + usage (lecture seule). | owner / admin workspace |

Lecture `GET .../quota/` — forme de réponse : `max_document_count`, `used_document_count`,
`remaining_document_count` (jamais négatif), idem `*_bytes`, `count_usage_percent`,
`size_usage_percent`, `is_count_exceeded`, `is_size_exceeded`, `is_exceeded`, `can_upload`,
`enforced`.

## Codes d'erreur (upload)

**HTTP 409 Conflict** avec un corps structuré :

- `workspace_file_count_quota_exceeded` → `quota:{max, used, requested, remaining}` ;
- `workspace_storage_quota_exceeded` → `quota:{max_bytes, used_bytes, requested_bytes, remaining_bytes}`.

Si les deux dimensions dépassent, la réponse liste les deux (`errors:[…]`). Le backend est la
**seule source de vérité** : aucune valeur d'usage envoyée par le client n'est acceptée.

Validation des limites (400) : `must_be_integer`, `must_be_positive`, `exceeds_hard_cap` ;
PATCH : `unknown_fields` (contrat fermé), `no_quota_change`, `workspace_not_found` (404).

## Audit

Tout changement produit un événement d'audit append-only (`emit_action_event`,
`resource_type="workspace"`) : `workspace.quota.created` (création) et
`workspace.quota.updated` (modification, métadonnées ancienne/nouvelle limite, `reason`,
état de dépassement). Aucun événement n'est émis sur un refus de permission (403 avant la
vue).

## Variables d'environnement

Voir [variables-environnement.md](variables-environnement.md) et
[feature-flags.md](feature-flags.md).

```bash
WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT=5000
WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES=10737418240
WORKSPACE_HARD_MAX_DOCUMENT_COUNT=100000
WORKSPACE_HARD_MAX_DOCUMENT_BYTES=1099511627776
WORKSPACE_QUOTAS_ENFORCED=1
```

## Migration / déploiement / rollback

- Migration `workspaces/0005_workspacequota.py` : crée la table + **backfill** de tous les
  workspaces existants aux défauts de plateforme (aucun workspace sans quota). Réversible.
- Rollout progressif : déployer avec `WORKSPACE_QUOTAS_ENFORCED=0` (quotas calculés/affichés,
  uploads non bloqués), vérifier les usages, puis activer `=1`.
- **Rollback** : repasser `WORKSPACE_QUOTAS_ENFORCED=0` désactive l'enforcement **sans perdre
  les valeurs de quota**.

## Relever les workspaces restés à un ancien défaut

Un nouveau défaut ne s'applique qu'aux workspaces créés ensuite. Pour rattraper les
workspaces existants, `manage.py upgrade_default_workspace_quota --from-count 1000 --reason <lot>`
(inventaire seul par défaut, `--apply` pour exécuter, `--workspace <id>` répétable pour
restreindre). Seuls sont relevés les quotas dont l'origine « défaut standard » est
**prouvée** : valeur égale à l'ancien défaut, ligne jamais modifiée, et aucun événement
`workspace.quota.created` / `workspace.quota.updated` pour ce workspace (la console audite
toute création explicite et toute modification). Tout autre cas est préservé et signalé.
Seul le nombre de documents change ; chaque relèvement est audité (`workspace.quota.updated`,
motif préfixé `upgrade_default_workspace_quota:`). Retour arrière : la console plateforme
(`PATCH .../quota/`), audité de la même façon.

## Références d'implémentation

- `backend/apps/workspaces/models.py` (`WorkspaceQuota`),
  `backend/apps/workspaces/migrations/0005_workspacequota.py`
- `backend/apps/workspaces/services/quota.py` (usage, validation, réservation, audit)
- `backend/apps/workspaces/management/commands/upgrade_default_workspace_quota.py` (relèvement à origine prouvée)
- `backend/api/v1/platform_admin/mutations.py` (création + `PlatformWorkspaceQuotaView`),
  `backend/api/v1/platform_admin/views.py` (`PlatformWorkspaceQuotaDefaultsView`, bloc quota
  du détail)
- `backend/api/v1/workspaces/views.py` (`WorkspaceQuotaView`)
- `backend/api/v1/documents/views_write.py` (enforcement upload / ingestion)
- `backend/apps/rag_core/core/config_resolver.py` (clés `KeySpec` des défauts)
- `backend/config/settings/base.py` (défauts, plafonds, flag)
- Frontend : `frontend/src/lib/quota.ts`, `frontend/src/app/platform-admin/_components/QuotaFields.tsx`,
  `PlatformModals.tsx`, `WorkspacesTab.tsx`,
  `frontend/src/app/admin-core/_components/WorkspaceQuotaDefaultsPanel.tsx`,
  `frontend/src/app/documents/page.tsx`

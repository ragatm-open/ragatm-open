# Console d'administration plateforme (superuser)

> Statut : **implémenté** (API + frontend), **non branché à un flag** (lecture/gouvernance
> pure) ; le **transfert de périmètre** est livré en **prévisualisation seule** — l'application
> effective est un **NO-GO explicite** (voir plus bas). Console réservée au **superuser**.

Gouvernance globale de la plateforme : utilisateurs, workspaces, memberships, owners et
**owner-scopes** (périmètres). Elle complète l'administration *intra-périmètre*
([owner-scope.md](owner-scope.md), gestion des `RagSpaceAccess`) par une vue *cross-workspace*.

## Doctrine de sécurité (non négociable)

- **Superuser sans bypass de contenu.** La console expose la gouvernance (identités,
  appartenances, périmètres, comptages, diagnostics) mais **jamais** le contenu (documents,
  chats, prompts, chunks, résultats d'analyse). `is_superuser` n'accorde **aucun** accès
  implicite au contenu des espaces : les vues plateforme ne renvoient que des métadonnées.
- **Backend autoritaire.** Le frontend ne calcule aucun owner-scope, ne déduit aucun owner,
  ne modifie aucune relation sans passer par les **services canoniques** transactionnels.
- **Pas de hard-delete.** Workspace → désactivation douce (`is_active=False`) ; accès RagSpace →
  révocation (`revoked_at`). Aucune suppression physique introduite.
- **Pas d'attribution silencieuse.** Toute opération sur un owner / owner-scope affiche ses
  conséquences, exige une confirmation, est **auditée**, refuse les incohérences, est
  transactionnelle, et ne choisit jamais un owner automatiquement en cas d'ambiguïté.

## Garde

Toutes les vues `/api/v1/platform/*` exigent **`IsRagATMOwner`** (= utilisateur **actif
superuser**, source de vérité `apps/accounts/permissions`). Un rôle workspace owner/admin ne
suffit **jamais**. Refus : **401** (anonyme), **403** (authentifié non-superuser, superuser
inactif). La console n'est pas masquée par anti-énumération vis-à-vis d'un authentifié.

## Endpoints (`/api/v1/platform/`)

Lecture (paginée : `?limit&offset`, enveloppe `{items,total,limit,offset}` ; aucun contenu) :

| Endpoint | Rôle |
|---|---|
| `GET users/` | Annuaire (filtres `search`, `is_active`, `is_superuser`, `workspace_id`, `role`), comptages memberships/owned |
| `GET users/<id>/` | Fiche : memberships, owner-scope effectif, owner ciblé, comptage `RagSpaceAccess` actifs |
| `GET workspaces/` | Liste (filtres `search`, `is_active`, `without_owner`, `with_anomalies`) + diagnostic **borné à la page** |
| `GET workspaces/<id>/` | Owners, memberships, owner-scopes, RagSpaces (**métadonnées minimales**), comptages, diagnostic |
| `GET owner-scope-diagnostics/` | Rapport complet, **cohérent byte-à-byte** avec `audit_workspace_owner_scopes` |
| `GET dashboard/` | Compteurs de gouvernance (users actifs, workspaces actifs/désactivés/sans owner, ragspaces sans périmètre, violations d'intégrité) |

Mutations (superuser, transactionnelles, auditées — délèguent aux services canoniques
`owner_scope_migration`) :

| Endpoint | Opération |
|---|---|
| `POST workspaces/` `{name, owner_user_id}` | Crée un workspace **et** son premier owner (atomique ; rollback si l'owner échoue) |
| `POST workspaces/<id>/owner/` `{user_id, allow_additional?, promote?}` | Attribue le premier owner (réutilise `create_workspace_owner`) |
| `POST workspaces/<id>/memberships/` `{user_id, role, owner_membership_id?}` | Ajoute un membre ; non-owner ⇒ owner-scope **obligatoire** |
| `PATCH workspaces/<id>/memberships/<mid>/owner-scope/` `{owner_membership_id, revoke_incompatible?}` | (Re)rattache un non-owner ; accès incompatibles ⇒ **409** puis révocation explicite |
| `POST workspaces/<id>/disable/` `{reason}` | Désactivation douce (motif **requis**) — même workspace sans owner |
| `POST workspaces/<id>/enable/` `{reason?}` | Réactivation — **refusée** si le workspace n'a pas d'owner (`workspace_without_owner`) |
| `PATCH workspaces/<id>/memberships/<mid>/` `{role, owner_membership_id?, revoke_incompatible?}` | Change le rôle workspace (promotion owner **interdite** ici ; D1 revalidé) |
| `DELETE workspaces/<id>/memberships/<mid>/` `{revoke_grants?}` | Retrait contrôlé (garde dernier owner / périmètre non vide / grants / jobs) |
| `POST users/<id>/disable/` `{reason, confirm_self?}` | Désactive un compte (`is_active=False`, motif requis, révoque les tokens) |
| `POST users/<id>/enable/` `{reason}` | Réactive un compte |
| `POST owner-scope-transfers/preview/` | **Prévisualisation** d'un transfert de périmètre (read-only) |
| `POST owner-scope-transfers/apply/` | **NO-GO explicite (501)** — non livré dans ce lot |

### Cycle de vie — doctrine (P1/P2)

- **Utilisateur** : jamais de hard-delete (désactivation `is_active=False` seule). Refus du
  **dernier superuser actif** (`last_active_superuser`) et de l'**auto-désactivation** sans
  `confirm_self` (`self_disable_requires_confirm`). Un compte inactif ne peut plus s'authentifier
  (DRF TokenAuth vérifie `is_active` ; les tokens sont en outre révoqués).
- **Rôle de membership** : promotion **owner interdite** par PATCH (workflow explicite
  `.../owner/`). Rétrograder le **dernier owner** est refusé (`last_owner`) ; un owner à
  **périmètre non vide** n'est pas rétrogradable (`owner_scope_not_empty`). Le **plafond D1** des
  `RagSpaceAccess` est revalidé (`grants_exceed_role` → 409, puis révocation explicite).
- **Retrait de membership** : refus du dernier owner et d'un owner à périmètre non vide ; blocage
  sur **grants actifs** (`active_grants`, sauf `revoke_grants=true`) et **jobs de réindex actifs**
  (`active_jobs`). Suppression physique du membership (pattern existant) après révocation des grants.
- **Workspace sans owner** : **peut être désactivé**, **ne peut PAS être réactivé** pour usage
  métier sans premier owner (`workspace_without_owner`). Action prioritaire : « Attribuer un owner ».
- **Purge physique d'un workspace** : **NO-GO** dans ce lot (la désactivation douce reste la voie).

### Accès multi-workspace (P0)

`GET /api/v1/workspaces/` est le **sélecteur métier** : il retourne **tous les memberships** de
l'appelant sur des workspaces **actifs** (option `?include_inactive=1` pour l'admin), enrichis de
`workspace_is_active`, `membership_id`, `effective_owner_scope_id`. Aucun filtre `is_superuser`,
aucune limite au premier workspace. `GET /api/v1/rag-spaces/?workspace_id=<X>` **refuse (403
`not_a_member`)** un workspace dont l'appelant n'est pas membre (défense en profondeur). Le
frontend expose un **sélecteur de workspace** (`WorkspaceSwitcher`) : la bascule vide immédiatement
les RagSpaces/stats du workspace précédent puis recharge ceux du nouveau (jamais de fusion).

Codes applicatifs stables (extraits) : `workspace_created`, `owner_created`/`already_owner`/
`workspace_has_owner`/`promoted`, `membership_created`/`already_member`/`owner_scope_required`/
`cross_workspace`/`owner_membership_invalid`, `grants_incompatible`,
`workspace_disabled`/`workspace_enabled`/`already_*`, `transfer_apply_not_implemented`.
Le frontend les traduit en **messages FR neutres** (jamais de code brut, cf. §UX).

## Transfert de périmètre — prévisualisation seule (NO-GO sur l'application)

Le transfert d'un owner-scope est l'opération la plus sensible (déplacement atomique de
memberships + RagSpaces + invalidation de grants). **Ce lot ne livre que la prévisualisation** :
impact complet (memberships/RagSpaces affectés, grants valides/invalides, documents en cours,
jobs de réindex actifs, anomalies d'intégrité), `allowed`/`blockers`, `preview_hash`. L'endpoint
`apply/` **refuse délibérément (501)**. L'exécution reste opérée par les commandes de gestion
sous contrôle (`assign_membership_owner_scope`, `assign_ragspace_owner_scope`).

## Audit

Chaque mutation émet un `ActionAuditEvent` (append-only, fail-open, métadonnées **sanitisées** —
jamais de contenu/secret) via `emit_action_event`, `action_name="platform.<verb>"` :
`platform.workspace_created`, `platform.owner_assigned`, `platform.membership_created`,
`platform.membership_owner_scope_changed`, `platform.membership_role_changed`,
`platform.membership_removed`, `platform.user_disabled`/`_enabled`,
`platform.workspace_disabled`/`_enabled`, `platform.owner_scope_transfer_apply_blocked`. Champs :
acteur, cible, workspace, membership, owner-scope source/cible, avant/après, motif, `request_id`.

## Frontend

Route **`/platform-admin`** (nav « Administration plateforme », **superuser only** via
`me.is_superuser` ; le backend reste autoritaire). Onglets : **Tableau de bord**, **Utilisateurs**,
**Workspaces** (liste + fiche + mutations), **Diagnostics** (anomalies **traduites**). Toutes les
opérations sensibles passent par une **modale de confirmation** ; les erreurs sont traduites en
messages neutres (aucun code interne ; 409 ⇒ « l'état a changé, rechargez »). Aucun contenu
documentaire n'est affiché ni atteignable depuis la console.

## Références d'implémentation

- API : `backend/api/v1/platform_admin/` (`common.py` garde/pagination/dicts, `views.py` lecture,
  `mutations.py` mutations, `transfers.py` preview/apply-NO-GO, `audit.py` audit) ; route montée
  dans `backend/api/v1/urls.py` (`platform/`).
- Diagnostic canonique partagé : `backend/apps/workspaces/services/owner_scope_diagnostics.py`
  (extrait de `audit_workspace_owner_scopes`, réutilisé par la commande **et** l'API).
- Services réutilisés : `backend/apps/workspaces/services/owner_scope_migration.py`
  (`create_workspace_owner`, `assign_membership_scope`, `set_workspace_active`, …),
  `apps/workspaces/services/access.py` (`validate_membership_owner_scope`,
  `effective_owner_scope_pk`), garde `IsRagATMOwner` (`apps/accounts/permissions.py`).
- `/api/v1/auth/me/` : champ `is_superuser` (gating UX).
- Frontend : `frontend/src/lib/platformAdmin.ts`, `frontend/src/app/platform-admin/`
  (`page.tsx`, `_components/`, `_lib/anomalies.ts`), nav `frontend/src/components/shell/Sidebar.tsx`.
- Accès multi-workspace (P0) : `backend/api/v1/workspaces/views.py` (`WorkspaceListView`),
  `backend/api/v1/ragspaces/views.py` (garde `not_a_member`), `frontend/src/components/shell/
  WorkspaceSwitcher.tsx` + `WorkspaceContext.tsx` (`setWorkspaceId` vide/recharge).
- Tests : `tests/tests_root/http_api/test_platform_admin_api.py`,
  `test_platform_user_lifecycle.py`, `test_workspace_multi_access.py`,
  `tests/frontend/components/{PlatformNav,PlatformUsersTab,WorkspaceSwitcher}.test.tsx`,
  `tests/frontend/lib/platformAdmin.test.ts`.

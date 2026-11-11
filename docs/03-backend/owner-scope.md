# Périmètres owner (`owner_scope`) & ACL par RagSpace

Objectif : cloisonner **plusieurs owners indépendants à l'intérieur d'un même workspace**.
Le workspace reste le tenant (aucune entité `Organization`), mais un owner ne voit, n'ouvre,
ne modifie, n'administre ni ne supprime **que** les RagSpaces de son propre périmètre ; les
admins/editors/viewers qu'il crée ou invite restent limités à ce périmètre.

Public : développeurs backend, auditeurs sécurité. Complète
[permissions-isolation.md](permissions-isolation.md).

> Gouvernance *cross-workspace* (superuser) — création de workspace, attribution d'owner,
> rattachement d'owner-scope, désactivation, diagnostics : voir
> [platform-admin-console.md](platform-admin-console.md).

## Statut

*Implémenté de bout en bout : schéma, service canonique, diagnostic, mode audit, revalidation
des tâches, ACL (grants/backfill/endpoints), **et câblage enforcement des endpoints HTTP**.
Activable par flag ; **désactivé par défaut**.*

Tant que `OWNER_SCOPE_ENABLED` et `RAGSPACE_ACL_ENABLED` sont à `0` (défaut), l'autorisation
est **strictement identique** à l'historique workspace-wide (la garde canonique délègue au
chemin legacy). Flags : voir [../10-reference/feature-flags.md](../10-reference/feature-flags.md).

### Câblage enforcement (garde canonique aux endpoints)

Tous les points d'entrée HTTP sensibles (ragspaces, documents, chat, core, analyses) appellent la
garde **canonique** `require_ragspace_access` (au lieu du legacy `require_rag_space_access`) ; la
liste passe par `visible_ragspaces_for_user` pour la **queryset réellement renvoyée**. Sémantique :

- **flags OFF** → la garde délègue au legacy → comportement **strictement identique** ;
- **`OWNER_SCOPE_ENABLED=1` + `AUDIT_ONLY=0`** → owner_scope **bloquant** : un membre du workspace
  hors périmètre est refusé. Un refus de **périmètre/accès** (`owner_scope_mismatch`,
  `access_missing`, `ragspace_without_scope`) → **404 anti-énumération** (indiscernable d'un
  inexistant) ; un refus de **niveau** (`space_role_insufficient`, ressource visible) → **403**.
- L'observation (`emit_ragspace_decision`) reste émise **une seule fois par requête** — la garde
  canonique **ne journalise plus** (évite le double événement).

Combinaison d'activation staging : `OWNER_SCOPE_ENABLED=1`, `OWNER_SCOPE_AUDIT_ONLY=0`,
`RAGSPACE_ACL_ENABLED=0`, `RAGSPACE_ACL_AUDIT_ONLY=1` (owner_scope bloquant, ACL observée).

## Modèle de données (ajouts)

- `Workspace.is_active` (bool, défaut `True`) — désactivation douce ; un workspace inactif est
  hors périmètre d'enforcement (jamais de hard-delete).
- `Membership.owner_scope` (self-FK, `PROTECT`, nullable) — le membership `owner` qui gouverne
  un membership non-owner. **Convention** : un `owner` a `owner_scope = NULL` (il **est** son
  propre périmètre — cf. `get_owner_scope_for_membership`) ; un non-owner **doit** pointer un
  `owner` du même workspace.
- `RagSpace.owner_scope` (FK `workspaces.Membership`, `PROTECT`, nullable) — périmètre
  propriétaire de l'espace. **Source de vérité de l'autorisation.**
- `RagSpace.created_by` (FK `User`, `SET_NULL`, nullable) — **audit uniquement**, jamais utilisé
  seul comme ACL.
- `RagSpaceAccess(rag_space, user, space_role ∈ {manager,editor,viewer}, assigned_by, created_at,
  revoked_at)` — accès explicite par espace. Unicité de la ligne **active** par `(rag_space, user)`
  (partielle `revoked_at IS NULL`). L'owner du périmètre n'a **pas** besoin de ligne.

Nullable partout : permet le **backfill progressif** avant l'`enforcement` (puis contraintes
`NOT NULL` en fin de trajectoire).

## Invariants de périmètre

Garantis au niveau service (`validate_membership_owner_scope`) : un `owner_scope` appartient au
même workspace que la ressource, a le rôle `owner`, n'est pas la ressource elle-même ; un
membership non-owner possède exactement un `owner_scope`. Le **client ne choisit jamais**
`owner_scope` : le backend le dérive du membership de l'acteur.

### Création d'un RagSpace — invariant (statut : **implémenté**)

> **Tout RagSpace créé porte un `owner_scope` non nul, écrit dans la MÊME
> transaction que l'objet.** Aucun état intermédiaire `owner_scope=None` n'est
> jamais visible.

Chemin unique : [`apps/workspaces/services/ragspace_creation.py`](../../backend/apps/workspaces/services/ragspace_creation.py)
(`create_ragspace`). L'API de création, l'endpoint `/api/v1/bootstrap/` et
`manage.py bootstrap_workspace` y passent tous. Un test de garde
(`test_no_product_code_creates_a_ragspace_outside_the_service`) énumère les seules
exceptions tolérées — seeds de benchmark et fixtures, dont les workspaces n'ont
**aucun membership** : personne ne peut les atteindre.

Ordre de résolution du périmètre (`resolve_owner_scope_for_ragspace`) :

| # | Source | Quand |
|---|---|---|
| 1 | périmètre **explicite** | appelants internes seulement (migration, replay) ; validé (rôle `owner`, même workspace) |
| 2 | **membership de l'acteur** | cas nominal — `effective_owner_scope` : owner → lui-même, non-owner → son `owner_scope` |
| 3 | **owner unique du workspace** | acteur absent (seeds) ou non-owner pas encore rattaché ; même règle déterministe que `backfill_workspace` |
| 4 | **refus** (`owner_scope_unresolved`) | 0 ou >1 owner : on ne devine pas lequel gouverne. Aucune écriture. |

Le remplissage est **systématique, y compris `OWNER_SCOPE_ENABLED=0`** : les données
sont prêtes avant l'activation, sans migration massive le jour J.

Deux refus explicites, **avant** toute écriture :

- `workspace_disabled` (409) — un espace créé dans un workspace désactivé ne serait
  traité par aucune tâche (`task_guard.WORKSPACE_DISABLED`) : ce serait un objet mort ;
- `owner_scope_unresolved` (409) — périmètre indéterminable sans ambiguïté.

Ces deux codes sont renvoyés **après** que `require_workspace_admin` a prouvé que
l'appelant administre ce workspace : ils ne révèlent donc rien, et
l'anti-énumération reste entière partout ailleurs.

> **Le défaut corrigé.** Jusqu'au 2026-07-28, la création laissait
> `owner_scope=None`. Sous enforcement, `_decide_ragspace` échoue alors en
> `ragspace_without_scope` et `require_ragspace_access` répond **404
> anti-énumération** : l'utilisateur voyait son espace dans la liste, l'ouvrait, et
> l'upload lui répondait « introuvable ». Constaté en staging. Le fail-closed n'est
> pas en cause — il est correct : un espace sans périmètre n'appartient à personne.
> C'est la donnée qui était fautive.

L'endpoint `/api/v1/bootstrap/` ne crée plus d'espace dans un workspace **sans
owner** : aucun périmètre n'y est déterminable, et promouvoir l'utilisateur owner
pour s'en sortir élargirait ses droits. Il lui fournit un workspace neuf dont il
est légitimement owner — la raison d'être de cet endpoint. Le workspace d'origine
n'est pas modifié.

### Modes ACL — contrat (statut : **implémenté**)

Une seule lecture des drapeaux est autorisée : `access.resolve_acl_mode()`.

| `RAGSPACE_ACL_ENABLED` | `RAGSPACE_ACL_AUDIT_ONLY` | Mode | Journalise | **Bloque** |
|---|---|---|---|---|
| 0 | 0 | `disabled` | non | non |
| 0 | 1 | `audit_only` | oui | **jamais** |
| 1 | 0 | `enforced` | oui | oui |
| 1 | 1 | `audit_only` | oui | **jamais** — cf. ci-dessous |

**Les deux drapeaux à 1 ⇒ audit-only.** C'est la convention DÉJÀ appliquée au
périmètre owner (`_owner_scope_enforced` = `ENABLED and not AUDIT_ONLY`) : poser
explicitement le drapeau d'observation est une déclaration d'intention, et la
respecter est le comportement le moins surprenant. Un system check
(`workspaces.W001`) le signale sans bloquer, pour qu'un exploitant qui croyait
avoir activé l'enforcement s'en aperçoive.

Deux décisions, jamais confondues :

| Fonction | Applique l'ACL quand | Sert à |
|---|---|---|
| `simulated_ragspace_decision` | mode ≠ `disabled` | **observer** — « qu'aurait décidé l'ACL ? » |
| `effective_ragspace_decision` | mode = `enforced` | **décider** — autoriser ou refuser |

`require_ragspace_access` (HTTP) et `task_guard.revalidate_task_access` (Celery)
utilisent **tous deux** la décision effective : les deux chemins ne peuvent plus
diverger, et un test le vérifie mode par mode.

> **Le défaut corrigé (P0-1).** `revalidate_task_access` appelait
> `target_ragspace_decision`, la fonction de SIMULATION (`ENABLED **or**
> AUDIT_ONLY`), et `enforce_or_log` traitait son verdict comme effectif. Sous
> `OWNER_SCOPE_ENABLED=1` + `RAGSPACE_ACL_AUDIT_ONLY=1`, l'ingestion de tout
> non-owner était donc **réellement annulée** (`access_missing`) par une ACL censée
> se contenter d'observer. Constaté en staging le 2026-07-28. Le chemin HTTP, lui,
> était correct depuis le début — d'où la divergence.

L'événement d'audit distingue désormais les deux verdicts :

```json
{"acl_mode": "audit_only", "decision": "allow", "simulated_decision": "deny",
 "simulated_reason": "access_missing", "acl_would_have_denied": true,
 "task_continued": true}
```

### Accès initial du créateur (statut : **implémenté**)

> **Tout acteur autorisé à créer un RagSpace peut l'utiliser immédiatement,
> y compris après revalidation asynchrone.**

Attribué dans la transaction de `create_ragspace` :

| Créateur | Grant | Pourquoi |
|---|---|---|
| **owner** du périmètre | aucun | son accès découle de son rôle (bypass ACL) ; matérialiser un grant créerait une seconde source de vérité |
| **non-owner** (admin) rattaché au périmètre | `editor` | minimum pour voir, uploader, ingérer, interroger. **Pas `manager`** : administrer les ACL et supprimer l'espace ne sont pas nécessaires pour l'utiliser |
| **non-owner hors du périmètre attribué** | aucun | membership non rattaché : le périmètre vient de la règle de repli `single_owner`, un grant y serait invalide. La création **reste valide** — le défaut de rattachement est une donnée à corriger, que `audit_workspace_owner_scopes` signale ; il n'a pas à faire échouer une création légitime |
| autres membres | aucun | aucun accès n'est diffusé automatiquement |

Idempotent : un accès actif existant n'est ni dupliqué, ni abaissé, ni élevé. Les
invariants de périmètre et le plafond D1 sont revalidés avant écriture. Le niveau
accordé est tracé dans l'audit de création (`creator_space_role`, `null` quand
aucun grant n'est posé).

> **Conséquence à connaître avant activation.** Un administrateur sans grant
> explicite — parce qu'il est hors périmètre, ou parce que son espace est
> antérieur à ce lot — perd l'usage de cet espace sous `RAGSPACE_ACL_ENABLED=1`.
> C'est le comportement attendu de l'ACL, pas un défaut ; c'est une décision
> d'exploitation à prendre avant l'activation, pas à découvrir après.

## Hiérarchie d'autorisation (cible)

`utilisateur actif → workspace actif → membership actif → périmètre owner compatible → accès
RagSpace → rôle espace suffisant → ressource active.` **L'appartenance au même workspace ne
suffit plus** quand `OWNER_SCOPE_ENABLED`.

**Résolution canonique unique** : la comparaison de périmètre passe TOUJOURS par
`effective_owner_scope_pk(membership)` (owner → sa propre PK ; non-owner → PK de son
`owner_scope`), jamais par `membership.owner_scope` brut — sinon un owner (owner_scope=NULL) ne
retrouverait pas son périmètre. La règle effective est
`effective_owner_scope_pk(membership) == rag_space.owner_scope_id` (l'owner responsable bypass en
plus l'ACL espace).

## Mode audit (Lot 2 — observation, sans blocage)

Wiring des endpoints en **observation** : la réponse HTTP reste la **décision legacy** ; le
service calcule en parallèle la **décision cible** et journalise **un seul événement par requête**
sur le logger `owner_scope.audit` (jamais par token/chunk/RagSpace). **Aucun contenu sensible**
n'est journalisé (ni question, ni prompt, ni nom de fichier, ni extrait) — uniquement des
identifiants et la décision. **No-op** tant que `OWNER_SCOPE_ENABLED` et `OWNER_SCOPE_AUDIT_ONLY`
sont tous deux faux (⇒ aucun événement avec les flags OFF).

Champs émis : `endpoint`, `request_id`, `user_id`, `workspace_id`, `owner_scope_id`,
`rag_space_id`, `permission`, `legacy_decision`, `target_decision`, `audit`, `reason`.

Classification (`audit`) :

| legacy | cible | réponse | `audit` |
|---|---|---|---|
| allow | allow | allow | `matched_allow` |
| deny | deny | deny | `matched_deny` |
| allow | deny | **allow** (inchangé) | `would_deny` |
| deny | allow | **deny** (inchangé) | `would_allow` |

`would_deny` révèle un resserrement cible (ex. IDOR document cross-périmètre) ; `would_allow`
révèle une cible plus permissive que le legacy (ex. ACL pas encore active). Surfaces câblées :
`ragspaces.list` (agrégé legacy vs cible), `ragspaces.stats/patch/delete`,
`ragspaces.reindex/active_reindex_job/reindex_job/llm_settings/llm_settings_test` (manage),
`documents.*` (dérivé du RagSpace **parent**, jamais du seul `document_id` ; couvre aussi reindex
document et résumés via le chokepoint `load_accessible_document`), `chat.sync`, `chat.stream`,
`chat.threads.list/create`, `chat.thread.messages/suggestions/patch`,
`analyses.create/access/thread`, `core.retrieve`, `core.document_status`. Raisons `fail-closed`
conservées :
`owner_scope_missing`, `ragspace_without_scope`, `owner_scope_mismatch`, `access_missing`,
`space_role_insufficient`, `not_member`.

Combinaison de flags pour ce mode : `OWNER_SCOPE_AUDIT_ONLY=1` (et éventuellement
`RAGSPACE_ACL_AUDIT_ONLY=1` pour simuler l'ACL) — jamais `RAGSPACE_ACL_*` seul (cf. `workspaces.E001`).

> **Caveat manage** : pour les surfaces `manage` (`ragspaces.patch/reindex/llm_*`,
> `chat.thread.patch`), `legacy_decision` reflète la garde workspace
> (`require_rag_space_access`), **pas** le contrôle owner/admin additionnel de l'endpoint. La
> décision **cible** évalue bien le rôle d'espace (`space_role`) quand l'ACL est simulée. Ces
> surfaces sont donc exclues du *gate de silence* du harnais pour ne pas induire en erreur.

### Harnais de rejeu

`python manage.py owner_scope_replay [--simulate-acl] [--output rapport.json]` — rejoue une matrice
`rôles × périmètres × endpoints` en audit-only, **sans rien persister** (jeu synthétique en
`transaction.atomic()` + `set_rollback`), capture les événements et produit une synthèse JSON :
`totals`, `by_endpoint/role/owner_scope/reason`, `would_deny`/`would_allow` (échantillons, ids
uniquement), `list_findings`, `endpoints_silent`, `http_changed`. **Exit ≠ 0** si une surface
attendue reste silencieuse ou si une réponse HTTP a changé. Répond aux questions d'observation :
où la cible resserrerait (`would_deny`), où elle serait plus permissive (`would_allow`), quelles
raisons dominent, quelles surfaces n'ont pas été exercées. À lancer sur une base **migrée**
(clone/staging) ; aucun contenu métier n'est journalisé.

## Tâches asynchrones & caches (Lot 3 — revalidation à l'exécution)

Une tâche autorisée à l'enqueue peut devenir illégitime avant son exécution. Le service
`task_guard.revalidate_task_access` **revalide l'état COURANT** au démarrage et renvoie une
décision + une **raison stable** ; `enforce_or_log` journalise (`event: task_revalidation`) et
décide de continuer. Gating identique : **flags OFF ⇒ `flags_off`, aucun changement** ;
`OWNER_SCOPE_AUDIT_ONLY` ⇒ observe sans annuler ; `OWNER_SCOPE_ENABLED` ⇒ **annule** (ni retry,
ni résultat partiel, statut terminal + raison).

Le contexte est **reconstruit depuis les données persistées** du job (`rag_space` +
`requested_by`/`user`) — aucun changement de signature de tâche. La comparaison courante
`effective_owner_scope(acteur) == rag_space.owner_scope` capture révocation, retrait de
membership, désactivation workspace, soft-delete et transfert de périmètre (du RagSpace **ou** de
l'acteur). Raisons stables : `actor_inactive`, `membership_missing`, `workspace_disabled`,
`ragspace_deleted`, `ragspace_without_scope`, `owner_scope_mismatch`, `owner_scope_transferred`
(si snapshot d'enqueue fourni), `access_missing`, `access_revoked`, `insufficient_space_role`.

Tâches câblées : `document_summary`, `ragspace_reindex` (job orchestrateur → `CANCELLED`),
`corpus_analysis` (→ `CANCELLED`), `rephrasing_suggestions` (→ `failed`), **ingestion/réindexation
document** (`document.ingestion` → `FAILED`, `failure_code=<raison>`).

**Ingestion (`require_context=True`)** : surface sensible (écrit chunks/embeddings). L'enqueue
transporte un `access_context` d'**identifiants seulement** (`{user_id, request_id}`) ; la tâche
recharge le RagSpace depuis le document (autoritatif) et **ne fait jamais confiance** à un rôle ou
scope transmis. Enqueue câblés : upload (`enqueue_async_or_503`), ingestion texte
(`ingest_document_from_text`), pipeline (`_enqueue_after_commit`), commande `reembed_documents`
(`--actor-user-id`, acteur de **service** explicite — jamais `None` comme bypass). Compatibilité :
`flags OFF` ⇒ historique ; `AUDIT_ONLY` + contexte absent ⇒ journalise `context_missing` et
continue ; **`ENABLED` + contexte absent ⇒ REFUS** (`context_missing`) — une tâche sensible sans
acteur identifiable ne continue pas silencieusement. Tous les appelants doivent transporter
l'acteur **avant** l'activation bloquante.

**Caches** : l'invariant reste `contrôle de permission → puis lecture du cache`. Les caches de
preview/résumé/analyse/stats sont **content-keyed** et lus **après** `load_accessible_document` /
`require_*` — un cache préexistant ne contourne jamais la garde (retrait d'accès ⇒ 404 avant tout
service). Aucun cache ne stocke de décision d'autorisation à invalider dans ce lot.

## Collecteur d'observation (agrégation offline)

`python manage.py owner_scope_observation --input staging.jsonl --output rapport.json` — agrège
**hors-ligne** (lecture seule, **aucun accès base**, déterministe, idempotent) les événements
`owner_scope.audit` d'un staging en `AUDIT_ONLY`. Ne lit qu'une **liste blanche** de champs
(identifiants, rôles, décisions, raisons, compteurs) → **aucune donnée métier** réémise. Tolère
lignes brutes (`… owner_scope_audit {json}`) et JSON structuré (champ `message`) ; les lignes
invalides sont comptées (`parse_errors`), jamais fatales ; déduplication par
`(request_id, event, endpoint/job_label)`.

Extraction staging (à adapter au format réel du logger) :

```
docker compose logs --no-color backend celery_worker | grep 'owner_scope_audit' > staging.jsonl
```

Rapport : `totals`, `http_vs_task` (sépare HTTP / listes agrégées / tâches — évite que le volume
des listes masque les erreurs unitaires), `by_endpoint/reason/role/owner_scope/task`,
`would_deny_breakdown` (**expected** `owner_scope_mismatch`/`access_missing` vs **anomalous**
`membership_missing`/`ragspace_without_scope`/`workspace_disabled`/`owner_scope_transferred`…),
`would_allow` (échantillons), `context_missing`, `silent_surfaces`, `request_id_coverage`,
`findings`. **Gates → exit ≠ 0** : `--fail-on-would-allow`, `--fail-on-context-missing`,
`--fail-on-silent-surface`, `--max-would-deny-rate`, `--min-request-id-coverage`. Le premier run
ne doit **pas** échouer sur `would_deny` (attendu avant le backfill `RagSpaceAccess`) — les signaux
prioritaires sont `would_allow`, `context_missing` et les surfaces silencieuses.

L'événement HTTP `ragspace_decision` porte désormais `workspace_role` + `space_role` (noms de
rôles, pas de contenu) ; `task_revalidation` porte `request_id` là où disponible.

## Garde-fous

- **Fail-closed** : sous enforcement, un RagSpace sans `owner_scope`, ou un non-owner sans
  périmètre, est refusé (jamais « ouvert par défaut »).
- **System check `workspaces.E001`** : refuse au démarrage `RAGSPACE_ACL_*` activé sans
  `OWNER_SCOPE_*` (l'ACL ne s'évalue qu'à l'intérieur du périmètre — sinon elle ne bloque rien).
- **`clean()` modèle** (défense en profondeur, via `full_clean()`) : `Membership` refuse un
  owner portant un `owner_scope`, ou un `owner_scope` visant un non-owner / un autre workspace /
  soi-même ; `RagSpaceAccess` refuse une ligne active incohérente (hors périmètre) ou dépassant
  le plafond D1. L'enforcement canonique reste le service, appelé dans la transaction d'écriture.

Doctrine des rôles espace (D1, plafonnement par le rôle workspace) : `owner` → complet dans son
périmètre ; `admin` → `manager|editor|viewer` ; `member` → `editor|viewer` ; `viewer` → `viewer`.

## Diagnostic & activation progressive

1. schéma → 2. **diagnostic** → 3. backfill mono-owner → 4. mapping manuel multi-owner →
   5. mode audit (`*_AUDIT_ONLY`) → 6. corrections → 7. UI → 8. enforcement → 9. `NOT NULL` →
   10. retrait du mode historique. **Ne pas activer l'enforcement tant qu'une ressource active
   reste ambiguë.**

> Les étapes 3 et 4 ne concernent plus que les lignes **historiques** : depuis le
> 2026-07-28, toute création renseigne le périmètre (cf. §Création d'un RagSpace).
> Le stock ne se reconstitue donc plus pendant qu'on le résorbe.
>
> L'étape 9 (`NOT NULL` sur `RagSpace.owner_scope`) reste **à faire** : elle exige
> zéro ligne nulle sur TOUS les environnements. Elle est délibérément séparée du
> code de création — corriger la source d'abord, contraindre le schéma ensuite.

`python manage.py audit_workspace_owner_scopes` (dry-run, n'écrit rien ; `--format json`,
`--workspace-id`, `--strict`). Codes stables : `workspace_without_owner`, `multiple_owners`,
`non_owner_without_scope`, `ragspace_without_scope`, `scope_cross_workspace`,
`scope_target_not_owner`, `access_scope_mismatch`, `multiple_active_accesses`, `inactive_owner`.
**Exit ≠ 0** dès qu'une anomalie d'**intégrité** est détectée (`scope_cross_workspace`,
`scope_target_not_owner`, `access_scope_mismatch`, `multiple_active_accesses`) — intégrable en CI ;
`--strict` échoue aussi sur les anomalies de décision (orphelin, multi-owner). Les codes
`*_without_scope` sont *pending backfill* (attendus avant migration) et ne font pas échouer la CI.

### Octroi d'accès RagSpaceAccess (lot ACL)

Doctrine d'octroi déterministe : **owner du périmètre → `manage` implicite** (aucun grant) ;
**manager** explicite → gestion ; **editor** → écriture ; **viewer** → lecture ; admin/member/viewer
workspace → **aucun accès automatique** sans grant. Le rôle workspace reste un **plafond D1**
(`validate_ragspace_access_invariants`). Révocation = `revoked_at` (historique conservé, jamais de
suppression). Service : `apps/workspaces/services/ragspace_access.py` (codes stables `granted`,
`already_granted`, `role_changed`, `conflict`, `owner_implicit`, `revoked`, `grant_not_found`,
`ragspace_deleted`, `invalid`).

- **Backfill** : `backfill_ragspace_access --workspace-id N --mapping-file m.json [--apply]
  [--allow-role-change]` — octrois **explicites** depuis un mapping `{workspace_id, grants:[{user_id,
  rag_space_id, space_role}]}` ; dry-run par défaut, transaction **par workspace** (all-or-nothing),
  `select_for_update`, idempotent, JSON, audité, **exit ≠ 0** sur erreur (D1, cross-scope, ragspace
  supprimé, conflit). Aucune attribution arbitraire.
- **Endpoints platform** (niveau **manage** = gouverne le périmètre ; hors périmètre → 404
  anti-énumération) :
  `GET|POST /api/v1/rag-spaces/<uuid>/access/` (list / grant),
  `PATCH|DELETE /api/v1/rag-spaces/<uuid>/access/<grant_id>/` (change role / revoke).
  Refusent cross-workspace, cross-owner-scope, dépassement de plafond, RagSpace supprimé.
- **Suppression définitive d'un RagSpace** `DELETE /api/v1/rag-spaces/<uuid>/` : niveau
  **manage**, garde canonique appliquée **avant** le contrôle de rôle workspace. Hors
  périmètre → `404` anti-énumération. Cet endpoint était le seul à ne PAS appliquer la
  garde : sous enforcement, un owner pouvait **détruire** un espace d'un autre périmètre
  alors que le `PATCH` lui refusait de le **renommer**. Corrigé (P0-1) — cf.
  [suppression-ragspace.md](suppression-ragspace.md).

### Enrichissement read-only pour l'UI (statut : implémenté)

Le frontend **ne recalcule jamais** la politique d'autorisation : le backend expose le rôle
**effectif** afin d'améliorer l'UX (afficher/masquer/désactiver les actions), sans aucun flag
supplémentaire. Toutes ces valeurs sont calculées par la **garde canonique** (autorité) et sont
**flag-aware** (elles suivent l'état d'enforcement owner_scope / ACL).

- `effective_ragspace_access(user, rag_space)` → `{permission_level, space_role, is_owner_implicit}`
  (`apps/workspaces/services/access.py`). `permission_level ∈ {read, write, manage, null}` —
  `null` = aucun accès. Flags OFF → repli legacy (viewer→read, member→write, owner/admin→manage).
- **Liste des espaces** `GET /api/v1/rag-spaces/?workspace_id=N` : chaque item porte désormais
  `permission_level`, `space_role`, `is_owner_implicit` (sérialiseur `RagSpaceListItemSerializer`).
- **Liste des grants** `GET .../access/` : chaque entrée porte l'objet `user {id, username, email}`
  en plus de `user_id` (pour l'affichage sans requête supplémentaire).
- **Éligibles à un octroi** `GET /api/v1/rag-spaces/<uuid>/access/eligible/` (manage-gated) :
  membres **du même périmètre** sans grant actif (owner implicite exclu), chacun avec
  `allowed_space_roles` (plafond **D1**). Hors périmètre → 404 anti-énumération.

### Commandes de backfill owner_scope (étapes 3–4)

Toutes **dry-run par défaut** (écriture seulement avec `--apply`), **idempotentes**, atomiques,
sortie `--format json`, codes de résultat stables (`assigned`, `already_scoped`, `skipped_owner`,
`conflict`, `invalid_target_not_owner`, `invalid_cross_workspace`, `no_single_owner`), **exit ≠ 0**
sur conflit/incohérence. Elles ne touchent **que** la donnée `owner_scope` — aucun flag, aucun
enforcement.

- `backfill_owner_scopes --workspace-id N | --all [--apply]` — backfill **déterministe** des
  workspaces **mono-owner** (non-owners + RagSpaces → l'owner unique). Les workspaces sans owner
  ou multi-owner sont **refusés** (`no_single_owner`, aucune écriture) → mapping manuel.
- `assign_membership_owner_scope --membership-id M --owner-membership-id O [--apply]` — mapping
  manuel d'un membership (workspaces multi-owner). Refuse un conflit (pas d'écrasement), une cible
  non-owner ou cross-workspace.
- `assign_ragspace_owner_scope --ragspace-id U --owner-membership-id O [--apply]` — idem pour un
  RagSpace.
- `assign_workspace_owner --workspace-id N --user-id U [--apply] [--allow-additional-owner]
  [--promote]` — crée le **premier owner** d'un workspace orphelin (réservé opérateur/superuser ;
  ne pas utiliser le superuser plateforme comme owner métier). Refuse un workspace ayant déjà un
  owner (sauf `--allow-additional-owner`) et un membre non-owner existant (sauf `--promote`).
- `disable_workspace --workspace-id N [--enable] [--apply]` — désactivation **douce**
  (`is_active=False`), jamais de hard-delete.

**Règle documentée `is_active`** : un workspace `is_active=False` est **explicitement hors
périmètre d'enforcement**. Le diagnostic le **liste toujours** (jamais ignoré silencieusement) avec
le seul code informatif `workspace_disabled` (non bloquant). Un workspace n'est **jamais** exclu par
simple vacuité — seule la désactivation explicite le sort du périmètre. L'owner technique d'un
workspace de benchmark doit être un **compte dédié non-superuser** (ex. `ragatm-benchmark-owner`),
distinct du superuser plateforme.

## Références d'implémentation

- Modèles : `backend/apps/workspaces/models.py` (`Membership.owner_scope`),
  `backend/apps/rag_core/models/rag_space.py` (`owner_scope`, `created_by`),
  `backend/apps/rag_core/models/ragspace_access.py` (`RagSpaceAccess`).
- Migrations : `workspaces/migrations/0003_membership_owner_scope.py`,
  `rag_core/migrations/0034_ragspace_created_by_ragspace_owner_scope_and_more.py`.
- Service canonique : `backend/apps/workspaces/services/access.py`
  (`require_platform_admin`, `get_owner_scope_for_membership`, `effective_owner_scope_pk`,
  `validate_membership_owner_scope`, `validate_ragspace_access_invariants`,
  `require_ragspace_access` / `_write` / `_manage`, `visible_ragspaces_for_user`,
  `effective_ragspace_access` — rôle effectif read-only pour l'UI).
- System check : `backend/apps/workspaces/checks.py` (`workspaces.E001`).
- `clean()` : `Membership` (`backend/apps/workspaces/models.py`), `RagSpaceAccess`
  (`backend/apps/rag_core/models/ragspace_access.py`).
- Diagnostic : `backend/apps/workspaces/management/commands/audit_workspace_owner_scopes.py`.
- Audit (Lot 2) : `backend/apps/workspaces/services/scope_audit.py` (`emit_ragspace_decision`,
  `emit_ragspace_list_decision`) ; câblage dans `api/v1/documents/views_common.py` (chokepoint),
  `views_read.py`, `views_write.py`, `api/v1/ragspaces/views.py`, `api/v1/chat/views.py`,
  `api/v1/core/views.py`. Décision cible : `target_ragspace_decision` (access.py).
- Harnais de rejeu : `backend/apps/workspaces/management/commands/owner_scope_replay.py`.
- Collecteur d'observation : `backend/apps/workspaces/management/commands/owner_scope_observation.py`.
- Revalidation tâches (Lot 3) : `backend/apps/workspaces/services/task_guard.py` ; câblage dans
  `apps/rag_core/tasks.py` (résumé, reindex job, analyse) et `apps/chat/tasks.py` (suggestions).
- Inventaire ACL (lecture seule) : commande `owner_scope_acl_inventory`.
- Octroi ACL : `backend/apps/workspaces/services/ragspace_access.py` ; commande
  `backfill_ragspace_access` ; endpoints `backend/api/v1/ragspaces/access_views.py`
  (list / grant / change / revoke + `RagSpaceAccessEligibleView`).
- Enrichissement UI read-only : `effective_ragspace_access` (access.py) ;
  `RagSpaceListItemSerializer` (`backend/api/v1/ragspaces/serializers.py`) ;
  `api/v1/ragspaces/views.py` (liste enrichie) ;
  tests `tests/tests_root/http_api/test_effective_ragspace_access.py`.
- Backfill : `backend/apps/workspaces/services/owner_scope_migration.py` +
  commandes `backfill_owner_scopes`, `assign_membership_owner_scope`,
  `assign_ragspace_owner_scope`.
- Flags : `backend/config/settings/base.py` (`OWNER_SCOPE_ENABLED`, `OWNER_SCOPE_AUDIT_ONLY`,
  `RAGSPACE_ACL_ENABLED`, `RAGSPACE_ACL_AUDIT_ONLY`).
- Tests : `tests/tests_root/unit/test_owner_scope_service.py`.

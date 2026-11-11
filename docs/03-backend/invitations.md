# Invitations workspace (domaine dédié)

*Statut : implémenté / activé par défaut (flag `WORKSPACE_INVITATIONS_V2_ENABLED`).*

Un propriétaire ou administrateur d'un workspace invite une adresse email avec un rôle,
récupère un **lien d'invitation à usage unique** (copiable en dev **et** en prod), et
l'invité rejoint le workspace après acceptation. Ce domaine remplace l'ancien flux
« invitation = lien de réinitialisation de mot de passe », jugé dangereux (prise de
contrôle d'un compte actif, token non lié au workspace, ancien lien encore valide après
régénération). Voir [permissions-isolation.md](permissions-isolation.md) pour le modèle
d'accès workspace.

## Principes

- **Distinct du reset password.** Le flux « mot de passe oublié »
  ([../05-api/authentification.md](../05-api/authentification.md)) reste réservé à
  l'utilisateur qui réinitialise **son propre** mot de passe. L'invitation n'utilise
  jamais `PasswordResetTokenGenerator`.
- **Aucun membership avant acceptation.** Avant acceptation : une `WorkspaceInvitation`
  existe, aucun `Membership`. Après acceptation : `accepted_at` renseigné, `Membership`
  créé avec le rôle **stocké dans l'invitation**.
- **Token brut one-shot.** Généré par `secrets.token_urlsafe(32)`, retourné **une seule
  fois** (création/régénération). La base ne stocke que son **hash SHA-256**
  (`token_hash`). Le brut n'apparaît jamais en base, log, audit, exception ou URL persistée.
- **Compte actif protégé.** Un lien d'invitation ne modifie **jamais** le mot de passe d'un
  compte actif existant (flux authentifié obligatoire, cf. [Utilisateur déjà actif](#utilisateur-déjà-actif)).
- **Rôle owner jamais attribuable.** Ni par invitation, ni par régénération, ni à
  l'acceptation. Garanti par le serializer, le service **et** une `CheckConstraint` DB.

## Modèle de données

`apps.workspaces.models.WorkspaceInvitation` (table `workspace_invitation`) :

| Champ | Rôle |
|---|---|
| `id` (UUID) | Clé primaire. |
| `workspace` (FK) | Workspace cible. |
| `email_normalized` | Email invité, **minuscule** (`strip().lower()`). |
| `role` | `admin` / `member` / `viewer` (owner exclu par `CheckConstraint`). |
| `token_hash` (unique) | SHA-256 hexdigest (64 car.) du token brut. |
| `generation` | Incrémenté à chaque régénération. |
| `created_by` / `created_at` | Auteur et date. |
| `expires_at` | Expiration stricte (UTC), obligatoire. |
| `accepted_at` / `accepted_by` | Acceptation (exclusif avec `revoked_at`). |
| `revoked_at` | Révocation (conserve la ligne). |
| `last_sent_at` | Dernier envoi email. |

**Contraintes :**

- unique partielle « une seule invitation en attente par `(workspace, email)` »
  (`WHERE accepted_at IS NULL AND revoked_at IS NULL`) ;
- `CheckConstraint` `role != owner` ;
- `CheckConstraint` `accepted_at` et `revoked_at` jamais simultanés.

Le **statut** (`pending` / `expired` / `accepted` / `revoked`) est **dérivé** de ces champs
(`WorkspaceInvitation.status()`), jamais fourni par le client.

## Endpoints

Détail dans [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md). Résumé :

| Route | Permission | Effet |
|---|---|---|
| `POST /api/v1/workspaces/<id>/invitations/` | OWNER/ADMIN | Crée l'invitation, renvoie le lien one-shot (`no-store`). |
| `GET /api/v1/workspaces/<id>/invitations/` | OWNER/ADMIN | Liste (jamais de token ni d'URL). |
| `POST /api/v1/workspaces/<id>/invitations/<uid>/regenerate/` | OWNER/ADMIN | Nouveau token (ancien invalidé), `generation+1`, nouveau lien. |
| `POST /api/v1/workspaces/<id>/invitations/<uid>/revoke/` | OWNER/ADMIN | Révoque (idempotent, refuse si déjà acceptée). |
| `POST /api/v1/invitations/validate/` | public | État du token + flux (compte nouveau/actif). |
| `POST /api/v1/invitations/accept/` | public | Acceptation compte nouveau/inactif (pose le mot de passe). |
| `POST /api/v1/invitations/accept-authenticated/` | authentifié | Acceptation compte actif (mot de passe inchangé). |

Les permissions passent par le service d'accès canonique
(`require_workspace_admin`), pas seulement par le frontend. Toute réponse contenant un lien
porte `Cache-Control: no-store` + `Pragma: no-cache` ; les réponses publiques ajoutent
`Referrer-Policy: no-referrer`.

## Acceptation atomique

`accept_invitation` s'exécute dans une transaction avec `select_for_update` sur
l'invitation : lookup par hash → contrôle acceptée/révoquée/expirée → résolution de
l'utilisateur (insensible à la casse) → doctrine compte actif/inactif → création du
`Membership` (rôle = invitation) → marquage `accepted_at`. Deux acceptations concurrentes
produisent **au plus un** membership (le verrou sérialise ; la seconde voit `accepted_at`).

### Utilisateur nouveau ou inactif

Compte créé (ou réutilisé) avec `set_password()` (politique de mot de passe standard) et
`is_active=True`, puis `Membership` au rôle de l'invitation.

### Utilisateur déjà actif

L'acceptation anonyme est **refusée** (`active_user_must_authenticate`). L'invité doit se
connecter puis appeler `accept-authenticated/`, qui vérifie que l'email de la session
correspond à l'invitation et **ne touche jamais** au mot de passe.

### Email non unique

`auth.User` stock ne garantit pas l'unicité DB de l'email. Résolution insensible à la casse ;
si **plusieurs** comptes correspondent à l'email normalisé, l'acceptation échoue
**fail-closed** (`ambiguous_account`) sans exposer les comptes. Aucune migration ne rend
l'email unique sans audit préalable.

### Membership existant

Si un membership existe déjà pour l'utilisateur, il est **conservé** (rôle inchangé, aucune
promotion implicite) et l'invitation est marquée acceptée.

## Régénération et révocation

- **Régénération** (même ligne) : nouveau `token_hash` (l'ancien lien meurt immédiatement),
  `generation+1`, nouvelle `expires_at`. Interdite si acceptée ou révoquée (doctrine
  explicite : une invitation révoquée ne se réactive pas, on en recrée une).
- **Révocation** : `revoked_at` renseigné (ligne conservée), idempotente, refusée si déjà
  acceptée. Le token devient immédiatement inutilisable.

## Audit et email

Événements `emit_action_event` (`action_name="workspace_invitation"`) :
`invitation_created`, `invitation_regenerated`, `invitation_revoked`, `invitation_accepted`,
`invitation_expired_rejected`, `invitation_failed`. Métadonnées : rôle, génération, email
**masqué** — jamais le token (redigé de toute façon par la sanitation d'audit, cf.
la documentation d'exploitation (hors périmètre de cette édition)). L'email est
best-effort ; le **lien manuel** reste la source fiable (le backend email par défaut est
`console`, cf. [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md)).

## Construction de l'URL

`{FRONTEND_APP_ORIGIN}/accept-invitation?token=<raw>`, origine lue **exclusivement** depuis
`FRONTEND_APP_ORIGIN` (jamais `Host`/`X-Forwarded-Host`/`Origin`/`Referer`). En prod cette
variable est obligatoire et absolue. La page d'acceptation retire le token de l'URL après
capture (mitigation historique navigateur / Referer).

## Configuration

Valeurs centralisées :

- durée de vie du lien : `WORKSPACE_INVITATION_TTL_HOURS`
  ([../10-reference/variables-environnement.md](../10-reference/variables-environnement.md)) ;
- flag et rollback : `WORKSPACE_INVITATIONS_V2_ENABLED`
  ([../10-reference/feature-flags.md](../10-reference/feature-flags.md)) ;
- débits anti-abus : `DRF_THROTTLE_INVITATION_CREATE` / `_REGENERATE` / `_ACCEPT`.

Le rollback (`WORKSPACE_INVITATIONS_V2_ENABLED=0`) réactive l'ancien endpoint
`POST /members/` **sans** réexposer le `reset_link` (toujours gouverné par
`INVITE_RETURN_RESET_LINK`, forcé `False` en prod).

## Références d'implémentation

- Modèle : `backend/apps/workspaces/models.py` (`WorkspaceInvitation`).
- Service : `backend/apps/workspaces/services/invitations.py`.
- Vues workspace : `backend/api/v1/workspaces/invitation_views.py`.
- Vues publiques : `backend/api/v1/invitations/`.
- Migration : `backend/apps/workspaces/migrations/0002_workspaceinvitation.py`.
- Frontend : `frontend/src/app/workspace-admin/page.tsx`,
  `frontend/src/app/accept-invitation/`.
- Tests : `tests/tests_root/unit/test_invitation_service.py`,
  `tests/tests_root/http_api/test_invitations_api.py`,
  `tests/tests_root/http_api/test_invitations_concurrency.py`,
  `tests/tests_root/http_api/test_invitations_confidentiality.py`,
  `tests/tests_root/http_api/test_invitation_role_guard.py`.

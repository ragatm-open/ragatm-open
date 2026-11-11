# Authentification à deux facteurs (MFA TOTP)

Statut : *implémenté*, *activé par défaut*, **non désactivable** (aucun flag). Décision :
[ADR-003](../01-overview/decisions/ADR-003-authentification-mfa-obligatoire.md).
Public : développeurs backend, exploitants.

Le CODE prévaut : `backend/apps/accounts/mfa.py`.

## Invariant

`password_validated != fully_authenticated`. Un mot de passe valide ne donne accès à
**rien** : ni `/api/v1/auth/me/`, ni `/api/v1/bootstrap/`, ni aucun workspace,
RagSpace, document, chat, tâche asynchrone ou fonction d'administration. Il ouvre un
challenge que seuls les endpoints `/api/v1/auth/mfa/*` acceptent. Le cookie
d'authentification n'est posé qu'après un code TOTP (ou un jeton de secours) valide.

S'applique à **tout** compte actif : utilisateurs, owners, superusers, comptes
invités. Aucune exemption.

## Cycle de connexion

```text
POST /auth/login/ {email, password}
   │  Axes : compte verrouillé ? → 403 account_locked
   │  authenticate() échoue      → 400 invalid_credentials (même réponse pour
   │                                email inconnu / mot de passe faux / compte inactif)
   ▼
MfaChallenge créé (anciens challenges du compte invalidés)
   cookie ragatm_mfa_challenge  (HttpOnly, SameSite=Strict, Path=/api/v1/auth/, 600 s)
   réponse {next: "mfa" | "mfa_setup"}         — AUCUN cookie d'auth
   │
   ├─ next = "mfa"        → POST /auth/mfa/verify/ {token}
   └─ next = "mfa_setup"  → POST /auth/mfa/setup/        → {provisioning_uri, secret}
                            POST /auth/mfa/setup/confirm/ {token}
   ▼
code valide → challenge consommé, AuthToken émis
   cookie ragatm_auth_token (HttpOnly) + rotation du jeton CSRF
   ▼
bootstrap autorisé → RagATM
```

## Garanties

| Menace | Protection |
|---|---|
| Accès sur le seul mot de passe | aucun `AuthToken` avant le second facteur ; balayage automatique de **toutes** les routes `/api/v1/` (test `test_no_protected_route_answers_before_mfa`) |
| Jetons historiques / créés par l'admin | `CookieTokenAuthentication` ne lit que `AuthToken` ; `authtoken.Token` purgés (migration `0005`) et refusés par cookie comme par en-tête |
| En-tête `Authorization: Token` | même modèle `AuthToken` : la clé n'existe qu'après MFA |
| Django admin | `OTPAdminSite` : le formulaire exige un code OTP ; une session sans second facteur est renvoyée vers la connexion |
| Force brute mot de passe | `django-axes` : 5 échecs → verrou 15 min (identifiant normalisé en minuscules + IP) ; débit `auth` 20/min |
| Force brute code | 5 essais par challenge puis invalidation ; temporisation croissante de `django-otp` par appareil ; débit `mfa` 10/min |
| Rejeu d'un code TOTP | `TOTPDevice.last_t` : un pas déjà accepté est refusé |
| Rejeu du challenge | consommé au succès, à l'épuisement, au nouveau login, au logout |
| Concurrence | challenge et appareils verrouillés (`select_for_update`) : deux vérifications simultanées du même code → une seule réussit |
| Détournement de challenge | le challenge est lié à son compte ; aucun identifiant n'est accepté du client |
| Enrôlement par mot de passe volé | `mfa/setup` refusé (`403 mfa_already_enrolled`) dès qu'un facteur confirmé existe |
| CSRF | vérification imposée sur `mfa/verify`, `mfa/setup`, `mfa/setup/confirm` (le challenge est un cookie) ; cookie `SameSite=Strict` |
| Fuite de secret | empreintes SHA-256 seules en base (challenge, jeton) ; secret TOTP renvoyé une seule fois (`Cache-Control: no-store`), jamais journalisé ; aucun stockage navigateur |
| Dérive d'horloge mémorisée | `OTP_TOTP_SYNC = False` (tolérance ±1 pas, rien n'est appris) |

## Sessions et révocation

| Événement | Effet |
|---|---|
| Logout | tous les `AuthToken` du compte et ses challenges sont révoqués (tous appareils) |
| Mot de passe changé (reset, admin, `changepassword`) | jetons et challenges révoqués (signal sur `User`) ; **le facteur est conservé** : le MFA reste exigé |
| Facteur supprimé (procédure opérateur, admin) | jetons révoqués (signal sur `TOTPDevice` / `StaticDevice`) |
| Compte désactivé | refusé à l'authentification ; jetons révoqués par la console plateforme |
| Compte supprimé | jetons, challenges et facteurs supprimés en cascade |
| Expiration | `AuthToken.expires_at` = émission + `AUTH_COOKIE_MAX_AGE` (14 j), vérifiée côté serveur |
| Redémarrage | jetons et challenges en base : rien n'est perdu |

## Jetons de secours

`StaticDevice` est accepté par `mfa/verify` (usage unique). Aucun endpoint n'en
génère : comme dans le projet de référence, il n'existe pas de libre-service de récupération.

## Identités non humaines

Aucune n'utilise l'API HTTP avec des identifiants humains. Les commandes
`create_visual_validation_identity` et `owner_scope_replay` emploient
`force_authenticate` **en processus** (accès shell à l'hôte requis), hors de la
couche HTTP. `/ops/metrics` a son propre jeton (`RAG_OPS_METRICS_TOKEN`) et n'expose
aucune donnée métier.

## Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `MFA_CHALLENGE_TTL_SECONDS` | 600 | durée du challenge (saisie du code et premier enrôlement) |
| `MFA_CHALLENGE_MAX_ATTEMPTS` | 5 | codes faux tolérés par challenge |
| `DRF_THROTTLE_AUTH` | 20/min | débit du login |
| `DRF_THROTTLE_MFA` | 10/min | débit des étapes MFA |

Valeurs centralisées : [configuration](../10-reference/configuration.md). Les seuils
Axes (5 échecs, 15 min) sont codés en dur, comme dans le projet de référence.

## Voir aussi

- [API — Authentification](../05-api/authentification.md)
- Récupération d'un second facteur
- Promotion production du MFA
- Sécurité

## Références d'implémentation

- `backend/apps/accounts/mfa.py` — orchestration (Axes, challenge, vérification, enrôlement, révocations)
- `backend/apps/accounts/authentication.py` — `CookieTokenAuthentication` (`AuthToken` seulement), cookies
- `backend/apps/accounts/models.py` — `AuthToken`, `MfaChallenge` ; migrations `0004_authtoken_mfachallenge`, `0005_purge_legacy_drf_tokens`
- `backend/apps/accounts/signals.py` — révocations sur changement de mot de passe et retrait de facteur
- `backend/apps/accounts/axes.py` — identifiant surveillé par Axes
- `backend/apps/accounts/management/commands/mfa_reset_device.py` — procédure opérateur
- `backend/api/v1/auth/views.py`, `backend/api/v1/auth/urls.py`
- `backend/config/urls.py` (`OTPAdminSite`), `backend/config/settings/base.py` (`MFA_*`, `AXES_*`, `OTP_TOTP_*`)
- `tests/tests_root/http_api/test_auth_mfa.py`, `backend/testing/mfa.py`
- `frontend/src/app/login/page.tsx`, `tests/frontend/components/LoginMfa.test.tsx`

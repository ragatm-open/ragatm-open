# API — Authentification

Objectif : décrire le mécanisme d'authentification de l'API v1 et son usage côté
navigateur (cookie + CSRF) comme côté client machine (header `Authorization`).
Public : développeurs frontend et intégrateurs.

Le CODE prévaut. Mécanisme vérifié dans
`backend/apps/accounts/authentication.py` et `backend/api/v1/auth/`.

## Modèle d'authentification

**Second facteur obligatoire** (*implémenté*, non désactivable) : le mot de passe seul
n'ouvre aucun accès. Détail et modèle de menace :
[authentification-mfa.md](../03-backend/authentification-mfa.md).

RagATM utilise un **jeton d'accès** (`apps.accounts.models.AuthToken`, émis
uniquement après le second facteur) et le transporte dans un **cookie HttpOnly**,
pas dans le stockage local du navigateur. Les jetons `rest_framework.authtoken` ne
sont **plus acceptés** :

- Classe d'auth par défaut (DRF) :
  `apps.accounts.authentication.CookieTokenAuthentication` (sous-classe de
  `TokenAuthentication`).
- Ordre de résolution du token dans une requête :
  1. En-tête `Authorization: Token <clé>` s'il est présent — voie **clients
     API/tests**, **sans CSRF**.
  2. Sinon, lecture du token dans le cookie `ragatm_auth_token` (nom par défaut,
     `AUTH_COOKIE_NAME`). Sur ce chemin, la protection **CSRF double-submit** est
     imposée pour les méthodes non sûres (POST/PUT/PATCH/DELETE).
- Un cookie **périmé/invalide** dégrade en anonyme (retourne `None`) plutôt que de
  répondre `401` : les endpoints publics restent accessibles.
- Le jeton expire côté serveur (`AUTH_COOKIE_MAX_AGE`, 14 j), indépendamment du cookie.

Ce qui **n'existe pas** : pas de JWT, pas d'auth de session pour l'API, **pas
d'inscription publique**. Les comptes sont créés par invitation (membres de
workspace) ou par activation via reset de mot de passe.

### Cookies

- `ragatm_mfa_challenge` — posé par `login/`. **HttpOnly**, `SameSite=Strict`,
  `Path=/api/v1/auth/`, 600 s. Accepté **uniquement** par `mfa/*` ; n'est pas une
  crédence pour le reste de l'API.
- `ragatm_auth_token` — posé par `mfa/verify/` ou `mfa/setup/confirm/` seulement.
  **HttpOnly**, porte le jeton d'accès. Inaccessible au JS.
- `csrftoken` — cookie CSRF, posé dès `login/` et **renouvelé** à la fin du second
  facteur. En prod, `CSRF_COOKIE_HTTPONLY=False` : le JS **doit** pouvoir le lire
  pour le renvoyer en en-tête (double-submit).

## CSRF sur les mutations

Pour toute méthode non sûre effectuée via le **cookie** (le cas du navigateur) :

- envoyer l'en-tête **`X-CSRFToken`** avec la valeur du cookie `csrftoken` ;
- le cookie `csrftoken` doit accompagner la requête.

Les clients qui utilisent `Authorization: Token` **n'ont pas** à gérer le CSRF.

Les étapes `mfa/verify/`, `mfa/setup/` et `mfa/setup/confirm/` exigent **toujours**
`X-CSRFToken`, même sans session : le challenge est un cookie envoyé
automatiquement par le navigateur.

## Endpoints d'authentification

Préfixe `/api/v1/auth/` (`backend/api/v1/auth/urls.py`).

| Méthode | Chemin | Permission | Corps / effet |
|---|---|---|---|
| POST | `login/` | `AllowAny`, débit `auth` | `{email, password}` → **1er facteur seulement** : `{ok, mfa_required: true, next: "mfa"\|"mfa_setup", expires_in}` + cookie challenge + cookie CSRF. Aucun cookie d'auth. `400` identifiants invalides (réponse unique), `403 account_locked` |
| GET | `mfa/status/` | `AllowAny` | `{pending, next, expires_in}` du challenge en cours (reprise après rafraîchissement) ; aucune identité |
| POST | `mfa/verify/` | challenge, CSRF, débit `mfa` | `{token}` (TOTP ou jeton de secours) → `{ok, id, email, username}` + cookie d'auth. `400 invalid_mfa_token`, `401 mfa_challenge_invalid`, `409 mfa_device_required` |
| POST | `mfa/setup/` | challenge, CSRF, débit `mfa` | compte **sans** facteur : `201 {method: "totp", provisioning_uri, secret}` (`no-store`). `403 mfa_already_enrolled` sinon |
| POST | `mfa/setup/confirm/` | challenge, CSRF, débit `mfa` | `{token}` → active l'appareil, `{ok, id, email, username}` + cookie d'auth |
| POST | `logout/` | `IsAuthenticated` | révoque **tous** les jetons du compte et ses challenges, efface les cookies |
| GET | `me/` | `IsAuthenticated` | `{id, email, username, is_ragatm_owner}` |
| POST | `password/reset/` | `AllowAny` | `{email}` → anti-énumération, envoie le lien par email (`debug_link` seulement en `DEBUG`) |
| POST | `password/reset/confirm/` | `AllowAny` | `{uid, token, new_password}` (ou `new_password1`/`new_password2`) → pose le mot de passe, **active** un compte inactif, révoque ses jetons. Le second facteur reste exigé |

## Amorçage du compte

Après la première connexion **complète** (second facteur validé), `POST /api/v1/bootstrap/` (authentifié, idempotent)
garantit que l'utilisateur possède un workspace (« Initial workspace » + membership
`owner`) et un RagSpace par défaut. Voir
[reference-endpoints.md](reference-endpoints.md#bootstrap).

## Exemples curl

Connexion en deux temps (le jar conserve challenge, CSRF puis jeton) :

```bash
curl -X POST http://127.0.0.1:6811/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -c cookies.txt -b cookies.txt \
  -d '{"email":"user@example.com","password":"exemple-mot-de-passe"}'
# → {"ok": true, "mfa_required": true, "next": "mfa", "expires_in": 600}

CSRF=$(grep csrftoken cookies.txt | awk '{print $7}')
curl -X POST http://127.0.0.1:6811/api/v1/auth/mfa/verify/ \
  -H 'Content-Type: application/json' -H "X-CSRFToken: $CSRF" \
  -c cookies.txt -b cookies.txt \
  -d '{"token":"123456"}'   # code affiché par l'application d'authentification
```

Appel authentifié via cookie (lecture, méthode sûre — pas de CSRF requis) :

```bash
curl http://127.0.0.1:6811/api/v1/auth/me/ -b cookies.txt
```

Mutation via cookie — CSRF requis (extraire `csrftoken` du jar) :

```bash
CSRF=$(grep csrftoken cookies.txt | awk '{print $7}')
curl -X POST http://127.0.0.1:6811/api/v1/bootstrap/ \
  -b cookies.txt \
  -H "X-CSRFToken: $CSRF"
```

Appel via header `Authorization` (sans CSRF) — **uniquement** avec la valeur d'un
cookie `ragatm_auth_token` obtenu par le parcours MFA ; il n'existe pas de jeton
machine :

```bash
curl http://127.0.0.1:6811/api/v1/workspaces/ \
  -H 'Authorization: Token valeur-du-cookie-ragatm_auth_token'
```

## Liens email (invitation / reset)

Les liens envoyés par email (invitation de membre, reset) sont construits à partir
de `FRONTEND_APP_ORIGIN` (source unique). Le token de reset ne transite **que** par
email en prod (`INVITE_RETURN_RESET_LINK` forcé à `False`). Voir
[variables d'environnement](../10-reference/variables-environnement.md).

## Voir aussi

- [Authentification à deux facteurs](../03-backend/authentification-mfa.md)
- [Vue d'ensemble de l'API](vue-ensemble.md)
- [Référence des endpoints](reference-endpoints.md)
- [Contrats d'erreurs](contrats-erreurs.md)
- [Permissions et isolation](../03-backend/permissions-isolation.md)
- Sécurité

## Références d'implémentation

- `backend/apps/accounts/authentication.py`
- `backend/apps/accounts/mfa.py`
- `backend/apps/accounts/permissions.py`
- `backend/api/v1/auth/urls.py`
- `backend/api/v1/auth/views.py`
- `backend/api/v1/bootstrap/views.py`
- `backend/config/settings/base.py` (`AUTH_COOKIE_*`, `MFA_*`, `AXES_*`, `REST_FRAMEWORK`)
- `backend/config/settings/prod.py` (cookies sécurisés, `CSRF_COOKIE_HTTPONLY`)

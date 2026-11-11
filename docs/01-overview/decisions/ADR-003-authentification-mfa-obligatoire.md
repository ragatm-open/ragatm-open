# ADR-003 — Second facteur obligatoire (TOTP)

Statut : **acceptée** — *implémenté* (lot AUTH-MFA-01), *activé par défaut*, **non
désactivable**. Date : 2026-10-30.

## Contexte

Jusqu'ici, `POST /api/v1/auth/login/` délivrait directement un token DRF
(`rest_framework.authtoken`) dans un cookie HttpOnly de 14 jours : le mot de passe
suffisait pour accéder à tout RagATM — chat, documents, console plateforme.
Le même token était accepté dans l'en-tête `Authorization: Token …`, et le Django
admin (`/admin/`) ouvrait une session sur le seul mot de passe.

Un projet interne de la même famille a déjà un second facteur en service. La consigne
est de reprendre ce mécanisme éprouvé plutôt que d'en inventer un.

## Mécanisme de référence

| Élément | Référence |
|---|---|
| Facteur | **TOTP** RFC 6238 via `django-otp` (`TOTPDevice`), jetons de secours `StaticDevice` acceptés à la vérification |
| Mot de passe | `django-axes` : 5 échecs → verrou 15 min sur (identifiant, IP) ; verrou **interrogé avant** `authenticate()` |
| Débit | scopes DRF `auth` 20/min et `mfa` 10/min |
| Dérive d'horloge | `OTP_TOTP_SYNC = False` (incident du 2026-09-07 : `drift` persistant → compte bloqué) |
| Session | session Django ; vérité = `user.is_verified()` (django-otp) ; permission `IsMfaVerified` par vue |
| Enrôlement | `POST /api/auth/mfa/setup/` (URI d'appairage une fois) puis `…/setup/confirm/` ; interdit si un facteur confirmé existe sans vérification |
| Récupération | **manuelle** par un superuser vérifié, après contrôle d'identité par canal indépendant ; jamais de libre-service |
| Admin Django | `AdminSiteOTPRequired` (django-two-factor-auth) |
| Frontend | écran de vérification et d'enrôlement ; QR calculé dans le navigateur (`qrcode`) |

Faiblesse non reproduite : dans le projet de référence, `requires_mfa()` ne vise que `is_staff`,
`is_superuser` et les groupes d'administration. RagATM exige le second facteur de
**tout** compte actif (consigne R2).

## Décision

1. **Mêmes bibliothèques et réglages** que le projet de référence : `django-otp` (TOTP + statique,
   anti-rejeu `last_t`, temporisation croissante après échec), `django-axes`
   (mêmes seuils, verrou interrogé avant `authenticate`), `OTP_TOTP_SYNC = False`,
   scopes `auth`/`mfa`, récupération manuelle.
2. **Adaptation au modèle par jeton de RagATM** (au lieu de migrer vers les sessions
   Django, refonte hors périmètre) :
   - le mot de passe ne crée qu'un **`MfaChallenge`** : clé aléatoire dans un cookie
     HttpOnly `SameSite=Strict` limité à `/api/v1/auth/`, empreinte SHA-256 en base,
     expiration (600 s), 5 essais, consommation atomique sous verrou de ligne ;
   - le jeton complet est un nouveau modèle **`AuthToken`** (empreinte seule en base,
     expiration serveur = durée du cookie), émis **uniquement** après le second
     facteur ; `CookieTokenAuthentication` n'accepte plus que ce modèle — un
     `authtoken.Token` (historique, admin, `drf_create_token`) n'ouvre plus rien, ni
     par cookie ni par en-tête.
3. **Aucune exemption** : ni superuser, ni owner, ni flag. Aucun réglage ne désactive
   le MFA ; seuls la durée et le nombre d'essais du challenge sont ajustables.
4. **Admin Django** : `django_otp.admin.OTPAdminSite` (formulaire de connexion avec
   code OTP) plutôt que `django-two-factor-auth`, qui n'apporterait que ses pages
   serveur (dépendances `phonenumbers`, `qrcode`, `formtools`) pour un usage
   technique marginal.
5. **Anciens jetons** : purgés par la migration `accounts.0005` ; chacun se
   reconnecte et passe le second facteur (ou s'enrôle).

## Conséquences

- Chaque utilisateur doit enrôler une application d'authentification à sa première
  connexion après déploiement. Le premier enrôlement repose sur le mot de passe
  (*trust on first use*, identique au projet de référence) : déployer puis faire enrôler les
  comptes sans délai.
- Un appareil perdu = procédure opérateur `manage.py mfa_reset_device` (voir
  [authentification-mfa.md](../../03-backend/authentification-mfa.md)).
- Le header `Authorization: Token` reste accepté, mais seulement pour un jeton issu
  du parcours MFA : il n'existe **aucun** mode machine-à-machine. Un besoin futur
  exigerait des identifiants séparés à privilèges limités, après décision dédiée.
- Les commandes de gestion (`force_authenticate` en processus) ne traversent pas la
  couche HTTP : elles exigent un accès shell à l'hôte, hors du périmètre MFA.
- Rollback applicatif possible (tables nouvelles, aucune colonne modifiée), mais le
  code antérieur réaccepterait les jetons `authtoken` : en production, un rollback
  vers une version sans MFA est une **décision explicite**, jamais un réflexe.

## Références d'implémentation

- `backend/apps/accounts/mfa.py`, `backend/apps/accounts/authentication.py`
- `backend/apps/accounts/models.py` (`AuthToken`, `MfaChallenge`), migrations `0004`, `0005`
- `backend/api/v1/auth/views.py`, `backend/config/urls.py` (`OTPAdminSite`)
- Détail : [authentification-mfa.md](../../03-backend/authentification-mfa.md)

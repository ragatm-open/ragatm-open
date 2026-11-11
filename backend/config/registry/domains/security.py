"""Domaine ``security`` — secrets, exposition HTTP, authentification, invitations."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="DJANGO_SECRET_KEY",
        domain="security",
        value_type="str",
        default="dev-secret-key",
        secret=True,
        description=(
            "Clé de signature Django. Le défaut du code est une valeur de "
            "DÉVELOPPEMENT connue : `config.settings.prod` refuse de démarrer avec, "
            "et le check `config.E003` la signale en staging."
        ),
        required=True,
        required_in=("staging", "prod"),
        consumers=("config.settings.base", "config.settings.prod"),
    ),
    D(
        name="DJANGO_SETTINGS_MODULE",
        domain="security",
        value_type="str",
        default="config.settings.dev",
        description=(
            "Module de settings actif. En staging/prod il DOIT valoir "
            "`config.settings.prod` : `base` laisserait ALLOWED_HOSTS à `*`."
        ),
        required_in=("staging", "prod"),
        source="environnement / manage.py",
        consumers=("config.settings.base",),
        default_is_dynamic=True,
    ),
    D(
        name="DJANGO_ALLOWED_HOSTS",
        domain="security",
        value_type="list",
        default=[],
        description=(
            "Hôtes autorisés (liste séparée par des virgules). Obligatoire et non vide "
            "en production : `config.settings.prod` refuse de démarrer sinon."
        ),
        required=True,
        required_in=("staging", "prod"),
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="DJANGO_CSRF_TRUSTED_ORIGINS",
        domain="security",
        value_type="list",
        default=[],
        description="Origines de confiance CSRF (obligatoire dès que le front est servi en HTTPS).",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="DJANGO_SECURE_SSL_REDIRECT",
        domain="security",
        value_type="bool",
        default=False,
        description="Redirection HTTP→HTTPS par Django (le proxy la fait généralement déjà).",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="DJANGO_USE_X_FORWARDED_HOST",
        domain="security",
        value_type="bool",
        default=False,
        description="Fait confiance à l'en-tête `X-Forwarded-Host` du reverse-proxy.",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="FRONTEND_APP_ORIGIN",
        domain="security",
        value_type="url",
        default="http://127.0.0.1:6300",
        description=(
            "Origine publique du frontend, utilisée pour les liens absolus envoyés par "
            "email (invitation, reset password). Source de vérité unique."
        ),
        required=True,
        required_in=("staging", "prod"),
        consumers=("apps.accounts", "apps.workspaces.services.invitations"),
        notes=(
            "En production, `config.settings.prod` exige une URL absolue : sans elle, "
            "les liens d'email sont cassés."
        ),
    ),
    D(
        name="AUTH_COOKIE_NAME",
        domain="security",
        value_type="str",
        default="ragatm_auth_token",
        description="Nom du cookie HttpOnly portant le token DRF.",
        consumers=("apps.accounts.authentication",),
    ),
    D(
        name="AUTH_COOKIE_MAX_AGE",
        domain="security",
        value_type="int",
        default=60 * 60 * 24 * 14,
        minimum=60,
        description="Durée de vie du cookie d'authentification (secondes). 14 jours.",
        consumers=("apps.accounts.authentication",),
    ),
    D(
        name="AUTH_COOKIE_SECURE",
        domain="security",
        value_type="bool",
        default=False,
        description=(
            "Attribut `Secure` du cookie d'authentification. Forcé à True en production ; "
            "désactivé en dev HTTP local (sinon le cookie n'est jamais posé)."
        ),
        consumers=("apps.accounts.authentication", "config.settings.prod"),
    ),
    D(
        name="AUTH_COOKIE_SAMESITE",
        domain="security",
        value_type="enum",
        choices=("Lax", "Strict", "None"),
        default="Lax",
        description=(
            "Attribut `SameSite` du cookie d'authentification. `Lax` volontairement : "
            "l'app est same-origin via le proxy Next et la 1re navigation depuis un "
            "lien email ne doit pas casser."
        ),
        consumers=("apps.accounts.authentication",),
    ),
    D(
        name="AUTH_COOKIE_PATH",
        domain="security",
        value_type="str",
        default="/",
        description="Chemin du cookie d'authentification.",
        consumers=("apps.accounts.authentication",),
    ),
    D(
        name="AUTH_PASSWORD_MIN_LENGTH",
        domain="security",
        value_type="int",
        default=10,
        minimum=8,
        maximum=128,
        description="Longueur minimale de mot de passe (MinimumLengthValidator).",
        consumers=("django.contrib.auth.password_validation",),
    ),
    D(
        name="INVITE_RETURN_RESET_LINK",
        domain="security",
        value_type="bool",
        default=False,
        description=(
            "Expose `reset_link` et `email_error` dans la réponse d'invitation. Pratique "
            "en dev sans serveur mail ; FUITE DE SECRET en production."
        ),
        environments=("dev", "test"),
        consumers=("api.v1.workspaces",),
        notes="Forcée à False par `config.settings.prod`, quelle que soit la variable.",
    ),
    D(
        name="WORKSPACE_INVITATIONS_V2_ENABLED",
        domain="security",
        value_type="bool",
        default=True,
        description=(
            "Domaine d'invitation v2 (WorkspaceInvitation). À 0, rollback vers l'ancien "
            "flux — sans réactiver l'exposition du reset_link."
        ),
        consumers=("apps.workspaces.services.invitations",),
    ),
    D(
        name="WORKSPACE_INVITATION_TTL_HOURS",
        domain="security",
        value_type="int",
        default=72,
        minimum=1,
        maximum=8760,
        description=(
            "Durée de vie d'un lien d'invitation (heures). Valeur positive obligatoire : "
            "jamais d'expiration infinie."
        ),
        consumers=("apps.workspaces.services.invitations",),
    ),
    D(
        name="DRF_THROTTLE_ANON",
        domain="security",
        value_type="str",
        default="30/min",
        description="Débit DRF des requêtes anonymes (production uniquement).",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="DRF_THROTTLE_USER",
        domain="security",
        value_type="str",
        default="120/min",
        description="Débit DRF des requêtes authentifiées (production uniquement).",
        source="backend/config/settings/prod.py",
        consumers=("config.settings.prod",),
    ),
    D(
        name="DRF_THROTTLE_INVITATION_CREATE",
        domain="security",
        value_type="str",
        default="",
        default_expr="(vide en dev) / 20/min en prod",
        description=(
            "Débit de création d'invitation. Vide => throttle désactivé (dev/tests) ; "
            "la production fixe `20/min`."
        ),
        consumers=("config.settings.base.REST_FRAMEWORK", "config.settings.prod"),
        notes="Appliqué aux vues `api.v1.workspaces` via le scope DRF, jamais lu par elles.",
        default_is_dynamic=True,
    ),
    D(
        name="DRF_THROTTLE_FEEDBACK_CREATE",
        domain="security",
        value_type="str",
        default="10/min",
        description=(
            "Débit de création de feedback, PAR UTILISATEUR. Contrairement aux "
            "invitations, une valeur est posée par défaut : l'endpoint est ouvert "
            "à tout compte authentifié. Jamais de limite globale — elle punirait "
            "tout le monde pour l'excès d'un seul."
        ),
        consumers=("config.settings.base.REST_FRAMEWORK",),
        notes="Appliqué aux vues de feedback via le scope DRF, jamais lu par elles.",
    ),
    D(
        name="DRF_THROTTLE_CHAT_THREAD_SEARCH",
        domain="security",
        value_type="str",
        default="60/min",
        description=(
            "Débit de la recherche de conversations (`GET /api/v1/chat/threads/search/`), "
            "PAR UTILISATEUR. Seau DÉDIÉ : la vue remplace les classes de débit par défaut, "
            "si bien que la frappe dans la palette ne consomme jamais le seau `user` "
            "(120/min) que partage `/auth/me/` — un 429 de recherche ne peut donc pas être "
            "pris pour une session expirée."
        ),
        consumers=("config.settings.base.REST_FRAMEWORK",),
        notes="Appliqué via le scope DRF `chat_thread_search`.",
    ),
    D(
        name="DRF_THROTTLE_INVITATION_REGENERATE",
        domain="security",
        value_type="str",
        default="",
        default_expr="(vide en dev) / 20/min en prod",
        description="Débit de régénération d'invitation. Vide => throttle désactivé.",
        consumers=("config.settings.base.REST_FRAMEWORK", "config.settings.prod"),
        notes="Appliqué aux vues `api.v1.workspaces` via le scope DRF, jamais lu par elles.",
        default_is_dynamic=True,
    ),
    D(
        name="DRF_THROTTLE_INVITATION_ACCEPT",
        domain="security",
        value_type="str",
        default="",
        default_expr="(vide en dev) / 10/min en prod",
        description="Débit d'acceptation d'invitation. Vide => throttle désactivé.",
        consumers=("config.settings.base.REST_FRAMEWORK", "config.settings.prod"),
        notes="Appliqué aux vues `api.v1.workspaces` via le scope DRF, jamais lu par elles.",
        default_is_dynamic=True,
    ),
    # --- Périmètres owner / ACL RagSpace -------------------------------------
    D(
        name="OWNER_SCOPE_ENABLED",
        domain="security",
        value_type="bool",
        default=False,
        description=(
            "Isolation par périmètre owner (listes, accès direct, documents, chat, Core, "
            "tâches). OFF => comportement d'autorisation strictement historique."
        ),
        consumers=("apps.workspaces.services.access",),
    ),
    D(
        name="OWNER_SCOPE_AUDIT_ONLY",
        domain="security",
        value_type="bool",
        default=False,
        description=(
            "Mode observation : n'échoue JAMAIS, journalise "
            "`would_allow`/`would_deny`/`owner_scope_mismatch`."
        ),
        consumers=("apps.workspaces.services.access",),
    ),
    D(
        name="RAGSPACE_ACL_ENABLED",
        domain="security",
        value_type="bool",
        default=False,
        description="Exige un `RagSpaceAccess` actif pour les non-owners.",
        consumers=("apps.workspaces.services.access",),
    ),
    D(
        name="RAGSPACE_ACL_AUDIT_ONLY",
        domain="security",
        value_type="bool",
        default=False,
        description="ACL RagSpace en mode observation, sans blocage.",
        consumers=("apps.workspaces.services.access",),
    ),
    # --- Amorçage administrateur --------------------------------------------
    D(
        name="SET_ADMIN_USERNAME",
        domain="security",
        value_type="str",
        default="",
        description="Compte administrateur créé/mis à jour par la commande d'amorçage.",
        source="backend/apps/accounts/management/commands",
        consumers=("apps.accounts.management.commands",),
        default_is_dynamic=True,
    ),
    D(
        name="SET_ADMIN_EMAIL",
        domain="security",
        value_type="str",
        default="",
        description="Adresse du compte administrateur d'amorçage.",
        source="backend/apps/accounts/management/commands",
        consumers=("apps.accounts.management.commands",),
        default_is_dynamic=True,
    ),
    D(
        name="SET_ADMIN_PASSWORD",
        domain="security",
        value_type="str",
        default="",
        secret=True,
        description="Mot de passe d'amorçage administrateur. Jamais versionné, jamais journalisé.",
        source="backend/apps/accounts/management/commands",
        consumers=("apps.accounts.management.commands",),
        default_is_dynamic=True,
    ),
    D(
        name="AUTH_COOKIE_HTTPONLY",
        domain="security",
        value_type="bool",
        default=True,
        description=(
            "Attribut `HttpOnly` du cookie porteur du token. À 0, un script de la "
            "page peut lire le token : c'est la protection qui rend le transport "
            "par cookie plus sûr qu'un stockage local. Ne la retirer que sciemment."
        ),
        consumers=("apps.accounts.authentication",),
    ),
    D(
        name="MFA_CHALLENGE_TTL_SECONDS",
        domain="security",
        value_type="int",
        default=600,
        minimum=60,
        maximum=1800,
        description=(
            "Durée de vie (secondes) du challenge de pré-authentification posé après "
            "un mot de passe valide. Couvre la saisie du code ET le premier enrôlement "
            "TOTP. Au-delà, retour à l'écran de connexion."
        ),
        consumers=("apps.accounts.mfa",),
        notes="Le MFA lui-même n'est désactivable par AUCUN réglage.",
    ),
    D(
        name="MFA_CHALLENGE_MAX_ATTEMPTS",
        domain="security",
        value_type="int",
        default=5,
        minimum=1,
        maximum=10,
        description=(
            "Codes OTP faux tolérés par challenge ; le challenge est invalidé au-delà "
            "(nouveau mot de passe exigé). S'ajoute à la temporisation de django-otp "
            "et au débit `mfa`."
        ),
        consumers=("apps.accounts.mfa",),
    ),
    D(
        name="DRF_THROTTLE_AUTH",
        domain="security",
        value_type="str",
        default="20/min",
        description=(
            "Débit HTTP de `POST /api/v1/auth/login/` (scope `auth`, par adresse). "
            "Protection primaire contre la force brute : django-axes."
        ),
        consumers=("config.settings.base.REST_FRAMEWORK",),
    ),
    D(
        name="DRF_THROTTLE_MFA",
        domain="security",
        value_type="str",
        default="10/min",
        description=(
            "Débit HTTP des étapes du second facteur (`/api/v1/auth/mfa/verify|setup|"
            "setup/confirm/`, scope `mfa`). Six chiffres se devinent si le débit est libre."
        ),
        consumers=("config.settings.base.REST_FRAMEWORK",),
    ),
)

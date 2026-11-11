import os
from pathlib import Path

from dotenv import load_dotenv

from config.env import env_bool, env_str
from config.settings.from_registry import env_value as reg

BASE_DIR = Path(__file__).resolve().parents[3]

DJANGO_ENV = os.getenv("DJANGO_ENV", "dev")

# Load .env in dev, and optionally in other envs when explicitly requested.
# Useful for local "prod-like" runs or tooling that relies on .env.
if DJANGO_ENV == "dev" or os.getenv("LOAD_DOTENV", "0") == "1":
    load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-key")

DEBUG = False

# Email (defaults Django: localhost:25) -> force via env
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "False").lower() in ("1", "true", "yes", "on")
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() in ("1", "true", "yes", "on")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "0")) or None

# Avoid invalid config: TLS and SSL are mutually exclusive.
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    # Prefer TLS (587) by default
    EMAIL_USE_SSL = False

DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL") or "ragatm@localhost"
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "")

# ============================================================
# Frontend origin (liens email: invitations + reset password)
# ============================================================
# Origine publique du frontend Next.js, utilisee pour construire les liens
# absolus envoyes par email (invitation workspace, reset password).
# Source unique de verite : NE PAS dupliquer (anciennement FRONTEND_BASE_URL/
# FRONTEND_URL cote reset password, desormais unifie ici).
# Defaut dev : 127.0.0.1:6300 (port front dev dedie RAGATM, coherent avec
# DJANGO_CSRF_TRUSTED_ORIGINS du .env dev). En prod, la valeur est obligatoire
# et validee dans config/settings/prod.py.
FRONTEND_APP_ORIGIN = os.getenv("FRONTEND_APP_ORIGIN", "http://127.0.0.1:6300")

# ============================================================
# Invitation membre : exposition du reset_link / email_error
# ============================================================
# L'endpoint POST /api/v1/workspaces/<id>/members/ peut renvoyer dans sa réponse
# JSON le `reset_link` (uid + token de réinitialisation VALIDE → prise de contrôle
# du compte invité) et `email_error` (exception SMTP brute). C'est pratique en
# DEV/support pour activer un compte sans serveur mail, mais c'est une fuite de
# secret en prod (admin/client API/log/APM qui intercepte la réponse).
# Désactivé par défaut ; en prod la valeur est forcée à False (cf. prod.py).
INVITE_RETURN_RESET_LINK = os.getenv("INVITE_RETURN_RESET_LINK", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# ============================================================
# Invitations workspace v2 (domaine dédié WorkspaceInvitation)
# ============================================================
# Durée de vie d'un lien d'invitation (heures). Le lien expire strictement
# (comparaison UTC). Valeur positive obligatoire ; jamais d'expiration infinie.
WORKSPACE_INVITATION_TTL_HOURS = int(os.getenv("WORKSPACE_INVITATION_TTL_HOURS", "72"))
if WORKSPACE_INVITATION_TTL_HOURS <= 0:
    raise RuntimeError("WORKSPACE_INVITATION_TTL_HOURS doit être un entier positif.")

# Feature flag du nouveau domaine d'invitation. À 1 (défaut) : le flux v2 est
# actif et l'ancien flux d'invitation par reset-link est neutralisé. À 0 :
# rollback vers l'ancien comportement (sans réactiver l'exposition du reset_link,
# qui reste gouvernée séparément par INVITE_RETURN_RESET_LINK / prod.py).
WORKSPACE_INVITATIONS_V2_ENABLED = os.getenv("WORKSPACE_INVITATIONS_V2_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


# --- Périmètres owner (owner_scope) & ACL par RagSpace ------------------------
# Modèle « workspace = tenant, périmètres owner cloisonnés à l'intérieur ».
# TOUS ces flags sont OFF par défaut : tant qu'ils le restent, le comportement
# d'autorisation est STRICTEMENT identique à l'historique workspace-wide
# (le service d'accès délègue au chemin legacy). Ordre d'activation prévu :
#   schéma -> diagnostic -> backfill mono-owner -> mapping multi-owner ->
#   mode audit -> corrections -> UI -> enforcement -> contraintes NOT NULL.
#
# OWNER_SCOPE_ENABLED      : applique l'isolation par périmètre owner (listes,
#                            accès direct, documents, chat, Core, tâches).
# OWNER_SCOPE_AUDIT_ONLY   : n'échoue JAMAIS ; journalise would_allow/would_deny
#                            /owner_scope_mismatch/owner_scope_missing (dry-run).
# RAGSPACE_ACL_ENABLED     : exige un RagSpaceAccess actif pour les non-owners.
# RAGSPACE_ACL_AUDIT_ONLY  : idem, mode observation sans blocage.
OWNER_SCOPE_ENABLED = _env_flag("OWNER_SCOPE_ENABLED", "0")
OWNER_SCOPE_AUDIT_ONLY = _env_flag("OWNER_SCOPE_AUDIT_ONLY", "0")
RAGSPACE_ACL_ENABLED = _env_flag("RAGSPACE_ACL_ENABLED", "0")
RAGSPACE_ACL_AUDIT_ONLY = _env_flag("RAGSPACE_ACL_AUDIT_ONLY", "0")

# ALLOWED_HOSTS permissif: acceptable UNIQUEMENT en dev/local.
# settings/base.py n'est JAMAIS censé être le module de settings actif en prod :
# la prod doit passer par config.settings.prod (qui restreint ALLOWED_HOSTS via
# DJANGO_ALLOWED_HOSTS). Ce garde-fou empêche un démarrage prod accidentel sur
# `base` (donc avec ALLOWED_HOSTS=["*"]).
ALLOWED_HOSTS = ["*"]

_active_settings_module = os.getenv("DJANGO_SETTINGS_MODULE", "")
_env_is_prod_like = DJANGO_ENV.strip().lower() in ("prod", "production", "staging")
_using_base_directly = (
    _active_settings_module.endswith(".base") or not _active_settings_module
)
if _env_is_prod_like and _using_base_directly:
    raise RuntimeError(
        "Configuration dangereuse refusee : DJANGO_ENV=%r mais les settings actifs "
        "sont 'base' (ALLOWED_HOSTS=['*']). En prod/staging, utilisez "
        "DJANGO_SETTINGS_MODULE=config.settings.prod, qui restreint ALLOWED_HOSTS "
        "via DJANGO_ALLOWED_HOSTS." % DJANGO_ENV
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    # Second facteur obligatoire (repris d'un projet interne) :
    # django-otp détient la vérité OTP, django-axes protège le mot de passe.
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "axes",
    "apps.accounts",
    "apps.audit",
    "apps.chat",
    "apps.corpus.apps.CorpusConfig",
    "apps.feedback.apps.FeedbackConfig",
    "apps.llm_router",
    "apps.rag_core",
    "apps.workspaces.apps.WorkspacesConfig",
]

REST_FRAMEWORK = {
    # les pannes Redis/courtier deviennent un 503 JSON stable (`dependency_unavailable`)
    # au lieu d'une page 500 Django ; tout le reste garde le traitement DRF par défaut.
    "EXCEPTION_HANDLER": "api.v1.exception_handler.open_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Token DRF lu depuis un cookie HttpOnly (chemin principal, non lisible en JS).
        # Conserve la compat header "Authorization: Token <key>" (tests / clients API),
        # qui n'est PAS soumise au CSRF. L'auth par cookie applique le controle CSRF.
        "apps.accounts.authentication.CookieTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Débits nommés (ScopedRateThrottle) des endpoints d'invitation. Par défaut
    # None (désactivé) : le throttle n'est réellement actif que si une valeur est
    # configurée (dev/tests laissés libres ; prod fixe des seuils, cf. prod.py).
    "DEFAULT_THROTTLE_RATES": {
        "invitation_create": os.getenv("DRF_THROTTLE_INVITATION_CREATE") or None,
        "invitation_regenerate": os.getenv("DRF_THROTTLE_INVITATION_REGENERATE") or None,
        "invitation_accept": os.getenv("DRF_THROTTLE_INVITATION_ACCEPT") or None,
        # Feedback : limite PAR UTILISATEUR (UserRateThrottle scopé), jamais
        # globale — une limite globale punirait tout le monde pour l'excès d'un
        # seul. Valeur par défaut posée (contrairement aux invitations) : le
        # feedback est un endpoint d'écriture ouvert à tout compte authentifié.
        "feedback_create": os.getenv("DRF_THROTTLE_FEEDBACK_CREATE", "10/min") or None,
        # Recherche des conversations (palette, frappe débouncée) : seau DÉDIÉ, jamais le
        # seau `user` que partage `/auth/me/` (incident 429 → faux logout).
        "chat_thread_search": os.getenv("DRF_THROTTLE_CHAT_THREAD_SEARCH", "60/min") or None,
        # Connexion (1er facteur) : borne de débit HTTP. La protection PRIMAIRE
        # contre la force brute reste django-axes (verrou par compte).
        "auth": os.getenv("DRF_THROTTLE_AUTH", "20/min") or None,
        # Second facteur : six chiffres se devinent si le débit est libre, et
        # Axes ne surveille que le mot de passe. Toujours posé par défaut.
        "mfa": os.getenv("DRF_THROTTLE_MFA", "10/min") or None,
    },
}

# ============================================================
# Cookie d'authentification (token DRF en cookie HttpOnly)
# ============================================================
# Le token DRF est depose dans ce cookie au login et n'est jamais expose au JS.
# Attributs surchargeables par environnement (Secure=True force en prod).
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "ragatm_auth_token")
AUTH_COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", str(60 * 60 * 24 * 14)))  # 14 jours
AUTH_COOKIE_HTTPONLY = True
# Dev en HTTP local : Secure desactive (sinon le cookie n'est jamais pose).
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on")
# Lax : suffisant en same-origin (navigateur -> proxy Next -> backend) et robuste
# pour la 1re navigation issue d'un lien email. Voir prod.py pour la justification.
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "Lax")
AUTH_COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")

# ============================================================
# Second facteur (MFA TOTP) — OBLIGATOIRE pour tout compte, sans flag
# ============================================================
# Voir docs/03-backend/authentification-mfa.md. Aucun réglage ne désactive le
# MFA : seuls la durée et le nombre d'essais du challenge sont ajustables.
MFA_CHALLENGE_TTL_SECONDS = int(os.getenv("MFA_CHALLENGE_TTL_SECONDS", "600"))
MFA_CHALLENGE_MAX_ATTEMPTS = int(os.getenv("MFA_CHALLENGE_MAX_ATTEMPTS", "5"))
OTP_TOTP_ISSUER = "RAGatm"
# La dérive d'horloge n'est PAS mémorisée (incident un autre client interne du 2026-09-07 :
# `drift` persistant et non borné → compte inaccessible avec un secret valide).
OTP_TOTP_SYNC = False

AUTHENTICATION_BACKENDS = [
    # Axes d'abord : verrouillage anti-force-brute avant toute vérification.
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # heures — 15 minutes
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_USERNAME_CALLABLE = "apps.accounts.axes.resolve_login_username"
AXES_RESET_ON_SUCCESS = True

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.request_id.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Expose `user.is_verified()` au Django admin (OTPAdminSite).
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Axes en dernier : il doit voir l'issue de l'authentification.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "ragatm"),
        "USER": os.getenv("DB_USER", "ragatm"),
        "PASSWORD": os.getenv("DB_PASSWORD", "ragatm"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": int(os.getenv("DB_PORT", "5432")),
    }
}

# --- Politique de mot de passe ---
# Appliquée par django.contrib.auth.password_validation.validate_password,
# utilisée dans le flux reset password (et tout futur endpoint de création).
# Sans cette liste, validate_password n'imposait AUCUNE contrainte.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": int(os.getenv("AUTH_PASSWORD_MIN_LENGTH", "10"))},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "backend" / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================
# ============ LIMITES APPLICATIVES D'UPLOAD =================
# ============================================================
# Validation cote code (independante de nginx/Docker). Toutes surchargeables via .env.
# Objectif: rejeter tot un upload abusif, AVANT tout traitement couteux
# (lecture complete, extraction PDF, chunking, embeddings).

# Taille maximale d'un fichier uploade (en Mo).
UPLOAD_MAX_FILE_SIZE_MB = float(os.getenv("UPLOAD_MAX_FILE_SIZE_MB", "50"))
UPLOAD_MAX_FILE_SIZE_BYTES = int(UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024)

# Nombre maximal de pages pour un PDF.
UPLOAD_PDF_MAX_PAGES = int(os.getenv("UPLOAD_PDF_MAX_PAGES", "500"))

# Timeout (secondes) de l'extraction de texte d'un PDF : evite qu'un fichier
# corrompu / malicieux bloque un worker indefiniment. Defaut 180s : >= budget OCR
# (DOCUMENT_OCR_TIMEOUT_SECONDS=60) pour eviter les faux "timeout" sur OCR/PDF complexe.
UPLOAD_EXTRACTION_TIMEOUT_S = int(os.getenv("UPLOAD_EXTRACTION_TIMEOUT_S", "180"))

# Aligne le seuil de bufferisation memoire de Django sur la limite d'upload :
# au-dela, Django streame le fichier sur disque au lieu de le garder en RAM.
DATA_UPLOAD_MAX_MEMORY_SIZE = UPLOAD_MAX_FILE_SIZE_BYTES

# ============================================================
# ======= QUOTAS DOCUMENTAIRES PAR WORKSPACE =================
# ============================================================
# Deux quotas par workspace : nombre de fichiers + taille cumulee des fichiers
# sources (octets). Voir docs/10-reference/quotas-workspace.md.
#
# DEFAUTS DE PLATEFORME : proposes a la creation d'un workspace et COPIES dans le
# WorkspaceQuota (jamais retroactifs). Ordre de resolution EFFECTIF (cf.
# apps.workspaces.services.quota) :
#   override CoreConfig scope=global  >  ces settings (env)  >  defaut de code.
# Les variables d'environnement ci-dessous NE remplacent PAS l'administration
# superuser : elles servent de fallback / d'initialisation.
WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT = int(
    os.getenv("WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT", "5000")
)
WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES = int(
    os.getenv("WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES", str(10 * 1024 ** 3))  # 10 Gio
)

# PLAFONDS ABSOLUS (garde-fous serveur) : un superuser ne peut PAS fixer un quota
# au-dela sans changer la configuration serveur. Settings only (non editables a
# chaud). 100 000 fichiers / 1 Tio par defaut.
WORKSPACE_HARD_MAX_DOCUMENT_COUNT = int(
    os.getenv("WORKSPACE_HARD_MAX_DOCUMENT_COUNT", "100000")
)
WORKSPACE_HARD_MAX_DOCUMENT_BYTES = int(
    os.getenv("WORKSPACE_HARD_MAX_DOCUMENT_BYTES", str(1024 ** 4))  # 1 Tio
)

# ENFORCEMENT du blocage des uploads depassant le quota. Defaut ON. A OFF, les
# quotas restent CALCULES et AFFICHES mais aucun upload n'est bloque (rollout
# progressif / rollback sans perte des valeurs de quota).
WORKSPACE_QUOTAS_ENFORCED = env_bool("WORKSPACE_QUOTAS_ENFORCED", True)

# ============================================================
# ======= EXTRACTION DOCUMENTAIRE (MD/DOCX/CSV/HTML/OCR) ======
# ============================================================
# Socle d'extraction multi-format. Toutes surchargeables via .env.
# Les valeurs taille/pages retombent sur les UPLOAD_* historiques si non definies.

# --- Global ---
DOCUMENT_MAX_UPLOAD_SIZE_MB = float(os.getenv("DOCUMENT_MAX_UPLOAD_SIZE_MB", str(UPLOAD_MAX_FILE_SIZE_MB)))
DOCUMENT_MAX_EXTRACTED_CHARS = int(os.getenv("DOCUMENT_MAX_EXTRACTED_CHARS", "5000000"))
DOCUMENT_EXTRACTION_TIMEOUT_SECONDS = int(
    os.getenv("DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", str(UPLOAD_EXTRACTION_TIMEOUT_S))
)
# Budget de la stratégie de reprise « délai étendu ». Sa cohérence avec les limites
# douces Celery est vérifiée par `manage.py check` (config.E012) : une valeur
# supérieure ferait tuer la tâche avant la fin de l'extraction.
DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = int(
    os.getenv("DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS", "480")
)
DOCUMENT_PDF_MAX_PAGES = int(os.getenv("DOCUMENT_PDF_MAX_PAGES", str(UPLOAD_PDF_MAX_PAGES)))

# --- Extraction PDF native : fallback multi-moteur (P0) ---
# Tentee UNIQUEMENT quand pypdf n'extrait pas assez de texte ; LOCAL, sans OCR ni reseau.
# Defauts alignes sur ingest/extraction/config.py (aucun changement de comportement).
DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED = os.getenv(
    "DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")
# Réglage `list` au registre : projeté en LISTE (jamais la chaîne brute de l'env).
DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES = reg("DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES")
DOCUMENT_PDF_MIN_TOTAL_CHARS = int(os.getenv("DOCUMENT_PDF_MIN_TOTAL_CHARS", "200"))

# --- OCR (DESACTIVE par defaut ; aucun appel externe sans configuration explicite) ---
DOCUMENT_OCR_ENABLED = os.getenv("DOCUMENT_OCR_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# Seul "tesseract" (LOCAL) est implemente. Un provider externe doit etre code + configure.
DOCUMENT_OCR_PROVIDER = os.getenv("DOCUMENT_OCR_PROVIDER", "tesseract")
DOCUMENT_OCR_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_OCR_TIMEOUT_SECONDS", "60"))
DOCUMENT_OCR_MAX_PAGES = int(os.getenv("DOCUMENT_OCR_MAX_PAGES", "50"))
DOCUMENT_OCR_MIN_CHARS_PER_PAGE = int(os.getenv("DOCUMENT_OCR_MIN_CHARS_PER_PAGE", "24"))
DOCUMENT_OCR_LANGUAGES = os.getenv("DOCUMENT_OCR_LANGUAGES", "fra+eng")
# Rastérisation PDF->image avant OCR (2 ≈ 144 DPI ; plans A3 -> 3-4) + correction d'orientation OSD.
DOCUMENT_OCR_RASTER_SCALE = float(os.getenv("DOCUMENT_OCR_RASTER_SCALE", "2.0"))
DOCUMENT_OCR_AUTO_ROTATE = os.getenv("DOCUMENT_OCR_AUTO_ROTATE", "1").strip().lower() in ("1", "true", "yes", "on")
# Second passage Tesseract (image_to_data) : sert UNIQUEMENT a la confiance OCR.
# 36 % du temps OCR median pour une valeur qu aucune decision ne lit (cf. docs 03-backend).
DOCUMENT_OCR_COMPUTE_CONFIDENCE = os.getenv("DOCUMENT_OCR_COMPUTE_CONFIDENCE", "1").strip().lower() in ("1", "true", "yes", "on")

# --- Images : limites techniques (Lot 1) + porte fonctionnelle (Lot 2) ---
# 0 => repli sur la limite d'upload globale. MAX_PIXELS = garde anti decompression-bomb.
DOCUMENT_IMAGE_MAX_BYTES = int(os.getenv("DOCUMENT_IMAGE_MAX_BYTES", "0"))
DOCUMENT_IMAGE_MAX_PIXELS = int(os.getenv("DOCUMENT_IMAGE_MAX_PIXELS", "40000000"))
DOCUMENT_IMAGE_PREPROCESS_ENABLED = os.getenv("DOCUMENT_IMAGE_PREPROCESS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
# Porte d'ingestion image (Lot 2), INDÉPENDANTE de DOCUMENT_OCR_ENABLED : une image
# acceptée est OCRisée automatiquement. Mettre 0 pour refuser tout upload image.
DOCUMENT_IMAGE_INGEST_ENABLED = os.getenv("DOCUMENT_IMAGE_INGEST_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
# Allowlist formats image (MVP). GIF/BMP hors MVP -> image_unsupported_format.
# Réglage `list` au registre : projeté en LISTE, comme le déclare le registre.
DOCUMENT_IMAGE_FORMATS = reg("DOCUMENT_IMAGE_FORMATS")
# Analyse visuelle via l'AI Gateway — FERMÉE par défaut. Sources
# opérationnelles : `image` (image autonome) et `pdf` (visuels INTÉGRÉS) ;
# `docx` reste réservée et ignorée. Le défaut n'ouvre que `image`. Active : les sources
# ouvertes sont forcées en asynchrone sur la file `ingest_visual`.
DOCUMENT_VISUAL_ANALYSIS_ENABLED = os.getenv("DOCUMENT_VISUAL_ANALYSIS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_VISUAL_ANALYSIS_SOURCES = reg("DOCUMENT_VISUAL_ANALYSIS_SOURCES")
# Vision V2 orchestrée des crops — ÉTEINTE par défaut, allowlist d'espaces
# vide = aucun espace, garde dure de production dédiée, DPI unique de rendu.
VISUAL_V2_ORCHESTRATION_ENABLED = reg("VISUAL_V2_ORCHESTRATION_ENABLED")
VISUAL_V2_RAGSPACE_ALLOWLIST = reg("VISUAL_V2_RAGSPACE_ALLOWLIST")
VISUAL_V2_PRODUCTION_ALLOWED = reg("VISUAL_V2_PRODUCTION_ALLOWED")
VISUAL_V2_RASTER_DPI = reg("VISUAL_V2_RASTER_DPI")
# Extraction structurée expérimentale chart/table
# ÉTEINTE par défaut, allowlist d'espaces vide = aucun espace, refusée en production sauf
# dérogation explicite (STRUCTVIS-PRODUCTION-GATE-1), tâches plafonnées à `table` en production.
STRUCTURED_VISUAL_ENABLED = reg("STRUCTURED_VISUAL_ENABLED")
STRUCTURED_VISUAL_PRODUCTION_ALLOWED = reg("STRUCTURED_VISUAL_PRODUCTION_ALLOWED")
STRUCTURED_VISUAL_ALLOWED_TASKS = reg("STRUCTURED_VISUAL_ALLOWED_TASKS")
STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = reg("STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST")
STRUCTURED_VISUAL_AUTO_ENABLED = reg("STRUCTURED_VISUAL_AUTO_ENABLED")
STRUCTURED_VISUAL_PDF_MAX_ASSETS = reg("STRUCTURED_VISUAL_PDF_MAX_ASSETS")
STRUCTURED_VISUAL_PDF_MAX_PER_PAGE = reg("STRUCTURED_VISUAL_PDF_MAX_PER_PAGE")
# Numérisation des courbes en mode SHADOW — ÉTEINTE par
# défaut, allowlist propre vide = aucun espace, refusée en production ; aucune sortie RAG.
STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED = reg("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED")
STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST = reg("STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST")

# --- Visuels PDF : bornes GÉNÉRIQUES de la découverte ---
# Ces bornes n'ouvrent rien : elles plafonnent le coût quand la source `pdf` est active.
# Un dépassement est COMPTÉ (`omitted_limit`), jamais un échec de document.
DOCUMENT_PDF_VISUAL_MAX_ASSETS = int(os.getenv("DOCUMENT_PDF_VISUAL_MAX_ASSETS", "12"))
DOCUMENT_PDF_VISUAL_MAX_PER_PAGE = int(os.getenv("DOCUMENT_PDF_VISUAL_MAX_PER_PAGE", "4"))
# Écartent puces, filets et icônes — critère de TAILLE, jamais de contenu.
DOCUMENT_PDF_VISUAL_MIN_SIDE_PX = int(os.getenv("DOCUMENT_PDF_VISUAL_MIN_SIDE_PX", "96"))
DOCUMENT_PDF_VISUAL_MIN_PIXELS = int(os.getenv("DOCUMENT_PDF_VISUAL_MIN_PIXELS", "20000"))
# Borne du contrat Vision V1 (2 Mpx) : au-delà l'image est RÉDUITE, jamais écartée.
DOCUMENT_PDF_VISUAL_MAX_PIXELS = int(os.getenv("DOCUMENT_PDF_VISUAL_MAX_PIXELS", "2000000"))
DOCUMENT_PDF_VISUAL_MAX_BYTES = int(os.getenv("DOCUMENT_PDF_VISUAL_MAX_BYTES", "8000000"))
# Au-delà de ce recouvrement, un tableau déjà extrait NATIVEMENT fait foi : le visuel
# qui le couvre est écarté (aucun doublon appauvri face à une structure ligne/colonne).
DOCUMENT_PDF_VISUAL_TABLE_OVERLAP_RATIO = float(os.getenv("DOCUMENT_PDF_VISUAL_TABLE_OVERLAP_RATIO", "0.6"))
# Empreinte répétée au-delà de ce seuil = décoratif (logo, bandeau, filigrane).
DOCUMENT_PDF_VISUAL_MAX_REPEATS = int(os.getenv("DOCUMENT_PDF_VISUAL_MAX_REPEATS", "3"))
# régions vectorielles rendues (OFF ; allowlist vide = aucun espace).
DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED = reg("DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED")
DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS = reg("DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS")
DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI = reg("DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI")
DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES = reg("DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES")

# --- Tableaux structurés (P3) : bornes de sécurité ---
DOCUMENT_TABLE_MAX_PER_DOC = int(os.getenv("DOCUMENT_TABLE_MAX_PER_DOC", "50"))
DOCUMENT_TABLE_MAX_CHARS = int(os.getenv("DOCUMENT_TABLE_MAX_CHARS", "100000"))
DOCUMENT_TABLE_MAX_FRAGMENTS = int(os.getenv("DOCUMENT_TABLE_MAX_FRAGMENTS", "40"))
# P3-b : reconstruction de tableaux depuis l'OCR (image-only/scanné). OPT-IN
# (fragile + coûteux) ; tableaux marqués confidence=low + requires_visual_check.
DOCUMENT_TABLE_OCR_ENABLED = os.getenv("DOCUMENT_TABLE_OCR_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# Tableaux PDF GÉNÉRIQUES (structure ligne/colonne/page reconstruite depuis la mise
# en page). OPT-IN : le rail par défaut reste l'indexation par page. Activation
# décidée par l'exploitation, jamais par la présence du code.
DOCUMENT_PDF_GENERIC_TABLES_ENABLED = os.getenv("DOCUMENT_PDF_GENERIC_TABLES_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# le drapeau ne suffit plus — seuls les RagSpace listés (UUID complets) en bénéficient.
DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = reg("DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS")

# --- Rail hybride Document Vision (MinerU) — prototype hors ligne, ÉTEINT ---
# Éteint, aucun code du paquet `docvision` n'est atteint : le chemin d'extraction
# reste celui de la ligne de base. Le rail est un enrichissement, jamais un
# remplacement du texte natif.
DOCUMENT_MINERU_HYBRID_ENABLED = os.getenv("DOCUMENT_MINERU_HYBRID_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_MINERU_SIDECAR_URL = os.getenv("DOCUMENT_MINERU_SIDECAR_URL", "http://127.0.0.1:16580/v1").strip()
DOCUMENT_MINERU_PAGE_TIMEOUT_S = int(os.getenv("DOCUMENT_MINERU_PAGE_TIMEOUT_S", "60"))
DOCUMENT_MINERU_DOCUMENT_TIMEOUT_S = int(os.getenv("DOCUMENT_MINERU_DOCUMENT_TIMEOUT_S", "300"))
DOCUMENT_MINERU_TIER = os.getenv("DOCUMENT_MINERU_TIER", "basic").strip()
DOCUMENT_MINERU_OUTPUT_FORMAT = os.getenv("DOCUMENT_MINERU_OUTPUT_FORMAT", "middle_json").strip()
DOCUMENT_MINERU_MAX_PAGES_PER_DOC = int(os.getenv("DOCUMENT_MINERU_MAX_PAGES_PER_DOC", "3"))
DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS = os.getenv("DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS", "").strip()
DOCUMENT_MINERU_MAX_INLINE_BYTES = int(os.getenv("DOCUMENT_MINERU_MAX_INLINE_BYTES", "33554432"))
# Porte de service liée à la licence MinerU (attribution). Défaut False : on ne
# présume pas la conformité. Sans effet sur le prototype hors ligne.
MINERU_SERVICE_USE_SCOPE = os.getenv("MINERU_SERVICE_USE_SCOPE", "unknown").strip().lower()
MINERU_SERVICE_ATTRIBUTION_SATISFIED = os.getenv("MINERU_SERVICE_ATTRIBUTION_SATISFIED", "0").strip().lower() in ("1", "true", "yes", "on")
# Borne sur les fragments (ligne logique x page) émis par le rail générique. Elle
# ne remplace pas DOCUMENT_TABLE_MAX_PER_DOC, qui borne des tableaux ATOMIQUES :
# un tableau générique de 150 pages produit légitimement des centaines de fragments.
DOCUMENT_TABLE_MAX_ROW_CHUNKS = int(os.getenv("DOCUMENT_TABLE_MAX_ROW_CHUNKS", "2000"))

# --- DOCX (anti zip-bomb) ---
DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB = float(os.getenv("DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB", "200"))
DOCUMENT_DOCX_MAX_ZIP_ENTRIES = int(os.getenv("DOCUMENT_DOCX_MAX_ZIP_ENTRIES", "2000"))
DOCUMENT_DOCX_MAX_COMPRESSION_RATIO = int(os.getenv("DOCUMENT_DOCX_MAX_COMPRESSION_RATIO", "200"))

# --- CSV ---
DOCUMENT_CSV_MAX_ROWS = int(os.getenv("DOCUMENT_CSV_MAX_ROWS", "100000"))
DOCUMENT_CSV_MAX_COLUMNS = int(os.getenv("DOCUMENT_CSV_MAX_COLUMNS", "256"))
DOCUMENT_CSV_MAX_CELL_CHARS = int(os.getenv("DOCUMENT_CSV_MAX_CELL_CHARS", "10000"))
DOCUMENT_CSV_ROWS_PER_BLOCK = int(os.getenv("DOCUMENT_CSV_ROWS_PER_BLOCK", "1"))

# --- HTML / Markdown ---
DOCUMENT_HTML_MAX_CHARS = int(os.getenv("DOCUMENT_HTML_MAX_CHARS", "2000000"))
DOCUMENT_MARKDOWN_MAX_CHARS = int(os.getenv("DOCUMENT_MARKDOWN_MAX_CHARS", "2000000"))

# --- EXCEL (.xlsx) — valeurs issues des MESURES du spike E1 ---
# cf. docs/reports/SPIKE_E1_OPENPYXL_RAGATM.md
DOCUMENT_EXCEL_ENABLED = os.getenv("DOCUMENT_EXCEL_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
# LA borne mémoire : openpyxl charge TOUTE la table de chaînes partagées, même en
# read_only. RSS ≈ 45 Mo + 3,5 × taille(sharedStrings.xml) -> 50 Mo ⇒ ~220 Mo max.
# Lisible dans le répertoire central du ZIP, donc vérifiable AVANT tout parsing.
DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB = float(os.getenv("DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB", "50"))
DOCUMENT_EXCEL_MAX_CELLS = int(os.getenv("DOCUMENT_EXCEL_MAX_CELLS", "1000000"))
# Largeur déclarée au-delà de laquelle la dimension d'une feuille est jugée FANTÔME
# (A1:XFD… = toute la largeur d'Excel). On ne refuse pas le classeur sur une telle
# dimension : on défère au comptage des cellules réelles pendant l'extraction en flux.
DOCUMENT_EXCEL_MAX_DECLARED_COLS = int(os.getenv("DOCUMENT_EXCEL_MAX_DECLARED_COLS", "2048"))
DOCUMENT_EXCEL_MAX_SHEETS = int(os.getenv("DOCUMENT_EXCEL_MAX_SHEETS", "100"))
DOCUMENT_EXCEL_MAX_COLUMNS = int(os.getenv("DOCUMENT_EXCEL_MAX_COLUMNS", "256"))
DOCUMENT_EXCEL_MAX_CELL_CHARS = int(os.getenv("DOCUMENT_EXCEL_MAX_CELL_CHARS", "2000"))
DOCUMENT_EXCEL_MAX_UNCOMPRESSED_MB = float(os.getenv("DOCUMENT_EXCEL_MAX_UNCOMPRESSED_MB", "200"))
DOCUMENT_EXCEL_MAX_BLOCKS = int(os.getenv("DOCUMENT_EXCEL_MAX_BLOCKS", "200"))
DOCUMENT_EXCEL_MAX_CHUNKS = int(os.getenv("DOCUMENT_EXCEL_MAX_CHUNKS", "2000"))
DOCUMENT_EXCEL_ROWS_PER_CHUNK = int(os.getenv("DOCUMENT_EXCEL_ROWS_PER_CHUNK", "40"))
DOCUMENT_EXCEL_CHUNK_MAX_CHARS = int(os.getenv("DOCUMENT_EXCEL_CHUNK_MAX_CHARS", "4000"))
DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN = int(os.getenv("DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN", "20"))
# Profondeur max d'un en-tête multi-lignes (bandeau fusionné + intitulés de colonnes).
DOCUMENT_EXCEL_MAX_HEADER_ROWS = int(os.getenv("DOCUMENT_EXCEL_MAX_HEADER_ROWS", "4"))
# Politique des feuilles masquées : none | hidden_only | all.
# `hidden` = commodité de présentation (réaffichable d'un clic) -> indexée.
# `veryHidden` = non modifiable depuis l'interface d'Excel, intention délibérée -> exclue.
DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = os.getenv(
    "DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY", "hidden_only"
)
# Déprécié — conservé pour ne casser aucun environnement (à 1, vaut « all »).
DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS = os.getenv(
    "DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS", "0"
).strip().lower() in ("1", "true", "yes", "on")

# --- POWERPOINT (.pptx) ---
# `.pptm` (macros) / `.ppt` (OLE2 hérité) / `.odp` restent REFUSÉS, par CONTENU.
# Aucune macro n'est exécutée, aucune relation externe n'est suivie, aucun OCR n'est
# lancé sur les images de diapositives.
DOCUMENT_PPTX_ENABLED = os.getenv("DOCUMENT_PPTX_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_PPTX_MAX_UNCOMPRESSED_SIZE_MB = float(os.getenv("DOCUMENT_PPTX_MAX_UNCOMPRESSED_SIZE_MB", "200"))
# Plus haut que DOCX (2000) : un deck illustré porte une entrée ZIP par média.
DOCUMENT_PPTX_MAX_ZIP_ENTRIES = int(os.getenv("DOCUMENT_PPTX_MAX_ZIP_ENTRIES", "5000"))
DOCUMENT_PPTX_MAX_COMPRESSION_RATIO = int(os.getenv("DOCUMENT_PPTX_MAX_COMPRESSION_RATIO", "200"))
DOCUMENT_PPTX_MAX_SLIDES = int(os.getenv("DOCUMENT_PPTX_MAX_SLIDES", "500"))
DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE = int(os.getenv("DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE", "500"))
DOCUMENT_PPTX_MAX_TABLE_CELLS = int(os.getenv("DOCUMENT_PPTX_MAX_TABLE_CELLS", "50000"))
DOCUMENT_PPTX_MAX_CHARS = int(os.getenv("DOCUMENT_PPTX_MAX_CHARS", "2000000"))
DOCUMENT_PPTX_GROUP_MAX_DEPTH = int(os.getenv("DOCUMENT_PPTX_GROUP_MAX_DEPTH", "10"))
# Notes du présentateur : OFF par défaut. Une note est un commentaire de l'orateur, pas
# le contenu publié de la diapositive. Activées -> chunks SÉPARÉS (kind="pptx_notes").
DOCUMENT_PPTX_INCLUDE_NOTES = os.getenv("DOCUMENT_PPTX_INCLUDE_NOTES", "0").strip().lower() in ("1", "true", "yes", "on")
# Texte alternatif des images : ON par défaut (texte SAISI par l'auteur, souvent la
# seule description d'un schéma). Rendu marqué « [Image : … ] », jamais confondu avec
# du texte de diapositive.
DOCUMENT_PPTX_INCLUDE_ALT_TEXT = os.getenv("DOCUMENT_PPTX_INCLUDE_ALT_TEXT", "1").strip().lower() in ("1", "true", "yes", "on")
# données natives des graphiques Office (cache c:chartSpace), OPT-IN.
DOCUMENT_OFFICE_CHART_DATA_ENABLED = reg("DOCUMENT_OFFICE_CHART_DATA_ENABLED")

# --- Outlook (.msg) — lot ingestion MSG ---
# DÉFAUT DE PROJECTION corrigé par le lot FORMATS-MSG : ces réglages étaient déclarés
# au registre et documentés, mais AUCUN n'était projeté dans les settings Django.
# `config.msg_enabled()` lit `getattr(settings, "DOCUMENT_MSG_ENABLED", False)` : le
# flag valait donc False quoi qu'on écrive dans l'environnement, et le format était
# INACTIVABLE. Les défauts ci-dessous sont IDENTIQUES à ceux du code appelant
# (`extraction/config.py`) : à environnement inchangé, le comportement est inchangé.
#
# Corps et métadonnées UNIQUEMENT. Les pièces jointes sont inventoriées et JAMAIS
# ingérées (aucun Document dérivé, aucun chunk, aucun retrieval).
DOCUMENT_MSG_ENABLED = os.getenv("DOCUMENT_MSG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# Plafond PROPRE au `.msg`, en plus de la limite d'upload globale. 25 Mio : valeur
# dérivée du corpus de référence (0,9 à 2,5 Mo), pas un héritage de l'ancienne limite
# globale d'upload — les deux sont indépendantes.
DOCUMENT_MSG_MAX_FILE_SIZE_MB = float(os.getenv("DOCUMENT_MSG_MAX_FILE_SIZE_MB", "25"))
DOCUMENT_MSG_MAX_BODY_CHARS = int(os.getenv("DOCUMENT_MSG_MAX_BODY_CHARS", "2000000"))
DOCUMENT_MSG_MAX_RECIPIENTS = int(os.getenv("DOCUMENT_MSG_MAX_RECIPIENTS", "500"))
DOCUMENT_MSG_MAX_ATTACHMENTS_INVENTORIED = int(os.getenv("DOCUMENT_MSG_MAX_ATTACHMENTS_INVENTORIED", "100"))

# --- Courriels : EML + contenu des pièces jointes ---
# Portée par ESPACE (liste d'UUID complets, vide = aucun) : les portes de format seules
# n'ouvrent jamais les courriels à tous les espaces.
DOCUMENT_EML_ENABLED = reg("DOCUMENT_EML_ENABLED")
DOCUMENT_EML_MAX_FILE_SIZE_MB = reg("DOCUMENT_EML_MAX_FILE_SIZE_MB")
DOCUMENT_EMAIL_RAGSPACE_IDS = reg("DOCUMENT_EMAIL_RAGSPACE_IDS")
# Disponibilité générale : tout espace, droits et quotas inchangés.
DOCUMENT_EMAIL_GLOBAL_ENABLED = reg("DOCUMENT_EMAIL_GLOBAL_ENABLED")
DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED = reg("DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED")

# --- Audio : transcription ASR AI Gateway, fermé par défaut ---
DOCUMENT_AUDIO_INGEST_ENABLED = reg("DOCUMENT_AUDIO_INGEST_ENABLED")
DOCUMENT_AUDIO_RAGSPACE_IDS = reg("DOCUMENT_AUDIO_RAGSPACE_IDS")
# Disponibilité générale : tout espace, droits/quotas/plafond inchangés.
DOCUMENT_AUDIO_GLOBAL_ENABLED = reg("DOCUMENT_AUDIO_GLOBAL_ENABLED")
DOCUMENT_AUDIO_MAX_UPLOAD_BYTES = reg("DOCUMENT_AUDIO_MAX_UPLOAD_BYTES")
DOCUMENT_AUDIO_MAX_DURATION_SECONDS = reg("DOCUMENT_AUDIO_MAX_DURATION_SECONDS")
AUDIO_ASR_MODEL_LOGICAL = reg("AUDIO_ASR_MODEL_LOGICAL")
AUDIO_ASR_PART_MAX_SECONDS = reg("AUDIO_ASR_PART_MAX_SECONDS")
AUDIO_ASR_PART_MAX_BYTES = reg("AUDIO_ASR_PART_MAX_BYTES")
AUDIO_ASR_POLL_INTERVAL_SECONDS = reg("AUDIO_ASR_POLL_INTERVAL_SECONDS")
AUDIO_ASR_JOB_DEADLINE_SECONDS = reg("AUDIO_ASR_JOB_DEADLINE_SECONDS")
AUDIO_SPOOL_DIR = reg("AUDIO_SPOOL_DIR")
DOCUMENT_EMAIL_MAX_ATTACHMENTS = reg("DOCUMENT_EMAIL_MAX_ATTACHMENTS")
DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB = reg("DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB")
DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB = reg("DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB")
DOCUMENT_EMAIL_MAX_NESTING_DEPTH = reg("DOCUMENT_EMAIL_MAX_NESTING_DEPTH")

# --- XML générique (.xml) — qualification interne ---
# defusedxml : DTD / entités / références externes REFUSÉES (`unsafe_xml`), aucun accès
# réseau ni fichier. FERMÉ par défaut (lot neuf) : à 0, un `.xml` reconnu est refusé
# (`xml_ingestion_disabled`). Limites STRUCTURELLES (racine = profondeur 1 ; éléments),
# distinctes de la limite d'upload et jamais réglables par le client.
DOCUMENT_XML_ENABLED = os.getenv("DOCUMENT_XML_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_XML_MAX_DEPTH = int(os.getenv("DOCUMENT_XML_MAX_DEPTH", "64"))
DOCUMENT_XML_MAX_NODES = int(os.getenv("DOCUMENT_XML_MAX_NODES", "200000"))

# --- DXF (.dxf) — qualification interne ---
# ezdxf en lecture STRICTE, sans rendu ni géométrie : textes, blocs/attributs, cotes,
# calques, layouts, XREF déclarées (jamais ouvertes). FERMÉ par défaut (lot neuf).
# Toujours asynchrone (décision produit, comme Excel/PPTX). Limite STRUCTURELLE en
# entités, distincte de la limite d'upload, jamais réglable par le client.
DOCUMENT_DXF_ENABLED = os.getenv("DOCUMENT_DXF_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_DXF_MAX_ENTITIES = int(os.getenv("DOCUMENT_DXF_MAX_ENTITIES", "250000"))

# --- IFC (.ifc) — qualification interne ---
# IfcOpenShell en extraction SÉMANTIQUE seule (jamais de géométrie), import paresseux.
# FERMÉ par défaut ; toujours asynchrone, routé vers la file dédiée `ingest_ifc` :
# NE PAS ACTIVER tant qu'un worker `--queues=ingest_ifc` ne tourne pas (les maquettes
# resteraient PENDING). Limite STRUCTURELLE en entités STEP, jamais réglable par le client.
DOCUMENT_IFC_ENABLED = os.getenv("DOCUMENT_IFC_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_IFC_MAX_ENTITIES = int(os.getenv("DOCUMENT_IFC_MAX_ENTITIES", "250000"))

# --- DWG (.dwg) — qualification interne ---
# Format binaire propriétaire : RagATM ne le parse pas. Conversion LOCALE par GNU
# LibreDWG 0.14 (`dwg2dxf`, exécutable séparé, sous-processus sans shell) vers un DXF
# ASCII TEMPORAIRE, puis extraction par le DxfExtractor déjà qualifié. FERMÉ par défaut ;
# toujours asynchrone, routé vers la file dédiée `ingest_dwg` : NE PAS ACTIVER tant qu'un
# worker `--queues=ingest_dwg` portant LibreDWG 0.14 ne tourne pas, ni avant la revue de
# conformité GPLv3+ et la qualification sur un DWG de consultation réel.
DOCUMENT_DWG_ENABLED = os.getenv("DOCUMENT_DWG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
DOCUMENT_DWG_CONVERTER_PATH = os.getenv("DOCUMENT_DWG_CONVERTER_PATH", "/usr/local/bin/dwg2dxf")
DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS", "60"))
DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB = int(os.getenv("DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB", "128"))

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ============================================================
# ======================= RAG POLICIES =======================
# ============================================================

RAG_MIN_SOURCES = int(os.getenv("RAG_MIN_SOURCES", "1"))
RAG_MIN_TEXT_RANK = float(os.getenv("RAG_MIN_TEXT_RANK", "0.0"))
# Seuil CALIBRÉ pour le profil de référence ollama/nomic-embed-text (dim 768), cf.
# benchmarks/calibration/demo-fr-extended-nomic-report.md. NE PAS réutiliser tel quel
# avec un autre modèle d'embeddings : le seuil est une propriété du couple
# modèle × corpus × fusion.
# ⚠️ Portée réelle : `should_answer` n'applique min_score que sur le chemin
# VECTORIEL-SEUL ; dès qu'un signal FTS existe, c'est RAG_MIN_TEXT_RANK qui décide
# (cf. apps/rag_core/domain/policies.py).
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.76"))

# --- Politique d'ADMISSION du retrieval (lot 4) ------------------------------
#
# `should_answer` (domain/policies.py) décide par un seuil ABSOLU, en booléen, et
# avec un ordre dont la conséquence est rarement énoncée : dès qu'un signal FTS
# existe, `RAG_MIN_SCORE` n'est plus appliqué. En mode hybride — la configuration
# réelle de staging ET de production — la décision se réduit donc à
# `ts_rank >= RAG_MIN_TEXT_RANK` (0,02), pendant que le seuil vectoriel vaut 0,08.
#
# Mesuré au lot 3 sur le corpus pilote : les scores des positifs et des négatifs se
# RECOUVRENT. Un seuil absolu seul ne peut pas les séparer, où qu'on le place. Ce qui
# discrimine, c'est la SÉPARATION top-1/top-2 : un corpus qui contient la réponse la
# fait ressortir ; un corpus qui ne la contient pas rend une grappe de passages
# également médiocres.
#
# Livré à `legacy` : comportement historique à l'octet près. C'est aussi
# l'interrupteur de rollback — aucune migration, aucune réindexation.
RAG_ADMISSION_POLICY = reg("RAG_ADMISSION_POLICY")
RAG_MIN_MARGIN = reg("RAG_MIN_MARGIN")
RAG_UNCERTAIN_SCORE = reg("RAG_UNCERTAIN_SCORE")
RAG_TEXT_ADMISSION_POLICY = reg("RAG_TEXT_ADMISSION_POLICY")

RAG_TOP_K = int(os.getenv("RAG_TOP_K", "8"))

# Source de vérité UNIQUE du drapeau hybride (le doublon historique plus bas a été
# supprimé). Parsing robuste : "True"/"yes"/"on" sont acceptés — un `== "1"` naïf
# résolvait `RAG_HYBRID_ENABLED=True` à False et désactivait le vectoriel en silence.
RAG_HYBRID_ENABLED = env_bool("RAG_HYBRID_ENABLED", True)
# Signaux forts d'une question (dates, valeurs, opérateurs temporels).
#
# DÉFAUT 0 : la qualification P3 mesure un gain net et reproductible (Recall@5
# 0,75 -> 0,89 ; temporel 0,57 -> 0,86 ; MRR 0,51 -> 0,75 sur trois ingestions) mais
# n'atteint PAS les deux seuils exigés (Recall@5 >= 0,90 et temporel = 1,00). Le code
# est donc livré INACTIF : il ne change rien tant qu'un exploitant ne l'ouvre pas.
#
# Strictement CONDITIONNEL une fois ouvert : sans signal détecté dans la question, le
# pipeline est inchangé et aucune requête supplémentaire n'est émise.
RAG_STRONG_SIGNALS_ENABLED = env_bool("RAG_STRONG_SIGNALS_ENABLED", False)

# --- P3-c : leg documentaire borné sur les métadonnées de date ---------------
#
# Une question temporelle GLOBALE (« la note la plus récente ») ne porte aucun thème :
# ni le lexical ni le vectoriel ne peuvent en constituer un pool fiable, et élargir
# `candidate_k` ne suffit pas (le candidat existe mais reste faible, donc la politique
# de nettoyage le retire). Le leg lit alors la métadonnée de date en SQL borné, filtré
# par RagSpace AVANT tri.
#
# STRICTEMENT CONDITIONNEL : sans opérateur temporel détecté, aucune requête
# supplémentaire n'est émise et le comportement historique est inchangé. Désactivé par
# défaut ; rollback immédiat en repassant la variable à 0/false.
RAG_TEMPORAL_CANDIDATES_ENABLED = env_bool("RAG_TEMPORAL_CANDIDATES_ENABLED", False)

# Borne ABSOLUE du leg temporel : nombre maximum de documents rapportés par la requête
# de métadonnées. Ce n'est pas un réglage de confort — c'est ce qui garantit que la
# requête reste bornée quelle que soit la taille du RagSpace. Validée à l'usage
# (défaut si absente / non entière / < 1 ; plafonnée au maximum absolu) : voir
# `services/query/temporal_candidates.temporal_candidate_limit`.
RAG_TEMPORAL_CANDIDATE_LIMIT = int(os.getenv("RAG_TEMPORAL_CANDIDATE_LIMIT", "100"))

# --- P3-d : seuil de pertinence vectorielle réglable PAR ESPACE --------------
#
# `RAG_MIN_SCORE` ci-dessus est un seuil ABSOLU sur une similarité cosinus, dont
# l'échelle dépend du modèle d'embedding et de l'appariement linguistique entre la
# question et le fonds. Mesuré sur un corpus bilingue (questions FR, documents EN) :
# les documents PERTINENTS culminent à 0,739 et les questions HORS SUJET à 0,687,
# alors que le défaut global vaut 0,76 — au-dessus des deux populations. Le seuil ne
# discriminait donc plus rien : il coupait des documents classés premiers au vectoriel.
#
# Ouvert, ce flag autorise `RagSpace.min_score` à primer sur le défaut global. Il ne
# BAISSE rien : le défaut global est inchangé, et un espace sans valeur explicite se
# comporte exactement comme avant. Fermé par défaut ; rollback immédiat en repassant à 0.
RAG_SPACE_MIN_SCORE_ENABLED = env_bool("RAG_SPACE_MIN_SCORE_ENABLED", False)

# Trajectoire RagATM_Core : le chat passe DÉSORMAIS toujours par la façade
# `apps.rag_core.core.retrieve`. Le flag `RAG_USE_CORE_RETRIEVE` qui sélectionnait
# l'ancien appel direct à `query_rag` a été retiré : la façade délègue à `query_rag`
# et expose son dict verbatim, les deux branches rendaient donc le même objet.
# Une variable encore posée dans un `.env` est désormais inconnue du registre et
# signalée par `config.W012` — elle n'a plus aucun effet.

# Politique de LANGUE DE RÉPONSE IA (P0). Flag plateforme, DISTINCT de la préférence
# utilisateur `answer_language` (auto|fr|en) et de la locale d'interface
# `preferred_locale`. OFF par défaut → prompts français historiques inchangés (à
# l'octet près). ON + answer_language=auto → langue de la dernière question ; ON +
# answer_language=fr|en → langue forcée. N'affecte NI le retrieval, NI l'answerability,
# NI les citations, NI les extraits, NI les messages persistés. Cf.
# apps/rag_core/services/answer_language.py.
#
# Lue via `reg()` (et non `os.getenv` brut) : le registre valide le type et REFUSE
# une valeur malformée au démarrage. Auparavant `RAG_...=true` valait silencieusement
# False — la politique restait éteinte sans que personne ne le voie. La clé reste
# STATIQUE (`runtime_editable=False`) : son activation passe par la variable
# d'environnement et un redéploiement, jamais par un override Admin Core.
RAG_ANSWER_LANGUAGE_POLICY_ENABLED = reg("RAG_ANSWER_LANGUAGE_POLICY_ENABLED")

# Retrieval bi-source (corpus métier, Phase 6) : à score équivalent, on favorise
# légèrement les documents PRIVÉS du workspace par rapport au socle corpus partagé.
# Boost MULTIPLICATIF appliqué au score avant fusion/rerank. 1.0 = neutre.
RAG_WORKSPACE_SOURCE_BOOST = float(os.getenv("RAG_WORKSPACE_SOURCE_BOOST", "1.10"))
RAG_DOMAIN_CORPUS_SOURCE_BOOST = float(os.getenv("RAG_DOMAIN_CORPUS_SOURCE_BOOST", "1.00"))

# Configuration FTS PostgreSQL (B3) : "simple" | "french" | "english".
# 'simple' (défaut) = robuste sur sigles/entités/bilingue mais sans stemming ni
# pluriels FR. 'french' = stemming + stop words FR (meilleur rappel morphologique),
# au prix d'un index GIN dédié (migration 0023) et d'un comportement plus agressif
# sur les noms propres. A/B-testable via le benchmark : --fts-config simple|french.
RAG_FTS_CONFIG = os.getenv("RAG_FTS_CONFIG", "simple")

# Chemin « vecteur lexical stocké » (colonne générée `text_tsv_french` + index GIN).
# Défaut FALSE : le chemin historique reste la référence et la cible du rollback.
# L'activation exige RAG_FTS_CONFIG='french' ET le schéma migré — refus au
# démarrage sinon (checks rag_core.E120/E121/E122).
RAG_FTS_STORED_VECTOR_ENABLED = os.getenv("RAG_FTS_STORED_VECTOR_ENABLED", "0").lower() in (
    "1", "true", "yes", "on",
)

RAG_VECTOR_TOP_K = int(os.getenv("RAG_VECTOR_TOP_K", "8"))
# Dimension des embeddings. ATTENTION : doit correspondre à la dimension figée
# de la colonne pgvector `rag_chunk.embedding` (768, cf. migration 0001). Un
# system check (`rag_core.E001`) échoue au démarrage en cas de divergence.
# Changer cette valeur exige une vraie migration dimensionnelle :
#   1) migration `AlterField(embedding, dimensions=N)` ;
#   2) réembedding complet (manage.py reembed_documents) ;
#   3) reconstruction des index vectoriels (HNSW/ivfflat).
# L'AI Gateway refuse les lots au-delà de 64 textes. L'indexation découpe donc en
# tranches : sans cela, tout document de plus de 64 chunks échoue ENTIÈREMENT.
RAG_EMBED_BATCH_SIZE = int(os.getenv("RAG_EMBED_BATCH_SIZE", "64"))

# --- Diversité du pool de candidats (P1.4) -------------------------------------------
# On récupère un pool ÉLARGI (candidate_k) avant de fusionner, on diversifie, puis on
# tronque au top_k. Un chunk au rang lexical 11 pouvait sinon ne jamais entrer, quand
# bien même plusieurs chunks d'une même feuille occupaient les 8 premières places.
RAG_RETRIEVAL_DIVERSITY_ENABLED = os.getenv("RAG_RETRIEVAL_DIVERSITY_ENABLED", "1") == "1"
RAG_RETRIEVAL_CANDIDATE_MULTIPLIER = int(os.getenv("RAG_RETRIEVAL_CANDIDATE_MULTIPLIER", "3"))
RAG_RETRIEVAL_CANDIDATE_K = int(os.getenv("RAG_RETRIEVAL_CANDIDATE_K", "0"))  # 0 = derive du multiplicateur
RAG_RETRIEVAL_MAX_PER_GROUP = int(os.getenv("RAG_RETRIEVAL_MAX_PER_GROUP", "2"))

# --- Pondération RRF lexicale conditionnelle (P1.5) ----------------------------------
# Pour une requête portant un identifiant fort (« Series A-1 », « 2020-1 »), le signal
# lexical exact est plus fiable que le vectoriel sémantique. On renforce alors la
# contribution lexicale de la fusion, AVANT la troncature candidate_k. Sans identifiant :
# poids (1, 1), fusion historique inchangée. Rollback : ENABLED=0.
RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED = os.getenv(
    "RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED", "1"
) == "1"
RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT = float(
    os.getenv("RAG_RRF_STRONG_IDENTIFIER_LEXICAL_WEIGHT", "1.5")
)

# Références à slash (`FIN01/FIN01-01`) : PostgreSQL les tokenise en UN lexème, que le
# plan de requête historique décompose en `fin01:* & fin01-01` — deux termes qui
# n'existent pas dans le vecteur, donc aucun match possible. Ajoute une requête
# `'fin01/fin01-01'` en tête de plan. Désactivé par défaut tant que non validé sur
# holdout (P2.5-b). Rollback : ENABLED=0.
RAG_FTS_SLASH_IDENTIFIERS_ENABLED = os.getenv("RAG_FTS_SLASH_IDENTIFIERS_ENABLED", "0") == "1"

# Normalisation de requête lexicale V2. Le plan FTS se réduit à UNE disjonction des
# termes porteurs, mots vides ANGLAIS retirés (le pendant français ne l'est pas : c'est
# la variante qualifiée par la campagne, pas une version « améliorée »), tirets et
# apostrophes conservés — `bring-along` et `anti-dilution` sont les termes les plus
# discriminants de leur question.
# Compromis MESURÉ : +8 preuves au contexte sur le benchmark gelé ; médiane FTS ×1,50,
# p95 ×0,74. Désactivé par défaut tant que non qualifié en staging. Rollback : =0.
RAG_FTS_QUERY_NORMALIZATION_V2 = os.getenv("RAG_FTS_QUERY_NORMALIZATION_V2", "0") == "1"

# Borne DURE du repli de plan FTS : nombre maximal de requêtes exécutées pour une même
# question, tous legs confondus (privé et corpus métier partagent la stratégie, cf.
# `rag_core.domain.fts_plan`). Le plan le plus long que sait produire
# `build_tsquery_plan` compte 6 éléments : ce défaut ne tronque donc aucun plan réel.
RAG_FTS_PLAN_MAX_ATTEMPTS = int(os.getenv("RAG_FTS_PLAN_MAX_ATTEMPTS", "6"))

# Fusion bi-source UNIFIÉE : les candidats des corpus métier votent dans la même
# fusion de rangs (RRF) que les candidats privés, au lieu d'être ajoutés APRÈS la
# fusion — donc sans `fusion_score`, donc systématiquement classés derrière eux par la
# diversification, quelle que soit leur pertinence. OFF par défaut : le classement de
# production ne change pas tant que le flag n'est pas posé. Rollback : ENABLED=0.
RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED = (
    os.getenv("RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED", "0") == "1"
)

# Poids des legs CORPUS MÉTIER dans la fusion de rangs unifiée. Les legs privés votent
# toujours à 1.0 ; les legs corpus votent à α ∈ ]0, 1].
#
# Pourquoi ce réglage existe. Scénario de référence (données synthétiques) : à α = 1.0, les deux legs corpus
# CONCORDENT (même corpus dense et thématiquement homogène), leurs chunks cumulent donc
# deux votes en tête, tandis que les chunks privés se dispersent entre leurs propres
# legs. Résultat : éviction quasi totale du privé (rétention 0,00 sur 3 questions
# cibles sur 4). Le levier correct n'est pas un quota de provenance — qui réserverait
# des places indépendamment de la pertinence — mais l'atténuation du VOTE du corpus.
#
# Sans effet tant que `RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED=0`.
# α = 1.0 reproduit exactement le comportement de la fusion unifiée non pondérée.
RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT = float(
    os.getenv("RAG_BUSINESS_CORPUS_RRF_LEG_WEIGHT", "1.0")
)

# Filtrage vectoriel des corpus métier par EMPREINTE D'INDEXATION.
#
# OFF (défaut) : les empreintes sont calculées, comptées et tracées, mais n'excluent
# rien — comportement historique à l'octet près. ON : seuls les documents portant
# l'empreinte attendue (provider, modèle, dimension, version de pipeline) et non
# marqués `reindex_required` sont comparés à la question.
#
# Pourquoi un flag plutôt qu'un basculement atomique : à la sortie de la migration,
# aucun document existant ne porte d'empreinte. Un filtrage immédiat exclurait 100 % des
# documents du corpus de production, tuerait son leg vectoriel et, par ricochet, son
# admission calibrée. Séquence sûre : déployer OFF, prouver les empreintes avec
# `backfill_corpus_embedding_fingerprints`, puis activer.
RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED = (
    os.getenv("RAG_BUSINESS_CORPUS_EMBEDDING_FINGERPRINT_ENFORCEMENT_ENABLED", "0") == "1"
)

# Porte d'ADMISSION calibrée des corpus métier. Mécanisme DISTINCT du poids α :
# l'admission décide si un corpus participe à la fusion, α décide du poids de son vote
# une fois admis. Un corpus rejeté ne vote pas du tout.
#
# Pourquoi. Mesuré sur 70 questions annotées : sans porte, 52 % des passages du top-8
# viennent du corpus métier MÊME sur les questions manifestement hors sujet, et α ne
# corrige pas cela (il réduit la contamination et le service utile dans le même rapport).
# Un seul signal discrimine — la moyenne des dix meilleures similarités du leg vectoriel
# du corpus (ROC-AUC 0.962) — et seulement CALIBRÉ par corpus : l'échelle des similarités
# dépend du modèle d'embeddings, elle n'est pas transposable.
#
# OFF par défaut, partout. Rollback : ENABLED=0.
RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED = (
    os.getenv("RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED", "0") == "1"
)

# Seuil sur la PROBABILITÉ calibrée (∈ [0, 1]), et non sur le score brut du signal.
# Politique d'exploitation globale ; la CALIBRATION, elle, est propre à chaque corpus.
# 0.65 est mesuré dominant : à service utile identique (124 passages corpus retenus sur
# les questions pertinentes, comme à 0.50), la contamination des questions hors sujet
# passe de 5,7 % à 1,4 %.
RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD = float(
    os.getenv("RAG_BUSINESS_CORPUS_ADMISSION_PROBABILITY_THRESHOLD", "0.65")
)

# Lue via le registre : la dimension est FIGÉE à 768 (bornes min=max=768). Une
# valeur divergente est refusée au démarrage plutôt qu'appliquée — le check
# `rag_core.E001` reste en place comme filet vis-à-vis de la colonne pgvector.
RAG_EMBEDDING_DIM = reg("RAG_EMBEDDING_DIM")

# HNSW ANN tuning (pgvector): contrôle le rappel/latence de la recherche vectorielle.
# Défaut pgvector = 40 ; monter (ex. 80) améliore le rappel au prix de la latence.
RAG_HNSW_EF_SEARCH = int(os.getenv("RAG_HNSW_EF_SEARCH", "40"))

# ============================================================
# ===================== RERANK (ÉTAPE 8) =====================
# ============================================================

RAG_RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "0") == "1"

RAG_RERANK_TRIGGER_MIN_CANDIDATES = int(
    os.getenv("RAG_RERANK_TRIGGER_MIN_CANDIDATES", "6")
)

RAG_RERANK_TOP_K = int(os.getenv("RAG_RERANK_TOP_K", "4"))

# --- Budgets ANALYTIQUES (modes explicatif et comparatif) --------------------
#
# Ces réglages étaient lus par l'orchestrateur via `getattr(settings, ..., défaut)` mais
# n'étaient DÉCLARÉS nulle part : les poser dans `.env` n'avait aucun effet, et la
# documentation annonçait des budgets distincts que le runtime n'appliquait pas
# (constat d'évaluation). Ils sont désormais explicites.
#
# Défauts = budgets STANDARDS : la déclaration ne change strictement rien au
# comportement (`max(top_k, top_k)`, `min(seuil, seuil)`). Elle rend seulement le
# runtime lisible et calibrable — tout élargissement doit être un acte délibéré,
# mesuré par benchmark.
RAG_EXPLAIN_TOP_K = int(os.getenv("RAG_EXPLAIN_TOP_K", str(RAG_TOP_K)))
RAG_EXPLAIN_VECTOR_TOP_K = int(os.getenv("RAG_EXPLAIN_VECTOR_TOP_K", str(RAG_VECTOR_TOP_K)))
RAG_EXPLAIN_MIN_TEXT_RANK = float(os.getenv("RAG_EXPLAIN_MIN_TEXT_RANK", str(RAG_MIN_TEXT_RANK)))
RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES = int(
    os.getenv("RAG_EXPLAIN_RERANK_TRIGGER_MIN_CANDIDATES", str(RAG_RERANK_TRIGGER_MIN_CANDIDATES))
)

# `RAG_EXPLAIN_MIN_SCORE` fait exception : il reste OPTIONNEL (None par défaut).
# L'orchestrateur applique `min(seuil_effectif, RAG_EXPLAIN_MIN_SCORE)`, et le seuil
# effectif peut venir du RagSpace. Le figer à la valeur globale PLAFONNERAIT le seuil
# des espaces qui en portent un plus élevé — un changement de comportement, précisément
# ce qu'une déclaration de rendu explicite ne doit pas provoquer.
_explain_min_score = os.getenv("RAG_EXPLAIN_MIN_SCORE", "").strip()

# Surcharges de budget par mode — MÊME défaut fonctionnel qu'avant, désormais
# projetées. Elles étaient lues par `getattr(settings, nom, budget_courant)` sans
# jamais être définies : vingt variables de ce type étaient posées en production
# sans le moindre effet. Sémantique inchangée : `max(budget, surcharge)`, donc
# `0` (le défaut du registre) vaut « aucune surcharge ».
RAG_INVENTORY_TOP_K = reg("RAG_INVENTORY_TOP_K")
RAG_INVENTORY_VECTOR_TOP_K = reg("RAG_INVENTORY_VECTOR_TOP_K")
RAG_INVENTORY_MIN_UNIQUE_DOCS = reg("RAG_INVENTORY_MIN_UNIQUE_DOCS")
RAG_RERANK_POOL_K = reg("RAG_RERANK_POOL_K")
RAG_INVENTORY_RERANK_POOL_K = reg("RAG_INVENTORY_RERANK_POOL_K")
RAG_INVENTORY_RERANK_TAKE = reg("RAG_INVENTORY_RERANK_TAKE")
RAG_EXPLAIN_RERANK_POOL_K = reg("RAG_EXPLAIN_RERANK_POOL_K")
RAG_EXPLAIN_RERANK_TAKE = reg("RAG_EXPLAIN_RERANK_TAKE")

# Réponse à faible confiance — même remarque : lues, jamais définies.
RAG_LOWCONF_ENABLED = reg("RAG_LOWCONF_ENABLED")
RAG_LOWCONF_FACTOR = reg("RAG_LOWCONF_FACTOR")
RAG_LOWCONF_MIN_ABS = reg("RAG_LOWCONF_MIN_ABS")
RAG_LOWCONF_MIN_SOURCES = reg("RAG_LOWCONF_MIN_SOURCES")
RAG_EXPLAIN_MIN_SCORE = float(_explain_min_score) if _explain_min_score else None

# --- Budget de PROMPT (troncature des sources transmises) --------------------
#
# Ces trois réglages étaient lus par `prompt_builder` via `getattr(settings, ..., défaut)`
# sans être déclarés : les poser dans `.env` n'avait aucun effet. Or c'est le budget qui
# TRONQUE réellement le contexte — mesuré sur un corpus à dominante tabulaire, une
# question factuelle recevait 3 sources sur 8, non par manque de candidats mais par
# saturation du budget (68 % des chunks viennent de classeurs et pèsent jusqu'à
# `RAG_SNIPPET_MAX_CHARS_TABLE`).
#
# Défauts INCHANGÉS (18 000 / 1 200 / 4 000) : la déclaration ne modifie rien, elle rend
# le réglage atteignable et mesurable.
#
# ⚠️ `RAG_MAX_PROMPT_CHARS` est couplé à `RAG_FACTUAL_NUM_CTX` : le prompt doit tenir dans
# la fenêtre du modèle, moins les tokens de génération. Le system check `rag_core.W003`
# signale l'incohérence au démarrage.
RAG_MAX_PROMPT_CHARS = int(os.getenv("RAG_MAX_PROMPT_CHARS", "18000"))
RAG_MIN_PROMPT_SOURCES = reg("RAG_MIN_PROMPT_SOURCES")

# Taille minimale utile de l'extrait de la DERNIÈRE source conservée sous pression de
# budget. Le budget de prompt ne peut plus supprimer la dernière source : il la TRONQUE.
# En dessous de ce seuil, l'extrait ne porte plus de phrase complète — la source resterait
# citable mais ne prouverait plus rien.
RAG_MIN_FINAL_SOURCE_CHARS = int(os.getenv("RAG_MIN_FINAL_SOURCE_CHARS", "400"))
RAG_SNIPPET_MAX_CHARS = int(os.getenv("RAG_SNIPPET_MAX_CHARS", "1200"))
RAG_SNIPPET_MAX_CHARS_TABLE = int(os.getenv("RAG_SNIPPET_MAX_CHARS_TABLE", "4000"))

# FENÊTRE de l'extrait, à taille INCHANGÉE : `head` garde les N premiers caractères
# (comportement historique, à l'octet près), `centered` prend les mêmes N caractères
# autour de la phrase la plus proche de la question. Livré à `head` : l'activation est
# un acte de déploiement mesuré, et c'est aussi l'interrupteur de rollback (aucune
# migration, aucune réindexation, aucun embedding recalculé).
RAG_SNIPPET_STRATEGY = reg("RAG_SNIPPET_STRATEGY")
# Extraits des fragments d'extraction STRUCTURÉE (graphiques, tableaux) : budget dédié,
# unités logiques préservées (STRUCTURED-VISUAL-AUTOMATION-AND-CONTEXT-2).
RAG_SNIPPET_MAX_CHARS_STRUCTURED = reg("RAG_SNIPPET_MAX_CHARS_STRUCTURED")

# CONFORMITÉ du rendu du prompt (lot PROMPT-RENDER-CONFORMANCE-FIX-1A) : marqueur de
# troncature tabulaire compté dans le budget, en-tête sans date/page répétées,
# compteurs de coût des règles conditionnelles. Livré OFF : le rendu historique est
# reproduit à l'octet près ; l'activation est un acte de qualification explicite, et
# c'est aussi l'interrupteur de rollback (aucune migration, aucune réindexation).
RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED = reg("RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED")

# Courriels = contenu NON FIABLE dans le prompt. OFF = rendu
# historique à l'octet près (édition Open : aucun connecteur de messagerie).
RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED = reg("RAG_EMAIL_UNTRUSTED_FRAMING_ENABLED")

# --- Bruit STRUCTUREL du contexte (passages interrogatifs / titres) ----------
#
# Dévalorisation CONSERVATRICE des passages structurellement peu affirmatifs. Ne
# classe aucun contenu métier : ni anomalie, ni insuffisance, ni constat. Livré
# DÉSACTIVÉ — même doctrine que les autres mécanismes de classement
# (`RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED`, `RAG_RETRIEVAL_DIVERSITY_ENABLED`) :
# activation staging explicite, puis production après benchmark.
#
# Rollback immédiat, sans réindexation : flag à 0 ou facteur à 1.00.
RAG_STRUCTURAL_NOISE_PENALTY_ENABLED = (
    os.getenv("RAG_STRUCTURAL_NOISE_PENALTY_ENABLED", "0") == "1"
)
RAG_STRUCTURAL_NOISE_PENALTY_FACTOR = float(
    os.getenv("RAG_STRUCTURAL_NOISE_PENALTY_FACTOR", "0.75")
)

RAG_RERANK_PROVIDER = os.getenv("RAG_RERANK_PROVIDER", "stub")

# --- Reranking de passages via l'AI Gateway (lot de gouvernance staging) ------------
# Distinct de RAG_RERANK_* (cross-encoder chargé localement) : ce chemin-ci ne connaît
# que le contrat Gateway et un nom LOGIQUE de modèle. OFF partout par défaut ; activé
# explicitement en staging, jamais en production tant qu'un lot de promotion ne l'a pas
# gouverné.
RAG_RERANKER_ENABLED = os.getenv("RAG_RERANKER_ENABLED", "0") == "1"
RAG_RERANKER_BASE_URL = os.getenv("RAG_RERANKER_BASE_URL", "")
RAG_RERANKER_MODEL_LOGICAL = os.getenv("RAG_RERANKER_MODEL_LOGICAL", "rerank-default")
RAG_RERANKER_POOL_K = int(os.getenv("RAG_RERANKER_POOL_K", "40"))
RAG_RERANKER_TIMEOUT_SECONDS = float(os.getenv("RAG_RERANKER_TIMEOUT_SECONDS", "2.0"))
RAG_RERANKER_FAILURE_POLICY = os.getenv("RAG_RERANKER_FAILURE_POLICY", "fail_open_rrf")
RAG_RERANKER_MAX_PER_GROUP = int(os.getenv("RAG_RERANKER_MAX_PER_GROUP", "3"))

# Jeton facultatif de l'endpoint `/ops/metrics`. Vide par défaut : la protection
# première est le cloisonnement réseau (la route n'est pas proxifiée publiquement).
# Renseigné, il exige `Authorization: Bearer <jeton>` — défense en profondeur.
RAG_OPS_METRICS_TOKEN = os.getenv("RAG_OPS_METRICS_TOKEN", "")

RAG_RERANK_MODEL_NAME = os.getenv(
    "RAG_RERANK_MODEL_NAME",
    "BAAI/bge-reranker-base",
)

RAG_RERANK_DEVICE = os.getenv("RAG_RERANK_DEVICE", "cuda")

RAG_RERANK_BATCH_SIZE = int(os.getenv("RAG_RERANK_BATCH_SIZE", "16"))

RAG_RERANK_MAX_TEXT_CHARS = int(
    os.getenv("RAG_RERANK_MAX_TEXT_CHARS", "2000")
)

# ============================================================
# ============ ANSWERABILITY CROSS-ENCODER (P2.0) ============
# ============================================================
# Décide s'il faut RÉPONDRE — à ne pas confondre avec le reranking, qui ORDONNE. Les deux
# peuvent partager le modèle, mais « le meilleur candidat » n'est pas « une réponse » :
# c'est justement le cas d'une question plausible dont la propriété demandée est absente.
# Ce réglage est donc indépendant de RAG_RERANK_ENABLED.
#
# Statut : MESURÉ, NON PROMU (P2.0). Désactivé par défaut. Le mécanisme sépare très bien
# les no-answer sur données réalistes, mais aucun seuil ne passe le gate sur les deux
# corpus — cf. docs/08-testing/benchmark-rag.md.
# --- Policy d'admission en mode FANTÔME --------------------------------------
# Calcule `balanced-v1` en OBSERVATION et l'inscrit dans la trace de retrieval. Ne change
# RIEN de ce que voit l'utilisateur : avec ce drapeau ON ou OFF la réponse servie est
# identique, et un test compare les deux exécutions champ par champ. Les SEUILS de la
# policy ne sont pas ici — les déplacer exige une nouvelle version dans
# `apps.rag_core.domain.admission_shadow`, pas une variable d'environnement.
RAG_ADMISSION_SHADOW_ENABLED = env_bool("RAG_ADMISSION_SHADOW_ENABLED", False)
RAG_CANDIDATE_TRACE_ENABLED = env_bool("RAG_CANDIDATE_TRACE_ENABLED", False)

RAG_ANSWERABILITY_ENABLED = env_bool("RAG_ANSWERABILITY_ENABLED", False)

RAG_ANSWERABILITY_PROVIDER = os.getenv("RAG_ANSWERABILITY_PROVIDER", "cross_encoder")

# Local uniquement (extra « rerank »). Aucun repli vers une API distante ni vers le LLM.
RAG_ANSWERABILITY_MODEL = os.getenv("RAG_ANSWERABILITY_MODEL", "BAAI/bge-reranker-base")

# `cuda` retombe automatiquement sur CPU si aucun GPU n'est présent (~300 ms/paire en CPU).
RAG_ANSWERABILITY_DEVICE = os.getenv("RAG_ANSWERABILITY_DEVICE", "cpu")

# Score SIGMOÏDE dans [0, 1] (bge-reranker-base : num_labels=1, activation Sigmoid).
# Ce n'est PAS un logit : une grille de seuils en −10..10 n'aurait aucun sens ici.
RAG_ANSWERABILITY_THRESHOLD = float(os.getenv("RAG_ANSWERABILITY_THRESHOLD", "0.5"))

# max | mean_top2 | max_guarded | composite (cf. services/answerability_ce.py).
RAG_ANSWERABILITY_STRATEGY = os.getenv("RAG_ANSWERABILITY_STRATEGY", "max")

# Coût mesuré (CPU, chunks de 2000 car.) : top-5 ≈ 1.6 s, top-10 ≈ 3.8 s (> budget).
RAG_ANSWERABILITY_TOP_K = int(os.getenv("RAG_ANSWERABILITY_TOP_K", "5"))

RAG_ANSWERABILITY_MAX_TEXT_CHARS = int(os.getenv("RAG_ANSWERABILITY_MAX_TEXT_CHARS", "2000"))

RAG_ANSWERABILITY_TIMEOUT_SECONDS = float(os.getenv("RAG_ANSWERABILITY_TIMEOUT_SECONDS", "3"))

# Fail-open par défaut : une panne du modèle ne doit pas se transformer en refus massif.
# L'indisponibilité est TRACÉE (decision="unavailable"), jamais un « answerable » muet.
RAG_ANSWERABILITY_FAIL_OPEN = env_bool("RAG_ANSWERABILITY_FAIL_OPEN", True)

# ============================================================
# ======================= LLM / EMBEDDINGS ===================
# ============================================================


# --- SOURCE DE VÉRITÉ EMBEDDINGS (décision produit 2026-07) ------------------------
# provider = ollama, model = nomic-embed-text, dimension = 768, dans les TROIS
# environnements (dev, staging, production). Le provider de GÉNÉRATION LLM peut être
# différent : cette décision ne concerne QUE les embeddings.
# Aucun repli implicite vers un autre provider : le défaut est explicite et vérifié par les
# system checks `rag_core.E100+` (bloquants hors dev).
EMBEDDINGS_PROVIDER = env_str("EMBEDDINGS_PROVIDER", "ai_gateway")
# "default" = sentinelle « laisser le provider décider » ; pour ollama, le modèle par
# défaut résolu est nomic-embed-text (apps/llm_router/embeddings.py).
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "default")
# Profil de référence attendu (utilisé par les checks et `audit_embeddings_config`).
# `EMBEDDINGS_EXPECTED_MODEL` est DÉCLARÉE configurable par le registre
# (défaut `legacy-768`) et documentée comme telle ; elle était pourtant figée ici, si bien
# qu'un profil Compact `fr-en-retrieval` déclenchait `rag_core.E101` sans issue possible
# autrement qu'en modifiant le code. Elle est désormais projetée depuis l'environnement.
EMBEDDINGS_EXPECTED_PROVIDER = "ai_gateway"
EMBEDDINGS_EXPECTED_MODEL = env_str("EMBEDDINGS_EXPECTED_MODEL", "legacy-768")

# --- Configuration MODÈLES d'un RagSpace imposée aux NON-superusers ------------
# Seul un superuser ACTIF peut choisir provider/modèle d'un RagSpace. Tout autre
# utilisateur reçoit ces valeurs canoniques, imposées PAR LE BACKEND (jamais une
# simple désactivation d'input). Source de vérité unique (consommée par le service
# apps/llm_router/model_policy.py ; ne pas dupliquer ces chaînes ailleurs).
DEFAULT_RAGSPACE_CHAT_PROVIDER = os.environ.get("DEFAULT_RAGSPACE_CHAT_PROVIDER", "ollama")
DEFAULT_RAGSPACE_CHAT_MODEL = os.environ.get("DEFAULT_RAGSPACE_CHAT_MODEL", "compact-generation-8")
DEFAULT_RAGSPACE_EMBEDDING_PROVIDER = os.environ.get("DEFAULT_RAGSPACE_EMBEDDING_PROVIDER", "ai_gateway")
DEFAULT_RAGSPACE_EMBEDDING_MODEL = os.environ.get("DEFAULT_RAGSPACE_EMBEDDING_MODEL", "legacy-768")

APPEND_SLASH = False

# --- Celery / Async ingest ---
# Redis broker: in docker-compose, service is usually "redis"
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "").strip() in ("1", "true", "yes")
CELERY_TASK_EAGER_PROPAGATES = True

# ============================================================
# Cache Django (epoch RagATM_Core runtime config)
# ============================================================
# Si DJANGO_CACHE_URL est défini => cache Redis PARTAGÉ (cohérence de l'epoch
# d'invalidation entre process HTTP et workers Celery). Sinon => LocMemCache
# explicite (dev/test ; suffisant tant que CORE_CONFIG_RUNTIME_ENABLED=0).
# IMPORTANT : en staging/prod avec CORE_CONFIG_RUNTIME_ENABLED=1, DJANGO_CACHE_URL
# DOIT être défini, sinon l'invalidation epoch n'est pas cohérente inter-process.
# Recommandé : redis://redis:6379/2 (DB Redis distincte du broker Celery).
DJANGO_CACHE_URL = reg("DJANGO_CACHE_URL")


#: Budget d'entrées-sorties des compteurs d'exploitation, en secondes.
#: Mesuré : après inactivité, une écriture Redis coûte p95 ~13-45 ms et jusqu'à 740 ms en
#: extrême (contention de `fsync` AOF côté serveur). 0,25 s laisse plus de cinq fois la
#: marge du pire p95 observé tout en écrêtant la queue extrême. Ce n'est PAS le délai du
#: reranking : l'observabilité doit avoir son propre budget, plus court.
OPS_COUNTERS_IO_TIMEOUT_SECONDS = float(os.getenv("OPS_COUNTERS_IO_TIMEOUT_SECONDS", "0.25"))


def _build_caches(cache_url: str) -> dict:
    if cache_url:
        return {
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": cache_url,
                "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            },
            # Alias DÉDIÉ aux compteurs d'exploitation. MÊME service Redis, MÊME base :
            # aucune instance nouvelle, aucune donnée déplacée. Ce qui change est le
            # profil de panne — un délai de socket court, propre à l'observabilité, pour
            # qu'une contention de persistance ne s'ajoute jamais à une requête
            # utilisateur. Le cache `default` reste strictement inchangé.
            "ops": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": cache_url,
                "OPTIONS": {
                    "CLIENT_CLASS": "django_redis.client.DefaultClient",
                    # Fabrique DÉDIÉE : sans elle, `django_redis` indexe ses pools
                    # sur la SEULE URL (`ConnectionFactory._pools`, attribut de
                    # classe), si bien que cet alias et `default` partagent le même
                    # objet pool — et que le délai court ci-dessous s'applique aussi
                    # au contrôle de débit DRF de toutes les requêtes API.
                    # Constaté en production le 22/08/2026 : 3 × HTTP 500.
                    # Voir `config/redis_pools.py`.
                    "CONNECTION_FACTORY": "config.redis_pools.OpsConnectionFactory",
                    "SOCKET_TIMEOUT": OPS_COUNTERS_IO_TIMEOUT_SECONDS,
                    "SOCKET_CONNECT_TIMEOUT": OPS_COUNTERS_IO_TIMEOUT_SECONDS,
                    "IGNORE_EXCEPTIONS": True,
                },
            },
        }
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ragatm-locmem",
        },
        "ops": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ragatm-locmem-ops",
        },
    }


CACHES = _build_caches(DJANGO_CACHE_URL)

# --- Résumé de document (Étape 4) ---
# Documents longs : génération map-reduce en tâche Celery (réponse 202 pending,
# polling côté front). Mettre à "0" pour forcer une génération inline synchrone
# (utile sans worker ; peut être lent sur de très longs documents). En mode
# EAGER (pas de broker), la génération est de toute façon inline.
SUMMARY_ASYNC_ENABLED = os.environ.get("SUMMARY_ASYNC_ENABLED", "1").strip() in ("1", "true", "yes")

# Bascule async fondée sur une ESTIMATION de coût (en plus du map-reduce) : un
# résumé dont l'estimation de tokens générés (nb d'appels LLM × RAG_REPORT_NUM_PREDICT)
# DÉPASSE ce seuil est exécuté en tâche Celery (réponse PENDING + polling), afin de
# ne pas bloquer une requête HTTP sur une génération longue (modèles lents).
# Défaut = RAG_REPORT_NUM_PREDICT (2048) => comportement historique préservé
# (mono-bloc synchrone, map-reduce asynchrone). Abaisser (ex. 1024) pour décharger
# aussi les rapports mono-bloc lourds quand l'espace utilise un modèle lent.
SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS = int(os.getenv("SUMMARY_ASYNC_MAX_SYNC_OUTPUT_TOKENS", "2048"))

# Résilience (Priorité C) : un résumé resté PENDING au-delà de ce délai est
# considéré comme bloqué (tâche perdue) et passé à FAILED par le reaper planifié
# (config/celery.py) / la commande `reap_stale_summaries`.
SUMMARY_PENDING_TIMEOUT_S = int(os.getenv("SUMMARY_PENDING_TIMEOUT_S", "900"))

# Version des prompts de résumé : incrémenter invalide les résumés cachés
# (intégré à l'empreinte). Voir summary_service.SUMMARY_PROMPT_VERSION.
SUMMARY_PROMPT_VERSION = int(os.getenv("SUMMARY_PROMPT_VERSION", "1"))

# Résumé des documents de CORPUS MÉTIER (cf. docs/03-backend/resume-corpus-metier.md).
# ACTIVÉ par défaut : le comportement attendu du produit est que les actions
# « Résumer le document / la page » du panneau de sources fonctionnent aussi sur une
# source de corpus métier. Désactivation explicite par DOMAIN_CORPUS_SUMMARY_ENABLED=0
# (la variable d'environnement reste prioritaire sur ce défaut).
#
# Source de vérité côté BACKEND — le frontend n'a aucune connaissance du flag et
# affiche toujours l'action ; c'est l'API qui refuse (404) quand il est à 0.
DOMAIN_CORPUS_SUMMARY_ENABLED = _env_flag("DOMAIN_CORPUS_SUMMARY_ENABLED", "1")

# Coûts LLM (périmètre C+) : grille tarifaire centralisée, versionnée et
# surchargeable. La source de vérité est apps.rag_core.services.pricing
# (PRICING_VERSION / table par défaut). Pour surcharger sans déploiement, définir
# LLM_PRICING_PER_1K = {"<model>": (prix_entrée_par_1k, prix_sortie_par_1k), ...}.
# Provider local (Ollama) = 0.0 ; modèle non tarifé ou tokens non décomposés = None.
# LLM_PRICING_PER_1K = {}  # ex. d'override (laisser commenté pour utiliser le défaut)

CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = os.environ.get("CELERY_TIMEZONE", "Europe/Paris")

# Optional: stop tasks from piling up forever
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.environ.get("CELERY_WORKER_PREFETCH_MULTIPLIER", "1"))

# Time limits (protègent les workers d'une tâche d'ingestion qui partirait en vrille).
# Soft < hard : la soft lève SoftTimeLimitExceeded (capturée -> Document FAILED),
# la hard tue le worker en dernier recours. Dimensionnées pour extraction (~30s)
# + embeddings d'un gros document. Surchargeables via l'environnement.
CELERY_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "540"))  # 9 min
CELERY_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "600"))  # 10 min

# ============================================================
# ============ ANALYSES GLOBALES DE CORPUS (L2) ==============
# ============================================================
# Queue DÉDIÉE : sans elle, une analyse longue affamerait l'ingestion et le chat
# sur les mêmes 4 slots du worker par défaut, et serait tuée par la limite globale
# de 540 s. Le routage est déclaré ici (défense en profondeur) ET répété à
# l'`apply_async` de la tâche.
# --- Ordonnancement périodique (Celery beat) — SOURCE UNIQUE (Lot 4-B) ------
# Avant ce lot, le schedule vivait dans `config/celery.py` et n'était référencé
# nulle part ailleurs ; les fréquences réelles, la documentation et les composes
# pouvaient diverger sans que rien ne le signale. Il est désormais déclaré ICI,
# lu par `config/celery.py` via `config_from_object`, vérifié par le system check
# `ragatm.E011` (tâches importables, queues connues, fréquences valides).
#
# Les fréquences sont configurables par variable d'environnement : une purge trop
# agressive ou un reaper trop lent doit pouvoir se corriger sans redéploiement de
# code. Les défauts sont ceux qui étaient déjà en vigueur.
CELERY_BEAT_REAP_REINDEX_SECONDS = int(
    os.environ.get("CELERY_BEAT_REAP_REINDEX_SECONDS", "300")          # 5 min
)
# Balayeur Vision V2 : 300 s par défaut ; cf. CELERY_BEAT_SCHEDULE.
CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS = int(
    os.environ.get("CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS", "300")
)
CELERY_BEAT_REAP_SUMMARIES_SECONDS = int(
    os.environ.get("CELERY_BEAT_REAP_SUMMARIES_SECONDS", "600")        # 10 min
)
CELERY_BEAT_AUDIT_PURGE_SECONDS = int(
    os.environ.get("CELERY_BEAT_AUDIT_PURGE_SECONDS", "86400")         # 1 j
)
CELERY_BEAT_HEARTBEAT_SECONDS = int(
    os.environ.get("CELERY_BEAT_HEARTBEAT_SECONDS", "60")              # 1 min
)

# --- Contrôles de sûreté du hard-delete des RagSpaces (P0-2 / P0-3) --------
# Les deux tâches ci-dessous sont READ-ONLY et **désactivées par défaut** : leur
# entrée de schedule existe toujours (elle reste validée par ragatm.E011), mais
# la tâche sort immédiatement tant que son interrupteur vaut 0. L'activation est
# un geste d'exploitation explicite, environnement par environnement — et le
# rollback est le même interrupteur, sans redéploiement.
#
# P0-2 — cohérence base ↔ fichiers. La purge d'un RagSpace supprime les fichiers
# AVANT les lignes : ce compromis n'est défendable que parce que l'anomalie
# résultante est DÉTECTABLE. Une détection qu'on ne lance jamais ne compense rien.
RAG_MEDIA_STORAGE_AUDIT_ENABLED = _env_flag("RAG_MEDIA_STORAGE_AUDIT_ENABLED", "0")
CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS = int(
    os.environ.get("CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS", "86400")  # 1 j
)
# P0-3 — supervision des purges en échec. L'audit `failed` existait déjà ; il
# n'atteignait personne.
RAG_RAGSPACE_DELETE_MONITORING_ENABLED = _env_flag(
    "RAG_RAGSPACE_DELETE_MONITORING_ENABLED", "0"
)
CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS = int(
    os.environ.get("CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS", "3600")  # 1 h
)
# Fenêtre de rattrapage. Le curseur Redis évite les doublons ; cette fenêtre est
# le filet quand il est absent (première exécution, Redis perdu). Large devant la
# période : une interruption d'un jour ne doit pas faire perdre les échecs.
RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS = int(
    os.environ.get("RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS", "86400")  # 1 j
)
# Au-delà de cet âge, le heartbeat est considéré périmé et le healthcheck signale
# Beat comme indisponible. Trois cycles manqués : tolère un redémarrage sans
# déclencher d'alerte, mais détecte un arrêt réel en moins de 4 minutes.
CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS = int(
    os.environ.get("CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS", "180")
)
# TTL des verrous de tâches périodiques. Doit dépasser la durée d'exécution la
# plus longue attendue, sinon deux exécutions se recouvrent.
PERIODIC_TASK_LOCK_TTL_SECONDS = int(
    os.environ.get("PERIODIC_TASK_LOCK_TTL_SECONDS", "900")
)

CELERY_BEAT_SCHEDULE = {
    # Reprise : items de réindexation au lease expiré (worker perdu) →
    # INTERRUPTED, documents INGESTING orphelins → FAILED. N'efface aucun chunk.
    "reap-stale-reindex-items": {
        "task": "apps.rag_core.tasks.reap_stale_reindex_items_task",
        "schedule": CELERY_BEAT_REAP_REINDEX_SECONDS,
        "options": {"queue": "reindex", "expires": CELERY_BEAT_REAP_REINDEX_SECONDS},
    },
    # Audio : relance des chaînes de transcription inactives (worker
    # audio redémarré, message perdu). Inoffensif sans tentative active.
    "audio-transcription-watchdog": {
        "task": "apps.rag_core.tasks.audio_transcription_watchdog_task",
        "schedule": 300,
        "options": {"queue": "maintenance", "expires": 300},
    },
    # Résilience : résumés bloqués en PENDING (tâche perdue).
    "reap-stale-summaries": {
        "task": "apps.rag_core.tasks.reap_stale_summaries_task",
        "schedule": CELERY_BEAT_REAP_SUMMARIES_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_REAP_SUMMARIES_SECONDS},
    },
    # Même filet pour les résumés de CORPUS MÉTIER (table sœur). Planifié
    # inconditionnellement : le reaper est un no-op tant qu'aucune ligne n'existe,
    # et il doit rester actif après une désactivation du flag pour rattraper les
    # tâches enfilées avant celle-ci.
    "reap-stale-domain-corpus-summaries": {
        "task": "apps.corpus.tasks.reap_stale_domain_corpus_summaries_task",
        "schedule": CELERY_BEAT_REAP_SUMMARIES_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_REAP_SUMMARIES_SECONDS},
    },
    # Rétention : la commande existait mais n'était ORDONNANCÉE NULLE PART —
    # la politique de rétention n'était donc pas appliquée (constat Lot 4).
    "purge-expired-audit-logs": {
        "task": "apps.audit.tasks.purge_expired_audit_logs_task",
        "schedule": CELERY_BEAT_AUDIT_PURGE_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_AUDIT_PURGE_SECONDS},
    },
    # Rétention des snapshots diagnostiques. Purge SÉPARÉE de `apps.audit.retention`
    # à dessein : celle-ci calcule un seuil global (`created_at` - N jours), alors
    # qu'un snapshot porte sa propre échéance (`expires_at`) — c'est ce qui rend
    # possible la prolongation, tracée, d'un incident précis sans toucher à la
    # règle globale. Le feedback, lui, n'est JAMAIS purgé par cette tâche.
    "purge-expired-feedback-snapshots": {
        "task": "apps.feedback.tasks.purge_expired_feedback_snapshots_task",
        "schedule": CELERY_BEAT_AUDIT_PURGE_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_AUDIT_PURGE_SECONDS},
    },
    # Battement de cœur : c'est LUI qui prouve que Beat ordonnance réellement.
    # Un processus vivant ne suffit pas — il peut être vivant et ne plus planifier.
    "celery-beat-heartbeat": {
        "task": "apps.rag_core.tasks.celery_beat_heartbeat_task",
        "schedule": CELERY_BEAT_HEARTBEAT_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_HEARTBEAT_SECONDS},
    },
    # Cohérence base ↔ fichiers (P0-2). READ-ONLY : signale `missing_file` /
    # `orphan_file`, ne supprime JAMAIS. Inerte tant que
    # RAG_MEDIA_STORAGE_AUDIT_ENABLED=0 (défaut).
    "audit-media-storage": {
        "task": "apps.rag_core.tasks.audit_media_storage_task",
        "schedule": CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS,
        "options": {
            "queue": "maintenance",
            "expires": CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS,
        },
    },
    # Supervision des purges de RagSpace en échec (P0-3). READ-ONLY : lit
    # l'audit, ne l'écrit pas. Inerte tant que
    # RAG_RAGSPACE_DELETE_MONITORING_ENABLED=0 (défaut).
    "monitor-ragspace-deletions": {
        "task": "apps.rag_core.tasks.monitor_ragspace_deletion_failures_task",
        "schedule": CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS,
        "options": {
            "queue": "maintenance",
            "expires": CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS,
        },
    },
}

# Balayeur Vision V2 : planifié UNIQUEMENT drapeau allumé et hors
# production — la tâche est de toute façon fail-closed (aucune requête sinon).
if VISUAL_V2_ORCHESTRATION_ENABLED and DJANGO_ENV.strip().lower() not in ("prod", "production"):
    CELERY_BEAT_SCHEDULE["visual-v2-sweeper"] = {
        "task": "apps.rag_core.tasks.visual_v2_sweep_task",
        "schedule": CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS,
        "options": {"queue": "maintenance", "expires": CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS},
    }

# `expires` sur chaque entrée : si le worker est indisponible, les messages
# périodiques ne doivent PAS s'empiler pour être tous exécutés d'un coup au
# retour. Une purge quotidienne manquée se rattrape au cycle suivant, elle ne se
# rejoue pas 30 fois.

CELERY_TASK_ROUTES = {
    "apps.rag_core.tasks.run_corpus_analysis_job": {"queue": "analytics"},
    "apps.rag_core.tasks.purge_expired_analysis_jobs_task": {"queue": "analytics"},
    # Réindexation durable : queue dédiée pour ne pas affamer ingestion/chat.
    "apps.rag_core.tasks.run_reindex_job_task": {"queue": "reindex"},
    "apps.rag_core.tasks.reindex_job_item_task": {"queue": "reindex"},
    "apps.rag_core.tasks.reap_stale_reindex_items_task": {"queue": "reindex"},
    # Maintenance : purge, reaper de résumés, heartbeat. Queue DÉDIÉE — une purge
    # de plusieurs dizaines de milliers de lignes ne doit pas occuper un slot du
    # worker interactif pendant que des utilisateurs attendent une réponse.
    "apps.audit.tasks.purge_expired_audit_logs_task": {"queue": "maintenance"},
    "apps.rag_core.tasks.reap_stale_summaries_task": {"queue": "maintenance"},
    "apps.corpus.tasks.reap_stale_domain_corpus_summaries_task": {"queue": "maintenance"},
    "apps.rag_core.tasks.celery_beat_heartbeat_task": {"queue": "maintenance"},
    # Contrôles de sûreté du hard-delete : read-only, mais un parcours complet du
    # volume documentaire n'a rien à faire dans une queue interactive.
    "apps.rag_core.tasks.audit_media_storage_task": {"queue": "maintenance"},
    "apps.rag_core.tasks.monitor_ragspace_deletion_failures_task": {"queue": "maintenance"},
}

# Réindexation durable — lease/reprise et garde EAGER (cf. reindex_jobs.py).
REINDEX_ITEM_LEASE_SECONDS = int(os.environ.get("REINDEX_ITEM_LEASE_SECONDS", "900"))
REINDEX_ITEM_SOFT_TIME_LIMIT = int(os.environ.get("REINDEX_ITEM_SOFT_TIME_LIMIT", "600"))
REINDEX_ITEM_TIME_LIMIT = int(os.environ.get("REINDEX_ITEM_TIME_LIMIT", "660"))
REINDEX_EAGER_MAX_SYNC_DOCUMENTS = int(os.environ.get("REINDEX_EAGER_MAX_SYNC_DOCUMENTS", "25"))
REINDEX_MAX_RETRY_SELECTION = int(os.environ.get("REINDEX_MAX_RETRY_SELECTION", "2000"))

# Limites de pré-vol. Chaque variable est RÉELLEMENT lue depuis l'environnement :
# le dépôt compte déjà ~25 réglages RAG_* lus via getattr() sans jamais être
# définis ici (no-op silencieux, cf. roadmap TODO 28). On ne reproduit pas cela.
CORPUS_ANALYTICS_MAX_DOCUMENTS = int(os.environ.get("CORPUS_ANALYTICS_MAX_DOCUMENTS", "2000"))
CORPUS_ANALYTICS_MAX_CHUNKS = int(os.environ.get("CORPUS_ANALYTICS_MAX_CHUNKS", "100000"))
CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS = int(
    os.environ.get("CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS", "5000000")
)
CORPUS_ANALYTICS_MAX_CONCURRENT_PER_WORKSPACE = int(
    os.environ.get("CORPUS_ANALYTICS_MAX_CONCURRENT_PER_WORKSPACE", "2")
)
CORPUS_ANALYTICS_RESULT_TTL_DAYS = int(os.environ.get("CORPUS_ANALYTICS_RESULT_TTL_DAYS", "30"))

# Limites de temps PROPRES à la tâche d'analyse : une analyse longue ne doit pas
# hériter des 540 s globaux, dimensionnés pour l'ingestion d'un document.
CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get("CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT", "3300")  # 55 min
)
CORPUS_ANALYTICS_TASK_TIME_LIMIT = int(
    os.environ.get("CORPUS_ANALYTICS_TASK_TIME_LIMIT", "3600")  # 60 min
)

# ---- L4 : recherche globale par préfiltrage FTS ----
# Au-delà de ce nombre de correspondances, la recherche est REFUSÉE plutôt que
# tronquée : prendre « les N premiers » présenterait un échantillon comme un
# parcours du corpus.
CORPUS_GLOBAL_SEARCH_MAX_CANDIDATE_CHUNKS = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_CANDIDATE_CHUNKS", "2000")
)
# Plafond RÉEL du coût : c'est le nombre de lots qui détermine le nombre d'appels
# LLM. Mesuré sur le corpus réel (9 478 extraits), un « OU » sur deux mots courants
# produisait 1 482 candidats = 142 lots. Inacceptable. Le planificateur resserre la
# requête (OU -> ET) plutôt que de tronquer, et refuse si rien ne tient.
CORPUS_GLOBAL_SEARCH_MAX_BATCHES = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_BATCHES", "20")
)
CORPUS_GLOBAL_SEARCH_MAX_ESTIMATED_INPUT_TOKENS = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_ESTIMATED_INPUT_TOKENS", "300000")
)
# Retry TRANSITOIRE uniquement (timeout, 429, 502/503/504). Jamais sur un JSON
# invalide, une source inventée, une annulation : rejouer n'y changerait rien.
CORPUS_GLOBAL_SEARCH_MAX_RETRIES = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_RETRIES", "2")
)
CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S = [2, 5]
CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS", "20")
)
CORPUS_GLOBAL_SEARCH_MAX_BATCH_TOKENS = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_BATCH_TOKENS", "6000")
)
CORPUS_GLOBAL_SEARCH_MAX_OUTPUT_ITEMS = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_OUTPUT_ITEMS", "10")
)
CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE = int(
    os.environ.get("CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE", "2")
)
# Fallback externe COUPÉ par défaut : une analyse globale enverrait tout le corpus
# candidat chez un tiers. Ce n'est pas le même contrat de confidentialité qu'une
# question isolée — l'activer exige un consentement explicite (non implémenté).
CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK = (
    os.environ.get("CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK", "0").strip() == "1"
)

# Découpage en fragments. Ces trois réglages étaient lus par le pipeline
# d'ingestion via `getattr(settings, nom, défaut)` sans jamais être projetés : la
# taille de chunk — qui conditionne tout le retrieval en aval — n'était pas
# configurable. Défauts inchangés (2500 / 250).
PREVIEW_CACHE_ENABLED = reg("PREVIEW_CACHE_ENABLED")
RAG_CHUNK_SIZE_CHARS = reg("RAG_CHUNK_SIZE_CHARS")
RAG_CHUNK_OVERLAP_CHARS = reg("RAG_CHUNK_OVERLAP_CHARS")

# Chunking des espaces GOUVERNÉS (provider d'embeddings `ai_gateway`, lot 3). Sans
# effet sur tout autre espace. Borné par la fenêtre du modèle : `fr-en-retrieval` est
# en `on_overflow: reject` à 1 200 caractères, donc un fragment plus long ferait
# échouer son document au lieu d'être tronqué.
RAG_GOVERNED_CHUNK_SIZE_CHARS = reg("RAG_GOVERNED_CHUNK_SIZE_CHARS")
RAG_GOVERNED_CHUNK_OVERLAP_CHARS = reg("RAG_GOVERNED_CHUNK_OVERLAP_CHARS")
RAG_CHUNKING_VERSION = reg("RAG_CHUNKING_VERSION")

# Observabilité : version applicative du battement de cœur, et péremption de la
# dernière sauvegarde. Même défaut structurel, même correction.
RAGATM_VERSION = reg("RAGATM_VERSION")
BACKUP_MAX_AGE_SECONDS = reg("BACKUP_MAX_AGE_SECONDS")

# Feature flag: enable async ingestion on API endpoints
RAG_INGEST_ASYNC_DEFAULT = os.environ.get("RAG_INGEST_ASYNC_DEFAULT", "").strip() in ("1", "true", "yes")

# NOTE : la définition historique en double de RAG_HYBRID_ENABLED vivait ici. Elle est
# supprimée — la source de vérité unique est plus haut (section RAG POLICIES), avec un
# parsing booléen robuste (config/env.py).

# Mode B: if no relevant sources, optionally fall back to a general LLM answer.
RAG_MODE_B_FALLBACK_ENABLED = os.getenv("RAG_MODE_B_FALLBACK_ENABLED", "0") == "1"

# ============================================================
# Audit persistant des actions (apps.audit.ActionAuditEvent) — lot 5
# ============================================================
# Émission ON par défaut ; rollback = ACTION_AUDIT_ENABLED=0 (le modèle/migration
# restent, aucun événement n'est plus écrit). L'audit est fail-open : jamais
# bloquant pour l'action métier.
ACTION_AUDIT_ENABLED = os.getenv("ACTION_AUDIT_ENABLED", "1") == "1"

# --- Rétention des journaux d'audit (jours) — Lot 4-D ---------------------
# La purge est désormais PLANIFIÉE (Celery beat, cf. CELERY_BEAT_SCHEDULE) et non
# plus seulement disponible en commande manuelle. Quatre catégories, quatre
# horizons — voir `apps.audit.retention` pour la doctrine et le plancher imposé
# aux audits de SÉCURITÉ. Toute valeur invalide bloque le démarrage (ragatm.E010).
ACTION_AUDIT_RETENTION_DAYS = int(os.getenv("ACTION_AUDIT_RETENTION_DAYS", "365"))
RAG_QUERY_LOG_RETENTION_DAYS = int(os.getenv("RAG_QUERY_LOG_RETENTION_DAYS", "365"))
# Audits de SÉCURITÉ : plancher de 90 jours (apps.audit.retention).
LLM_EGRESS_AUDIT_RETENTION_DAYS = int(os.getenv("LLM_EGRESS_AUDIT_RETENTION_DAYS", "365"))
# Les ouvertures de politique d'egress sont le geste le plus sensible de la
# plateforme : 3 ans, pour pouvoir répondre à « qui a autorisé quoi, et quand ».
EGRESS_POLICY_CHANGE_RETENTION_DAYS = int(
    os.getenv("EGRESS_POLICY_CHANGE_RETENTION_DAYS", "1095")
)

# Interrupteur d'exploitation : suspendre les purges en incident SANS toucher à
# l'audit lui-même (les événements continuent d'être écrits). Le changement doit
# être tracé par l'opérateur — cf. runbook rétention.
AUDIT_PURGE_ENABLED = os.getenv("AUDIT_PURGE_ENABLED", "1") == "1"
# Bornes d'exécution : une purge non bornée tiendrait une transaction ouverte
# plusieurs minutes sur un an d'arriéré et bloquerait les écritures.
AUDIT_PURGE_BATCH_SIZE = int(os.getenv("AUDIT_PURGE_BATCH_SIZE", "1000"))
AUDIT_PURGE_MAX_ROWS_PER_RUN = int(os.getenv("AUDIT_PURGE_MAX_ROWS_PER_RUN", "50000"))

# Minimisation optionnelle de RagQueryLog (défaut : comportement PRODUCTION inchangé
# = on stocke question + réponse). À 0 : on stocke un hash + longueur au lieu du texte.
RAG_QUERY_LOG_STORE_QUESTION = os.getenv("RAG_QUERY_LOG_STORE_QUESTION", "1") == "1"
RAG_QUERY_LOG_STORE_ANSWER = os.getenv("RAG_QUERY_LOG_STORE_ANSWER", "1") == "1"

# ============================================================
# Feedback utilisateur durable et incidents diagnostiques
# ============================================================
# Doctrine complète : docs/03-backend/feedback-incidents.md
#
# Le signal utilisateur (`AnswerFeedback`) est DURABLE et survit à la suppression
# de l'espace ; le matériel de diagnostic (`FeedbackDiagnosticSnapshot`) est
# SENSIBLE et expire. Les deux se pilotent séparément, et c'est volontaire.

# Sel de pseudonymisation des auteurs de feedback. Repli sur SECRET_KEY si vide
# (jamais d'état « non salé »), mais un sel dédié permet de faire tourner l'un
# sans invalider l'autre.
FEEDBACK_PSEUDONYM_SALT = reg("FEEDBACK_PSEUDONYM_SALT")

# Écriture des colonnes historiques RagQueryLog.feedback_* EN PLUS de la
# nouvelle table. Période de transition : à mettre à 0 une fois les tableaux de
# bord et requêtes SQL existants bascules sur `answer_feedback`.
FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG = env_bool("FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG", True)

# Révision de code déployée (sinon, repli `git rev-parse` en développement).
APP_GIT_SHA = env_str("APP_GIT_SHA", "")

# --- Lot 2 : capture diagnostique -----------------------------------------
# Tampon Redis des candidats de retrieval. OFF par défaut : la capture est une
# collecte supplémentaire, elle doit être un choix explicite, espace par espace.
RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED = reg("RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED")
RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS = reg("RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS")
RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TOP_K = reg("RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TOP_K")
RAG_FEEDBACK_DIAGNOSTIC_SNAPSHOT_RETENTION_DAYS = reg(
    "RAG_FEEDBACK_DIAGNOSTIC_SNAPSHOT_RETENTION_DAYS"
)
RAG_FEEDBACK_DIAGNOSTIC_MAX_SNAPSHOT_BYTES = reg("RAG_FEEDBACK_DIAGNOSTIC_MAX_SNAPSHOT_BYTES")
# Capture du prompt COMPLET dans le snapshot. OFF par défaut : le prompt contient
# le contenu documentaire injecté. Sans lui, `generation_metadata.prompt_capture`
# vaut "hash_only" et le replay `prompt-only` n'est pas proposé.
RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT = reg("RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT")
# Replay tardif quand le tampon a expiré. Il produit un snapshot marqué
# `capture_mode=replay` — JAMAIS présenté comme la capture d'origine.
RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED = reg("RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED")

# --- Lot 3 : export d'incident --------------------------------------------
# Aucun export n'est possible tant que ce drapeau est à 0. Il n'existe AUCUN
# chemin d'export automatique : ce drapeau ouvre une action manuelle, approuvée
# et auditée, jamais un déclenchement par le clic utilisateur.
FEEDBACK_INCIDENT_EXPORT_ENABLED = reg("FEEDBACK_INCIDENT_EXPORT_ENABLED")
FEEDBACK_INCIDENT_EXPORT_TTL_DAYS = reg("FEEDBACK_INCIDENT_EXPORT_TTL_DAYS")
# Clé de chiffrement/signature des paquets (Fernet, urlsafe base64 32 octets).
# Vide = les exports contenant du contenu de production sont REFUSÉS.
FEEDBACK_INCIDENT_EXPORT_KEY = reg("FEEDBACK_INCIDENT_EXPORT_KEY")
FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT = env_str("FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT", "dev")

# --- Lot 4 : import / replay (développement uniquement) --------------------
# Garde-fou d'environnement : les commandes d'import et de replay refusent de
# s'exécuter si ce drapeau n'est pas ouvert. Il ne doit JAMAIS l'être en prod.
FEEDBACK_INCIDENT_IMPORT_ENABLED = reg("FEEDBACK_INCIDENT_IMPORT_ENABLED")

# Routeur d'actions central (apps.actions) — lot 3.
# OFF (défaut) : chemins de chat historiques strictement inchangés.
# ON : ChatView/ChatStreamView délèguent la détection au routeur central, qui
# réutilise les handlers historiques (search.answer/news délégués verbatim à
# chat_query ; listing relocalisé ; summarize -> clarification, exécution différée).
# Aucune action mutante n'est déclenchable par une phrase (natural_language_enabled=False).
ACTIONS_ROUTER_ENABLED = os.getenv("ACTIONS_ROUTER_ENABLED", "0") == "1"

# Classification LLM structurée du routeur (lot 4) — OFF par défaut.
# N'est invoquée QUE sur formulation ambiguë non tranchée par les matchers, jamais
# sur le chemin nominal. Local uniquement (aucun fallback externe, aucun retry).
# Ne peut escalader que vers search.list_documents / document.summarize (news reste
# géré par chat_query ; mutations impossibles). Rollback = mettre 0.
ACTIONS_LLM_CLASSIFIER_ENABLED = os.getenv("ACTIONS_LLM_CLASSIFIER_ENABLED", "0") == "1"
# 5s : le benchmark staging a montré que 3s provoquait des timeouts parasites
# (cold-load Gateway) comptés comme JSON invalide ; 5s laisse une marge (p95 mesuré
# ~0,75s pour qwen3:14b, marge pour cold-load).
ACTIONS_LLM_CLASSIFIER_TIMEOUT_SECONDS = float(os.getenv("ACTIONS_LLM_CLASSIFIER_TIMEOUT_SECONDS", "5"))
ACTIONS_LLM_CLASSIFIER_PROVIDER = os.getenv("ACTIONS_LLM_CLASSIFIER_PROVIDER", "local")
ACTIONS_LLM_CLASSIFIER_MODEL = os.getenv("ACTIONS_LLM_CLASSIFIER_MODEL", "")
# Modèles autorisés (admin) — le modèle ne vient JAMAIS de la requête utilisateur.
# Vide = pas de restriction supplémentaire (le modèle settings fait autorité).
ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS = [
    m.strip() for m in os.getenv("ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS", "").split(",") if m.strip()
]

# ============================================================
# ======================= LLM / GENERATION ===================
# ============================================================

# Defaults globaux si RagSpace n'en définit pas
RAG_LLM_PROVIDER = os.getenv("RAG_LLM_PROVIDER") or os.getenv("DEFAULT_LLM_PROVIDER", "ollama")
RAG_LLM_MODEL = os.getenv("RAG_LLM_MODEL") or os.getenv("OLLAMA_DEFAULT_MODEL", "compact-generation-8")

LLM_TIMEOUT_S = int(os.getenv("LLM_TIMEOUT_S", "120"))

# --- Presets de génération par type de tâche (options Gateway) --------------
# Transmis à l'AI Gateway (temperature/num_ctx/num_predict) par les services
# métier (chat factuel, résumé/rapport, réécriture de requête). N'affectent NI le
# modèle par défaut, NI les embeddings, NI le routage par workspace. Un paramètre
# explicitement fourni par un appelant reste prioritaire sur ces presets.
# RAG factuel (chat)
RAG_FACTUAL_TEMPERATURE = float(os.getenv("RAG_FACTUAL_TEMPERATURE", "0.1"))
# Graine OPTIONNELLE : vide => aucune graine transmise, comportement historique.
# Le défaut ne doit surtout pas être une valeur : une graine posée en dur figerait
# la génération de tout le monde au nom d'une commodité de mesure.
_factual_seed = os.getenv("RAG_FACTUAL_SEED", "").strip()
RAG_FACTUAL_SEED = int(_factual_seed) if _factual_seed else None
RAG_FACTUAL_NUM_CTX = int(os.getenv("RAG_FACTUAL_NUM_CTX", "8192"))
RAG_FACTUAL_NUM_PREDICT = int(os.getenv("RAG_FACTUAL_NUM_PREDICT", "2048"))
# Synthèse / rapport (résumé)
RAG_REPORT_TEMPERATURE = float(os.getenv("RAG_REPORT_TEMPERATURE", "0.1"))
RAG_REPORT_NUM_CTX = int(os.getenv("RAG_REPORT_NUM_CTX", "8192"))
RAG_REPORT_NUM_PREDICT = int(os.getenv("RAG_REPORT_NUM_PREDICT", "2048"))
# Réécriture de requête
RAG_REWRITE_TEMPERATURE = float(os.getenv("RAG_REWRITE_TEMPERATURE", "0.0"))
RAG_REWRITE_NUM_CTX = int(os.getenv("RAG_REWRITE_NUM_CTX", "2048"))
RAG_REWRITE_NUM_PREDICT = int(os.getenv("RAG_REWRITE_NUM_PREDICT", "128"))

# Suggestions de reformulation (cas no_answer uniquement) -------------------
# Quand une requête documentaire aboutit à une VRAIE non-réponse, on propose 2-3
# reformulations concrètes pour aider l'utilisateur au prochain essai. Génération
# LLM bornée (temp basse, budget de tokens court, timeout dédié, sans retry) via
# le provider/modèle de l'espace, avec fallback déterministe sûr. N'affecte NI le
# modèle par défaut, NI le provider, NI les embeddings, NI le retrieval. Aucun
# repli cloud implicite (allow_external_fallback=False dans le service).
#
# DÉSACTIVÉ PAR DÉFAUT (opt-in) : flag absent / "0" / "false" => aucune génération,
# non-réponse historique immédiate. La génération, quand activée, est NON BLOQUANTE
# (sync => [] immédiat ; stream => événement SSE tardif "rephrasing_suggestions").
RAG_REPHRASING_SUGGESTIONS_ENABLED = os.getenv(
    "RAG_REPHRASING_SUGGESTIONS_ENABLED", "0"
).strip().lower() in ("1", "true", "yes", "on")
RAG_REPHRASING_MAX = int(os.getenv("RAG_REPHRASING_MAX", "3"))
RAG_REPHRASING_TEMPERATURE = float(os.getenv("RAG_REPHRASING_TEMPERATURE", "0.1"))
RAG_REPHRASING_NUM_CTX = int(os.getenv("RAG_REPHRASING_NUM_CTX", "2048"))
RAG_REPHRASING_NUM_PREDICT = int(os.getenv("RAG_REPHRASING_NUM_PREDICT", "256"))
# Timeout dédié COURT (secondes) réellement appliqué jusqu'au client Gateway :
# une lenteur/panne de génération ne prolonge jamais le flux au-delà de ce budget.
RAG_REPHRASING_TIMEOUT_S = float(os.getenv("RAG_REPHRASING_TIMEOUT_S", "3"))

# --- Routage OPT-IN du modèle par type de tâche (EXPÉRIMENTAL) ---------------
# Désactivé par défaut => aucun changement (le modèle du workspace est utilisé).
# Quand activé, override UNIQUEMENT le nom de modèle (jamais le provider) et
# UNIQUEMENT pour les espaces locaux (provider=ollama, servis par la Gateway) ;
# un override vide laisse le modèle du workspace inchangé. Cf. apps.llm_router.task_routing.
RAG_TASK_ROUTING_ENABLED = os.getenv("RAG_TASK_ROUTING_ENABLED", "0").strip() in ("1", "true", "yes")
RAG_TASK_MODEL_FACTUAL = os.getenv("RAG_TASK_MODEL_FACTUAL", "").strip()
RAG_TASK_MODEL_REPORT = os.getenv("RAG_TASK_MODEL_REPORT", "").strip()
RAG_TASK_MODEL_REWRITE = os.getenv("RAG_TASK_MODEL_REWRITE", "").strip()

# ============================================================
# AI Gateway (PR 5a — client interne INERTE)
# ============================================================
# Client Python interne vers la passerelle IA du serveur IA. En PR 5a le client
# existe mais n'est branché nulle part : `generate_text` reste inchangé. Désactivé
# par défaut => AUCUN appel réseau. La passerelle elle-même (PR 5b) et le routage de
# la génération via flag (PR 5c) viendront ensuite.
# NB : distinct de l'historique AI_GATEWAY_URL (forward-looking, docker-compose).
#
# Ce bloc est lu par le REGISTRE canonique (`config.registry`) : types, défauts et
# bornes y sont déclarés une seule fois, et l'alias déprécié AI_GATEWAY_URL est
# résolu par le pont `config.settings.from_registry` (warning, jamais silencieux ;
# conflit de valeurs => refus de démarrer).
AI_GATEWAY_ENABLED = reg("AI_GATEWAY_ENABLED")
# Nom canonique : AI_GATEWAY_BASE_URL (le client construit {base}/v1/generate).
# Compat ascendante avec l'historique AI_GATEWAY_URL ; à terme ne garder que
# AI_GATEWAY_BASE_URL dans la doc et les .env.
AI_GATEWAY_BASE_URL = reg("AI_GATEWAY_BASE_URL")
AI_GATEWAY_TIMEOUT_SECONDS = reg("AI_GATEWAY_TIMEOUT_SECONDS")
# Vision V1 (`/v1/vision/analyze`) : profil LOGIQUE (vide = défaut
# Gateway) et timeout de lecture provisoire. Client présent sans appelant de production.
AI_GATEWAY_VISION_MODEL = reg("AI_GATEWAY_VISION_MODEL")
AI_GATEWAY_VISION_TIMEOUT_SECONDS = reg("AI_GATEWAY_VISION_TIMEOUT_SECONDS")
AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS = reg("AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS")
STRUCTURED_VISUAL_TIMEOUT_SECONDS = reg("STRUCTURED_VISUAL_TIMEOUT_SECONDS")
STRUCTURED_VISUAL_SCHEMA_VERSION = reg("STRUCTURED_VISUAL_SCHEMA_VERSION")
# Streaming natif de la Gateway pour le chat interactif (`/v1/generate/stream`).
# OFF par défaut : c'est le levier de repli d'exploitation. À 0, `chat_query_stream`
# garde le pseudo-stream (génération non-streaming puis un seul événement SSE).
AI_GATEWAY_STREAMING_ENABLED = reg("AI_GATEWAY_STREAMING_ENABLED")

# Provider/modèle PRIMAIRES envoyés à l'AI Gateway (chemin nominal).
# DÉCOUPLÉS de RAG_LLM_PROVIDER (chemin direct de développement) : la Gateway route vers
# le modèle local du serveur IA (nom LOGIQUE du registre Compact, ex. compact-generation-8).
# Édition Open : il n'existe
# aucun fournisseur de repli.
AI_GATEWAY_PROVIDER = (reg("AI_GATEWAY_PROVIDER") or "ollama").lower()
AI_GATEWAY_MODEL = reg("AI_GATEWAY_MODEL") or "compact-generation-8"
# Si la génération via Gateway échoue (timeout, injoignable...), on retombe sur le
# provider de fallback EXPLICITE (cf. AI_GATEWAY_FALLBACK_PROVIDER). Passer à 0 pour
# un mode strict (l'erreur Gateway typée remonte au lieu du fallback).
AI_GATEWAY_FALLBACK_ENABLED = reg("AI_GATEWAY_FALLBACK_ENABLED")
# Sondes Gateway de la console d'administration : fermées par défaut (INTEGRATION-3R).
CAPABILITIES_GATEWAY_PROBES_ENABLED = reg("CAPABILITIES_GATEWAY_PROBES_ENABLED")

# Provider de repli quand l'AI Gateway est indisponible. Édition Open : seule valeur
# autorisée "none" (erreur contrôlée, aucun repli). "ollama" est INTERDIT — policy Configuration IT : RagATM ne doit
# JAMAIS appeler Ollama en direct. Le system check llm_router.E004 refuse toute
# autre valeur ; le router ne retombe jamais sur Ollama direct.
AI_GATEWAY_FALLBACK_PROVIDER = (reg("AI_GATEWAY_FALLBACK_PROVIDER") or "none").lower()

# URL cible canonique de l'AI Gateway (réseau privé). Les system checks
# refusent toute URL loopback (127.0.0.1 / localhost / 0.0.0.0) en env officiel.
AI_GATEWAY_REQUIRED_BASE_URL = reg("AI_GATEWAY_REQUIRED_BASE_URL")
# Hôtes reconnus comme AI Gateway Pool (route `pool`) ; vide = accès direct à une Gateway.
AI_GATEWAY_POOL_HOSTS = reg("AI_GATEWAY_POOL_HOSTS")

# --- Embeddings GOUVERNÉS (lot 3) -------------------------------------------
#
# Service DISTINCT de la génération, et c'est délibéré : le pilote Granite vise la
# Gateway de STAGING (:8200) pendant que la génération reste sur l'instance de son
# environnement. Réutiliser `AI_GATEWAY_BASE_URL` aurait lié les deux — impossible de
# piloter les embeddings sans déplacer aussi la génération.
#
# À 0 (défaut), le provider `ai_gateway` est REFUSÉ : un RagSpace qui le porte échoue
# explicitement plutôt que de retomber en silence sur Ollama, ce qui écrirait des
# vecteurs d'un autre pipeline sous une identité qui prétendrait le contraire.
# C'est aussi l'interrupteur de rollback du lot : le repasser à 0 coupe le chemin
# gouverné sans migration ni réindexation.
EXTERNAL_EMBEDDINGS_EGRESS_ENABLED = reg("EXTERNAL_EMBEDDINGS_EGRESS_ENABLED")
AI_GATEWAY_EMBEDDINGS_ENABLED = reg("AI_GATEWAY_EMBEDDINGS_ENABLED")
AI_GATEWAY_EMBEDDINGS_BASE_URL = reg("AI_GATEWAY_EMBEDDINGS_BASE_URL")
AI_GATEWAY_EMBEDDINGS_MODEL = reg("AI_GATEWAY_EMBEDDINGS_MODEL")
AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS = reg("AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS")
AI_GATEWAY_BUSY_THRESHOLD_MS = reg("AI_GATEWAY_BUSY_THRESHOLD_MS")

# Échappatoire RÉSERVÉE aux tests unitaires / mode dégradé explicite : autorise
# AI_GATEWAY_ENABLED=0 ou RAG_LLM_PROVIDER=ollama dans un env officiel sans faire
# échouer les system checks (ils émettent alors un Warning au lieu d'un Error).
# NE JAMAIS activer en déploiement réel (le poste de développement/le serveur applicatif).
ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS = reg("ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS")

# PR-F : édition des overrides de config Core (admin). Désactivée par défaut et
# JAMAIS active en prod (gate serveur). N'affecte pas le runtime (overrides
# diagnostic-only). À activer uniquement en staging pour des tests contrôlés.
CORE_CONFIG_EDIT_ENABLED = reg("CORE_CONFIG_EDIT_ENABLED")

# PR-G1 : branchement runtime de l'overlay de config Core. Désactivé par défaut =>
# le runtime lit STRICTEMENT les settings (overrides diagnostic-only). Quand activé,
# le résolveur runtime (apps.rag_core.core.runtime_config) lit les overrides DB avec
# cache/epoch. En PR-G1 ce résolveur n'est branché NULLE PART (inerte).
CORE_CONFIG_RUNTIME_ENABLED = reg("CORE_CONFIG_RUNTIME_ENABLED")

# Ollama defaults
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
OLLAMA_TOP_P = float(os.getenv("OLLAMA_TOP_P", "0.9"))
OLLAMA_REPEAT_PENALTY = float(os.getenv("OLLAMA_REPEAT_PENALTY", "1.1"))

#chat memory
CHAT_MEMORY_ENABLED = reg("CHAT_MEMORY_ENABLED")
CHAT_MEMORY_MAX_MESSAGES = int(os.getenv("CHAT_MEMORY_MAX_MESSAGES", "12"))
CHAT_MEMORY_MAX_CHARS = int(os.getenv("CHAT_MEMORY_MAX_CHARS", "3000"))

# Mémoire conversation (niveau 2)
# Résumé glissant. Les défauts viennent du registre et reprennent les valeurs
# RÉELLEMENT appliquées jusqu'ici (18 messages, 6000 caractères d'entrée) : le code
# lisait `CHAT_MEMORY_MIN_MESSAGES_BEFORE_SUMMARY` et `CHAT_MEMORY_SUMMARIZE_CHUNK_
# MAX_CHARS`, deux noms que rien ne projetait. Les noms déclarés ici (24 / 12000)
# n'ont donc jamais été appliqués — les aligner sur le comportement observé évite de
# changer la production en corrigeant la nomenclature.
CHAT_MEMORY_SUMMARY_ENABLED = reg("CHAT_MEMORY_SUMMARY_ENABLED")
CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES = reg("CHAT_MEMORY_SUMMARY_TRIGGER_MESSAGES")
CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS = reg("CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS")
CHAT_MEMORY_SUMMARY_MAX_CHARS = reg("CHAT_MEMORY_SUMMARY_MAX_CHARS")
CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S = reg("CHAT_MEMORY_SUMMARY_LLM_TIMEOUT_S")

# Résumé de DOCUMENT (distinct du résumé de conversation ci-dessus). Ces deux-là
# étaient lus par `getattr(settings, nom, défaut)` sans être projetés : les poser
# dans un `.env` n'avait aucun effet. Même défaut, désormais effectif.
SUMMARY_MAX_CONTEXT_CHARS = reg("SUMMARY_MAX_CONTEXT_CHARS")
SUMMARY_MAX_CHARS_PER_CHUNK = reg("SUMMARY_MAX_CHARS_PER_CHUNK")

# =============================================================================
# MOTEUR TABULAIRE DÉTERMINISTE (lot 1) — Excel / CSV
# =============================================================================
# Le moteur calcule lui-même : le LLM ne produit ni SQL, ni Python, ni le résultat.
# Cf. docs/03-backend/tabular-query-architecture.md
#
# Défaut : TOUT est à 0. Tant que `TABULAR_QUERY_ENABLED=0`, le comportement
# historique est strictement conservé — la garde `excel_guard` refuse les questions
# analytiques sur classeur. L'activation est un acte explicite, jamais un effet de bord
# du déploiement.
TABULAR_QUERY_ENABLED = env_bool("TABULAR_QUERY_ENABLED", False)
# Planner LLM : hors périmètre du lot 1. À n'activer QUE lorsque le moteur
# déterministe est validé en staging — et jamais en même temps que lui.
TABULAR_LLM_PLANNER_ENABLED = env_bool("TABULAR_LLM_PLANNER_ENABLED", False)
# Narration LLM du résultat déjà calculé. Ne peut modifier aucune valeur.
TABULAR_NARRATION_ENABLED = env_bool("TABULAR_NARRATION_ENABLED", False)

# --- Limites de ressources — toute limite atteinte produit un CODE explicite,
#     jamais une troncature silencieuse.
TABULAR_MAX_ROWS_SCANNED = int(os.getenv("TABULAR_MAX_ROWS_SCANNED", "200000"))
TABULAR_MAX_COLUMNS = int(os.getenv("TABULAR_MAX_COLUMNS", "256"))
TABULAR_MAX_FILTERS = int(os.getenv("TABULAR_MAX_FILTERS", "16"))
TABULAR_MAX_RESULT_ROWS = int(os.getenv("TABULAR_MAX_RESULT_ROWS", "1000"))
TABULAR_MAX_GROUPS = int(os.getenv("TABULAR_MAX_GROUPS", "1000"))
TABULAR_MAX_QUERY_SECONDS = float(os.getenv("TABULAR_MAX_QUERY_SECONDS", "10"))
TABULAR_MAX_MEMORY_MB = int(os.getenv("TABULAR_MAX_MEMORY_MB", "512"))
TABULAR_MAX_CANDIDATE_TABLES = int(os.getenv("TABULAR_MAX_CANDIDATE_TABLES", "50"))
TABULAR_MAX_CANDIDATE_COLUMNS = int(os.getenv("TABULAR_MAX_CANDIDATE_COLUMNS", "512"))

# --- Résolution de colonnes
# Score minimal pour qu'un candidat soit retenu, et écart minimal avec le second pour
# que le premier soit jugé DOMINANT. Sous cet écart -> clarification, jamais un choix
# arbitraire par position.
# --- Capacité concurrente. Le sémaphore est PAR PROCESSUS : la capacité réelle d'un
# déploiement vaut GUNICORN_WORKERS × cette valeur (défaut 2 × 2 = 4). Cf.
# apps/rag_core/tabular/capacity.py et docs/07-operations/tabular-query-rollout.md.
# Valeur fondée sur la mesure : 2 workers donnent 1,73× le débit séquentiel pour
# 211 Mo, contre 289 Mo à 10 workers pour un gain marginal.
TABULAR_MAX_CONCURRENT_QUERIES = int(os.getenv("TABULAR_MAX_CONCURRENT_QUERIES", "2"))

# Marge mémoire exigée au-dessus du besoin ESTIMÉ de la capacité tabulaire. Le besoin
# est une estimation prudente (cf. deployment.ESTIMATED_MB_PER_*), jamais un plafond
# exact : elle doit se tromper du côté sûr.
TABULAR_MIN_MEMORY_HEADROOM_MB = int(os.getenv("TABULAR_MIN_MEMORY_HEADROOM_MB", "512"))

# --- Préflight volumétrique : refuser AVANT de lire.
# Bornes de TAILLE, complémentaires des bornes de LIGNES. Un fichier peut respecter la
# limite de lignes et rester pathologique (cellules énormes, colonnes innombrables) ;
# inversement, la taille se lit sans rien décompresser, donc son contrôle est gratuit.
TABULAR_MAX_INPUT_FILE_MB = float(os.getenv("TABULAR_MAX_INPUT_FILE_MB", "200"))
TABULAR_MAX_UNCOMPRESSED_MB = float(os.getenv("TABULAR_MAX_UNCOMPRESSED_MB", "500"))

TABULAR_COLUMN_MATCH_MIN_SCORE = float(os.getenv("TABULAR_COLUMN_MATCH_MIN_SCORE", "0.60"))
TABULAR_COLUMN_MATCH_MIN_MARGIN = float(os.getenv("TABULAR_COLUMN_MATCH_MIN_MARGIN", "0.15"))

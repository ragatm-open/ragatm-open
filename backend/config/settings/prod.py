# backend/config/settings/prod.py
from .base import *
import os

DEBUG = False

# ============================================================
# Environment guardrails
# ============================================================
# `config.settings.prod` est le module durci partagé par la prod ET le staging
# (cf. docs/ENVIRONMENTS.md §1 : staging réutilise les settings prod pour valider
# la config durcie avant la vraie prod). On accepte donc DJANGO_ENV ∈ {prod, staging}.
# Toute autre valeur (dev, base, vide) reste refusée : pas de démarrage durci accidentel.
DJANGO_ENV = os.getenv("DJANGO_ENV", "prod").strip().lower()
if DJANGO_ENV not in ("prod", "staging"):
    raise RuntimeError(
        "DJANGO_ENV must be 'prod' or 'staging' when using config.settings.prod "
        f"(got {DJANGO_ENV!r})"
    )
# Exposé pour le reste de la config / les vues (bandeau staging, logs, etc.).
IS_STAGING = DJANGO_ENV == "staging"

# En prod on refuse toute clé faible / fallback
if SECRET_KEY == "dev-secret-key":
    raise RuntimeError("DJANGO_SECRET_KEY must be set (no fallback) in prod")

# Défense en profondeur : ne JAMAIS exposer le reset_link / email_error d'invitation
# en prod, quelle que soit la variable d'environnement. Le token de reset doit
# transiter uniquement par email.
INVITE_RETURN_RESET_LINK = False

# Ne pas accepter ALLOWED_HOSTS wildcard en prod
_raw_hosts = os.getenv("DJANGO_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _raw_hosts.split(",") if h.strip()]
if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in prod (comma-separated)")

# En prod on refuse le provider d'embeddings "stub" (vecteurs sémantiquement vides):
# la recherche vectorielle serait quasi-aléatoire et le RAG dégraderait silencieusement
# en FTS seul. Exiger un vrai provider.
# On relit l'environnement (et non `globals()`) pour rester correct sous
# `importlib.reload(prod)` : `from .base import *` ne ré-exécute pas base.py, donc
# globals() porterait une valeur périmée. Le défaut est aligné sur la source de vérité
# (`ollama`) — plus de repli implicite vers "stub".
_emb_provider = os.getenv("EMBEDDINGS_PROVIDER", "ai_gateway").strip().lower()
if _emb_provider != "ai_gateway":
    raise RuntimeError(
        "EMBEDDINGS_PROVIDER must be 'ai_gateway' in prod (RAGatm Open: governed AI Gateway "
        f"pipeline only, got '{_emb_provider or 'stub'}')."
    )


MEDIA_URL = os.getenv("MEDIA_URL", "/media/")
# Défaut = la valeur calculée par base.py (BASE_DIR/backend/media, soit
# /app/backend/media dans l'image), et NON un chemin absolu codé en dur.
# Historique : le défaut était "/backend/media", qui ne correspond à AUCUN point de
# montage (le volume `media` est monté sur /app/backend/media). Un environnement
# oubliant MEDIA_ROOT écrivait donc les documents dans la couche du conteneur :
# perdus au redéploiement, et invisibles pour les workers (autre conteneur).
# Cf. docs/01-overview/decisions/ADR-001-stockage-documentaire.md.
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(MEDIA_ROOT)))  # noqa: F405 (MEDIA_ROOT vient de base)

# ============================================================
# HTTPS / Reverse-proxy hardening
# (Assume TLS termination at proxy that sets X-Forwarded-Proto)
# ============================================================
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "0") in ("1", "true", "True")
USE_X_FORWARDED_HOST = os.getenv("DJANGO_USE_X_FORWARDED_HOST", "0") in ("1", "true", "True")


# ============================================================
# Cookies strict
# ============================================================
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Strictest option (can break certain cross-site flows)
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"

# Django also provides this protection:
CSRF_COOKIE_HTTPONLY = True  # stricter; prevents JS reading CSRF cookie

# If you don't use Django sessions at all (token auth), you can additionally:
SESSION_COOKIE_HTTPONLY = True

# ============================================================
# HSTS strict
# WARNING: only enable if you're sure HTTPS is correctly configured
# ============================================================
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# ============================================================
# Clickjacking / MIME sniffing hardening
# ============================================================
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"

# Strong default referrer policy
SECURE_REFERRER_POLICY = "same-origin"

# ============================================================
# CSRF trusted origins (must be set if you serve the frontend on HTTPS domain)
# Example: https://ragatm.example.com
# ============================================================
_raw_csrf = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _raw_csrf.split(",") if o.strip()]

# ============================================================
# Optional: DRF throttling (anti-bruteforce) — recommended in enterprise mode
# ============================================================
# Débits nommés : on PART de ceux de `base.py` et on les complète, au lieu de
# les remplacer.
#
# Le remplacement en bloc était un piège : tout scope déclaré dans `base.py` mais
# oublié dans cette liste disparaissait en staging ET en production, et DRF lève
# alors `ImproperlyConfigured: No default throttle rate set for '<scope>'` — donc
# un **HTTP 500 sur chaque appel** de l'endpoint concerné. Le défaut est invisible
# en développement, qui n'utilise que `base.py`.
#
# Constaté en qualification staging le 2026-07-28 : `feedback_create`, déclaré
# dans `base.py`, était absent ici — tout clic sur le pouce aurait renvoyé 500 en
# production. Fusionner supprime la classe de bug, pas seulement son instance.
_THROTTLE_RATES = dict(REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES") or {})
_THROTTLE_RATES.update(
    {
        "anon": os.getenv("DRF_THROTTLE_ANON", "30/min"),
        "user": os.getenv("DRF_THROTTLE_USER", "120/min"),
        # Débits dédiés des endpoints d'invitation (anti-abus).
        "invitation_create": os.getenv("DRF_THROTTLE_INVITATION_CREATE", "20/min"),
        "invitation_regenerate": os.getenv("DRF_THROTTLE_INVITATION_REGENERATE", "20/min"),
        "invitation_accept": os.getenv("DRF_THROTTLE_INVITATION_ACCEPT", "10/min"),
    }
)

REST_FRAMEWORK.update(
    {
        "DEFAULT_THROTTLE_CLASSES": [
            "rest_framework.throttling.UserRateThrottle",
            "rest_framework.throttling.AnonRateThrottle",
        ],
        "DEFAULT_THROTTLE_RATES": _THROTTLE_RATES,
    }
)

# ============================================================
# Frontend origin (OBLIGATOIRE en prod)
# ============================================================
# Plus de valeur en dur : lue depuis l'environnement. Meme garde-fou que
# DJANGO_SECRET_KEY / DJANGO_ALLOWED_HOSTS / EMBEDDINGS_PROVIDER : on refuse
# de demarrer si elle est absente, vide ou non absolue (liens email casses sinon).
FRONTEND_APP_ORIGIN = os.getenv("FRONTEND_APP_ORIGIN", "").strip().rstrip("/")
if not FRONTEND_APP_ORIGIN or not FRONTEND_APP_ORIGIN.startswith(("http://", "https://")):
    raise RuntimeError(
        "FRONTEND_APP_ORIGIN must be set to an absolute URL in prod "
        f"(got '{FRONTEND_APP_ORIGIN or ''}'). "
        "Ex: FRONTEND_APP_ORIGIN=https://ragatm.example.org"
    )

# ============================================================
# Cookie d'authentification (durcissement prod)
# ============================================================
# HttpOnly+Secure obligatoires ; SameSite=Lax (et non Strict) volontairement :
# l'app est same-origin via le proxy Next, Lax suffit et evite de casser la 1re
# navigation depuis un lien email (invitation / reset password).
AUTH_COOKIE_SECURE = True
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "Lax")

# Strategie CSRF = double-submit cookie + header X-CSRFToken. Le JS doit pouvoir
# LIRE le cookie csrftoken pour le renvoyer en header : on relache donc
# explicitement CSRF_COOKIE_HTTPONLY (mis a True dans le durcissement plus haut).
# Le token d'auth, lui, reste HttpOnly et inaccessible au JS.
CSRF_COOKIE_HTTPONLY = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django.request": {  # tracebacks sur 500
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        # si tu veux aussi les erreurs de ton app
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
    },
}

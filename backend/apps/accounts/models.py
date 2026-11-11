from __future__ import annotations

from django.conf import settings
from django.db import models

# Locales supportées par l'interface. SOURCE DE VÉRITÉ backend, alignée sur la
# constante frontend `SUPPORTED_LOCALES` (frontend/src/i18n/config.ts). Le
# français reste la langue par défaut.
LOCALE_CHOICES = [
    ("fr", "Français"),
    ("en", "English"),
]
DEFAULT_LOCALE = "fr"
SUPPORTED_LOCALES = [code for code, _ in LOCALE_CHOICES]

# Préférence de LANGUE DE RÉPONSE IA — DISTINCTE de la locale d'interface.
# `auto` = langue de la question (cf. apps/rag_core/services/answer_language.py).
ANSWER_LANGUAGE_CHOICES = [
    ("auto", "Automatique"),
    ("fr", "Français"),
    ("en", "English"),
]
DEFAULT_ANSWER_LANGUAGE = "auto"


class Profile(models.Model):
    """Préférences utilisateur non sensibles adossées au compte Django par défaut.

    Le projet utilise `django.contrib.auth.User` (pas de modèle utilisateur
    personnalisé) : on porte donc la préférence de langue dans un profil 1-1
    plutôt que d'altérer le modèle utilisateur. Le profil est créé paresseusement
    (cf. `get_or_create_profile`) et rétro-rempli pour les comptes existants par
    la migration de données.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    preferred_locale = models.CharField(
        max_length=5,
        choices=LOCALE_CHOICES,
        default=DEFAULT_LOCALE,
        help_text="Langue de l'interface (fr | en). N'affecte ni la langue des documents ni celle des réponses IA.",
    )
    preferred_answer_language = models.CharField(
        max_length=5,
        choices=ANSWER_LANGUAGE_CHOICES,
        default=DEFAULT_ANSWER_LANGUAGE,
        help_text="Langue des réponses IA (auto | fr | en). DISTINCTE de la locale d'interface ; auto = langue de la question.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "accounts"

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"Profile<{self.user_id}:{self.preferred_locale}>"


def get_or_create_profile(user) -> Profile:
    """Retourne le profil de l'utilisateur, le créant au besoin (défaut fr)."""
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def get_preferred_locale(user) -> str:
    """Locale préférée du compte, ou `DEFAULT_LOCALE` (fail-safe, sans écriture)."""
    if not getattr(user, "is_authenticated", False):
        return DEFAULT_LOCALE
    profile = Profile.objects.filter(user=user).only("preferred_locale").first()
    locale = profile.preferred_locale if profile else DEFAULT_LOCALE
    return locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE


_ANSWER_LANGUAGE_VALUES = [code for code, _ in ANSWER_LANGUAGE_CHOICES]


def get_preferred_answer_language(user) -> str:
    """Préférence de langue de réponse (`auto|fr|en`), ou `auto` (fail-safe)."""
    if not getattr(user, "is_authenticated", False):
        return DEFAULT_ANSWER_LANGUAGE
    profile = Profile.objects.filter(user=user).only("preferred_answer_language").first()
    val = profile.preferred_answer_language if profile else DEFAULT_ANSWER_LANGUAGE
    return val if val in _ANSWER_LANGUAGE_VALUES else DEFAULT_ANSWER_LANGUAGE


class AuthToken(models.Model):
    """Jeton d'authentification COMPLÈTE — émis uniquement après le second facteur.

    Remplace `rest_framework.authtoken.Token` comme crédence acceptée par
    `CookieTokenAuthentication`. Un jeton `authtoken` (historique, créé par le
    Django admin ou `drf_create_token`) n'est donc plus jamais accepté : la seule
    fabrique de ce modèle est `apps.accounts.mfa.issue_auth_token`, appelée après
    une vérification OTP réussie.

    Seule l'empreinte SHA-256 de la clé est stockée : une fuite de la table ne
    donne aucun jeton utilisable. La clé en clair ne vit que dans le cookie
    HttpOnly du navigateur.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="auth_tokens",
    )
    key_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    # Classe de l'appareil OTP qui a validé la connexion (`totp`, `static`) —
    # traçabilité seulement, jamais un secret.
    mfa_method = models.CharField(max_length=16)

    class Meta:
        app_label = "accounts"
        indexes = [models.Index(fields=["user", "expires_at"])]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"AuthToken<{self.user_id}:{self.pk}>"


class MfaChallenge(models.Model):
    """État de PRÉ-authentification : mot de passe validé, second facteur attendu.

    `password_validated != fully_authenticated` : ce challenge n'est accepté que
    par les endpoints `/api/v1/auth/mfa/*`. Il est expirant, borné en tentatives
    et consommé atomiquement (verrou de ligne) — jamais réutilisable.

    Comme pour `AuthToken`, seule l'empreinte de la clé du cookie est stockée.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mfa_challenges",
    )
    key_digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    # Posé au succès comme à l'invalidation (tentatives épuisées, nouveau login,
    # logout) : un challenge consommé n'est plus jamais accepté.
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "accounts"

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"MfaChallenge<{self.user_id}:{self.pk}>"

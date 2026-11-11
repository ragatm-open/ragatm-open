"""Authentification par cookie HttpOnly (token DRF).

Objectif : ne plus exposer le token DRF au JavaScript (fin du localStorage).
Le token est déposé dans un cookie HttpOnly au login et lu côté backend ici.

Stratégie :
- Chemin principal : cookie `AUTH_COOKIE_NAME` (HttpOnly, non lisible en JS).
- Compat transitoire : header `Authorization: Token <key>` (tests / clients API
  non-navigateur). Ce chemin n'est PAS soumis au contrôle CSRF, car un header
  custom n'est jamais envoyé automatiquement par le navigateur en cross-site.
- Auth par cookie => contrôle CSRF (double-submit cookie + header X-CSRFToken)
  appliqué sur les méthodes non sûres, comme le fait DRF SessionAuthentication.

MFA obligatoire : les deux chemins n'acceptent QUE des `AuthToken`, émis par
`apps.accounts.mfa.issue_auth_token` après le second facteur, non expirés côté
serveur. Les jetons `rest_framework.authtoken` (historiques, Django admin,
`drf_create_token`) ne sont plus jamais acceptés : le header n'est donc pas un
contournement du second facteur.
"""
from __future__ import annotations

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework import authentication, exceptions


def _auth_cookie_name() -> str:
    return getattr(settings, "AUTH_COOKIE_NAME", "ragatm_auth_token")


def _cookie_kwargs() -> dict:
    return {
        "max_age": getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 14),
        "secure": getattr(settings, "AUTH_COOKIE_SECURE", False),
        "httponly": getattr(settings, "AUTH_COOKIE_HTTPONLY", True),
        "samesite": getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
        "path": getattr(settings, "AUTH_COOKIE_PATH", "/"),
    }


def set_auth_cookie(response, token_key: str):
    """Dépose le token DRF dans le cookie HttpOnly (utilisé au login)."""
    response.set_cookie(_auth_cookie_name(), token_key, **_cookie_kwargs())
    return response


def delete_auth_cookie(response):
    """Supprime le cookie d'auth (utilisé au logout). Mêmes Path/SameSite."""
    response.delete_cookie(
        _auth_cookie_name(),
        path=getattr(settings, "AUTH_COOKIE_PATH", "/"),
        samesite=getattr(settings, "AUTH_COOKIE_SAMESITE", "Lax"),
    )
    return response


class _CSRFCheck(CsrfViewMiddleware):
    """CsrfViewMiddleware exposant la raison du rejet (cf. DRF SessionAuthentication)."""

    def _reject(self, request, reason):
        return reason


# Constante, pas un réglage : rien ne justifie de la faire varier par environnement.
MFA_CHALLENGE_COOKIE = "ragatm_mfa_challenge"


def mfa_challenge_cookie_name() -> str:
    return MFA_CHALLENGE_COOKIE


def _challenge_cookie_path() -> str:
    # Le challenge n'a d'usage que sur /api/v1/auth/ : inutile de l'envoyer ailleurs.
    return "/api/v1/auth/"


def set_mfa_challenge_cookie(response, key: str, max_age: int):
    """Dépose la clé du challenge de pré-authentification (HttpOnly, jamais lisible en JS)."""
    response.set_cookie(
        mfa_challenge_cookie_name(),
        key,
        max_age=max_age,
        secure=getattr(settings, "AUTH_COOKIE_SECURE", False),
        httponly=True,
        samesite="Strict",
        path=_challenge_cookie_path(),
    )
    return response


def delete_mfa_challenge_cookie(response):
    response.delete_cookie(mfa_challenge_cookie_name(), path=_challenge_cookie_path(), samesite="Strict")
    return response


class CookieTokenAuthentication(authentication.TokenAuthentication):
    """TokenAuthentication DRF lisant le token depuis un cookie HttpOnly.

    Ne connaît que les `AuthToken` (post-MFA), recherchés par empreinte.
    """

    def authenticate_credentials(self, key):
        from apps.accounts.mfa import find_auth_token

        token = find_auth_token(key)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid token.")
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.")
        return (token.user, token)

    @staticmethod
    def _dummy_get_response(request):  # pragma: no cover - requis par le middleware
        return None

    def authenticate(self, request):
        # 1) Header Authorization (compat clients API / tests) — sans CSRF.
        header_auth = super().authenticate(request)
        if header_auth is not None:
            return header_auth

        # 2) Cookie HttpOnly (chemin principal du frontend).
        key = request.COOKIES.get(_auth_cookie_name())
        if not key:
            return None

        # Un cookie d'auth périmé/invalide (typiquement après un reset de base ou
        # l'expiration/suppression du token) NE DOIT PAS bloquer les endpoints
        # publics (AllowAny) comme le login : le navigateur renvoie ce cookie
        # automatiquement, et lever AuthenticationFailed ici produirait un 401
        # AVANT même d'atteindre la vue. On dégrade donc en requête ANONYME
        # (return None) — les routes protégées (IsAuthenticated) restent refusées
        # normalement, et un login réussi réécrit un cookie valide.
        # Le chemin header Authorization ci-dessus, lui, reste strict (401
        # explicite pour les clients API/tests).
        try:
            user, token = self.authenticate_credentials(key)
        except exceptions.AuthenticationFailed:
            return None

        # Auth portée par cookie => on impose le contrôle CSRF.
        self.enforce_csrf(request)
        return (user, token)

    def enforce_csrf(self, request):
        """Contrôle CSRF (méthodes non sûres uniquement) — calque DRF SessionAuthentication."""
        check = _CSRFCheck(self._dummy_get_response)
        # process_request renseigne le secret CSRF depuis le cookie csrftoken.
        check.process_request(request)
        # process_view est un no-op sur GET/HEAD/OPTIONS/TRACE.
        reason = check.process_view(request, None, (), {})
        if reason:
            raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")

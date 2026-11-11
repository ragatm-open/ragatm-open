"""P-B — Authentification par cookie HttpOnly + CSRF.

Couvre :
- login pose le cookie auth HttpOnly et ne renvoie pas le token dans le JSON ;
- /me fonctionne via cookie, échoue sans cookie, échoue avec un cookie invalide ;
- logout supprime le cookie et révoque le token ;
- requête mutante via cookie : refusée sans header CSRF, acceptée avec ;
- header Authorization (compat) : accepté, et NON soumis au CSRF.

Depuis le MFA obligatoire (AUTH-MFA-01), le cookie d'auth n'est posé qu'au terme
du parcours `login` → `mfa/verify` : `_login` le parcourt réellement.
"""
import pytest
from django.conf import settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.accounts.mfa import issue_auth_token
from apps.accounts.models import AuthToken
from testing.mfa import enroll_totp, login_full

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login/"
ME = "/api/v1/auth/me/"
LOGOUT = "/api/v1/auth/logout/"
BOOTSTRAP = "/api/v1/bootstrap/"
CSRF_COOKIE = "csrftoken"


@pytest.fixture(autouse=True)
def _throttles_neufs():
    from django.core.cache import cache

    cache.clear()


def _login(client, user):
    device = user.totpdevice_set.first() or enroll_totp(user)
    res = login_full(client, "u1@example.com", "pw", device)
    assert res.status_code == 200
    return res


def test_login_sets_httponly_auth_cookie_without_token_in_body(user_with_email):
    client = APIClient()
    res = _login(client, user_with_email)
    assert "token" not in res.data
    cookie = res.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cookie is not None and cookie.value
    assert cookie["httponly"]
    assert cookie["samesite"]  # SameSite défini
    # cookie CSRF également posé pour les requêtes mutantes suivantes
    assert CSRF_COOKIE in res.cookies


def test_me_works_with_cookie(user_with_email):
    client = APIClient()
    _login(client, user_with_email)  # le client conserve le cookie d'auth
    res = client.get(ME)
    assert res.status_code == 200
    assert res.data["email"] == "u1@example.com"


def test_me_without_cookie_is_401(db):
    client = APIClient()
    assert client.get(ME).status_code == 401


def test_invalid_cookie_token_is_401(db):
    client = APIClient()
    client.cookies[settings.AUTH_COOKIE_NAME] = "deadbeefdeadbeef"
    assert client.get(ME).status_code == 401


def test_logout_revokes_token_and_clears_cookie(user_with_email):
    client = APIClient(enforce_csrf_checks=True)
    _login(client, user_with_email)
    csrf = client.cookies[CSRF_COOKIE].value

    res = client.post(LOGOUT, HTTP_X_CSRFTOKEN=csrf)
    assert res.status_code == 204
    # cookie d'auth vidé côté réponse
    cleared = res.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cleared is not None
    assert cleared.value == ""
    # token révoqué en base
    assert AuthToken.objects.filter(user=user_with_email).count() == 0


def test_cookie_mutation_without_csrf_is_rejected(user_with_email):
    client = APIClient(enforce_csrf_checks=True)
    _login(client, user_with_email)
    # POST mutant authentifié par cookie SANS header CSRF → 403
    res = client.post(BOOTSTRAP, {}, format="json")
    assert res.status_code == 403


def test_cookie_mutation_with_csrf_succeeds(user_with_email):
    client = APIClient(enforce_csrf_checks=True)
    _login(client, user_with_email)
    csrf = client.cookies[CSRF_COOKIE].value
    res = client.post(BOOTSTRAP, {}, format="json", HTTP_X_CSRFTOKEN=csrf)
    assert res.status_code == 200


def test_authorization_header_compat_bypasses_csrf(user):
    """Le chemin header Authorization reste utilisable sans CSRF (clients API)…

    …mais seulement avec un jeton émis APRÈS le second facteur (`issue_auth_token`).
    """
    key = issue_auth_token(user, method="totp")
    client = APIClient(enforce_csrf_checks=True)
    client.credentials(HTTP_AUTHORIZATION=f"Token {key}")
    res = client.post(BOOTSTRAP, {}, format="json")
    assert res.status_code == 200


def test_legacy_drf_token_is_rejected_on_header_and_cookie(user):
    """Un jeton `authtoken` (historique, Django admin, drf_create_token) n'ouvre rien."""
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    assert client.get(ME).status_code == 401

    client = APIClient()
    client.cookies[settings.AUTH_COOKIE_NAME] = token.key
    assert client.get(ME).status_code == 401

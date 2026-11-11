"""Tests des endpoints d'authentification (login / me / logout / password reset)."""
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from testing.mfa import MFA_VERIFY, enroll_totp, fresh_totp_code

pytestmark = pytest.mark.django_db


def test_login_sets_cookie_and_hides_token(user_with_email):
    """Le token n'est jamais dans le JSON : il est déposé en cookie HttpOnly…

    …et seulement au terme du second facteur : le mot de passe seul n'en pose aucun.
    """
    device = enroll_totp(user_with_email)
    client = APIClient()
    res = client.post(
        "/api/v1/auth/login/", {"email": "u1@example.com", "password": "pw"}, format="json"
    )
    assert res.status_code == 200
    assert "token" not in res.data
    assert res.cookies.get(settings.AUTH_COOKIE_NAME) is None

    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 200
    assert "token" not in res.data
    cookie = res.cookies.get(settings.AUTH_COOKIE_NAME)
    assert cookie is not None
    assert cookie.value  # contient bien le token
    assert cookie["httponly"]


def test_login_wrong_password(user_with_email):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/login/", {"email": "u1@example.com", "password": "bad"}, format="json"
    )
    assert res.status_code == 400


def test_login_unknown_email(db):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/login/", {"email": "nobody@example.com", "password": "x"}, format="json"
    )
    assert res.status_code == 400


def test_me_requires_auth(db):
    client = APIClient()
    assert client.get("/api/v1/auth/me/").status_code == 401


def test_me_returns_identity(user_with_email):
    client = APIClient()
    client.force_authenticate(user=user_with_email)
    res = client.get("/api/v1/auth/me/")
    assert res.status_code == 200
    assert res.data["email"] == "u1@example.com"


def test_logout(user):
    client = APIClient()
    client.force_authenticate(user=user)
    assert client.post("/api/v1/auth/logout/").status_code == 204


def test_password_reset_always_ok(db):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/password/reset/", {"email": "ghost@example.com"}, format="json"
    )
    assert res.status_code == 200
    assert res.data.get("ok") is True


def test_password_reset_confirm_invalid_token(db):
    client = APIClient()
    res = client.post(
        "/api/v1/auth/password/reset/confirm/",
        {"uid": "bad", "token": "bad", "new_password": "longenough123"},
        format="json",
    )
    assert res.status_code == 400

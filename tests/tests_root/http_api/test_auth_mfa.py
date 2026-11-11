"""AUTH-MFA-01 — second facteur obligatoire : invariants de sécurité.

Invariant fondamental : `password_validated != fully_authenticated`. Un mot de
passe valide ne produit qu'un challenge, accepté par les seuls endpoints
`/api/v1/auth/mfa/*`. Le cookie d'authentification n'est posé qu'après un code
TOTP (ou jeton de secours) valide.

Aucun test de ce module ne pose d'état « vérifié » à la main : chaque connexion
passe par les vrais endpoints avec un vrai code TOTP (cf. backend/testing/mfa.py).
"""
import logging
import threading
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient

from apps.accounts.models import AuthToken, MfaChallenge
from testing.mfa import (
    MFA_SETUP,
    MFA_SETUP_CONFIRM,
    MFA_STATUS,
    MFA_VERIFY,
    auth_cookie_value,
    csrf_header,
    enroll_totp,
    fresh_totp_code,
    login_full,
    login_password,
    totp_code,
)

pytestmark = pytest.mark.django_db

User = get_user_model()

ME = "/api/v1/auth/me/"
LOGOUT = "/api/v1/auth/logout/"
BOOTSTRAP = "/api/v1/bootstrap/"
CHALLENGE_COOKIE = "ragatm_mfa_challenge"
EMAIL = "u1@example.com"


@pytest.fixture(autouse=True)
def _throttles_neufs():
    # Les débits `auth`/`mfa` vivent dans le cache : chaque test part d'un seau vide.
    cache.clear()


@pytest.fixture
def device(user_with_email):
    return enroll_totp(user_with_email)


def _wait_out_otp_throttle():
    """Équivaut à laisser s'écouler la temporisation de django-otp (sans dormir)."""
    TOTPDevice.objects.update(throttling_failure_timestamp=timezone.now() - timedelta(hours=1))


def _challenged(client=None):
    client = client or APIClient()
    res = login_password(client, EMAIL, "pw")
    assert res.status_code == 200, res.content
    return client, res


# ---------------------------------------------------------------- 1er facteur


def test_password_alone_never_authenticates(user_with_email, device):
    client, res = _challenged()
    assert res.data == {"ok": True, "mfa_required": True, "next": "mfa", "expires_in": 600}
    assert auth_cookie_value(res) is None
    assert client.get(ME).status_code == 401
    assert client.post(BOOTSTRAP, {}, format="json").status_code == 401
    assert AuthToken.objects.count() == 0


def test_challenge_cookie_is_httponly_strict_and_scoped(user_with_email, device):
    _, res = _challenged()
    cookie = res.cookies[CHALLENGE_COOKIE]
    assert cookie.value
    assert cookie["httponly"]
    assert cookie["samesite"] == "Strict"
    assert cookie["path"] == "/api/v1/auth/"
    # Seule l'empreinte est en base, jamais la clé.
    assert not MfaChallenge.objects.filter(key_digest=cookie.value).exists()


def test_challenge_key_is_not_an_auth_credential(user_with_email, device):
    client, res = _challenged()
    key = res.cookies[CHALLENGE_COOKIE].value
    forged = APIClient()
    forged.cookies[settings.AUTH_COOKIE_NAME] = key
    assert forged.get(ME).status_code == 401
    header = APIClient()
    header.credentials(HTTP_AUTHORIZATION=f"Token {key}")
    assert header.get(ME).status_code == 401


def test_wrong_password_unknown_email_and_inactive_are_indistinguishable(user_with_email, device):
    client = APIClient()
    wrong = login_password(client, EMAIL, "bad")
    unknown = login_password(client, "nobody@example.com", "pw")
    user_with_email.is_active = False
    user_with_email.save(update_fields=["is_active"])
    inactive = login_password(client, EMAIL, "pw")
    for res in (wrong, unknown, inactive):
        assert res.status_code == 400
        assert res.data == {"non_field_errors": ["Invalid credentials."], "code": "invalid_credentials"}
        assert CHALLENGE_COOKIE not in res.cookies
    assert MfaChallenge.objects.count() == 0


def test_password_bruteforce_locks_account_even_with_right_password(user_with_email, device):
    client = APIClient()
    for _ in range(settings.AXES_FAILURE_LIMIT):
        assert login_password(client, EMAIL, "bad").status_code == 400
    locked = login_password(client, EMAIL, "pw")
    assert locked.status_code == 403
    assert locked.data["code"] == "account_locked"
    assert CHALLENGE_COOKIE not in locked.cookies
    # Alterner la casse ne multiplie pas le quota.
    assert login_password(client, EMAIL.upper(), "pw").status_code == 403


# --------------------------------------------------------------- 2nd facteur


def test_correct_totp_completes_authentication(user_with_email, device):
    client, _ = _challenged()
    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 200
    assert res.data["email"] == EMAIL
    cookie = res.cookies[settings.AUTH_COOKIE_NAME]
    assert cookie.value and cookie["httponly"]
    assert res.cookies[CHALLENGE_COOKIE].value == ""  # challenge effacé
    assert client.get(ME).status_code == 200
    assert client.post(BOOTSTRAP, {}, format="json").status_code == 200
    token = AuthToken.objects.get(user=user_with_email)
    assert token.mfa_method == "totp"
    assert token.key_digest != cookie.value  # empreinte seulement


def test_wrong_totp_is_refused_and_counted(user_with_email, device):
    client, _ = _challenged()
    good = fresh_totp_code(device)
    bad = "000000" if good != "000000" else "111111"
    res = client.post(MFA_VERIFY, {"token": bad}, format="json")
    assert res.status_code == 400
    assert res.data["code"] == "invalid_mfa_token"
    assert auth_cookie_value(res) is None
    assert client.get(ME).status_code == 401
    assert MfaChallenge.objects.get().attempts == 1


def test_expired_challenge_is_refused(user_with_email, device):
    client, _ = _challenged()
    MfaChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 401
    assert res.data["code"] == "mfa_challenge_invalid"
    assert AuthToken.objects.count() == 0


def test_challenge_cannot_be_reused_after_success(user_with_email, device):
    client, res = _challenged()
    key = res.cookies[CHALLENGE_COOKIE].value
    assert client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json").status_code == 200
    replay = APIClient()
    replay.cookies[CHALLENGE_COOKIE] = key
    res = replay.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 401
    assert AuthToken.objects.count() == 1


def test_totp_code_cannot_be_replayed(user_with_email, device):
    code = fresh_totp_code(device)
    first, _ = _challenged()
    assert first.post(MFA_VERIFY, {"token": code}, format="json").status_code == 200
    second, _ = _challenged()
    res = second.post(MFA_VERIFY, {"token": code}, format="json")
    assert res.status_code == 400
    assert second.get(ME).status_code == 401


def test_challenge_is_bound_to_its_user(user_with_email, device, other_user):
    other_device = enroll_totp(other_user)
    client, _ = _challenged()  # challenge de u1
    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(other_device)}, format="json")
    assert res.status_code == 400
    assert not AuthToken.objects.filter(user=other_user).exists()
    assert client.get(ME).status_code == 401


def test_too_many_wrong_codes_invalidate_the_challenge(user_with_email, device):
    client, _ = _challenged()
    good = fresh_totp_code(device)
    bad = "000000" if good != "000000" else "111111"
    for _ in range(settings.MFA_CHALLENGE_MAX_ATTEMPTS):
        assert client.post(MFA_VERIFY, {"token": bad}, format="json").status_code == 400
    _wait_out_otp_throttle()
    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 401
    assert res.data["code"] == "mfa_challenge_invalid"
    assert AuthToken.objects.count() == 0


def test_new_login_invalidates_previous_challenge(user_with_email, device):
    old, _ = _challenged()
    new, _ = _challenged()
    assert old.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json").status_code == 401
    assert new.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json").status_code == 200


def test_mfa_verify_is_rate_limited(user_with_email, device):
    client, _ = _challenged()
    statuses = [client.post(MFA_VERIFY, {"token": "000000"}, format="json").status_code for _ in range(11)]
    assert statuses[-1] == 429


def test_static_backup_token_is_single_use(user_with_email):
    static = StaticDevice.objects.create(user=user_with_email, name="backup", confirmed=True)
    StaticToken.objects.create(device=static, token="abcd1234")
    client, _ = _challenged()
    res = client.post(MFA_VERIFY, {"token": "abcd1234"}, format="json")
    assert res.status_code == 200
    assert AuthToken.objects.get().mfa_method == "static"
    again, _ = _challenged()
    assert again.post(MFA_VERIFY, {"token": "abcd1234"}, format="json").status_code == 400


def test_verify_requires_csrf_when_enforced(user_with_email, device):
    client = APIClient(enforce_csrf_checks=True)
    _challenged(client)
    res = client.post(MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 403
    res = client.post(
        MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json", **csrf_header(client)
    )
    assert res.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_concurrent_verifications_of_one_challenge_succeed_once(user_with_email, device):
    client, res = _challenged()
    key = res.cookies[CHALLENGE_COOKIE].value
    code = fresh_totp_code(device)
    results = []
    barrier = threading.Barrier(4)

    def attempt():
        try:
            c = APIClient()
            c.cookies[CHALLENGE_COOKIE] = key
            barrier.wait()
            results.append(c.post(MFA_VERIFY, {"token": code}, format="json").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(200) == 1
    assert AuthToken.objects.count() == 1


# ----------------------------------------------------------------- enrôlement


def test_account_without_factor_must_enroll(user_with_email):
    client, res = _challenged()
    assert res.data["next"] == "mfa_setup"
    assert client.post(MFA_VERIFY, {"token": "123456"}, format="json").status_code == 409
    assert client.get(ME).status_code == 401

    setup = client.post(MFA_SETUP)
    assert setup.status_code == 201
    assert setup["Cache-Control"] == "no-store"
    assert setup.data["provisioning_uri"].startswith("otpauth://totp/RAGatm")
    assert setup.data["secret"]
    pending = TOTPDevice.objects.get(user=user_with_email)
    assert pending.confirmed is False
    assert client.get(ME).status_code == 401

    bad = client.post(MFA_SETUP_CONFIRM, {"token": "000000" if totp_code(pending) != "000000" else "111111"}, format="json")
    assert bad.status_code == 400
    assert client.get(ME).status_code == 401
    # django-otp temporise après un échec (1 s, puis croissant) : on simule l'attente.
    _wait_out_otp_throttle()

    ok = client.post(MFA_SETUP_CONFIRM, {"token": fresh_totp_code(pending)}, format="json")
    assert ok.status_code == 200
    assert auth_cookie_value(ok)
    pending.refresh_from_db()
    assert pending.confirmed is True
    assert client.get(ME).status_code == 200


def test_password_alone_cannot_enroll_a_second_factor(user_with_email, device):
    client, _ = _challenged()
    res = client.post(MFA_SETUP)
    assert res.status_code == 403
    assert res.data["code"] == "mfa_already_enrolled"
    assert TOTPDevice.objects.filter(user=user_with_email).count() == 1
    res = client.post(MFA_SETUP_CONFIRM, {"token": fresh_totp_code(device)}, format="json")
    assert res.status_code == 403
    assert client.get(ME).status_code == 401


def test_setup_requires_a_challenge(user_with_email):
    assert APIClient().post(MFA_SETUP).status_code == 401
    assert TOTPDevice.objects.count() == 0


def test_status_reports_step_without_identity(user_with_email):
    anonymous = APIClient().get(MFA_STATUS)
    assert anonymous.status_code == 200
    assert anonymous.data == {"pending": False, "next": None, "expires_in": 0}
    client, _ = _challenged()
    res = client.get(MFA_STATUS)
    assert res.data["pending"] is True
    assert res.data["next"] == "mfa_setup"
    assert EMAIL not in res.content.decode()


# ------------------------------------------------------------ sessions/jetons


def test_logout_revokes_tokens(user_with_email, device):
    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    other = APIClient()
    assert login_full(other, EMAIL, "pw", device).status_code == 200
    assert client.post(LOGOUT).status_code == 204
    assert client.get(ME).status_code == 401
    assert other.get(ME).status_code == 401  # logout = tous les appareils
    assert AuthToken.objects.count() == 0


def test_password_reset_revokes_access_but_keeps_mfa(user_with_email, device):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    res = APIClient().post(
        "/api/v1/auth/password/reset/confirm/",
        {
            "uid": urlsafe_base64_encode(force_bytes(user_with_email.pk)),
            "token": default_token_generator.make_token(user_with_email),
            "new_password": "Une-phrase-longue-42",
        },
        format="json",
    )
    assert res.status_code == 200
    assert client.get(ME).status_code == 401
    again, res = _challenged_with(APIClient(), "Une-phrase-longue-42")
    assert res.data["next"] == "mfa"  # le facteur n'a pas été retiré
    assert again.get(ME).status_code == 401


def _challenged_with(client, password):
    res = login_password(client, EMAIL, password)
    assert res.status_code == 200, res.content
    return client, res


def test_deactivated_or_deleted_account_loses_access(user_with_email, device):
    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    user_with_email.is_active = False
    user_with_email.save(update_fields=["is_active"])
    assert client.get(ME).status_code == 401
    user_with_email.delete()
    assert AuthToken.objects.count() == 0


def test_removed_factor_revokes_tokens(user_with_email, device):
    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    device.delete()
    assert client.get(ME).status_code == 401


def test_expired_token_is_refused_server_side(user_with_email, device):
    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    AuthToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get(ME).status_code == 401


def test_superuser_has_no_exemption(user_with_email):
    user_with_email.is_superuser = True
    user_with_email.is_staff = True
    user_with_email.save(update_fields=["is_superuser", "is_staff"])
    client, res = _challenged()
    assert res.data["next"] == "mfa_setup"
    assert auth_cookie_value(res) is None
    assert client.get(ME).status_code == 401
    assert client.get("/api/v1/platform/users/").status_code == 401


def test_new_user_must_enroll_before_any_access(db):
    User.objects.create_user(username="new", email="new@example.com", password="pw")
    client = APIClient()
    res = login_password(client, "new@example.com", "pw")
    assert res.data["next"] == "mfa_setup"
    assert client.get(ME).status_code == 401


# ---------------------------------------------------------- Django admin


def test_django_admin_refuses_password_only_session(user_with_email):
    user_with_email.is_superuser = True
    user_with_email.is_staff = True
    user_with_email.save(update_fields=["is_superuser", "is_staff"])
    from django.test import Client

    browser = Client()
    browser.post("/admin/login/", {"username": "u1", "password": "pw"})
    assert browser.get("/admin/").status_code == 302  # renvoyé vers la connexion OTP

    session_only = Client()
    session_only.force_login(user_with_email)  # session Django SANS second facteur
    res = session_only.get("/admin/")
    assert res.status_code == 302
    assert "/admin/login/" in res["Location"]


# ------------------------------------------------------------- fuites


def test_no_secret_in_logs_or_responses(user_with_email, caplog):
    caplog.set_level(logging.DEBUG)
    client, login_res = _challenged()
    setup = client.post(MFA_SETUP)
    secret = setup.data["secret"]
    pending = TOTPDevice.objects.get(user=user_with_email)
    code = fresh_totp_code(pending)
    ok = client.post(MFA_SETUP_CONFIRM, {"token": code}, format="json")
    auth_key = auth_cookie_value(ok)
    challenge_key = login_res.cookies[CHALLENGE_COOKIE].value
    for body in (login_res.content, ok.content):
        text = body.decode()
        assert auth_key not in text and challenge_key not in text and secret not in text
    logs = caplog.text
    for value in (secret, code, auth_key, challenge_key, pending.key, "pw"):
        assert f"={value}" not in logs and f" {value} " not in logs and f"'{value}'" not in logs


# ------------------------------------------- balayage des routes protégées

# Endpoints ANONYMES par conception. Toute autre route sous /api/v1/ doit refuser
# un client qui n'a passé que le premier facteur.
_PUBLIC_ROUTES = {
    "api/v1/auth/login/",
    "api/v1/auth/mfa/status/",
    "api/v1/auth/mfa/verify/",
    "api/v1/auth/mfa/setup/",
    "api/v1/auth/mfa/setup/confirm/",
    "api/v1/auth/password/reset/",
    "api/v1/auth/password/reset/confirm/",
    "api/v1/invitations/validate/",
    "api/v1/invitations/accept/",
}


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern)


def _concrete(route: str) -> str:
    import re

    route = route.lstrip("^").rstrip("$")
    route = re.sub(r"<uuid:[^>]+>", "00000000-0000-4000-8000-000000000001", route)
    route = re.sub(r"<int:[^>]+>", "1", route)
    route = re.sub(r"<(?:str|slug|path):[^>]+>|<[^>]+>", "x", route)
    return route


def _api_routes():
    routes = sorted({_concrete(r) for r in _walk(get_resolver().url_patterns)})
    return [r for r in routes if r.startswith("api/v1/") and "(?P" not in r]


def test_route_sweep_finds_routes():
    routes = _api_routes()
    assert len(routes) > 50
    # La liste blanche ne doit pas masquer une route disparue ou renommée.
    assert _PUBLIC_ROUTES <= set(routes)


@pytest.mark.parametrize("route", [r for r in _api_routes() if r not in _PUBLIC_ROUTES])
def test_no_protected_route_answers_before_mfa(route, user_with_email):
    user_with_email.is_superuser = True
    user_with_email.is_staff = True
    user_with_email.save(update_fields=["is_superuser", "is_staff"])
    client, res = _challenged()
    key = res.cookies[CHALLENGE_COOKIE].value
    header = APIClient()
    header.credentials(HTTP_AUTHORIZATION=f"Token {key}")
    for c in (client, header):
        for method in ("get", "post"):
            status = getattr(c, method)("/" + route, {}, format="json").status_code
            assert status in (401, 403, 404, 405), (route, method, status)


# --------------------------------------------------- procédure opérateur


def test_mfa_reset_device_preview_then_apply(user_with_email, device):
    from io import StringIO

    from django.core.management import call_command

    from apps.audit.models import ActionAuditEvent

    client = APIClient()
    assert login_full(client, EMAIL, "pw", device).status_code == 200
    args = ["--email", EMAIL.upper(), "--reason", "TICKET-1", "--verified-by", "appel"]

    call_command("mfa_reset_device", *args, stdout=StringIO())
    assert TOTPDevice.objects.filter(user=user_with_email).count() == 1  # aperçu seul
    assert client.get(ME).status_code == 200

    call_command("mfa_reset_device", *args, "--apply", stdout=StringIO())
    assert TOTPDevice.objects.filter(user=user_with_email).count() == 0
    assert client.get(ME).status_code == 401
    event = ActionAuditEvent.objects.get(event_name="auth.mfa_device_reset")
    assert event.metadata["target_user_id"] == user_with_email.pk
    # Le compte doit se réenrôler : le MFA n'est pas levé.
    _, res = _challenged()
    assert res.data["next"] == "mfa_setup"


def test_mfa_reset_device_refuses_unknown_email(db):
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError):
        call_command("mfa_reset_device", "--email", "ghost@example.com", "--reason", "x", "--verified-by", "y", "--apply")

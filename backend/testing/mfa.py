"""Outils de test du second facteur — parcours RÉEL, aucun contournement.

Les tests qui se connectent passent par les vrais endpoints (`login` puis
`mfa/verify`) avec un vrai code TOTP calculé depuis le secret de l'appareil, comme
le ferait une application d'authentification. Rien ici ne pose d'état « vérifié »
à la main : un test qui veut un utilisateur authentifié sans tester l'auth utilise
`force_authenticate`, qui ne traverse pas la couche d'authentification HTTP.
"""
from __future__ import annotations

import time

from django.conf import settings
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

LOGIN = "/api/v1/auth/login/"
MFA_STATUS = "/api/v1/auth/mfa/status/"
MFA_VERIFY = "/api/v1/auth/mfa/verify/"
MFA_SETUP = "/api/v1/auth/mfa/setup/"
MFA_SETUP_CONFIRM = "/api/v1/auth/mfa/setup/confirm/"


def enroll_totp(user) -> TOTPDevice:
    """Appareil TOTP confirmé (équivalent d'un enrôlement terminé)."""
    return TOTPDevice.objects.create(user=user, name="default", confirmed=True)


def totp_code(device: TOTPDevice, *, offset_steps: int = 0) -> str:
    """Code TOTP valide maintenant (ou décalé de `offset_steps` pas)."""
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time() + offset_steps * device.step
    return f"{totp.token():0{device.digits}d}"


def fresh_totp_code(device: TOTPDevice) -> str:
    """Code acceptable par l'anti-rejeu : le plus petit pas > `last_t` dans la tolérance.

    Permet jusqu'à trois connexions par fenêtre de 30 s sur un même appareil.
    """
    device.refresh_from_db()
    now_t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    now_t.time = time.time()
    current = now_t.t()
    for offset in range(-device.tolerance, device.tolerance + 1):
        if current + offset > device.last_t:
            return totp_code(device, offset_steps=offset)
    raise AssertionError("aucun code TOTP neuf dans la fenêtre de tolérance")


def login_password(client, email: str, password: str):
    return client.post(LOGIN, {"email": email, "password": password}, format="json")


def csrf_header(client) -> dict:
    cookie = client.cookies.get("csrftoken")
    return {"HTTP_X_CSRFTOKEN": cookie.value} if cookie else {}


def login_full(client, email: str, password: str, device: TOTPDevice):
    """Connexion complète par le parcours réel ; retourne la réponse de `mfa/verify`."""
    res = login_password(client, email, password)
    assert res.status_code == 200, res.content
    assert res.data["next"] == "mfa"
    return client.post(
        MFA_VERIFY, {"token": fresh_totp_code(device)}, format="json", **csrf_header(client)
    )


def auth_cookie_value(response) -> str | None:
    cookie = response.cookies.get(settings.AUTH_COOKIE_NAME)
    return cookie.value if cookie is not None else None

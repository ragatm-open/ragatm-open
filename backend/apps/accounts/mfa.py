"""Second facteur obligatoire (TOTP) — orchestration du cycle de connexion.

Mécanisme repris d'un projet interne :

- `django-axes` protège le mot de passe (verrou interrogé AVANT `authenticate`) ;
- `django-otp` détient la vérité du second facteur : fenêtre temporelle,
  anti-rejeu TOTP (`last_t`), jetons de secours à usage unique, temporisation
  croissante après échec. Aucun calcul OTP n'est réimplémenté ici.

Adaptation RagATM : un autre client interne ouvre une session Django et lit `is_verified()` à
chaque requête. RagATM s'authentifie par jeton en cookie HttpOnly ; l'état
intermédiaire est donc porté par un `MfaChallenge` (cookie dédié, empreinte en
base, expirant, borné en tentatives) et le jeton complet (`AuthToken`) n'est
émis qu'APRÈS le second facteur. Différence assumée avec un autre client interne : le MFA est
obligatoire pour TOUT compte actif — aucune exemption, aucun flag.

Aucun mot de passe, jeton OTP, secret ou clé de cookie n'est journalisé.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from django.utils import timezone
from django_otp import devices_for_user
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import AuthToken, MfaChallenge

logger = logging.getLogger(__name__)

User = get_user_model()

NEXT_VERIFY = "mfa"
NEXT_SETUP = "mfa_setup"


class MfaError(Exception):
    """Refus à code stable, traduit en réponse HTTP par la vue."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _new_key() -> str:
    return secrets.token_urlsafe(32)


def challenge_ttl_seconds() -> int:
    return int(getattr(settings, "MFA_CHALLENGE_TTL_SECONDS", 600))


def challenge_max_attempts() -> int:
    return int(getattr(settings, "MFA_CHALLENGE_MAX_ATTEMPTS", 5))


# --- premier facteur -------------------------------------------------------


def authenticate_password(request, *, email: str, password: str):
    """Vérifie email + mot de passe sous la surveillance d'Axes.

    Le verrou est interrogé AVANT `authenticate()` (cf. un autre client interne ADR-006) : le
    backend Axes signale un verrou par `PermissionDenied`, que `authenticate`
    convertit en `None` — un compte verrouillé serait alors indiscernable d'un
    mot de passe faux, et chaque essai prolongerait le verrou.

    Email inconnu, mot de passe faux, compte inactif : un seul refus,
    `invalid_credentials`, sans détail (pas d'oracle d'existence de compte).
    """
    from axes.handlers.proxy import AxesProxyHandler

    normalized = (email or "").strip().lower()
    credentials = {"email": normalized}
    if not AxesProxyHandler.is_allowed(request, credentials):
        raise MfaError("account_locked", 403)

    try:
        candidate = User.objects.get(email__iexact=normalized)
        username = candidate.get_username()
    except (User.DoesNotExist, User.MultipleObjectsReturned):
        # On appelle quand même `authenticate` : Axes compte l'échec, et le
        # temps de réponse ne distingue pas un email inconnu.
        username = normalized

    user = authenticate(request=request, username=username, password=password, email=normalized)
    if user is None or not user.is_active:
        raise MfaError("invalid_credentials", 400)
    return user


# --- état du second facteur -----------------------------------------------


def confirmed_devices(user) -> list:
    """Appareils OTP confirmés (TOTP et jetons de secours statiques)."""
    return list(devices_for_user(user, confirmed=True))


def has_confirmed_device(user) -> bool:
    return bool(confirmed_devices(user))


def next_step(user) -> str:
    return NEXT_VERIFY if has_confirmed_device(user) else NEXT_SETUP


# --- challenge de pré-authentification ------------------------------------


def open_challenge(user) -> tuple[str, MfaChallenge]:
    """Crée le challenge et retourne la clé à déposer dans le cookie.

    Les challenges encore ouverts de ce compte sont invalidés : un seul état de
    pré-authentification vivant par compte.
    """
    now = timezone.now()
    key = _new_key()
    with transaction.atomic():
        MfaChallenge.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=now)
        challenge = MfaChallenge.objects.create(
            user=user,
            key_digest=_digest(key),
            expires_at=now + timedelta(seconds=challenge_ttl_seconds()),
        )
    return key, challenge


def _live_challenge_qs(key: str | None):
    if not key:
        return MfaChallenge.objects.none()
    return MfaChallenge.objects.filter(
        key_digest=_digest(key),
        consumed_at__isnull=True,
        expires_at__gt=timezone.now(),
        user__is_active=True,
    )


def peek_challenge(key: str | None) -> MfaChallenge | None:
    """Lecture SANS verrou ni effet, pour l'état exposé au frontend."""
    return _live_challenge_qs(key).select_related("user").first()


def _lock_challenge(key: str | None) -> MfaChallenge:
    """Verrouille le challenge vivant (à appeler dans une transaction).

    Inconnu, expiré, consommé, compte désactivé : même refus, sans détail.
    """
    challenge = _live_challenge_qs(key).select_for_update().select_related("user").first()
    if challenge is None:
        raise MfaError("mfa_challenge_invalid", 401)
    return challenge


def _record_failure(challenge: MfaChallenge) -> None:
    challenge.attempts += 1
    fields = ["attempts"]
    if challenge.attempts >= challenge_max_attempts():
        challenge.consumed_at = timezone.now()
        fields.append("consumed_at")
    challenge.save(update_fields=fields)


def revoke_challenges(user) -> None:
    MfaChallenge.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())


# --- jetons d'authentification complète -----------------------------------


def auth_token_ttl_seconds() -> int:
    return int(getattr(settings, "AUTH_COOKIE_MAX_AGE", 60 * 60 * 24 * 14))


def issue_auth_token(user, *, method: str) -> str:
    """SEULE fabrique d'`AuthToken` : appelée après un second facteur valide."""
    key = _new_key()
    AuthToken.objects.create(
        user=user,
        key_digest=_digest(key),
        expires_at=timezone.now() + timedelta(seconds=auth_token_ttl_seconds()),
        mfa_method=method,
    )
    return key


def find_auth_token(key: str) -> AuthToken | None:
    return (
        AuthToken.objects.select_related("user")
        .filter(key_digest=_digest(key), expires_at__gt=timezone.now())
        .first()
    )


def revoke_auth_tokens(user) -> int:
    """Révoque tous les jetons du compte (logout, mot de passe, facteur retiré)."""
    deleted, _ = AuthToken.objects.filter(user=user).delete()
    return deleted


# --- vérification ----------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    user: object
    auth_key: str


def _method_of(device) -> str:
    return "static" if isinstance(device, StaticDevice) else "totp"


def verify(challenge_key: str | None, *, token: str) -> Completed:
    """Valide le second facteur d'un compte enrôlé et émet le jeton complet.

    Le challenge ET les appareils sont verrouillés : deux requêtes concurrentes
    portant le même code ne peuvent pas réussir toutes les deux (le second
    `verify_token` voit `last_t` déjà avancé, ou le jeton statique consommé).
    """
    with transaction.atomic():
        challenge = _lock_challenge(challenge_key)
        user = challenge.user
        devices = [
            *TOTPDevice.objects.select_for_update().filter(user=user, confirmed=True).order_by("pk"),
            *StaticDevice.objects.select_for_update().filter(user=user, confirmed=True).order_by("pk"),
        ]
        if not devices:
            raise MfaError("mfa_device_required", 409)

        for device in devices:
            if device.verify_token(token):
                challenge.consumed_at = timezone.now()
                challenge.save(update_fields=["consumed_at"])
                key = issue_auth_token(user, method=_method_of(device))
                logger.info("mfa.verified", extra={"user_id": user.pk, "method": _method_of(device)})
                return Completed(user=user, auth_key=key)

        _record_failure(challenge)
    logger.info("mfa.failed", extra={"user_id": user.pk})
    raise MfaError("invalid_mfa_token", 400)


# --- enrôlement ------------------------------------------------------------


def start_enrollment(challenge_key: str | None) -> tuple[str, str]:
    """Crée un appareil TOTP NON confirmé ; retourne (URI d'appairage, secret base32).

    Interdit à un compte qui possède déjà un facteur confirmé : sinon le seul mot
    de passe suffirait à enrôler un nouvel appareil et à contourner le MFA. Le
    remplacement d'un facteur passe par la procédure opérateur
    (`manage.py mfa_reset_device`).
    """
    from base64 import b32encode

    with transaction.atomic():
        challenge = _lock_challenge(challenge_key)
        user = challenge.user
        if has_confirmed_device(user):
            raise MfaError("mfa_already_enrolled", 403)
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=False)
    logger.info("mfa.enrollment_started", extra={"user_id": user.pk})
    return device.config_url, b32encode(device.bin_key).decode("ascii")


def confirm_enrollment(challenge_key: str | None, *, token: str) -> Completed:
    """Active l'appareil en attente sur preuve d'un premier code valide."""
    with transaction.atomic():
        challenge = _lock_challenge(challenge_key)
        user = challenge.user
        if has_confirmed_device(user):
            raise MfaError("mfa_already_enrolled", 403)
        device = (
            TOTPDevice.objects.select_for_update()
            .filter(user=user, confirmed=False)
            .order_by("-pk")
            .first()
        )
        if device is None:
            raise MfaError("mfa_enrollment_not_started", 409)

        if device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            challenge.consumed_at = timezone.now()
            challenge.save(update_fields=["consumed_at"])
            key = issue_auth_token(user, method="totp")
            logger.info("mfa.enrolled", extra={"user_id": user.pk})
            return Completed(user=user, auth_key=key)

        _record_failure(challenge)
    logger.info("mfa.failed", extra={"user_id": user.pk, "phase": "enrollment"})
    raise MfaError("invalid_mfa_token", 400)


# --- procédure opérateur ---------------------------------------------------


def reset_devices(user) -> int:
    """Retire TOUS les facteurs du compte et révoque ses accès.

    Le compte devra se réenrôler à sa prochaine connexion (mot de passe puis
    nouvel appareil). Réservé à la procédure opérateur documentée.
    """
    with transaction.atomic():
        removed = 0
        for model in (TOTPDevice, StaticDevice):
            count, _ = model.objects.filter(user=user).delete()
            removed += count
        revoke_challenges(user)
        revoke_auth_tokens(user)
    return removed

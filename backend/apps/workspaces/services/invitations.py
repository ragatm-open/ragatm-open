"""Service métier des invitations workspace (domaine dédié, distinct du reset password).

Toute la logique d'invitation vit ici — les vues ne font qu'orchestrer HTTP.

Garanties portées par ce module :
- token brut généré par ``secrets.token_urlsafe`` ; **seul son hash SHA-256 est
  persistant** ; le brut n'est jamais écrit en base, log, audit ou exception ;
- acceptation **atomique** (``select_for_update`` sur l'invitation) ⇒ au plus un
  membership, même en concurrence ;
- un **compte actif** ne voit jamais son mot de passe modifié par un lien ;
- le rôle vient **toujours** de l'invitation en base, jamais du client ;
- ``owner`` n'est jamais attribuable ;
- URL construite **exclusivement** depuis ``settings.FRONTEND_APP_ORIGIN``.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.workspaces.models import Membership, Workspace, WorkspaceInvitation

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ exceptions
class InvitationError(Exception):
    """Erreur métier d'invitation. ``code`` / ``http_status`` mappés par la vue."""

    http_status = 400
    code = "invitation_error"

    def __init__(self, message: str = "", *, code: str | None = None, http_status: int | None = None):
        super().__init__(message or (code or self.code))
        self.message = message or (code or self.code)
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status


class InvitationRoleForbidden(InvitationError):
    http_status = 400
    code = "role_forbidden"


class InvitationWorkspaceNotFound(InvitationError):
    http_status = 404
    code = "workspace_not_found"


class InvitationAlreadyPending(InvitationError):
    http_status = 409
    code = "invitation_already_pending"


class InvitationNotFound(InvitationError):
    http_status = 404
    code = "invitation_not_found"


class InvitationInvalidToken(InvitationError):
    # Volontairement générique (anti-énumération) : on ne distingue pas
    # « token inconnu » de « token mal formé ».
    http_status = 400
    code = "invalid_token"


class InvitationExpired(InvitationError):
    http_status = 400
    code = "invitation_expired"


class InvitationRevoked(InvitationError):
    http_status = 400
    code = "invitation_revoked"


class InvitationAlreadyAccepted(InvitationError):
    http_status = 409
    code = "invitation_already_accepted"


class InvitationNotRegenerable(InvitationError):
    http_status = 409
    code = "invitation_not_regenerable"


class ActiveUserMustAuthenticate(InvitationError):
    http_status = 409
    code = "active_user_must_authenticate"


class EmailMismatch(InvitationError):
    http_status = 403
    code = "email_mismatch"


class AmbiguousAccount(InvitationError):
    http_status = 409
    code = "ambiguous_account"


class WeakPassword(InvitationError):
    http_status = 400
    code = "weak_password"


class PasswordMismatch(InvitationError):
    http_status = 400
    code = "password_mismatch"


# ------------------------------------------------------------------ helpers
def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def hash_token(raw_token: str) -> str:
    """SHA-256 hexdigest (64 hex chars) du token brut."""
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """(token_brut_url_safe, token_hash). Le brut ne doit JAMAIS être persisté."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def mask_email(email: str | None) -> str:
    e = normalize_email(email)
    if "@" not in e:
        return "***"
    local, _, domain = e.partition("@")
    if len(local) <= 2:
        masked = (local[:1] or "*") + "*"
    else:
        masked = local[0] + ("*" * (len(local) - 2)) + local[-1]
    return f"{masked}@{domain}"


def build_invitation_url(raw_token: str) -> str:
    """URL d'acceptation. Source UNIQUE : settings.FRONTEND_APP_ORIGIN.

    Jamais construite depuis Host / X-Forwarded-Host / Origin / Referer. En prod
    FRONTEND_APP_ORIGIN est obligatoire et absolu (cf. config/settings/prod.py).
    """
    origin = str(getattr(settings, "FRONTEND_APP_ORIGIN", "") or "").rstrip("/")
    return f"{origin}/accept-invitation?token={raw_token}"


def _ttl() -> timedelta:
    return timedelta(hours=int(getattr(settings, "WORKSPACE_INVITATION_TTL_HOURS", 72)))


def _allowed_roles() -> set[str]:
    return {c[0] for c in WorkspaceInvitation.ROLE_CHOICES}


def _validate_role(role: str | None) -> str:
    role = role or Membership.ROLE_MEMBER
    if role == Membership.ROLE_OWNER:
        raise InvitationRoleForbidden("Le rôle owner ne peut pas être attribué.")
    if role not in _allowed_roles():
        raise InvitationRoleForbidden("Rôle invalide.", code="role_invalid")
    return role


def _audit(
    event_name: str,
    *,
    invitation: WorkspaceInvitation | None = None,
    actor=None,
    workspace_id="",
    status: str = "completed",
    error_code: str = "",
    request_id: str = "",
    extra: dict | None = None,
):
    """Émet un événement d'audit SANS jamais exposer le token.

    Le token brut n'est pas passé ; par précaution la sanitation d'audit
    redigerait de toute façon toute clé contenant « token ».
    """
    from apps.audit.action_audit import emit_action_event

    md: dict = {}
    if invitation is not None:
        md.update(
            {
                "role": invitation.role,
                "generation": invitation.generation,
                "email_masked": mask_email(invitation.email_normalized),
            }
        )
    if extra:
        md.update(extra)

    emit_action_event(
        event_name=event_name,
        action_name="workspace_invitation",
        actor=actor if getattr(actor, "pk", None) else None,
        status=status,
        workspace_id=str(workspace_id or (invitation.workspace_id if invitation else "") or ""),
        resource_type="workspace_invitation",
        resource_id=str(invitation.id) if invitation is not None else None,
        request_id=request_id or "",
        error_code=error_code or "",
        metadata=md,
    )


# ------------------------------------------------------------------ création
def create_invitation(*, workspace_id: int, email: str, role: str | None = None, actor=None, request_id: str = ""):
    """Crée une invitation en attente et renvoie ``(invitation, raw_token)``.

    Le token brut n'est retourné qu'ici (one-shot). Lève ``InvitationAlreadyPending``
    s'il existe déjà une invitation en attente pour ce couple (workspace, email).
    """
    email_n = normalize_email(email)
    role = _validate_role(role)

    if not Workspace.objects.filter(id=workspace_id).exists():
        raise InvitationWorkspaceNotFound("Workspace introuvable.")

    raw, token_hash = generate_token()
    now = timezone.now()

    try:
        with transaction.atomic():
            existing = (
                WorkspaceInvitation.objects.select_for_update()
                .filter(
                    workspace_id=workspace_id,
                    email_normalized=email_n,
                    accepted_at__isnull=True,
                    revoked_at__isnull=True,
                )
                .first()
            )
            if existing is not None:
                raise InvitationAlreadyPending("Une invitation est déjà en attente pour cet email.")

            inv = WorkspaceInvitation.objects.create(
                workspace_id=workspace_id,
                email_normalized=email_n,
                role=role,
                token_hash=token_hash,
                generation=1,
                created_by=actor if getattr(actor, "pk", None) else None,
                expires_at=now + _ttl(),
            )
    except IntegrityError:
        # Course perdue sur la contrainte partielle unique (création concurrente).
        raise InvitationAlreadyPending("Une invitation est déjà en attente pour cet email.")

    _audit("invitation_created", invitation=inv, actor=actor, request_id=request_id)
    return inv, raw


# ------------------------------------------------------------------ régénération
def regenerate_invitation(*, workspace_id: int, invitation_id, actor=None, request_id: str = ""):
    """Régénère le token : nouveau hash, generation+1, nouvelle expiration.

    L'ancien token est **immédiatement invalidé** (le hash en base est remplacé).
    Interdit si l'invitation est acceptée ou révoquée (doctrine explicite).
    """
    raw, token_hash = generate_token()
    now = timezone.now()

    with transaction.atomic():
        inv = (
            WorkspaceInvitation.objects.select_for_update()
            .filter(id=invitation_id, workspace_id=workspace_id)
            .first()
        )
        if inv is None:
            raise InvitationNotFound("Invitation introuvable.")
        if inv.accepted_at is not None:
            raise InvitationAlreadyAccepted("Invitation déjà acceptée : régénération impossible.")
        if inv.revoked_at is not None:
            # Doctrine EXPLICITE : une invitation révoquée ne se régénère pas
            # (pas de réactivation implicite). Recréer une invitation à la place.
            raise InvitationNotRegenerable("Invitation révoquée : régénération interdite.")

        inv.token_hash = token_hash
        inv.generation = inv.generation + 1
        inv.expires_at = now + _ttl()
        inv.last_sent_at = None
        inv.save(update_fields=["token_hash", "generation", "expires_at", "last_sent_at"])

    _audit("invitation_regenerated", invitation=inv, actor=actor, request_id=request_id)
    return inv, raw


# ------------------------------------------------------------------ révocation
def revoke_invitation(*, workspace_id: int, invitation_id, actor=None, request_id: str = ""):
    """Révoque une invitation. Idempotent si déjà révoquée. Renvoie ``(inv, changed)``.

    Refuse si l'invitation est déjà acceptée. Conserve la ligne (pas de suppression).
    Le token devient immédiatement invalide (statut révoqué contrôlé à l'acceptation).
    """
    with transaction.atomic():
        inv = (
            WorkspaceInvitation.objects.select_for_update()
            .filter(id=invitation_id, workspace_id=workspace_id)
            .first()
        )
        if inv is None:
            raise InvitationNotFound("Invitation introuvable.")
        if inv.accepted_at is not None:
            raise InvitationAlreadyAccepted("Invitation déjà acceptée : révocation impossible.")
        if inv.revoked_at is not None:
            return inv, False  # idempotent
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])

    _audit("invitation_revoked", invitation=inv, actor=actor, request_id=request_id)
    return inv, True


# ------------------------------------------------------------------ consultation
def lookup_invitation(*, raw_token: str) -> dict:
    """Validation publique d'un token pour l'écran d'acceptation.

    Ne renvoie AUCUN secret (ni token ni hash). Renvoie l'état et le minimum
    nécessaire au frontend : nom du workspace, rôle, email masqué et un booléen
    ``account_active`` orientant le flux (nouveau compte vs compte existant).
    """
    token_hash = hash_token(raw_token or "")
    inv = (
        WorkspaceInvitation.objects.select_related("workspace")
        .filter(token_hash=token_hash)
        .first()
    )
    if inv is None:
        raise InvitationInvalidToken("Lien invalide.")

    st = inv.status()
    account_active = False
    if st == WorkspaceInvitation.STATUS_PENDING:
        account_active = _has_active_account(inv.email_normalized)

    return {
        "status": st,
        "workspace_name": inv.workspace.name,
        "role": inv.role,
        "email_masked": mask_email(inv.email_normalized),
        "account_active": account_active,
    }


def _matching_users(email_normalized: str) -> list:
    User = get_user_model()
    return list(User.objects.filter(email__iexact=email_normalized))


def _has_active_account(email_normalized: str) -> bool:
    return any(u.is_active and u.has_usable_password() for u in _matching_users(email_normalized))


# ------------------------------------------------------------------ acceptation
def accept_invitation(
    *,
    raw_token: str,
    password: str | None = None,
    password_confirmation: str | None = None,
    authenticated_user=None,
    request_id: str = "",
) -> dict:
    """Acceptation ATOMIQUE. Renvoie un dict avec ``membership`` / ``created`` / ``user``.

    Deux modes :
    - **anonyme** (``password`` fourni) : réservé aux comptes nouveaux ou inactifs ;
      pose le mot de passe et active le compte. Refusé pour un compte actif.
    - **authentifié** (``authenticated_user`` fourni) : n'altère jamais le mot de
      passe ; exige que l'email de la session corresponde à l'invitation.
    Le rôle vient de l'invitation (jamais du payload).
    """
    token_hash = hash_token(raw_token or "")
    now = timezone.now()
    User = get_user_model()

    with transaction.atomic():
        inv = (
            WorkspaceInvitation.objects.select_for_update()
            .filter(token_hash=token_hash)
            .first()
        )
        if inv is None:
            raise InvitationInvalidToken("Lien invalide.")
        # Ordre : accepté → révoqué → expiré.
        if inv.accepted_at is not None:
            raise InvitationAlreadyAccepted("Invitation déjà utilisée.")
        if inv.revoked_at is not None:
            raise InvitationRevoked("Invitation révoquée.")
        if inv.expires_at <= now:
            _audit(
                "invitation_expired_rejected",
                invitation=inv,
                status="denied",
                error_code="invitation_expired",
                request_id=request_id,
            )
            raise InvitationExpired("Invitation expirée.")

        ws = Workspace.objects.filter(id=inv.workspace_id).first()
        if ws is None:
            raise InvitationError("Workspace supprimé.", code="workspace_gone", http_status=409)

        email_n = inv.email_normalized
        matches = _matching_users(email_n)
        if len(matches) > 1:
            # FAIL CLOSED : ne jamais choisir arbitrairement un compte.
            _audit(
                "invitation_failed",
                invitation=inv,
                status="failed",
                error_code="ambiguous_account",
                request_id=request_id,
            )
            raise AmbiguousAccount("Plusieurs comptes correspondent à cet email.")
        existing = matches[0] if matches else None

        if authenticated_user is not None:
            # --- Acceptation authentifiée : jamais de set_password. ---
            if normalize_email(getattr(authenticated_user, "email", "")) != email_n:
                raise EmailMismatch("Le compte connecté ne correspond pas à l'invitation.")
            user = authenticated_user
        else:
            # --- Acceptation anonyme : comptes nouveaux/inactifs uniquement. ---
            if existing is not None and existing.is_active and existing.has_usable_password():
                raise ActiveUserMustAuthenticate(
                    "Ce compte existe déjà. Connectez-vous pour accepter l'invitation."
                )
            if not password or not password_confirmation:
                raise PasswordMismatch("Mot de passe requis.")
            if password != password_confirmation:
                raise PasswordMismatch("Les mots de passe ne correspondent pas.")
            try:
                validate_password(password, user=existing)
            except DjangoValidationError as e:
                raise WeakPassword(" ".join(e.messages))

            if existing is None:
                user = User(email=email_n, is_active=True)
                # auth.User stock : username unique obligatoire → on le fixe à l'email.
                if getattr(User, "USERNAME_FIELD", "username") != "email" and hasattr(user, "username"):
                    user.username = email_n
                user.set_password(password)
                try:
                    user.save()
                except IntegrityError:
                    # Collision (username/email) apparue en concurrence → fail closed.
                    raise AmbiguousAccount("Conflit de compte lors de l'acceptation.")
            else:
                user = existing
                user.set_password(password)
                if not user.is_active:
                    user.is_active = True
                    user.save(update_fields=["password", "is_active"])
                else:
                    user.save(update_fields=["password"])

        # Membership : rôle issu de l'invitation. Existant → conservé (pas de
        # promotion implicite).
        membership, created = Membership.objects.get_or_create(
            workspace_id=inv.workspace_id,
            user=user,
            defaults={"role": inv.role},
        )

        inv.accepted_at = now
        inv.accepted_by = user if getattr(user, "pk", None) else None
        inv.save(update_fields=["accepted_at", "accepted_by"])

    _audit(
        "invitation_accepted",
        invitation=inv,
        actor=user,
        status="completed",
        request_id=request_id,
        extra={"membership_created": bool(created), "role_kept": (not created)},
    )
    return {
        "membership": membership,
        "membership_created": created,
        "user": user,
        "invitation": inv,
        "workspace": ws,
    }


# ------------------------------------------------------------------ email
def send_invitation_email(*, invitation: WorkspaceInvitation, raw_token: str, invited_by_email: str = "") -> tuple[bool, str | None]:
    """Envoi best-effort du lien par email. Ne lève jamais. Renvoie (sent, error).

    Le lien envoyé est EXACTEMENT celui retourné à la création/régénération.
    """
    to_email = normalize_email(invitation.email_normalized)
    if not to_email:
        return False, "missing_to_email"

    link = build_invitation_url(raw_token)
    ttl_h = int(getattr(settings, "WORKSPACE_INVITATION_TTL_HOURS", 72))
    subject_prefix = getattr(settings, "EMAIL_SUBJECT_PREFIX", "") or ""

    # Langue de l'email = préférence du destinataire s'il possède déjà un compte,
    # sinon français par défaut (l'invité n'a en général pas encore de compte).
    # On ne mélange jamais deux langues dans un même email.
    from apps.accounts.models import get_preferred_locale, DEFAULT_LOCALE

    recipient = next(iter(_matching_users(to_email)), None)
    locale = get_preferred_locale(recipient) if recipient else DEFAULT_LOCALE

    role_labels = {
        "fr": {"admin": "Administrateur", "member": "Éditeur", "viewer": "Lecteur", "owner": "Propriétaire"},
        "en": {"admin": "Administrator", "member": "Editor", "viewer": "Viewer", "owner": "Owner"},
    }[locale]
    role_label = role_labels.get(invitation.role, invitation.role)

    if locale == "en":
        subject = f"{subject_prefix}Invitation to join a workspace".strip()
        body = (
            "Hello,\n\n"
            f"You have been invited to join the workspace “{invitation.workspace.name}” "
            f"on RAG-ATM with the role “{role_label}”.\n\n"
            "To accept the invitation, open this link:\n"
            f"{link}\n\n"
            f"This link expires in {ttl_h} hours.\n"
            f"{('Invited by: ' + invited_by_email + chr(10)) if invited_by_email else ''}"
            "\nFor your security, do not share this link with anyone else. "
            "If this does not concern you, ignore this email.\n"
        )
    else:
        subject = f"{subject_prefix}Invitation à rejoindre un espace de travail".strip()
        body = (
            "Bonjour,\n\n"
            f"Vous avez été invité(e) à rejoindre l'espace de travail « {invitation.workspace.name} » "
            f"sur RAG-ATM avec le rôle « {role_label} ».\n\n"
            "Pour accepter l'invitation, ouvrez ce lien :\n"
            f"{link}\n\n"
            f"Ce lien expire dans {ttl_h} heures.\n"
            f"{('Invité par : ' + invited_by_email + chr(10)) if invited_by_email else ''}"
            "\nPour votre sécurité, ne transmettez ce lien à personne d'autre. "
            "Si vous n'êtes pas concerné(e), ignorez cet email.\n"
        )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "SERVER_EMAIL", None) or "no-reply@localhost"

    try:
        send_mail(subject=subject, message=body, from_email=from_email, recipient_list=[to_email], fail_silently=False)
        WorkspaceInvitation.objects.filter(pk=invitation.pk).update(last_sent_at=timezone.now())
        return True, None
    except Exception as e:  # best-effort : ne jamais casser l'API sur un souci SMTP
        logger.warning("invitation_email_failed invitation=%s error=%s", invitation.pk, type(e).__name__)
        return False, type(e).__name__

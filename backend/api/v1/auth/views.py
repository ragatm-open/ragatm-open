from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.middleware.csrf import get_token as get_csrf_token, rotate_token
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts import mfa
from apps.accounts.authentication import (
    CookieTokenAuthentication,
    delete_auth_cookie,
    delete_mfa_challenge_cookie,
    mfa_challenge_cookie_name,
    set_auth_cookie,
    set_mfa_challenge_cookie,
)

from .serializers import (
    EmailLoginSerializer,
    MfaTokenSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
)

User = get_user_model()

# NOTE: pas d'endpoint public d'inscription. La création de comptes est
# réservée à l'admin / aux invitations (activation via reset password). On a
# supprimé l'ancienne vue `register` qui renvoyait un token d'auth en JSON
# (token exposé au JavaScript) : le seul flux d'auth est cookie HttpOnly + CSRF
# via le parcours `login` → `mfa/verify` (ou `mfa/setup/confirm`). Un test
# (test_no_public_register_endpoint) verrouille l'absence de route /register.


def _mfa_error(e: mfa.MfaError) -> Response:
    return Response({"detail": e.code, "code": e.code}, status=e.status)


def _enforce_csrf(request) -> None:
    """CSRF imposé sur les vues du parcours, même sans session authentifiée.

    DRF ne vérifie le jeton CSRF que pour une requête déjà authentifiée par
    cookie. Le challenge MFA EST un cookie envoyé automatiquement par le
    navigateur : sans cette garde, un site tiers pourrait faire valider un code
    à la place de l'utilisateur. (Repris de un autre client interne `EnforceCSRFMixin`.)
    """
    CookieTokenAuthentication().enforce_csrf(request)


def _challenge_key(request) -> str | None:
    return request.COOKIES.get(mfa_challenge_cookie_name())


def _identity_payload(user) -> dict:
    return {
        "ok": True,
        "id": str(user.id),
        "email": getattr(user, "email", ""),
        "username": getattr(user, "username", ""),
    }


def _complete(request, completed: mfa.Completed) -> Response:
    """Authentification COMPLÈTE : seul endroit où le cookie d'auth est posé."""
    resp = Response(_identity_payload(completed.user), status=status.HTTP_200_OK)
    set_auth_cookie(resp, completed.auth_key)
    delete_mfa_challenge_cookie(resp)
    # Le jeton CSRF tourne au changement de privilège (comme `django.contrib.auth.login`).
    rotate_token(request)
    get_csrf_token(request)
    return resp


class LoginView(APIView):
    """POST /api/v1/auth/login/ — PREMIER facteur seulement.

    Un mot de passe valide n'ouvre AUCUN accès : la réponse porte `next`
    (`mfa` ou `mfa_setup`) et un cookie de challenge que seuls les endpoints
    `/api/v1/auth/mfa/*` acceptent. Aucun cookie d'authentification n'est posé.
    """

    # Un cookie d'auth périmé ne doit ni bloquer ni compter dans le seau `user`.
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        s = EmailLoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            user = mfa.authenticate_password(
                request,
                email=s.validated_data["email"],
                password=s.validated_data["password"],
            )
        except mfa.MfaError as e:
            if e.code == "invalid_credentials":
                # Contrat historique conservé (400 + message unique).
                return Response(
                    {"non_field_errors": ["Invalid credentials."], "code": e.code},
                    status=e.status,
                )
            return _mfa_error(e)

        key, challenge = mfa.open_challenge(user)
        resp = Response(
            {
                "ok": True,
                "mfa_required": True,
                "next": mfa.next_step(user),
                "expires_in": mfa.challenge_ttl_seconds(),
            },
            status=status.HTTP_200_OK,
        )
        set_mfa_challenge_cookie(resp, key, mfa.challenge_ttl_seconds())
        # Pose le cookie CSRF (double-submit) exigé par les étapes MFA suivantes.
        get_csrf_token(request)
        return resp


login = LoginView.as_view()


class MfaStatusView(APIView):
    """GET /api/v1/auth/mfa/status/ — étape restante du challenge en cours.

    Permet au frontend de reprendre le parcours après un rafraîchissement. Ne
    révèle ni l'email ni l'identité : seulement l'étape et le délai restant.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        get_csrf_token(request)
        challenge = mfa.peek_challenge(_challenge_key(request))
        if challenge is None:
            return Response({"pending": False, "next": None, "expires_in": 0})
        remaining = int((challenge.expires_at - timezone.now()).total_seconds())
        return Response(
            {"pending": True, "next": mfa.next_step(challenge.user), "expires_in": max(0, remaining)}
        )


class _MfaStepView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa"


class MfaVerifyView(_MfaStepView):
    """POST /api/v1/auth/mfa/verify/ {token} — second facteur d'un compte enrôlé."""

    def post(self, request):
        _enforce_csrf(request)
        ser = MfaTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            completed = mfa.verify(_challenge_key(request), token=ser.validated_data["token"])
        except mfa.MfaError as e:
            return _mfa_error(e)
        return _complete(request, completed)


class MfaSetupView(_MfaStepView):
    """POST /api/v1/auth/mfa/setup/ — enrôlement TOTP d'un compte SANS facteur.

    L'URI d'appairage (et le secret pour saisie manuelle) n'est renvoyée qu'ici,
    à l'appelant qui détient le challenge ; elle n'est ni journalisée ni
    relisible. Réponse `no-store` : aucun cache intermédiaire ne la conserve.
    """

    def post(self, request):
        _enforce_csrf(request)
        try:
            uri, secret = mfa.start_enrollment(_challenge_key(request))
        except mfa.MfaError as e:
            return _mfa_error(e)
        resp = Response(
            {"method": "totp", "provisioning_uri": uri, "secret": secret},
            status=status.HTTP_201_CREATED,
        )
        resp["Cache-Control"] = "no-store"
        return resp


class MfaSetupConfirmView(_MfaStepView):
    """POST /api/v1/auth/mfa/setup/confirm/ {token} — active l'appareil et connecte."""

    def post(self, request):
        _enforce_csrf(request)
        ser = MfaTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            completed = mfa.confirm_enrollment(
                _challenge_key(request), token=ser.validated_data["token"]
            )
        except mfa.MfaError as e:
            return _mfa_error(e)
        return _complete(request, completed)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    # Révoque TOUS les jetons du compte (comportement historique : un logout
    # déconnecte aussi les autres appareils) et tout challenge en cours.
    mfa.revoke_auth_tokens(request.user)
    mfa.revoke_challenges(request.user)
    resp = Response(status=status.HTTP_204_NO_CONTENT)
    delete_auth_cookie(resp)
    delete_mfa_challenge_cookie(resp)
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    u = request.user
    # GET sûr : on en profite pour (re)poser le cookie CSRF si besoin, afin que le
    # frontend dispose toujours d'un jeton CSRF après un refresh de page.
    get_csrf_token(request)
    from apps.accounts.permissions import is_ragatm_owner
    from apps.accounts.models import get_preferred_locale, get_preferred_answer_language

    return Response(
        {
            "id": str(u.id),
            "email": getattr(u, "email", ""),
            "username": getattr(u, "username", ""),
            # Langue d'interface persistée (fr | en). Source de vérité côté compte ;
            # le frontend synchronise le cookie `ragatm_locale` avec cette valeur.
            "preferred_locale": get_preferred_locale(u),
            # Langue des réponses IA persistée (auto | fr | en), DISTINCTE de l'interface.
            "preferred_answer_language": get_preferred_answer_language(u),
            # Gating UX de l'admin corpus métier (le backend reste la source de
            # vérité : tous les endpoints corpus exigent IsRagATMOwner).
            "is_ragatm_owner": is_ragatm_owner(u),
            # Gating UX de la console d'administration PLATEFORME (superuser).
            # Le backend reste autoritaire : /api/v1/platform/* exige IsRagATMOwner.
            "is_superuser": bool(getattr(u, "is_superuser", False) and getattr(u, "is_active", False)),
        },
        status=status.HTTP_200_OK,
    )


def _frontend_base_url() -> str:
    """
    Base URL front utilisée dans le lien reset password.
    Source unique unifiée : settings.FRONTEND_APP_ORIGIN (cf. base.py / prod.py).
    Les anciens settings FRONTEND_BASE_URL / FRONTEND_URL (jamais définis) sont
    supprimés au profit de cette variable unique, partagée avec les invitations.
    """
    base = getattr(settings, "FRONTEND_APP_ORIGIN", "") or ""
    if base:
        return str(base).rstrip("/")
    return "http://127.0.0.1:6300"


class PasswordResetRequestView(APIView):
    """
    Anti-enumeration: retourne toujours {ok:true}, même si l'email n'existe pas.
    En DEBUG, renvoie aussi debug_link pour tester sans SMTP.

    NOTE:
    - On ne doit JAMAIS 500 à cause du SMTP, donc fail_silently=True.
    - Si tu veux logger les erreurs SMTP, tu peux les logger dans l'except.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        ser = PasswordResetRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"].strip()

        debug_link = None

        user = User.objects.filter(email__iexact=email).first()
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)

            qs = urlencode({"uid": uid, "token": token})
            link = f"{_frontend_base_url()}/reset-password?{qs}"
            debug_link = link

            # Langue de l'email = préférence du destinataire (défaut fr). On ne
            # mélange jamais deux langues dans un même email.
            from apps.accounts.models import get_preferred_locale

            locale = get_preferred_locale(user)
            if locale == "en":
                subject = "Reset your password"
                body = (
                    "Hello,\n\n"
                    "You requested a password reset.\n"
                    "Click this link to choose a new password:\n\n"
                    f"{link}\n\n"
                    "If you did not request this, you can safely ignore this email.\n"
                )
            else:
                subject = "Réinitialisation de votre mot de passe"
                body = (
                    "Bonjour,\n\n"
                    "Vous avez demandé une réinitialisation de mot de passe.\n"
                    "Cliquez sur ce lien pour choisir un nouveau mot de passe :\n\n"
                    f"{link}\n\n"
                    "Si vous n'êtes pas à l'origine de cette demande, vous pouvez ignorer cet email.\n"
                )

            # En prod: configure EMAIL_BACKEND + DEFAULT_FROM_EMAIL
            from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "SERVER_EMAIL", None)

            try:
                send_mail(
                    subject=subject,
                    message=body,
                    from_email=from_email,
                    recipient_list=[user.email],
                    fail_silently=True,  # ne jamais casser l'API sur un souci SMTP
                )
            except Exception:
                # Sécurité: ne jamais faire planter le reset password.
                pass

        payload = {"ok": True}
        if getattr(settings, "DEBUG", False) and debug_link:
            payload["debug_link"] = debug_link

        return Response(payload, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Attendu (POST JSON):
          - uid
          - token
          - new_password      (recommandé)
        OU (compat front legacy):
          - uid
          - token
          - new_password1
          - new_password2

        Le serializer normalise en validated_data["new_password"].
        """
        ser = PasswordResetConfirmSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        uid = ser.validated_data["uid"]
        token = ser.validated_data["token"]
        new_password = ser.validated_data["new_password"]

        try:
            user_id = force_str(urlsafe_base64_decode(uid))
            user = User.objects.filter(pk=user_id).first()
        except Exception:
            user = None

        if not user or not default_token_generator.check_token(user, token):
            return Response({"detail": "invalid_token"}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)

        # Optionnel: si tu crées des users invités avec compte "inactif"
        if hasattr(user, "is_active") and user.is_active is False:
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
        else:
            user.save(update_fields=["password"])

        return Response({"ok": True}, status=status.HTTP_200_OK)

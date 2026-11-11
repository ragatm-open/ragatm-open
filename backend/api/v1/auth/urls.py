from django.urls import path

from .views import (
    login,
    logout,
    me,
    MfaSetupConfirmView,
    MfaSetupView,
    MfaStatusView,
    MfaVerifyView,
    PasswordResetRequestView,
    PasswordResetConfirmView,
)

urlpatterns = [
    # Auth
    path("login/", login),
    path("logout/", logout),
    path("me/", me),

    # Second facteur (obligatoire) — seuls endpoints qui acceptent le challenge
    path("mfa/status/", MfaStatusView.as_view()),
    path("mfa/verify/", MfaVerifyView.as_view()),
    path("mfa/setup/", MfaSetupView.as_view()),
    path("mfa/setup/confirm/", MfaSetupConfirmView.as_view()),

    # Password reset flow
    path("password/reset/", PasswordResetRequestView.as_view()),
    path("password/reset/confirm/", PasswordResetConfirmView.as_view()),
]

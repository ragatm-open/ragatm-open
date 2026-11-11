from django.urls import path

from .views import (
    InvitationAcceptAuthenticatedView,
    InvitationAcceptView,
    InvitationValidateView,
)

urlpatterns = [
    path("validate/", InvitationValidateView.as_view(), name="invitation-validate"),
    path("accept/", InvitationAcceptView.as_view(), name="invitation-accept"),
    path(
        "accept-authenticated/",
        InvitationAcceptAuthenticatedView.as_view(),
        name="invitation-accept-authenticated",
    ),
]

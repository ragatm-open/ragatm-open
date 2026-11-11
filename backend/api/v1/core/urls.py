# backend/api/v1/core/urls.py
from django.urls import path

from .views import CoreDocumentStatusView, CoreHealthView, CoreRetrieveView

urlpatterns = [
    path("health/", CoreHealthView.as_view(), name="core-health"),
    path("retrieve/", CoreRetrieveView.as_view(), name="core-retrieve"),
    path(
        "documents/<uuid:document_id>/status/",
        CoreDocumentStatusView.as_view(),
        name="core-document-status",
    ),
]

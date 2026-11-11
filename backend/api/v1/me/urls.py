from django.urls import path

from .views import PreferencesView

urlpatterns = [
    path("preferences/", PreferencesView.as_view(), name="me-preferences"),
]

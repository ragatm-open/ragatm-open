from django.urls import path
from .views import BootstrapView

urlpatterns = [
    path("", BootstrapView.as_view(), name="bootstrap"),
]

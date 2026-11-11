# backend/api/v1/admin_core_config/urls.py
"""Routes admin RagATM_Core (lecture seule), montées sous /api/v1/admin/core-config/.

Protégées par IsRagATMOwner (vues). Imports paresseux, cohérents avec le projet.
"""
from django.urls import path


def _v(name):
    def _factory():
        from api.v1.admin_core_config import views

        return getattr(views, name).as_view()

    return _factory()


urlpatterns = [
    path("effective/", _v("CoreConfigEffectiveView"), name="admin-core-config-effective"),
    path("health/", _v("CoreConfigHealthView"), name="admin-core-config-health"),
    path("audit/", _v("CoreConfigAuditView"), name="admin-core-config-audit"),
    path("config-audit/", _v("CoreConfigAuditEventsView"), name="admin-core-config-config-audit"),
    # capacités documentaires effectives (lecture seule).
    path("document-capabilities/", _v("DocumentCapabilitiesView"), name="admin-core-config-document-capabilities"),
    # Écriture (PR-F) — staging only, flag-gated, owner-only.
    path("override/", _v("CoreConfigOverrideWriteView"), name="admin-core-config-override"),
    path("override/unset/", _v("CoreConfigOverrideUnsetView"), name="admin-core-config-override-unset"),
]

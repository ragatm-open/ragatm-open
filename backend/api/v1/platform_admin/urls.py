"""Routes d'administration PLATEFORME, montées sous /api/v1/platform/.

Toutes protégées par IsRagATMOwner (superuser) au niveau des vues. Imports
paresseux (cohérent projet). Lot P1 : lecture seule (users + workspaces).
"""
from django.urls import path


def _v(name):
    def _factory():
        from api.v1.platform_admin import views

        return getattr(views, name).as_view()

    return _factory()


def _m(name):
    def _factory():
        from api.v1.platform_admin import mutations

        return getattr(mutations, name).as_view()

    return _factory()


def _t(name):
    def _factory():
        from api.v1.platform_admin import transfers

        return getattr(transfers, name).as_view()

    return _factory()


urlpatterns = [
    path("users/", _v("PlatformUserListView"), name="platform-users"),
    path("users/<int:user_id>/", _v("PlatformUserDetailView"), name="platform-user-detail"),
    path("users/<int:user_id>/disable/", _m("PlatformUserDisableView"), name="platform-user-disable"),
    path("users/<int:user_id>/enable/", _m("PlatformUserEnableView"), name="platform-user-enable"),
    path("workspaces/", _v("PlatformWorkspaceListView"), name="platform-workspaces"),
    path("workspaces/<int:workspace_id>/", _v("PlatformWorkspaceDetailView"), name="platform-workspace-detail"),
    path("workspace-quota-defaults/", _v("PlatformWorkspaceQuotaDefaultsView"), name="platform-workspace-quota-defaults"),
    path("owner-scope-diagnostics/", _v("PlatformDiagnosticsView"), name="platform-diagnostics"),
    path("dashboard/", _v("PlatformDashboardView"), name="platform-dashboard"),
    # --- Mutations (Lot P2/P3) ---
    path("workspaces/<int:workspace_id>/quota/", _m("PlatformWorkspaceQuotaView"), name="platform-workspace-quota"),
    path("workspaces/<int:workspace_id>/owner/", _m("PlatformWorkspaceOwnerView"), name="platform-workspace-owner"),
    path("workspaces/<int:workspace_id>/memberships/", _m("PlatformMembershipCreateView"), name="platform-memberships"),
    path("workspaces/<int:workspace_id>/memberships/<int:membership_id>/",
         _m("PlatformMembershipDetailView"), name="platform-membership-detail"),
    path("workspaces/<int:workspace_id>/memberships/<int:membership_id>/owner-scope/",
         _m("PlatformMembershipOwnerScopeView"), name="platform-membership-owner-scope"),
    path("workspaces/<int:workspace_id>/disable/", _m("PlatformWorkspaceDisableView"), name="platform-workspace-disable"),
    path("workspaces/<int:workspace_id>/enable/", _m("PlatformWorkspaceEnableView"), name="platform-workspace-enable"),
    # --- Transfert de périmètre (P3) : preview seule ; apply = NO-GO explicite ---
    path("owner-scope-transfers/preview/", _t("PlatformOwnerScopeTransferPreviewView"), name="platform-transfer-preview"),
    path("owner-scope-transfers/apply/", _t("PlatformOwnerScopeTransferApplyView"), name="platform-transfer-apply"),
]
# NB : POST /workspaces/ (création) est servi par PlatformWorkspaceListView.post
# (même URL que la liste GET).

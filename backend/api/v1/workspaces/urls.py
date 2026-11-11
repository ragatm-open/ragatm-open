from django.urls import path

from .views import (
    WorkspaceListView,
    WorkspaceQuotaView,
    WorkspaceMembersView,
    WorkspaceMemberDetailView,
    WorkspaceRagSpaceListCreateView,
    WorkspaceDomainCorpusListView,
)
from .invitation_views import (
    WorkspaceInvitationsView,
    WorkspaceInvitationRegenerateView,
    WorkspaceInvitationRevokeView,
)

urlpatterns = [
    # Workspaces accessibles par l'utilisateur courant
    path(
        "",
        WorkspaceListView.as_view(),
        name="workspaces-list",
    ),

    # Quotas documentaires du workspace (OWNER / ADMIN) — LECTURE SEULE
    path(
        "<int:workspace_id>/quota/",
        WorkspaceQuotaView.as_view(),
        name="workspace-quota",
    ),

    # Members d'un workspace (OWNER / ADMIN)
    # GET  : list members
    # POST : invite / add member by email
    path(
        "<int:workspace_id>/members/",
        WorkspaceMembersView.as_view(),
        name="workspace-members",
    ),

    # Member detail (OWNER / ADMIN)
    # PATCH  : update role
    # DELETE : remove member
    path(
        "<int:workspace_id>/members/<int:membership_id>/",
        WorkspaceMemberDetailView.as_view(),
        name="workspace-member-detail",
    ),

    # Invitations d'un workspace (OWNER / ADMIN) — domaine dédié WorkspaceInvitation
    # GET  : liste des invitations (sans secret)
    # POST : créer une invitation, renvoie le lien one-shot
    path(
        "<int:workspace_id>/invitations/",
        WorkspaceInvitationsView.as_view(),
        name="workspace-invitations",
    ),
    path(
        "<int:workspace_id>/invitations/<uuid:invitation_id>/regenerate/",
        WorkspaceInvitationRegenerateView.as_view(),
        name="workspace-invitation-regenerate",
    ),
    path(
        "<int:workspace_id>/invitations/<uuid:invitation_id>/revoke/",
        WorkspaceInvitationRevokeView.as_view(),
        name="workspace-invitation-revoke",
    ),

    # RagSpaces d'un workspace (OWNER / ADMIN)
    # GET  : list ragspaces (non deleted)
    # POST : create ragspace
    path(
        "<int:workspace_id>/ragspaces/",
        WorkspaceRagSpaceListCreateView.as_view(),
        name="workspace-ragspaces",
    ),

    # Corpus métier associés au workspace (LECTURE SEULE, tout membre)
    path(
        "<int:workspace_id>/domain-corpora/",
        WorkspaceDomainCorpusListView.as_view(),
        name="workspace-domain-corpora",
    ),
]

# backend/api/v1/ragspaces/urls.py
from django.urls import path


def _ragspace_list_view():
    from api.v1.ragspaces.views import RagSpaceListView
    return RagSpaceListView.as_view()


def _ragspace_detail_view():
    from api.v1.ragspaces.views import RagSpaceDetailView
    return RagSpaceDetailView.as_view()


def _ragspace_stats_view():
    from api.v1.ragspaces.views import RagSpaceStatsView
    return RagSpaceStatsView.as_view()


def _ragspace_access_list_create_view():
    from api.v1.ragspaces.access_views import RagSpaceAccessListCreateView
    return RagSpaceAccessListCreateView.as_view()


def _ragspace_access_detail_view():
    from api.v1.ragspaces.access_views import RagSpaceAccessDetailView
    return RagSpaceAccessDetailView.as_view()


def _ragspace_access_eligible_view():
    from api.v1.ragspaces.access_views import RagSpaceAccessEligibleView
    return RagSpaceAccessEligibleView.as_view()


def _ragspace_llm_settings_view():
    from api.v1.ragspaces.views import RagSpaceLLMSettingsView
    return RagSpaceLLMSettingsView.as_view()


def _ragspace_llm_settings_test_view():
    from api.v1.ragspaces.views import RagSpaceLLMSettingsTestView
    return RagSpaceLLMSettingsTestView.as_view()


def _ragspace_reindex_view():
    from api.v1.ragspaces.views import RagSpaceReindexView
    return RagSpaceReindexView.as_view()


def _ragspace_active_reindex_job_view():
    from api.v1.ragspaces.views import RagSpaceActiveReindexJobView
    return RagSpaceActiveReindexJobView.as_view()


def _ragspace_retry_failed_documents_view():
    from api.v1.ragspaces.views import RagSpaceRetryFailedDocumentsView
    return RagSpaceRetryFailedDocumentsView.as_view()


def _ragspace_retry_failed_documents_preview_view():
    from api.v1.ragspaces.views import RagSpaceRetryFailedDocumentsPreviewView
    return RagSpaceRetryFailedDocumentsPreviewView.as_view()


def _ragspace_analysis_create_view():
    from api.v1.analyses.views import RagSpaceAnalysisCreateView
    return RagSpaceAnalysisCreateView.as_view()


urlpatterns = [
    # Liste des RagSpaces accessibles (member-friendly)
    path("", _ragspace_list_view(), name="ragspaces-list"),

    # Detail / update / delete (admin / owner)
    path("<uuid:rag_space_id>/", _ragspace_detail_view(), name="ragspaces-detail"),

    # Stats (documents, chunks, usage, etc.)
    path("<uuid:rag_space_id>/stats/", _ragspace_stats_view(), name="ragspaces-stats"),

    # Affectations RagSpaceAccess (manage) — list / grant / change / revoke.
    path("<uuid:rag_space_id>/access/", _ragspace_access_list_create_view(), name="ragspaces-access"),
    path("<uuid:rag_space_id>/access/eligible/", _ragspace_access_eligible_view(), name="ragspaces-access-eligible"),
    path("<uuid:rag_space_id>/access/<uuid:grant_id>/", _ragspace_access_detail_view(), name="ragspaces-access-detail"),

    # LLM settings (capacités) — owner/admin
    path("<uuid:rag_space_id>/llm-settings/", _ragspace_llm_settings_view(), name="ragspaces-llm-settings"),

    # LLM settings — test de connexion provider — owner/admin
    path(
        "<uuid:rag_space_id>/llm-settings/test/",
        _ragspace_llm_settings_test_view(),
        name="ragspaces-llm-settings-test",
    ),

    # Réindexation globale de l'espace (job durable, 202) — owner/admin
    path("<uuid:rag_space_id>/reindex/", _ragspace_reindex_view(), name="ragspaces-reindex"),

    # Job de réindexation actif (reprise du suivi après refresh) — owner/admin
    path(
        "<uuid:rag_space_id>/active-reindex-job/",
        _ragspace_active_reindex_job_view(),
        name="ragspaces-active-reindex-job",
    ),

    # Reprise en masse des documents en ERREUR (job durable, 202) — owner/admin
    # NB: la route `preview/` est déclarée AVANT la route parente : Django résout
    # dans l'ordre, et l'inverse ferait matcher la création sur l'URL de preview.
    path(
        "<uuid:rag_space_id>/retry-failed-documents/preview/",
        _ragspace_retry_failed_documents_preview_view(),
        name="ragspaces-retry-failed-documents-preview",
    ),
    path(
        "<uuid:rag_space_id>/retry-failed-documents/",
        _ragspace_retry_failed_documents_view(),
        name="ragspaces-retry-failed-documents",
    ),

    # Analyses globales de corpus (L2) — création. Lecture/annulation : /analyses/.
    path(
        "<uuid:rag_space_id>/analyses/",
        _ragspace_analysis_create_view(),
        name="ragspaces-analyses-create",
    ),
]


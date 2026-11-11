# backend/api/v1/urls.py
from django.urls import include, path
from api.v1.chat.views import ChatFeedbackView


# --------------------
# Chat
# --------------------
def _chat_view():
    from api.v1.chat.views import ChatView
    return ChatView.as_view()


def _chat_stream_view():
    from api.v1.chat.views import ChatStreamView
    return ChatStreamView.as_view()


def _chat_threads_view():
    from api.v1.chat.views import ChatThreadsView
    return ChatThreadsView.as_view()


def _chat_thread_search_view():
    from api.v1.chat.views import ChatThreadSearchView
    return ChatThreadSearchView.as_view()


def _chat_thread_messages_view():
    from api.v1.chat.views import ChatThreadMessagesView
    return ChatThreadMessagesView.as_view()


def _chat_thread_detail_view():
    from api.v1.chat.views import ChatThreadDetailView
    return ChatThreadDetailView.as_view()


def _chat_feedback_detail_view():
    from api.v1.chat.views import ChatFeedbackDetailView
    return ChatFeedbackDetailView.as_view()


def _chat_rephrasing_suggestions_view():
    from api.v1.chat.views import ChatRephrasingSuggestionsView
    return ChatRephrasingSuggestionsView.as_view()


# --------------------
# Documents
# --------------------
def _documents_list_view():
    from api.v1.documents.views import DocumentListView
    return DocumentListView.as_view()


def _documents_search_view():
    # Implemented in views_read.py and re-exported in api.v1.documents.views (your setup)
    from api.v1.documents.views import DocumentsSearchView
    return DocumentsSearchView.as_view()


def _documents_detail_view():
    from api.v1.documents.views import DocumentDetailView
    return DocumentDetailView.as_view()


def _documents_ingest_view():
    from api.v1.documents.views import DocumentIngestView
    return DocumentIngestView.as_view()


def _documents_upload_view():
    from api.v1.documents.views import DocumentUploadView
    return DocumentUploadView.as_view()


def _documents_audio_upload_view():
    from api.v1.documents.views import DocumentAudioUploadView
    return DocumentAudioUploadView.as_view()


def _documents_delete_view():
    from api.v1.documents.views import DocumentDeleteView
    return DocumentDeleteView.as_view()


def _documents_reindex_view():
    from api.v1.documents.views import DocumentReindexView
    return DocumentReindexView.as_view()


def _reindex_job_detail_view():
    from api.v1.ragspaces.views import RagSpaceReindexJobDetailView
    return RagSpaceReindexJobDetailView.as_view()


def _reindex_job_retry_failed_view():
    from api.v1.ragspaces.views import ReindexJobRetryFailedView
    return ReindexJobRetryFailedView.as_view()


def _documents_download_view():
    from api.v1.documents.views import DocumentDownloadView
    return DocumentDownloadView.as_view()


def _documents_tabular_preview_view():
    from api.v1.documents.views_preview import DocumentTabularPreviewView
    return DocumentTabularPreviewView.as_view()


def _documents_structured_preview_view():
    from api.v1.documents.views_document_preview import DocumentStructuredPreviewView
    return DocumentStructuredPreviewView.as_view()


def _documents_presentation_preview_view():
    from api.v1.documents.views_presentation_preview import (
        DocumentPresentationPreviewView,
    )
    return DocumentPresentationPreviewView.as_view()


def _documents_summary_view():
    from api.v1.documents.views import DocumentSummaryView
    return DocumentSummaryView.as_view()


# --------------------
# Capabilities (global, non sensible)
# --------------------
def _capabilities_view():
    from api.v1.capabilities.views import CapabilitiesView
    return CapabilitiesView.as_view()



def _model_catalog_view():
    from api.v1.model_catalog.views import ModelCatalogView
    return ModelCatalogView.as_view()


def _thread_analyses_view():
    from api.v1.analyses.views import ThreadAnalysesView
    return ThreadAnalysesView.as_view()


def _tabular_query_view():
    from api.v1.tabular.views import TabularQueryView
    return TabularQueryView.as_view()


def _tabular_deployment_view():
    from api.v1.tabular.views import TabularDeploymentView
    return TabularDeploymentView.as_view()


def _tabular_capabilities_view():
    from api.v1.tabular.views import TabularCapabilitiesView
    return TabularCapabilitiesView.as_view()


urlpatterns = [
    # ====================
    # Moteur tabulaire déterministe (Excel / CSV)
    # ====================
    path("tabular/query/", _tabular_query_view(), name="tabular-query"),
    path("tabular/capabilities/", _tabular_capabilities_view(),
         name="tabular-capabilities"),
    # Diagnostic de déploiement (admin) : capacité réelle, workers, réplicas, mémoire.
    path("tabular/deployment/", _tabular_deployment_view(), name="tabular-deployment"),

    # ====================
    # Chat
    # ====================
    path("chat/", _chat_view(), name="chat"),
    path("chat/stream/", _chat_stream_view(), name="chat-stream"),
    path("chat/threads/", _chat_threads_view(), name="chat-threads"),
    # Recherche des conversations de l'utilisateur (titre + contenu), multi-espaces.
    # Avant `<uuid:thread_id>/` par lisibilité (le convertisseur uuid n'y entre pas).
    path("chat/threads/search/", _chat_thread_search_view(), name="chat-threads-search"),
    path(
        "chat/threads/<uuid:thread_id>/messages/",
        _chat_thread_messages_view(),
        name="chat-thread-messages",
    ),
    path(
        "chat/threads/<uuid:thread_id>/rephrasing-suggestions/",
        _chat_rephrasing_suggestions_view(),
        name="chat-thread-rephrasing-suggestions",
    ),
    # Analyses globales lancées depuis ce fil (reprise après rechargement) — L4.
    path(
        "chat/threads/<uuid:thread_id>/analyses/",
        _thread_analyses_view(),
        name="chat-thread-analyses",
    ),
    path(
        "chat/threads/<uuid:thread_id>/",
        _chat_thread_detail_view(),
        name="chat-thread-detail",
    ),

    # ✅ Feedback (canonical)
    path("feedback/", ChatFeedbackView.as_view(), name="chat-feedback"),

    # ✅ Feedback (compat alias for frontend calling /chat/feedback/)
    path("chat/feedback/", ChatFeedbackView.as_view(), name="chat-feedback-alias"),

    # Correction EXPLICITE d'un feedback existant (contrepartie du 409 sur POST).
    path(
        "chat/feedback/<uuid:feedback_id>/",
        _chat_feedback_detail_view(),
        name="chat-feedback-detail",
    ),

    # ====================
    # Documents
    # ====================
    path("documents/", _documents_list_view(), name="documents-list"),

    # ✅ NEW: listing/search documents mentioning a term (Nordexia, etc.)
    path("documents/search/", _documents_search_view(), name="documents-search"),

    path("documents/<uuid:document_id>/", _documents_detail_view(), name="documents-detail"),
    path("documents/ingest/", _documents_ingest_view(), name="documents-ingest"),
    path("documents/upload/", _documents_upload_view(), name="documents-upload"),
    # chemin RÉSERVÉ à l'audio (corps jusqu'à 600 Mo au proxy).
    path("documents/upload-audio/", _documents_audio_upload_view(), name="documents-upload-audio"),
    path("documents/<uuid:document_id>/delete/", _documents_delete_view(), name="documents-delete"),
    path("documents/<uuid:document_id>/reindex/", _documents_reindex_view(), name="documents-reindex"),
    # Jobs de réindexation durable (consultation + relance des échecs)
    path("reindex-jobs/<uuid:job_id>/", _reindex_job_detail_view(), name="reindex-job-detail"),
    path(
        "reindex-jobs/<uuid:job_id>/retry-failed/",
        _reindex_job_retry_failed_view(),
        name="reindex-job-retry-failed",
    ),
    path("documents/<uuid:document_id>/download/", _documents_download_view(), name="documents-download"),
    # Aperçu tabulaire borné d'un classeur .xlsx (feuille + plage citées).
    # Endpoint DÉDIÉ : `/download/` sert des octets bruts avec sa propre
    # sécurisation, y greffer un rendu JSON mêlerait deux modèles de menace.
    path(
        "documents/<uuid:document_id>/tabular-preview/",
        _documents_tabular_preview_view(),
        name="documents-tabular-preview",
    ),
    # Aperçu documentaire structuré d'un .docx (titres, paragraphes, listes
    # aplaties, tableaux préformatés). Contrat DISTINCT du tabulaire : blocs et
    # offsets n'ont rien de commun avec feuilles et plages.
    path(
        "documents/<uuid:document_id>/document-preview/",
        _documents_structured_preview_view(),
        name="documents-document-preview",
    ),
    # Aperçu d'une présentation .pptx : navigation par diapositive, blocs
    # textuels, tableaux préformatés. Contrat DISTINCT du tabulaire et du
    # documentaire — une diapositive n'est ni une feuille ni un flux de blocs.
    path(
        "documents/<uuid:document_id>/presentation-preview/",
        _documents_presentation_preview_view(),
        name="documents-presentation-preview",
    ),
    path("documents/<uuid:document_id>/summary/", _documents_summary_view(), name="documents-summary"),

    # Documents de CORPUS MÉTIER (lecture côté consommateur). Route délibérément
    # distincte de `documents/` : les deux familles ont des espaces d'identifiants
    # disjoints, et aucune vue privée ne doit accepter un identifiant de corpus.
    path("", include("api.v1.corpus_documents.urls")),

    # ====================
    # Other APIs
    # ====================
    path("capabilities/", _capabilities_view(), name="capabilities"),
    path("ai/model-catalog/", _model_catalog_view(), name="model-catalog"),
    path("prompts/", include("api.v1.prompts.urls")),
    path("auth/", include("api.v1.auth.urls")),
    # Préférences de l'utilisateur courant (langue d'interface).
    path("me/", include("api.v1.me.urls")),
    path("workspaces/", include("api.v1.workspaces.urls")),
    # Acceptation publique d'invitation (token = crédence, cf. api/v1/invitations)
    path("invitations/", include("api.v1.invitations.urls")),
    path("rag-spaces/", include("api.v1.ragspaces.urls")),

    # Analyses globales de corpus (L2) — consultation et annulation.
    path("analyses/", include("api.v1.analyses.urls")),
    path("bootstrap/", include("api.v1.bootstrap.urls")),
    path("audit/", include("api.v1.audit.urls")),

    # RagATM_Core — exposition HTTP de la façade interne (PR 3).
    # Récupération de passages SANS génération ; la génération reste à /chat.
    path("core/", include("api.v1.core.urls")),

    # Admin RagATM_Core — diagnostic LECTURE SEULE (PR-A, superuser only).
    # Placé avant l'include "admin/" générique pour un matching explicite.
    path("admin/core-config/", include("api.v1.admin_core_config.urls")),

    # Admin global corpus métier (réservé au propriétaire global ragATM)
    path("admin/", include("api.v1.admin_corpus.urls")),

    # Console d'administration PLATEFORME (superuser only) — gouvernance des
    # users/workspaces/memberships/owner-scopes, SANS accès au contenu.
    path("platform/", include("api.v1.platform_admin.urls")),
]


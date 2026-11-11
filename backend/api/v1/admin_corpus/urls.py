# backend/api/v1/admin_corpus/urls.py
"""Routes admin globales corpus métier. Toutes protégées par IsRagATMOwner (vues).

Montées sous ``/api/v1/admin/`` (cf. api.v1.urls). Imports de vues paresseux,
cohérents avec le reste du projet.
"""
from django.urls import path


def _v(name):
    def _factory():
        from api.v1.admin_corpus import views

        return getattr(views, name).as_view()

    return _factory()


urlpatterns = [
    # Domaines métier
    path("business-domains/", _v("BusinessDomainListCreateView"), name="admin-business-domains"),
    path("business-domains/<uuid:domain_id>/", _v("BusinessDomainDetailView"), name="admin-business-domain-detail"),

    # Corpus métier
    path("domain-corpora/", _v("DomainCorpusListCreateView"), name="admin-domain-corpora"),
    path("domain-corpora/<uuid:corpus_id>/", _v("DomainCorpusDetailView"), name="admin-domain-corpus-detail"),
    path("domain-corpora/<uuid:corpus_id>/documents/", _v("DomainCorpusDocumentsView"), name="admin-domain-corpus-documents"),
    path("domain-corpora/<uuid:corpus_id>/reindex/", _v("DomainCorpusReindexView"), name="admin-domain-corpus-reindex"),

    # Documents de corpus (par id de document)
    path("domain-corpus-documents/<uuid:document_id>/", _v("DomainCorpusDocumentDetailView"), name="admin-domain-corpus-document-detail"),
    path("domain-corpus-documents/<uuid:document_id>/reindex/", _v("DomainCorpusDocumentReindexView"), name="admin-domain-corpus-document-reindex"),

    # Associations workspace <-> corpus
    path("workspaces/<int:workspace_id>/domain-corpora/", _v("WorkspaceCorpusAccessView"), name="admin-workspace-domain-corpora"),
    path("workspaces/<int:workspace_id>/domain-corpora/<uuid:corpus_id>/", _v("WorkspaceCorpusAccessDeleteView"), name="admin-workspace-domain-corpus-delete"),
]

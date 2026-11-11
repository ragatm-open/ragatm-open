"""Routes de lecture des documents de corpus métier (côté consommateur).

Préfixe distinct de ``documents/`` : les deux familles de documents ont deux
espaces de noms d'identifiants disjoints, et le choix de la route est ce qui
matérialise ce fait. Distinct aussi de ``admin/domain-corpus-documents/``, qui est
réservé au propriétaire global (gestion du corpus) — ici, on ne fait que lire.
"""
from django.urls import path


def _summary_view():
    from api.v1.corpus_documents.views import DomainCorpusDocumentSummaryView

    return DomainCorpusDocumentSummaryView.as_view()


urlpatterns = [
    path(
        "domain-corpus-documents/<uuid:document_id>/summary/",
        _summary_view(),
        name="domain-corpus-document-summary",
    ),
]

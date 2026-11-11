from django.urls import path
from .views import LlmCostSummaryView, RagQueryLogDetailView, RagQueryLogListView

urlpatterns = [
    path("rag-queries/", RagQueryLogListView.as_view(), name="audit-rag-queries"),
    path("rag-queries/<uuid:log_id>/", RagQueryLogDetailView.as_view(), name="audit-rag-query-detail"),
    path("llm-costs/", LlmCostSummaryView.as_view(), name="audit-llm-costs"),
]

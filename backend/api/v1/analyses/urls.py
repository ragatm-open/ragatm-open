# backend/api/v1/analyses/urls.py
from django.urls import path


def _detail_view():
    from api.v1.analyses.views import AnalysisDetailView
    return AnalysisDetailView.as_view()


def _cancel_view():
    from api.v1.analyses.views import AnalysisCancelView
    return AnalysisCancelView.as_view()


urlpatterns = [
    path("<uuid:analysis_id>/", _detail_view(), name="analyses-detail"),
    path("<uuid:analysis_id>/cancel/", _cancel_view(), name="analyses-cancel"),
]

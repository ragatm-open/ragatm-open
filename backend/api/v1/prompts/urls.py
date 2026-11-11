from django.urls import path
from .views import PromptTemplateListCreateView, PromptTemplateDetailView, PromptTemplateActivateView

urlpatterns = [
    path("", PromptTemplateListCreateView.as_view(), name="prompts-list-create"),
    path("<uuid:prompt_id>/", PromptTemplateDetailView.as_view(), name="prompts-detail"),
    path("<uuid:prompt_id>/activate/", PromptTemplateActivateView.as_view(), name="prompts-activate"),
]

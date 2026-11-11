"""Priorité et collisions explicites (audit §13)."""
from __future__ import annotations

from apps.actions.router import ActionRouter

router = ActionRouter()


def test_summary_beats_listing():
    # « Résume la liste ... » : résumé prioritaire sur listing (comme aujourd'hui
    # où la pré-détection résumé du frontend court-circuite tout).
    plan = router.route("Résume la liste des articles sur Nordexia")
    assert plan.action == "document.summarize"
    assert "search.list_documents" in plan.candidates


def test_listing_beats_news_collision():
    # « Liste les dernières actualités sur Nordexia » : listing gagne (parité : la vue
    # tranche avant le service). News reste exposé comme candidat.
    plan = router.route("Liste les dernières actualités sur Nordexia")
    assert plan.action == "search.list_documents"
    assert "search.news" in plan.candidates


def test_pure_question_is_answer_with_no_candidates():
    plan = router.route("Quelle est la date d'échéance ?")
    assert plan.action == "search.answer"
    assert plan.candidates == ()

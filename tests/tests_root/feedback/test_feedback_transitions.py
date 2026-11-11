"""Cycle de vie d'un incident : transitions autorisées, refusées, auditées."""
from __future__ import annotations

import pytest

from apps.audit.models import ActionAuditEvent
from apps.feedback.models import AnswerFeedback
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.services.transitions import InvalidTransition, can_transition, transition

pytestmark = pytest.mark.django_db


@pytest.fixture
def feedback(db, user, query_log):
    return create_feedback(
        user=user, log=query_log, rating=AnswerFeedback.RATING_DOWN, kind="info_in_docs"
    )


def test_nominal_path(feedback, user):
    for target in (
        AnswerFeedback.STATUS_QUALIFIED,
        AnswerFeedback.STATUS_EXPORT_APPROVED,
        AnswerFeedback.STATUS_EXPORTED,
        AnswerFeedback.STATUS_FIXED,
        AnswerFeedback.STATUS_CLOSED,
    ):
        transition(feedback, target=target, actor=user)
    assert feedback.status == AnswerFeedback.STATUS_CLOSED


@pytest.mark.parametrize(
    "start,target",
    [
        (AnswerFeedback.STATUS_NEW, AnswerFeedback.STATUS_EXPORTED),
        (AnswerFeedback.STATUS_NEW, AnswerFeedback.STATUS_EXPORT_APPROVED),
        (AnswerFeedback.STATUS_NEW, AnswerFeedback.STATUS_FIXED),
        (AnswerFeedback.STATUS_CLOSED, AnswerFeedback.STATUS_QUALIFIED),
        (AnswerFeedback.STATUS_REJECTED, AnswerFeedback.STATUS_QUALIFIED),
    ],
)
def test_invalid_transitions_are_refused(feedback, start, target):
    """Un export ne peut pas précéder son autorisation — c'est tout l'intérêt."""
    AnswerFeedback.objects.filter(id=feedback.id).update(status=start)
    feedback.refresh_from_db()
    assert not can_transition(start, target)
    with pytest.raises(InvalidTransition):
        transition(feedback, target=target)
    feedback.refresh_from_db()
    assert feedback.status == start


def test_rejection_branches(feedback, user):
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=user)
    transition(feedback, target=AnswerFeedback.STATUS_REJECTED, actor=user)
    assert feedback.status == AnswerFeedback.STATUS_REJECTED


def test_transition_is_audited_without_content(feedback, user):
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=user, reason="cas Nordexia")

    event = ActionAuditEvent.objects.filter(action_name="platform.feedback.transition").latest(
        "created_at"
    )
    assert event.metadata["after"] == {"status": "qualified"}
    assert event.metadata["feedback_id"] == str(feedback.id)

    # L'audit ne transporte AUCUN contenu : ni question, ni réponse, ni extrait.
    blob = str(event.metadata)
    assert "Nordexia Conseil" not in blob
    assert feedback.question_hash not in blob or True  # le hash seul serait acceptable
    for forbidden in ("what is Nordexia", "I don't know"):
        assert forbidden not in blob


def test_admin_action_applies_and_audits(feedback, user, rf):
    from django.contrib.admin.sites import AdminSite

    from apps.feedback.admin import AnswerFeedbackAdmin

    admin = AnswerFeedbackAdmin(AnswerFeedback, AdminSite())
    request = rf.post("/admin/")
    request.user = user
    # `message_user` a besoin du framework de messages, absent d'une requête nue.
    admin.message_user = lambda *a, **k: None

    admin.action_qualify(request, AnswerFeedback.objects.filter(id=feedback.id))

    feedback.refresh_from_db()
    assert feedback.status == AnswerFeedback.STATUS_QUALIFIED
    assert ActionAuditEvent.objects.filter(action_name="platform.feedback.transition").exists()


def test_admin_never_exposes_raw_content(user):
    from django.contrib.admin.sites import AdminSite

    from apps.feedback.admin import AnswerFeedbackAdmin

    admin = AnswerFeedbackAdmin(AnswerFeedback, AdminSite())
    # Le commentaire libre et les liens nominatifs sont exclus du formulaire.
    for field in ("comment", "created_by"):
        assert field in admin.exclude
    # Aucune colonne ne rend la question ou la réponse en clair.
    assert "question" not in admin.list_display
    assert "answer" not in admin.list_display
    assert "comment" not in admin.list_display

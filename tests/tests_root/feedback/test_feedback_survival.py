"""INVARIANT P0 — un incident survit à la disparition de tout ce qui l'entoure.

C'est le défaut que ce lot corrige. Le signal utilisateur vivait dans trois
colonnes de ``RagQueryLog``, dont la clé étrangère vers ``RagSpace`` est en
``CASCADE`` : supprimer un espace détruisait la requête ET le signalement qui
disait qu'elle avait mal tourné. Or c'est souvent après la suppression de
l'espace qu'on cherche à comprendre.

Chaque test vérifie DEUX choses, et les deux comptent :

1. la ligne ``AnswerFeedback`` existe toujours ;
2. les identifiants HISTORIQUES sont intacts — un feedback qui survit sans
   savoir de quel espace, de quel modèle et de quelle question il parle ne
   servirait à rien.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.audit.models import RagQueryLog
from apps.chat.models import ChatMessage, ChatThread
from apps.feedback.models import AnswerFeedback
from apps.feedback.services.create_feedback import create_feedback
from apps.rag_core.models import Document, RagSpace
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def incident(db, user, workspace, rag_space, query_log):
    """Un feedback complet, rattaché à un fil et un message réels."""
    thread = ChatThread.objects.create(user=user, rag_space=rag_space, title="t")
    message = ChatMessage.objects.create(
        thread=thread, role="assistant", content="I don't know.", rag_query_log_id=query_log.id
    )
    feedback = create_feedback(
        user=user,
        log=query_log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        expected_term="Nordexia Conseil",
        message_id=message.id,
        thread_id=thread.id,
    )
    return {
        "feedback": feedback,
        "thread": thread,
        "message": message,
        "log": query_log,
        "snapshot": {
            "rag_space_id_hist": feedback.rag_space_id_hist,
            "workspace_id_hist": feedback.workspace_id_hist,
            "query_log_id_hist": feedback.query_log_id_hist,
            "question_hash": feedback.question_hash,
            "llm_model_hist": feedback.llm_model_hist,
            "policy_hist": feedback.policy_hist,
            "message_id": feedback.message_id,
            "thread_id": feedback.thread_id,
        },
    }


def _assert_history_intact(feedback: AnswerFeedback, expected: dict) -> None:
    for field, value in expected.items():
        assert getattr(feedback, field) == value, f"identifiant historique perdu : {field}"


def test_survives_rag_space_deletion(incident, rag_space):
    RagSpace.objects.filter(id=rag_space.id).delete()

    assert not RagQueryLog.objects.filter(id=incident["log"].id).exists()
    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    # Le lien VIVANT s'efface ; l'histoire, non.
    assert fb.query_log_id is None
    _assert_history_intact(fb, incident["snapshot"])
    assert fb.expected_term == "Nordexia Conseil"


def test_survives_permanent_deletion_service(incident, rag_space, user):
    """Le service de purge RÉEL (fichiers + base), pas seulement la cascade SQL."""
    from apps.rag_core.services.rag_space_deletion import delete_rag_space_permanently

    delete_rag_space_permanently(rag_space_id=str(rag_space.id), actor=user)

    assert not RagSpace.objects.filter(id=rag_space.id).exists()
    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    assert fb.query_log_id is None
    _assert_history_intact(fb, incident["snapshot"])


def test_survives_workspace_deletion(incident, workspace):
    Workspace.objects.filter(id=workspace.id).delete()

    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    assert fb.query_log_id is None
    _assert_history_intact(fb, incident["snapshot"])


def test_survives_user_deletion(incident, user):
    pseudonym = incident["feedback"].user_pseudonym
    get_user_model().objects.filter(pk=user.pk).delete()

    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    assert fb.created_by_id is None
    # Le pseudonyme demeure : « plusieurs utilisateurs signalent le même cas »
    # reste calculable après la suppression des comptes.
    assert fb.user_pseudonym == pseudonym
    _assert_history_intact(fb, incident["snapshot"])


def test_survives_thread_and_message_deletion(incident):
    ChatThread.objects.filter(id=incident["thread"].id).delete()

    assert not ChatMessage.objects.filter(id=incident["message"].id).exists()
    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    _assert_history_intact(fb, incident["snapshot"])


def test_survives_query_log_purge(incident):
    """Purge de rétention d'audit : le signal ne doit pas partir avec le log."""
    RagQueryLog.objects.filter(id=incident["log"].id).delete()

    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    assert fb.query_log_id is None
    _assert_history_intact(fb, incident["snapshot"])


def test_survives_document_deletion(incident, rag_space):
    doc = Document.objects.create(rag_space=rag_space, filename="note-interne-2023.pdf")
    Document.objects.filter(id=doc.id).delete()

    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    _assert_history_intact(fb, incident["snapshot"])


def test_unique_constraint_released_after_log_purge(incident, user, workspace, rag_space):
    """Après la purge du log, la contrainte d'unicité ne bloque plus.

    Sans la condition partielle sur ``query_log`` non nul, deux feedbacks
    orphelins du même utilisateur entreraient en collision — et la purge d'audit
    ferait échouer des écritures sans rapport avec elle.
    """
    RagQueryLog.objects.filter(id=incident["log"].id).delete()

    new_log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="q2", status="ok"
    )
    create_feedback(user=user, log=new_log, rating=AnswerFeedback.RATING_DOWN)
    RagQueryLog.objects.filter(id=new_log.id).delete()

    assert AnswerFeedback.objects.filter(created_by=user, query_log__isnull=True).count() == 2


def test_historic_fields_cannot_be_rewritten(incident):
    """Une donnée « historique » réinscriptible ne serait plus une preuve."""
    fb = AnswerFeedback.objects.get(id=incident["feedback"].id)
    fb.rag_space_id_hist = None
    with pytest.raises(ValueError, match="non modifiables"):
        fb.save()

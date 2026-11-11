"""Désactiver le diagnostic doit VRAIMENT le désactiver.

Avant cette garde, un clic produisait un `FeedbackDiagnosticSnapshot` en mode
`partial` même avec `BUFFER=0` et `REPLAY=0` — donc une seconde copie de la
question, de la réponse et des extraits, conservée 30 jours. Quatre problèmes
d'un coup : surprise opérateur, collecte supérieure à l'annonce, duplication de
contenu conversationnel, et rollback du lot 2 incomplet.

Constaté en smoke test PRODUCTION du 2026-07-28.

La matrice ci-dessous est le contrat. `CAPTURE_PROMPT` n'y figure pas : il ne
gouverne pas l'EXISTENCE du snapshot, seulement son contenu sensible.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.audit.models import RagQueryLog
from apps.feedback.diagnostics.policy import diagnostics_enabled
from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.tasks import capture_feedback_snapshot_task

pytestmark = pytest.mark.django_db

OFF = dict(
    RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=False,
    RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=False,
)


# ---------------------------------------------------------------------------
# La matrice
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "buffer_on,replay_on,expected",
    [
        (False, False, False),  # aucun mécanisme -> aucun snapshot
        (True, False, True),    # capture exacte
        (False, True, True),    # replay seul
        (True, True, True),     # les deux : priorité au tampon
    ],
)
def test_matrix(buffer_on, replay_on, expected):
    with override_settings(
        RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=buffer_on,
        RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=replay_on,
    ):
        assert diagnostics_enabled() is expected


@pytest.mark.parametrize("prompt_on", [True, False])
def test_capture_prompt_never_decides_snapshot_existence(prompt_on):
    """`CAPTURE_PROMPT` ne contrôle que le contenu, jamais l'existence."""
    with override_settings(
        RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=prompt_on, **OFF
    ):
        assert diagnostics_enabled() is False


# ---------------------------------------------------------------------------
# Producteur : aucune tâche enfilée
# ---------------------------------------------------------------------------
@override_settings(**OFF)
def test_no_task_enqueued_when_disabled(
    user, query_log, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.feedback import tasks as tasks_module

    calls = []
    monkeypatch.setattr(
        tasks_module.capture_feedback_snapshot_task, "delay",
        lambda *a, **k: calls.append(a),
    )

    # `transaction.on_commit` ne s'exécute pas dans la transaction de test :
    # sans cette capture, l'assertion serait vide de sens.
    with django_capture_on_commit_callbacks(execute=True):
        feedback = create_feedback(user=user, log=query_log, rating=-1, kind="info_in_docs")

    assert calls == [], "aucune tâche ne doit être enfilée"
    assert AnswerFeedback.objects.filter(id=feedback.id).exists(), "le feedback existe"
    assert not FeedbackDiagnosticSnapshot.objects.exists(), "aucun snapshot"


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True,
                   RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=False)
def test_task_enqueued_when_buffer_enabled(
    user, query_log, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.feedback import tasks as tasks_module

    calls = []
    monkeypatch.setattr(
        tasks_module.capture_feedback_snapshot_task, "delay",
        lambda *a, **k: calls.append(a),
    )
    with django_capture_on_commit_callbacks(execute=True):
        create_feedback(user=user, log=query_log, rating=-1)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Worker : la garde qui tient l'invariant
# ---------------------------------------------------------------------------
@override_settings(**OFF)
def test_direct_task_call_is_refused(user, query_log):
    """Appel DIRECT de la tâche : elle doit refuser elle-même."""
    feedback = create_feedback(user=user, log=query_log, rating=-1)

    result = capture_feedback_snapshot_task(str(feedback.id))

    assert result["status"] == "diagnostics_disabled"
    assert result["feedback_id"] == str(feedback.id)
    assert not FeedbackDiagnosticSnapshot.objects.exists()


def test_flag_switched_off_between_enqueue_and_execution(user, query_log):
    """Bascule de configuration pendant qu'une tâche attend en file.

    C'est le cas que la garde du producteur ne peut pas couvrir : au moment de
    l'enfilement le diagnostic était actif, il ne l'est plus à l'exécution.
    """
    with override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True):
        feedback = create_feedback(user=user, log=query_log, rating=-1)

    with override_settings(**OFF):
        result = capture_feedback_snapshot_task(str(feedback.id))

    assert result["status"] == "diagnostics_disabled"
    assert not FeedbackDiagnosticSnapshot.objects.exists()


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_buffer_enabled_still_produces_a_snapshot(user, query_log):
    feedback = create_feedback(user=user, log=query_log, rating=-1)
    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["status"] == "captured"
    assert FeedbackDiagnosticSnapshot.objects.filter(feedback=feedback).exists()


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=False,
                   RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=True)
def test_replay_enabled_still_produces_a_snapshot(user, query_log):
    feedback = create_feedback(user=user, log=query_log, rating=-1)
    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["status"] == "captured"
    snap = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    # Sans corpus rejouable ici, le mode retombe sur `partial` — mais il EXISTE,
    # parce qu'une capacité diagnostique est explicitement activée.
    assert snap.capture_mode in ("replay", "partial")


@override_settings(**OFF)
def test_skip_is_logged_without_any_content(user, query_log, caplog):
    import logging

    caplog.set_level(logging.INFO)
    feedback = create_feedback(user=user, log=query_log, rating=-1, kind="info_in_docs")
    capture_feedback_snapshot_task(str(feedback.id))

    messages = " ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("apps.feedback")
    )
    assert "feedback_snapshot_skipped" in messages
    assert "reason=diagnostics_disabled" in messages
    # Aucun contenu : ni question, ni réponse, ni terme déclaré.
    for forbidden in ("Nordexia", "I don't know", "note-interne-2023.pdf"):
        assert forbidden not in messages


# ---------------------------------------------------------------------------
# Non-régression : la désactivation ne casse rien d'autre
# ---------------------------------------------------------------------------
@override_settings(**OFF)
def test_duplicate_still_returns_409(api_client, query_log):
    first = api_client.post(
        "/api/v1/chat/feedback/", {"log_id": str(query_log.id), "score": -1}, format="json"
    )
    assert first.status_code == 200
    second = api_client.post(
        "/api/v1/chat/feedback/", {"log_id": str(query_log.id), "score": 1}, format="json"
    )
    assert second.status_code == 409


@override_settings(**OFF)
def test_dual_write_still_maintained(api_client, query_log):
    api_client.post(
        "/api/v1/chat/feedback/",
        {"log_id": str(query_log.id), "score": -1, "kind": "wrong_answer"},
        format="json",
    )
    query_log.refresh_from_db()
    assert query_log.feedback_score == -1
    assert query_log.feedback_reason == "incorrect"


@override_settings(**OFF)
def test_feedback_still_survives_ragspace_deletion(user, query_log, rag_space):
    from apps.rag_core.models import RagSpace

    feedback = create_feedback(user=user, log=query_log, rating=-1, kind="info_in_docs")
    space_hist = feedback.rag_space_id_hist
    question_hash = feedback.question_hash

    RagSpace.objects.filter(id=rag_space.id).delete()

    feedback.refresh_from_db()
    assert feedback.query_log_id is None
    assert feedback.rag_space_id_hist == space_hist
    assert feedback.question_hash == question_hash


def test_existing_snapshots_are_untouched_by_disabling(user, query_log):
    """Désactiver n'efface RIEN : les snapshots antérieurs vivent jusqu'à leur purge."""
    with override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True):
        feedback = create_feedback(user=user, log=query_log, rating=-1)
        capture_feedback_snapshot_task(str(feedback.id))
    snapshot_id = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback).id

    with override_settings(**OFF):
        assert FeedbackDiagnosticSnapshot.objects.filter(id=snapshot_id).exists()
        # Et la purge de rétention continue de fonctionner normalement.
        from apps.feedback.retention import purge_expired_snapshots

        assert purge_expired_snapshots(apply=False)["eligible"] == 0


@override_settings(**OFF)
def test_export_of_an_existing_incident_still_works(user, query_log, rag_space):
    """Un incident déjà capturé reste exportable après désactivation."""
    from django.contrib.auth import get_user_model

    from apps.feedback.services.export_service import approve_export, generate_package
    from apps.feedback.services.transitions import transition

    with override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True):
        feedback = create_feedback(user=user, log=query_log, rating=-1)
        capture_feedback_snapshot_task(str(feedback.id))

    su = get_user_model().objects.create_user(
        username="root_gate", password="pw", email="rg@e.com", is_superuser=True, is_staff=True
    )
    feedback.refresh_from_db()
    transition(feedback, target=AnswerFeedback.STATUS_QUALIFIED, actor=su)

    with override_settings(
        FEEDBACK_INCIDENT_EXPORT_ENABLED=True,
        FEEDBACK_INCIDENT_EXPORT_KEY="U4DzV1ONux1P7TEAq1XUUbqcAX_8P7ReJ-lDAZXGqaI=",
        **OFF,
    ):
        approve_export(feedback, level_value=1, user=su)
        result = generate_package(feedback, level_value=1, user=su)

    assert result["payload"], "l'export d'un incident existant reste possible"

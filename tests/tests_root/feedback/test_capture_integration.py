"""Bout en bout RÉEL : pipeline de retrieval -> tampon -> clic -> snapshot.

C'est le test qui prouve le lot. Les précédents vérifient des morceaux avec des
traces fabriquées ; celui-ci passe par le vrai ``query_rag``, sur un vrai corpus
indexé, et vérifie que la trace capturée décrit bien l'exécution qui a eu lieu.

Il vérifie aussi ce qui compte autant : le pipeline n'est PAS modifié par
l'observation, et le drapeau à 0 ne laisse aucune trace.
"""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.audit.models import RagQueryLog
from apps.feedback.diagnostics import buffer
from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.tasks import capture_feedback_snapshot_task
from apps.rag_core.services.query.orchestrator import query_rag

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus(rag_space, ingest_text):
    """Un corpus minimal où le terme cherché EXISTE — le cas Nordexia."""
    ingest_text(
        rag_space,
        "Nordexia Conseil est un cabinet de conseil en organisation "
        "qui accompagne des fondations et des fonds de dotation.",
        "nordexia.txt",
    )
    ingest_text(
        rag_space,
        "Un Multi-office LP désigne un limited partner présent dans plusieurs "
        "bureaux régionaux du groupe.",
        "multi-office.txt",
    )
    ingest_text(rag_space, "Note de synthèse trimestrielle sans rapport.", "autre.txt")
    return rag_space


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_real_pipeline_writes_a_usable_trace(corpus, user):
    result = query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    log_id = result["log_id"]
    assert log_id, "le journal doit exister pour que la trace soit rattachable"

    trace = buffer.read_trace(log_id)
    assert trace is not None, "la trace doit être capturée pendant l'exécution"
    assert trace["log_id"] == str(log_id)
    assert trace["candidates"], "le pool de candidats doit être capturé"

    # Chaque candidat porte de quoi diagnostiquer : rang, score, sort.
    first = trace["candidates"][0]
    assert set(first) >= {
        "chunk_id",
        "document_id",
        "leg",
        "rank_fused",
        "score",
        "selected",
        "excluded_by",
    }
    assert first["rank_fused"] == 1

    # Les seuils appliqués sont figés avec la trace : un refus dû à un seuil mal
    # calibré doit être distinguable d'un corpus qui ne contient pas la réponse.
    assert "min_score" in trace["selection"]["thresholds"]
    assert trace["selection"]["answerability_policy"]

    # Confidentialité : la trace ne porte AUCUN contenu.
    import json

    blob = json.dumps(trace)
    for forbidden in ("Nordexia Conseil", "conseil en organisation", "nordexia.txt", ".txt"):
        assert forbidden not in blob


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_full_journey_from_click_to_snapshot(corpus, user):
    """Le parcours complet : réponse -> pouce bas -> snapshot exact."""
    result = query_rag(
        rag_space_id=corpus.id,
        question="what is a Multi-office LP?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    log = RagQueryLog.objects.get(id=result["log_id"])

    feedback = create_feedback(
        user=user,
        log=log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        expected_document="multi-office.txt",
        expected_term="Multi-office LP",
    )
    capture_feedback_snapshot_task(str(feedback.id))

    snapshot = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert snapshot.capture_mode == FeedbackDiagnosticSnapshot.MODE_BUFFER
    assert snapshot.is_exact_capture is True
    assert snapshot.retrieval_trace["candidates"]

    # La question que l'existant ne savait PAS trancher : le bon chunk
    # était-il seulement candidat ?
    candidate_docs = {c["document_id"] for c in snapshot.retrieval_trace["candidates"]}
    assert candidate_docs, "les documents candidats sont désormais connus"

    # Le terme déclaré est vérifié en base : la priorité monte parce que le fait
    # est établi, pas parce que l'utilisateur l'a affirmé.
    feedback.refresh_from_db()
    assert feedback.priority_score >= 3


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_abstention_is_captured_too(rag_space, user, ingest_text):
    """« Je ne sais pas » est précisément le cas à diagnostiquer."""
    ingest_text(rag_space, "Contenu totalement hors sujet sur la météo.", "meteo.txt")

    result = query_rag(
        rag_space_id=rag_space.id,
        question="quel est le chiffre d'affaires consolidé du groupe ?",
        user=user,
        rag_space=rag_space,
        entrypoint="chat_sync",
    )
    trace = buffer.read_trace(result["log_id"])
    assert trace is not None, "une abstention doit produire une trace, même vide"
    # Une trace vide n'est pas une trace inutile : elle oriente vers les seuils
    # plutôt que vers la génération.
    assert "candidates" in trace
    assert trace["selection"]["thresholds"]


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=False)
def test_flag_off_leaves_no_trace(corpus, user):
    result = query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    assert buffer.read_trace(result["log_id"]) is None


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_capture_failure_never_breaks_the_answer(corpus, user, monkeypatch):
    """Une panne de capture ne coûte JAMAIS une réponse à l'utilisateur."""
    from apps.feedback.diagnostics import hook

    def _boom(**kwargs):
        raise RuntimeError("capture cassée")

    monkeypatch.setattr(hook, "capture_retrieval_trace", _boom)

    result = query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=user,
        rag_space=corpus,
        entrypoint="chat_sync",
    )
    assert result["status"] in ("ok", "no_answer")
    assert result["log_id"]


def test_trace_sink_observes_without_persisting(corpus, user):
    """Le ``trace_sink`` sert le replay : il observe, il n'écrit rien.

    Sans utilisateur, aucun ``RagQueryLog`` n'est créé — c'est ce qui permet au
    replay de reconstituer les candidats sans polluer l'audit métier.
    """
    seen: list[dict] = []
    before = RagQueryLog.objects.count()

    query_rag(
        rag_space_id=corpus.id,
        question="what is Nordexia Conseil?",
        user=None,
        rag_space=corpus,
        entrypoint="internal",
        trace_sink=seen.append,
    )

    assert seen, "le sink doit recevoir la trace même sans tampon activé"
    assert seen[0]["candidates"]
    assert RagQueryLog.objects.count() == before, "aucun journal métier ne doit être créé"
    assert buffer.read_trace("") is None

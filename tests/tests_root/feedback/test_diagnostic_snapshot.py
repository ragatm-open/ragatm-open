"""Snapshot diagnostique : modes de capture, immutabilité, bornes, rétention.

Le champ décisif est ``capture_mode``. Un ``replay`` présenté comme un
``buffer`` ferait conclure sur un état qui n'est pas celui de l'incident — c'est
la seule façon de se tromper *avec* des données correctes.
"""
from __future__ import annotations

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import RagQueryLog
from apps.feedback.diagnostics import buffer, snapshot as snap
from apps.feedback.models import AnswerFeedback, FeedbackDiagnosticSnapshot
from apps.feedback.services.create_feedback import create_feedback
from apps.feedback.tasks import capture_feedback_snapshot_task

pytestmark = pytest.mark.django_db


def _trace(log_id: str) -> dict:
    return {
        "schema_version": 1,
        "log_id": log_id,
        "created_at": "2026-07-28T10:00:00+00:00",
        "embedding_model": "nomic-embed-text",
        "pool_size": 8,
        "truncated": False,
        "candidates": [
            {
                "chunk_id": "11111111-1111-1111-1111-111111111111",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "leg": "hybrid",
                "rank_fts": 3,
                "rank_vector": 7,
                "rank_fused": 1,
                "score": 0.71,
                "fusion_score": 0.032,
                "rerank_score": None,
                "selected": True,
                "excluded_by": None,
            },
            {
                "chunk_id": "33333333-3333-3333-3333-333333333333",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "leg": "fts",
                "rank_fts": 11,
                "rank_vector": None,
                "rank_fused": 2,
                "score": 0.05,
                "fusion_score": 0.016,
                "rerank_score": None,
                "selected": False,
                "excluded_by": "diversity_max_per_group",
            },
        ],
        "selection": {
            "selected_chunk_ids": ["11111111-1111-1111-1111-111111111111"],
            "answerability_policy": "no_answer",
            "thresholds": {"min_score": 0.76},
            "budgets": {"top_k": 4},
        },
    }


@pytest.fixture
def feedback(db, user, query_log):
    return create_feedback(
        user=user,
        log=query_log,
        rating=AnswerFeedback.RATING_DOWN,
        kind=AnswerFeedback.KIND_INFO_IN_DOCS,
        expected_term="Nordexia Conseil",
    )


# ---------------------------------------------------------------------------
# Modes de capture
# ---------------------------------------------------------------------------
@override_settings(RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True)
def test_buffer_hit_produces_exact_capture(feedback, query_log):
    buffer.write_trace(query_log.id, _trace(str(query_log.id)))

    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["capture_mode"] == FeedbackDiagnosticSnapshot.MODE_BUFFER
    assert result["candidate_count"] == 2

    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.is_exact_capture is True
    # Le candidat ÉCARTÉ est là — c'est toute la valeur de la capture.
    excluded = [c for c in row.retrieval_trace["candidates"] if not c["selected"]]
    assert excluded[0]["excluded_by"] == "diversity_max_per_group"
    # La trace promue libère le tampon.
    assert buffer.read_trace(query_log.id) is None


@override_settings(
    RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=False
)
def test_buffer_miss_without_replay_is_partial(feedback):
    """Sans tampon ni replay, on livre ce qui reste — et on le DIT."""
    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["capture_mode"] == FeedbackDiagnosticSnapshot.MODE_PARTIAL

    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.is_exact_capture is False
    assert row.retrieval_trace["unavailable_reason"] == "buffer_expired_and_replay_unavailable"
    # Les sources RETENUES restent disponibles via le journal.
    assert row.selected_chunks["sources"]


@override_settings(
    RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=True
)
def test_replay_is_never_labelled_as_a_capture(feedback, query_log, rag_space, ingest_text):
    ingest_text(rag_space, "Nordexia Conseil est un conseil en organisation.", "note.txt")

    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["capture_mode"] == FeedbackDiagnosticSnapshot.MODE_REPLAY

    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.is_exact_capture is False
    assert row.retrieval_trace["replayed_at"]
    assert "pas la capture" in row.retrieval_trace["replay_disclaimer"]
    # Le replay ne crée AUCUN journal métier supplémentaire.
    assert RagQueryLog.objects.count() == 1


@override_settings(
    RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED=True
)
def test_replay_reports_embedding_model_divergence(feedback, query_log, rag_space, ingest_text):
    ingest_text(rag_space, "Nordexia Conseil.", "note.txt")
    rag_space.embedding_model = "un-autre-modele"
    rag_space.save(update_fields=["embedding_model"])

    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.retrieval_trace["embedding_model_at_replay"] == "un-autre-modele"


def test_snapshot_after_log_purge_is_partial(feedback, query_log):
    RagQueryLog.objects.filter(id=query_log.id).delete()
    feedback.refresh_from_db()

    result = capture_feedback_snapshot_task(str(feedback.id))
    assert result["capture_mode"] == FeedbackDiagnosticSnapshot.MODE_PARTIAL
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.request_payload["question_unavailable"] is True


def test_task_is_idempotent(feedback):
    capture_feedback_snapshot_task(str(feedback.id))
    again = capture_feedback_snapshot_task(str(feedback.id))
    assert again["status"] == "already_captured"
    assert FeedbackDiagnosticSnapshot.objects.filter(feedback=feedback).count() == 1


def test_task_on_missing_feedback_is_silent():
    assert capture_feedback_snapshot_task("00000000-0000-0000-0000-000000000000") == {
        "status": "not_found"
    }


# ---------------------------------------------------------------------------
# Immutabilité et bornes
# ---------------------------------------------------------------------------
def test_snapshot_is_append_only(feedback):
    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)

    row.capture_mode = FeedbackDiagnosticSnapshot.MODE_BUFFER
    with pytest.raises(ValueError, match="append-only"):
        row.save()

    with pytest.raises(ValueError, match="suppression unitaire interdite"):
        row.delete()


@override_settings(
    RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=True, RAG_FEEDBACK_DIAGNOSTIC_MAX_SNAPSHOT_BYTES=4096
)
def test_oversized_snapshot_is_truncated_explicitly(feedback, query_log):
    trace = _trace(str(query_log.id))
    trace["candidates"] = [
        {**trace["candidates"][0], "chunk_id": f"c{i}", "padding": "x" * 200} for i in range(200)
    ]
    buffer.write_trace(query_log.id, trace)

    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.truncated is True
    assert row.truncation_reason == "snapshot_max_bytes"
    assert len(row.retrieval_trace["candidates"]) < 200


# ---------------------------------------------------------------------------
# Prompt : hash par défaut, complet seulement si autorisé
# ---------------------------------------------------------------------------
def test_prompt_is_hash_only_by_default(query_log):
    meta = snap.build_generation_metadata(query_log, prompt="PROMPT COMPLET AVEC EXTRAITS")
    assert meta["prompt_capture"] == snap.PROMPT_CAPTURE_HASH_ONLY
    assert "prompt" not in meta


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=True)
def test_prompt_capture_verifies_the_hash(query_log):
    import hashlib

    prompt = "PROMPT COMPLET AVEC EXTRAITS"
    query_log.prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    query_log.save(update_fields=["prompt_hash"])

    meta = snap.build_generation_metadata(query_log, prompt=prompt)
    assert meta["prompt_capture"] == snap.PROMPT_CAPTURE_FULL
    assert meta["prompt_hash_verified"] is True


@override_settings(RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=True)
def test_prompt_hash_mismatch_is_reported(query_log):
    meta = snap.build_generation_metadata(query_log, prompt="un autre prompt")
    # `query_log.prompt_hash` vaut "aaaa..." : la divergence doit être VISIBLE
    # plutôt que de laisser croire à une reproduction fidèle.
    assert meta["prompt_hash_verified"] is False


# ---------------------------------------------------------------------------
# contains_production_content : calculé, jamais déclaré
# ---------------------------------------------------------------------------
def test_contains_production_content_is_computed(feedback, query_log):
    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    # Le journal porte des extraits et un nom de fichier -> contenu de production.
    assert row.contains_production_content is True


def test_contains_production_content_false_without_content(feedback, query_log):
    query_log.sources = []
    query_log.answer = ""
    query_log.question = ""
    query_log.save(update_fields=["sources", "answer", "question"])

    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    assert row.contains_production_content is False


# ---------------------------------------------------------------------------
# Rétention : purger le snapshot ne purge JAMAIS le signal
# ---------------------------------------------------------------------------
def test_purge_removes_snapshot_and_preserves_feedback(feedback):
    from apps.feedback.retention import purge_expired_snapshots

    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)
    FeedbackDiagnosticSnapshot.objects.filter(pk=row.pk).update(
        expires_at=timezone.now() - timezone.timedelta(days=1)
    )

    result = purge_expired_snapshots(apply=True)
    assert result["deleted"] == 1
    assert not FeedbackDiagnosticSnapshot.objects.exists()

    # L'INVARIANT : le signal survit à la purge de son matériel sensible.
    feedback.refresh_from_db()
    assert feedback.id
    assert feedback.priority_score >= 0
    assert feedback.dedup_fingerprint


def test_purge_dry_run_deletes_nothing(feedback):
    from apps.feedback.retention import purge_expired_snapshots

    capture_feedback_snapshot_task(str(feedback.id))
    FeedbackDiagnosticSnapshot.objects.all().update(
        expires_at=timezone.now() - timezone.timedelta(days=1)
    )
    result = purge_expired_snapshots(apply=False)
    assert result["eligible"] == 1 and result["deleted"] == 0
    assert FeedbackDiagnosticSnapshot.objects.count() == 1


def test_retention_extension_requires_a_reason_and_is_audited(feedback, user):
    from apps.audit.models import ActionAuditEvent
    from apps.feedback.retention import extend_retention

    capture_feedback_snapshot_task(str(feedback.id))
    row = FeedbackDiagnosticSnapshot.objects.get(feedback=feedback)

    with pytest.raises(ValueError, match="motif"):
        extend_retention(row, days=60, actor=user, reason="  ")

    with pytest.raises(ValueError, match="plafond"):
        extend_retention(row, days=365, actor=user, reason="investigation longue")

    before = row.expires_at
    extend_retention(row, days=90, actor=user, reason="incident Nordexia en cours")
    row.refresh_from_db()
    assert row.expires_at > before
    assert ActionAuditEvent.objects.filter(
        action_name="platform.feedback.retention_extended"
    ).exists()


def test_deleting_feedback_cascades_the_snapshot(feedback):
    """Un snapshot sans son feedback n'aurait aucun sens — CASCADE dans CE sens."""
    capture_feedback_snapshot_task(str(feedback.id))
    AnswerFeedback.objects.filter(id=feedback.id).delete()
    assert not FeedbackDiagnosticSnapshot.objects.exists()


def test_concurrent_capture_is_idempotent_not_an_error(feedback, monkeypatch):
    """Une course entre deux exécutions ne doit pas produire une tâche en erreur.

    La garde `hasattr(feedback, "diagnostic_snapshot")` lit une relation MISE EN
    CACHE : elle ne voit pas un snapshot créé entre le chargement de l'instance
    et l'écriture. C'est arrivé en qualification staging — le worker réel avait
    déjà capturé, et un second appel partait en `autoretry_for=Exception` pour
    échouer trois fois alors que le travail était fait.

    La contrainte d'unicité reste le vrai garde-fou ; la tâche doit simplement la
    reconnaître au lieu de la propager.
    """
    from django.db import IntegrityError

    from apps.feedback.diagnostics import snapshot as snap_module

    def _race(*args, **kwargs):
        raise IntegrityError(
            'duplicate key value violates unique constraint '
            '"feedback_diagnostic_snapshot_feedback_id_key"'
        )

    monkeypatch.setattr(snap_module, "create_snapshot", _race)

    result = capture_feedback_snapshot_task(str(feedback.id))

    assert result["status"] == "already_captured"
    assert result["feedback_id"] == str(feedback.id)

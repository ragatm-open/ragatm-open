"""API de feedback — création, compatibilité, doublons, throttle.

Ce fichier fixe le CONTRAT de l'endpoint. Deux points méritent d'être signalés
parce qu'ils changent un comportement existant :

- un second POST ne réécrit plus le premier en silence : il répond ``409`` ;
- l'ancien payload (``reason``) reste accepté et traduit — un frontend non
  redéployé ne doit pas voir ses signalements refusés.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.audit.models import RagQueryLog
from apps.feedback.models import AnswerFeedback

pytestmark = pytest.mark.django_db


ENDPOINT = "/api/v1/chat/feedback/"


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------
def test_thumb_up_creates_durable_feedback(api_client, user, query_log):
    res = api_client.post(ENDPOINT, {"log_id": str(query_log.id), "score": 1}, format="json")
    assert res.status_code == 200, res.content
    assert res.data["ok"] is True

    fb = AnswerFeedback.objects.get(id=res.data["id"])
    assert fb.rating == AnswerFeedback.RATING_UP
    assert fb.created_by_id == user.pk
    assert fb.status == AnswerFeedback.STATUS_NEW
    # Un pouce haut n'est pas un incident : il n'entre pas dans la file de triage.
    assert fb.priority_score == 0


def test_thumb_down_captures_historic_identifiers(api_client, query_log, rag_space, workspace):
    res = api_client.post(
        ENDPOINT,
        {
            "log_id": str(query_log.id),
            "score": -1,
            "kind": "info_in_docs",
            "comment": "la réponse est dans le document résumé juste avant",
            "expected_document": "note-interne-2023.pdf",
            "expected_term": "Nordexia Conseil",
        },
        format="json",
    )
    assert res.status_code == 200, res.content

    fb = AnswerFeedback.objects.get(id=res.data["id"])
    assert fb.kind == AnswerFeedback.KIND_INFO_IN_DOCS
    assert fb.expected_term == "Nordexia Conseil"

    # Tout ce qui rend l'incident interprétable est figé DÈS LE CLIC.
    assert fb.query_log_id_hist == query_log.id
    assert fb.rag_space_id_hist == rag_space.id
    assert fb.workspace_id_hist == str(workspace.id)
    assert fb.policy_hist == "no_answer"
    assert fb.llm_model_hist == "qwen3:30b-instruct"
    assert fb.prompt_hash == "a" * 64
    assert fb.log_created_at == query_log.created_at
    assert len(fb.question_hash) == 64 and len(fb.answer_hash) == 64
    assert len(fb.sources_fingerprint) == 64
    assert len(fb.dedup_fingerprint) == 64

    # Aucun contenu en clair : ni question, ni réponse, ni extrait.
    stored = {fb.question_hash, fb.answer_hash, fb.sources_fingerprint}
    assert "Nordexia" not in " ".join(stored)


def test_priority_rewards_declared_information_in_documents(api_client, query_log):
    res = api_client.post(
        ENDPOINT,
        {"log_id": str(query_log.id), "score": -1, "kind": "info_in_docs"},
        format="json",
    )
    fb = AnswerFeedback.objects.get(id=res.data["id"])
    # +3 (info_in_docs) +2 (policy no_answer)
    assert fb.priority_score == 5


def test_priority_penalises_signal_free_report(api_client, query_log):
    query_log.policy = "ok"
    query_log.save(update_fields=["policy"])
    res = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "kind": "other"}, format="json"
    )
    fb = AnswerFeedback.objects.get(id=res.data["id"])
    assert fb.priority_score == -1


def test_several_reporters_raise_priority(api_client, query_log, other_user, workspace, rag_space):
    from rest_framework.test import APIClient

    api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "kind": "wrong_sources"}, format="json"
    )

    # Même question, même espace, même modèle, mêmes sources -> même empreinte.
    other_log = RagQueryLog.objects.create(
        user=other_user,
        workspace=workspace,
        rag_space=rag_space,
        question=query_log.question,
        answer=query_log.answer,
        status=query_log.status,
        policy=query_log.policy,
        llm_model=query_log.llm_model,
        sources=query_log.sources,
    )
    client2 = APIClient()
    client2.force_authenticate(user=other_user)
    res2 = client2.post(
        ENDPOINT, {"log_id": str(other_log.id), "score": -1, "kind": "wrong_sources"}, format="json"
    )
    fb2 = AnswerFeedback.objects.get(id=res2.data["id"])

    fb1 = AnswerFeedback.objects.exclude(id=fb2.id).get()
    assert fb1.dedup_fingerprint == fb2.dedup_fingerprint
    # +2 wrong_sources +2 no_answer +2 multi-signalement
    assert fb2.priority_score == 6


# ---------------------------------------------------------------------------
# Compatibilité ascendante
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("missing_sources", AnswerFeedback.KIND_WRONG_SOURCES),
        ("irrelevant", AnswerFeedback.KIND_WRONG_SOURCES),
        ("incorrect", AnswerFeedback.KIND_WRONG_ANSWER),
        ("incomplete", AnswerFeedback.KIND_WRONG_ANSWER),
        ("other", AnswerFeedback.KIND_OTHER),
        ("valeur_inconnue", AnswerFeedback.KIND_OTHER),
    ],
)
def test_legacy_reason_is_translated(api_client, query_log, legacy, expected):
    res = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "reason": legacy}, format="json"
    )
    assert res.status_code == 200, res.content
    assert AnswerFeedback.objects.get(id=res.data["id"]).kind == expected


def test_legacy_duplicate_fields_are_ignored(api_client, query_log):
    """Les doublons historiques du payload frontend ne doivent rien casser."""
    res = api_client.post(
        ENDPOINT,
        {
            "log_id": str(query_log.id),
            "score": -1,
            "reason": "incorrect",
            "comment": "c",
            "feedback_score": -1,
            "feedback_reason": "incorrect",
            "feedback_comment": "c",
        },
        format="json",
    )
    assert res.status_code == 200, res.content


def test_dual_write_keeps_rag_query_log_columns(api_client, query_log):
    res = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "kind": "wrong_answer"}, format="json"
    )
    assert res.status_code == 200
    query_log.refresh_from_db()
    assert query_log.feedback_score == -1
    assert query_log.feedback_reason == "incorrect"  # motif historique équivalent


@override_settings(FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG=False)
def test_dual_write_can_be_switched_off(api_client, query_log):
    api_client.post(ENDPOINT, {"log_id": str(query_log.id), "score": -1}, format="json")
    query_log.refresh_from_db()
    assert query_log.feedback_score is None
    assert AnswerFeedback.objects.count() == 1


# ---------------------------------------------------------------------------
# Validation, autorisation, doublons
# ---------------------------------------------------------------------------
def test_invalid_score_rejected(api_client, query_log):
    res = api_client.post(ENDPOINT, {"log_id": str(query_log.id), "score": 9}, format="json")
    assert res.status_code == 400
    assert AnswerFeedback.objects.count() == 0


def test_unknown_kind_falls_back_to_other(api_client, query_log):
    res = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "kind": "n_importe_quoi"}, format="json"
    )
    assert res.status_code == 200
    assert AnswerFeedback.objects.get().kind == AnswerFeedback.KIND_OTHER


def test_cannot_feedback_other_users_log(api_client, other_user, workspace, rag_space):
    foreign = RagQueryLog.objects.create(
        user=other_user, workspace=workspace, rag_space=rag_space, question="q", status="ok"
    )
    res = api_client.post(ENDPOINT, {"log_id": str(foreign.id), "score": -1}, format="json")
    assert res.status_code == 404
    assert AnswerFeedback.objects.count() == 0


def test_second_post_returns_409_and_does_not_overwrite(api_client, query_log):
    first = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "comment": "premier"}, format="json"
    )
    assert first.status_code == 200

    second = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": 1, "comment": "second"}, format="json"
    )
    assert second.status_code == 409
    assert second.data["code"] == "feedback_exists"
    assert second.data["feedback_id"] == str(first.data["id"])

    # Le premier signalement est INTACT : c'est tout l'objet du 409.
    fb = AnswerFeedback.objects.get()
    assert fb.comment == "premier"
    assert fb.rating == AnswerFeedback.RATING_DOWN


def test_patch_updates_own_feedback(api_client, query_log):
    created = api_client.post(
        ENDPOINT, {"log_id": str(query_log.id), "score": -1, "kind": "other"}, format="json"
    )
    fid = created.data["id"]

    res = api_client.patch(
        f"{ENDPOINT}{fid}/",
        {"kind": "info_in_docs", "expected_term": "Multi-office LP", "comment": "précision"},
        format="json",
    )
    assert res.status_code == 200

    fb = AnswerFeedback.objects.get(id=fid)
    assert fb.kind == AnswerFeedback.KIND_INFO_IN_DOCS
    assert fb.expected_term == "Multi-office LP"
    assert fb.comment == "précision"
    # La note ne se réécrit pas : changer d'avis crée un cas, pas une rature.
    assert fb.rating == AnswerFeedback.RATING_DOWN
    # Le score de priorité suit la nouvelle catégorie (+3 au lieu de -1).
    assert fb.priority_score == 5


def test_patch_of_another_user_feedback_is_not_found(api_client, query_log, other_user):
    from rest_framework.test import APIClient

    created = api_client.post(ENDPOINT, {"log_id": str(query_log.id), "score": -1}, format="json")
    client2 = APIClient()
    client2.force_authenticate(user=other_user)
    res = client2.patch(f"{ENDPOINT}{created.data['id']}/", {"comment": "x"}, format="json")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Limitation de débit
# ---------------------------------------------------------------------------
def _post_n(api_client, user, workspace, rag_space, n: int) -> list[int]:
    codes = []
    for i in range(n):
        log = RagQueryLog.objects.create(
            user=user, workspace=workspace, rag_space=rag_space, question=f"q{i}", status="ok"
        )
        res = api_client.post(ENDPOINT, {"log_id": str(log.id), "score": -1}, format="json")
        codes.append(res.status_code)
    return codes


def test_throttle_limits_per_user(api_client, user, workspace, rag_space):
    """Le débit CONFIGURÉ PAR DÉFAUT (10/min) est bien appliqué.

    Le test exerce la valeur réellement livrée plutôt qu'un débit de test :
    c'est le défaut qui protège la production, donc c'est lui qui doit être
    vérifié.
    """
    cache.clear()
    codes = _post_n(api_client, user, workspace, rag_space, 12)
    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


def test_throttle_is_per_user_not_global(api_client, user, other_user, workspace, rag_space):
    """Un utilisateur saturé ne bloque PAS les autres."""
    from rest_framework.test import APIClient

    cache.clear()
    saturating = _post_n(api_client, user, workspace, rag_space, 11)
    assert saturating[-1] == 429

    client2 = APIClient()
    client2.force_authenticate(user=other_user)
    log = RagQueryLog.objects.create(
        user=other_user, workspace=workspace, rag_space=rag_space, question="q", status="ok"
    )
    res = client2.post(ENDPOINT, {"log_id": str(log.id), "score": -1}, format="json")
    assert res.status_code == 200

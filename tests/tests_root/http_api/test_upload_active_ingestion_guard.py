"""Un ré-upload ne ré-enfile JAMAIS un document dont l'ingestion est déjà en cours.

Dette mesurée en staging le 21/09/2026 : 515 uploads acceptés pour 502 documents créés,
257 messages `ingest_visual` pour 247 documents — 8 documents en file 2 ou 3 fois, et
chacun traité autant de fois par un worker visuel à un seul slot. Cause : un doublon de
contenu SANS chunk était ré-enfilé, alors que « 0 chunk » veut aussi dire « pas encore
traité ». L'autorité est désormais l'ÉTAT d'ingestion (PENDING / INGESTING), comme pour
la réindexation (`reindex_already_in_progress`).

On compte les ENFILEMENTS réels (`ingest_document_task.delay`), pas les réponses HTTP.
"""
import io
from unittest import mock

import pytest

from api.v1.documents import views_common, views_write
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.models.document import DocumentStatus

pytestmark = pytest.mark.django_db

CONTENT = b"Cahier des clauses techniques particulieres. Lot 02 charpente metallique."


def _upload(api_client, space, content=CONTENT, name="cctp.txt"):
    f = io.BytesIO(content)
    f.name = name
    return api_client.post(
        "/api/v1/documents/upload/?async=true",
        data={"rag_space_id": str(space.id), "file": f},
        format="multipart",
    )


@pytest.fixture
def non_eager(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    return settings


@pytest.fixture
def broker_ok(monkeypatch):
    monkeypatch.setattr(views_common, "broker_ready", lambda: True)


@pytest.fixture
def spy_delay(monkeypatch):
    spy = mock.Mock()
    monkeypatch.setattr(views_common.ingest_document_task, "delay", spy)
    return spy


def test_double_upload_while_pending_enqueues_once(api_client, rag_space, non_eager, broker_ok, spy_delay):
    first = _upload(api_client, rag_space)
    assert first.status_code == 200, first.data
    assert first.data["queued"] is True and first.data["deduped"] is False
    second = _upload(api_client, rag_space)
    assert second.status_code == 200, second.data
    assert second.data["deduped"] is True
    assert second.data["detail"] == views_write.INGESTION_ALREADY_IN_PROGRESS
    assert second.data["document_id"] == first.data["document_id"]
    assert Document.objects.filter(rag_space=rag_space).count() == 1
    assert spy_delay.call_count == 1


def test_triple_upload_before_processing_enqueues_once(api_client, rag_space, non_eager, broker_ok, spy_delay):
    ids = {(_upload(api_client, rag_space)).data["document_id"] for _ in range(3)}
    assert len(ids) == 1
    assert Document.objects.filter(rag_space=rag_space).count() == 1
    assert spy_delay.call_count == 1


def test_duplicate_while_ingesting_is_not_enqueued(api_client, rag_space, non_eager, broker_ok, spy_delay):
    doc_id = _upload(api_client, rag_space).data["document_id"]
    Document.objects.filter(id=doc_id).update(status=DocumentStatus.INGESTING)
    res = _upload(api_client, rag_space)
    assert res.data["status"] == DocumentStatus.INGESTING
    assert spy_delay.call_count == 1


def test_concurrent_race_caught_by_unique_index_is_not_enqueued(
    api_client, rag_space, non_eager, broker_ok, spy_delay, monkeypatch
):
    """Course réelle : la 2e requête a lu « aucun existant » AVANT que la 1re ne crée la
    ligne ; c'est l'index unique partiel (`create_or_get_by_sha`) qui la rattrape en
    `deduped=True`. Le garde doit tenir sur CE chemin aussi, pas seulement sur le lookup."""
    _upload(api_client, rag_space)
    monkeypatch.setattr(views_write, "find_existing_by_sha", lambda **kw: None)
    res = _upload(api_client, rag_space)
    assert res.status_code == 200, res.data
    assert res.data["deduped"] is True
    assert Document.objects.filter(rag_space=rag_space).count() == 1
    assert spy_delay.call_count == 1


def test_same_file_in_another_space_is_independent(api_client, rag_space, non_eager, broker_ok, spy_delay):
    other = RagSpace.objects.create(name="space-b", workspace=rag_space.workspace)
    a = _upload(api_client, rag_space)
    b = _upload(api_client, other)
    assert b.status_code == 200, b.data
    assert b.data["deduped"] is False
    assert a.data["document_id"] != b.data["document_id"]
    assert spy_delay.call_count == 2


def test_failed_document_can_still_be_reuploaded(api_client, rag_space, non_eager, broker_ok, spy_delay):
    """Un FAILED n'est pas une ingestion active : le ré-upload reste une voie de reprise."""
    doc_id = _upload(api_client, rag_space).data["document_id"]
    Document.objects.filter(id=doc_id).update(status=DocumentStatus.FAILED)
    res = _upload(api_client, rag_space)
    assert res.status_code == 200, res.data
    assert res.data.get("detail") != views_write.INGESTION_ALREADY_IN_PROGRESS
    assert spy_delay.call_count == 2


def test_ready_duplicate_keeps_historical_contract(api_client, rag_space, non_eager, broker_ok, spy_delay):
    """READY sans chunk : comportement historique inchangé (hors mandat) — ré-enfilé."""
    doc_id = _upload(api_client, rag_space).data["document_id"]
    Document.objects.filter(id=doc_id).update(status=DocumentStatus.READY)
    res = _upload(api_client, rag_space)
    assert res.data.get("detail") != views_write.INGESTION_ALREADY_IN_PROGRESS
    assert spy_delay.call_count == 2


def test_explicit_reindex_is_not_blocked_by_the_upload_guard(api_client, rag_space, non_eager, broker_ok, spy_delay):
    """La réindexation garde SON contrat : 409 sans force sur un doc actif, enfilée avec force."""
    doc_id = _upload(api_client, rag_space).data["document_id"]
    _upload(api_client, rag_space)  # doublon : aucun enfilement
    assert spy_delay.call_count == 1
    refused = api_client.post(f"/api/v1/documents/{doc_id}/reindex/", {}, format="json")
    assert refused.status_code == 409
    forced = api_client.post(f"/api/v1/documents/{doc_id}/reindex/?force=true&async=true", {"force": True}, format="json")
    assert forced.status_code == 200, forced.data
    assert forced.data["queued"] is True
    assert spy_delay.call_count == 2

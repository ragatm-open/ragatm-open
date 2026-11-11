"""routage applicatif vers la file `ingest_ifc` : relance (I10).

Une relance Celery (`self.retry`) reconstruit la signature depuis la requête courante :
avec Redis (exchange vide, routing_key = nom de la file) la file d'origine est
conservée. Prouvé sur la vraie tâche `ingest_document_task`, sans broker.
"""
from __future__ import annotations

from apps.rag_core.services.ingest.queues import INGEST_IFC_QUEUE, ingest_queue_for
from apps.rag_core.tasks import ingest_document_task, reindex_document_task
from celery.app.task import Context


def _request(queue: str) -> Context:
    """Requête telle que le worker la voit sous Redis : exchange vide, routing_key = file."""
    return Context(
        id="task-1", retries=0, args=(), kwargs={"document_id": "d1"},
        delivery_info={"exchange": "", "routing_key": queue, "priority": None, "redelivered": False},
    )


class TestRetryKeepsTheIfcQueue:
    def test_signature_from_request_preserves_ingest_ifc(self):
        for task in (ingest_document_task, reindex_document_task):
            sig = task.signature_from_request(_request(INGEST_IFC_QUEUE), (), {"document_id": "d1"})
            assert sig.options.get("queue") == INGEST_IFC_QUEUE, sig.options

    def test_signature_from_request_preserves_the_default_queue(self):
        sig = ingest_document_task.signature_from_request(_request("celery"), (), {"document_id": "d1"})
        assert sig.options.get("queue") == "celery"


class TestNoFallback:
    def test_helper_never_returns_the_general_queue(self, settings):
        settings.DOCUMENT_IFC_ENABLED = True
        assert ingest_queue_for(filename="m.ifc") == INGEST_IFC_QUEUE
        settings.DOCUMENT_IFC_ENABLED = False
        assert ingest_queue_for(filename="m.ifc") is None  # refusé à l'admission, jamais « celery »

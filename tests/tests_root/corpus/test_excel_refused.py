"""Lot E2 — le corpus métier reste FERMÉ aux classeurs Excel.

Raison, et elle est bloquante : l'ingestion du corpus est **100 % synchrone** (aucune
tâche Celery — cf. docs/follow-ups/corpus/02-async-celery-corpus-ingestion.md). Un
classeur de 500 000 cellules prend ~7 s (mesuré en E1) : il bloquerait une requête
HTTP. Excel y restera refusé tant que le corpus n'est pas asynchrone.

Le chemin PRIVÉ, lui, est ouvert (Celery + async forcé).
"""
import io
import sys
from pathlib import Path

import pytest

from apps.corpus.models import DomainCorpusDocument

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import build_xlsx  # noqa: E402

pytestmark = pytest.mark.django_db


def _xlsx():
    return build_xlsx({"Budget": [["Projet", "Budget"], ["Atlas", 250000]]})


def _file(data, name="budget.xlsx"):
    f = io.BytesIO(data)
    f.name = name
    return f


def test_corpus_upload_refuses_excel_before_creating_anything(
    api_client, global_owner, domain_corpus
):
    api_client.force_authenticate(user=global_owner)
    res = api_client.post(
        f"/api/v1/admin/domain-corpora/{domain_corpus.id}/documents/",
        data={"file": _file(_xlsx())},
        format="multipart",
    )
    assert res.status_code == 400, res.data
    assert res.data["detail"] == "excel_not_supported_here"
    assert "CSV" in res.data["error"]
    # Aucun document corpus créé : le refus tombe AVANT toute écriture.
    assert not DomainCorpusDocument.objects.filter(corpus=domain_corpus).exists()


def test_corpus_service_layer_also_refuses(domain_corpus):
    """Défense en profondeur : la réindexation corpus passe par le même service."""
    from apps.corpus.services.ingest import ingest_corpus_document_from_upload
    from apps.corpus.models.document import CorpusDocumentStatus

    res = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="b.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        file_bytes=_xlsx(),
    )
    doc = DomainCorpusDocument.objects.get(id=res.document_id)
    assert doc.status == CorpusDocumentStatus.FAILED
    assert "excel_not_supported_here" in doc.error_message
    # Aucun chunk : rien n'a été extrait.
    assert res.chunks_created == 0

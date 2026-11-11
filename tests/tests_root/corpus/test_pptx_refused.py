"""Lot P1 — le corpus métier reste FERMÉ aux présentations PowerPoint.

Même raison, bloquante, que pour Excel : l'ingestion du corpus est **100 % synchrone**
(aucune tâche Celery — cf. docs/follow-ups/corpus/02-async-celery-corpus-ingestion.md).
Une présentation est une archive compressée au nombre de formes imprévisible : elle
bloquerait une requête HTTP. Le `.pptx` y restera refusé tant que le corpus n'est pas
asynchrone — c'est exactement pour ça qu'il est forcé en async sur le chemin privé.

Le chemin PRIVÉ, lui, est ouvert (Celery + async forcé).
"""
import io
import sys
from pathlib import Path

import pytest
from apps.corpus.models import DomainCorpusDocument

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))
from _builders import build_pptx  # noqa: E402

pytestmark = pytest.mark.django_db

PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _pptx():
    return build_pptx([{"title": "Architecture", "bullets": ["Frontend", "Backend"]}])


def _file(data, name="deck.pptx"):
    f = io.BytesIO(data)
    f.name = name
    return f


def test_corpus_upload_refuses_pptx_before_creating_anything(
    api_client, global_owner, domain_corpus
):
    api_client.force_authenticate(user=global_owner)
    res = api_client.post(
        f"/api/v1/admin/domain-corpora/{domain_corpus.id}/documents/",
        data={"file": _file(_pptx())},
        format="multipart",
    )
    assert res.status_code == 400, res.data
    assert res.data["detail"] == "pptx_not_supported_here"
    assert "PDF" in res.data["error"]
    # Aucun document corpus créé : le refus tombe AVANT toute écriture.
    assert not DomainCorpusDocument.objects.filter(corpus=domain_corpus).exists()


def test_corpus_service_layer_also_refuses(domain_corpus):
    """Défense en profondeur : la réindexation corpus passe par le même service."""
    from apps.corpus.models.document import CorpusDocumentStatus
    from apps.corpus.services.ingest import ingest_corpus_document_from_upload

    res = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="deck.pptx",
        content_type=PPTX_CT,
        file_bytes=_pptx(),
    )
    doc = DomainCorpusDocument.objects.get(id=res.document_id)
    assert doc.status == CorpusDocumentStatus.FAILED
    assert "pptx_not_supported_here" in doc.error_message
    assert res.chunks_created == 0


def test_corpus_still_accepts_the_formats_it_always_did(domain_corpus):
    """Non-régression : fermer PPTX ne doit rien fermer d'autre."""
    from apps.corpus.models.document import CorpusDocumentStatus
    from apps.corpus.services.ingest import ingest_corpus_document_from_upload

    res = ingest_corpus_document_from_upload(
        corpus=domain_corpus, filename="note.md", content_type="text/markdown",
        file_bytes=b"# Titre\n\nUn contenu suffisamment long pour produire un chunk.",
    )
    doc = DomainCorpusDocument.objects.get(id=res.document_id)
    assert doc.status == CorpusDocumentStatus.READY

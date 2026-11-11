"""Tests du pré-filtrage des chunks vides/blancs avant embeddings (ingestion).

Vérifie : (1) le filtre pur préserve l'ordre et l'alignement spec→vecteur et ne
laisse passer aucun blanc ; (2) dans le chemin d'indexation réel, un chunk blanc
est écarté AVANT ``embed_texts`` (jamais transmis au provider) et le document
atteint READY avec les chunks non-blancs alignés.
"""
from __future__ import annotations

import pytest

from apps.llm_router.embeddings import is_blank_text
from apps.rag_core.services.chunking import ChunkSpec
from apps.rag_core.services.ingest.utils import filter_nonblank_specs


def _spec(i: int, text: str) -> ChunkSpec:
    return ChunkSpec(chunk_index=i, text=text, page_number=None, char_start=0, char_end=len(text), source="f.txt")


# ------------------------------------------------------------------ filtre pur

def test_filter_drops_blanks_preserves_order():
    specs = [_spec(0, "alpha"), _spec(1, ""), _spec(2, "   "), _spec(3, "\n\t"), _spec(4, "beta")]
    kept = filter_nonblank_specs(specs)
    assert [s.text for s in kept] == ["alpha", "beta"]


def test_filter_result_has_no_blank_for_embeddings():
    specs = [_spec(0, "a"), _spec(1, "  "), _spec(2, "b")]
    kept = filter_nonblank_specs(specs)
    texts = [s.text for s in kept]
    assert all(not is_blank_text(t) for t in texts)   # embed_texts ne lèvera pas


def test_alignment_spec_to_vector_strict():
    specs = [_spec(0, "a"), _spec(1, ""), _spec(2, "b"), _spec(3, "  "), _spec(4, "c")]
    kept = filter_nonblank_specs(specs)
    texts = [s.text for s in kept]
    fake_vectors = [[float(i)] for i in range(len(texts))]   # 1 vecteur par texte
    # strict=True échoue si les longueurs diffèrent => prouve l'alignement.
    paired = list(zip(kept, fake_vectors, strict=True))
    assert [s.text for s, _ in paired] == ["a", "b", "c"]


def test_filter_empty_input():
    assert filter_nonblank_specs([]) == []
    assert filter_nonblank_specs([_spec(0, "  "), _spec(1, "")]) == []


# ------------------------------------------------------------ intégration index

@pytest.mark.django_db
def test_index_skips_blank_chunk_and_stays_ready(rag_space, monkeypatch):
    from apps.rag_core.models import Chunk, Document
    from apps.rag_core.models.document import DocumentStatus
    from apps.rag_core.services.ingest import indexing
    from apps.rag_core.services.ingest.extraction.base import ExtractionResult, ExtractionStatus

    doc = Document.objects.create(rag_space=rag_space, filename="doc.txt", status="INGESTING")
    result = ExtractionResult(text="contenu non vide", status=ExtractionStatus.SUCCESS)

    specs = [_spec(0, "premier chunk"), _spec(1, "   "), _spec(2, "second chunk")]
    monkeypatch.setattr(indexing, "_build_chunks", lambda **k: (specs, {}))
    monkeypatch.setattr(indexing, "_build_table_specs", lambda **k: [])

    captured = {}

    def spy_embed(texts, dim=None, **k):
        captured["texts"] = list(texts)
        d = int(dim or 768)
        # Depuis le lot 3, la couture rend un EmbeddingBatch (vecteurs + identité de
        # pipeline) et non plus une liste nue.
        from apps.llm_router.embeddings_gateway import EmbeddingBatch

        return EmbeddingBatch(
            vectors=[[0.0] * d for _ in texts],
            pipeline_id="legacy:stub:test:768:legacy-v1",
            dimension=d,
            task="document",
        )

    monkeypatch.setattr(indexing, "embed_texts_with_identity", spy_embed)

    res = indexing.index_extraction_result(
        doc=doc, space=rag_space, result=result, filename="doc.txt",
        chunk_size=1000, overlap=100, dim=768,
    )

    # Le blanc n'a JAMAIS été transmis au provider d'embeddings.
    assert captured["texts"] == ["premier chunk", "second chunk"]
    # Document indexé (pas d'échec à cause du chunk blanc).
    assert res.status == DocumentStatus.READY
    stored = list(Chunk.objects.filter(document=doc).order_by("chunk_index"))
    assert [c.text for c in stored] == ["premier chunk", "second chunk"]
    assert all(not is_blank_text(c.text) for c in stored)

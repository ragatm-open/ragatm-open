"""Tests du manifeste de figement d'un corpus réel + détection de dérive (P1.9).

Un corpus dev/prod n'est pas versionnable : on fige son EMPREINTE. Toute dérive
(document ajouté/supprimé/modifié, re-chunking, changement de profil) doit rendre la
comparaison INVALIDE plutôt que produire des métriques trompeuses.
"""
import pytest

from apps.rag_core.benchmark import manifest as mf
from apps.rag_core.benchmark.runner import CaseResult, aggregate_by_format


def _doc(doc_id, filename="a.pdf", texts=("t1",), status="READY"):
    return {
        "document_id": doc_id,
        "filename": filename,
        "format": filename.rsplit(".", 1)[-1],
        "checksum": mf.document_checksum(filename, list(texts)),
        "status": status,
        "chunk_count": len(texts),
    }


def _manifest(docs, **kw):
    base = dict(
        rag_space_id="space-1", rag_space_name="Mission",
        embedding_provider="ollama", embedding_model="nomic-embed-text",
        embedding_dimension=768, chunking_version=None,
        documents=docs, created_at="2026-07-16T00:00:00+00:00",
    )
    base.update(kw)
    return mf.build_manifest(**base)


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------
def test_document_checksum_changes_when_text_changes():
    a = mf.document_checksum("f.pdf", ["hello"])
    b = mf.document_checksum("f.pdf", ["hello!"])
    assert a != b


def test_document_checksum_changes_when_rechunked():
    """Même contenu global mais découpage différent => empreinte différente."""
    a = mf.document_checksum("f.pdf", ["hello world"])
    b = mf.document_checksum("f.pdf", ["hello", "world"])
    assert a != b


def test_document_checksum_stable_and_filename_sensitive():
    assert mf.document_checksum("f.pdf", ["x"]) == mf.document_checksum("f.pdf", ["x"])
    assert mf.document_checksum("f.pdf", ["x"]) != mf.document_checksum("g.pdf", ["x"])


def test_manifest_checksum_is_order_independent():
    m1 = _manifest([_doc("a"), _doc("b", "b.pdf", ("z",))])
    m2 = _manifest([_doc("b", "b.pdf", ("z",)), _doc("a")])
    assert m1["manifest_checksum"] == m2["manifest_checksum"]


def test_manifest_contains_no_document_content():
    """Confidentialité : le manifeste ne doit porter AUCUN texte de chunk."""
    m = _manifest([_doc("a", "secret.pdf", ("donnée client confidentielle",))])
    blob = mf.dump_manifest(m)
    assert "donnée client confidentielle" not in blob
    assert m["document_count"] == 1 and m["chunk_count"] == 1


# --------------------------------------------------------------------------
# Détection de dérive
# --------------------------------------------------------------------------
def test_drift_none_when_identical():
    ref = _manifest([_doc("a"), _doc("b", "b.pdf")])
    cur = _manifest([_doc("a"), _doc("b", "b.pdf")])
    d = mf.diff_manifests(ref, cur)
    assert d["compatible"] is True and d["reasons"] == []


def test_drift_document_added_and_removed():
    ref = _manifest([_doc("a")])
    cur = _manifest([_doc("b", "b.pdf")])
    d = mf.diff_manifests(ref, cur)
    assert d["compatible"] is False
    assert d["added"] == ["b"] and d["removed"] == ["a"]


def test_drift_document_modified():
    ref = _manifest([_doc("a", "a.pdf", ("v1",))])
    cur = _manifest([_doc("a", "a.pdf", ("v2",))])
    d = mf.diff_manifests(ref, cur)
    assert d["compatible"] is False and d["changed"] == ["a"]


def test_drift_embedding_profile_change():
    ref = _manifest([_doc("a")])
    cur = _manifest([_doc("a")], embedding_model="text-embedding-3-small")
    d = mf.diff_manifests(ref, cur)
    assert d["compatible"] is False
    assert any("embedding_model" in r for r in d["reasons"])


def test_drift_dimension_and_space_change():
    ref = _manifest([_doc("a")])
    assert mf.diff_manifests(ref, _manifest([_doc("a")], embedding_dimension=1536))["compatible"] is False
    assert mf.diff_manifests(ref, _manifest([_doc("a")], rag_space_id="other"))["compatible"] is False


def test_drift_chunking_version_change():
    ref = _manifest([_doc("a")], chunking_version="v1")
    cur = _manifest([_doc("a")], chunking_version="v2")
    assert mf.diff_manifests(ref, cur)["compatible"] is False


# --------------------------------------------------------------------------
# Métriques par format
# --------------------------------------------------------------------------
def _cr(cid, fmt, rank):
    return CaseResult(
        case_id=cid, rag_space="s", status="ok", should_answer=True, answered=True,
        no_answer_correct=True, latency_ms=1.0, n_sources=1, retrieved_docs=[],
        expected_docs=["d"], recall={1: 1.0 if rank == 1 else 0.0, 3: 1.0, 5: 1.0},
        reciprocal_rank=1.0 / rank, first_relevant_rank=rank, tags=[], format=fmt,
    )


def test_aggregate_by_format_separates_pdf_docx_xlsx():
    results = [_cr("p1", "pdf", 1), _cr("p2", "pdf", 1),
               _cr("d1", "docx", 1), _cr("x1", "xlsx", 3)]
    by = aggregate_by_format(results, ks=(1, 3, 5))
    assert set(by) == {"pdf", "docx", "xlsx"}
    assert by["pdf"]["n"] == 2
    assert by["pdf"]["recall_at_k"]["1"] == 1.0
    # xlsx trouvé au rang 3 -> recall@1 nul, MRR 1/3
    assert by["xlsx"]["recall_at_k"]["1"] == 0.0
    assert by["xlsx"]["mrr"] == pytest.approx(1 / 3, abs=1e-3)


def test_aggregate_by_format_unlabelled_bucket():
    by = aggregate_by_format([_cr("u", "", 1)], ks=(1,))
    assert "(non étiqueté)" in by

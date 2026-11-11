"""Tests purs de la normalisation des sources pour l'audit."""
from apps.rag_core.services.query.audit import (
    SOURCES_SCHEMA_VERSION,
    normalize_request_id,
    normalize_sources_for_audit,
)


def test_schema_version_constant():
    # v2 : sources typées (corpus métier). Cf. apps.corpus + Phase 7.
    # v3 (lot E3) : `meta` porte la citation Excel (feuille + plage). Additif — les
    # logs v1/v2 restent lisibles et ne sont pas migrés.
    assert SOURCES_SCHEMA_VERSION == 3


def test_missing_source_type_is_unknown_not_workspace():
    """Une provenance ABSENTE est auditée `unknown`, jamais supposée privée.

    L'audit est ce qu'on relit lors d'un incident de provenance : y consigner
    « workspace_document » pour une source dont l'origine a été perdue en amont
    produirait une trace fausse, et indiscernable d'une trace juste.
    """
    chunks = [{"chunk_id": "c1", "text": "hi", "score": 0.5, "source": {"document_id": "d1"}}]
    out = normalize_sources_for_audit(chunks)
    assert out[0]["source_type"] == "unknown"


def test_explicit_workspace_source_type_is_preserved():
    chunks = [
        {
            "chunk_id": "c1",
            "text": "hi",
            "score": 0.5,
            "source_type": "workspace_document",
            "source": {"document_id": "d1", "source_type": "workspace_document"},
        }
    ]
    out = normalize_sources_for_audit(chunks)
    assert out[0]["source_type"] == "workspace_document"


def test_unrecognised_source_type_falls_back_to_unknown():
    chunks = [{"chunk_id": "c1", "text": "hi", "score": 0.5, "source_type": "something_new"}]
    out = normalize_sources_for_audit(chunks)
    assert out[0]["source_type"] == "unknown"


def test_corpus_source_type_and_fields():
    chunks = [
        {
            "chunk_id": "c2",
            "text": "hi",
            "score": 0.5,
            "source_type": "domain_corpus",
            "source": {
                "document_id": "d2",
                "source_type": "domain_corpus",
                "corpus_id": "co1",
                "corpus_name": "Procédures qualité",
                "business_domain_id": "bd1",
                "business_domain_name": "Logistique",
            },
        }
    ]
    out = normalize_sources_for_audit(chunks)
    s = out[0]
    assert s["source_type"] == "domain_corpus"
    assert s["corpus_id"] == "co1"
    assert s["corpus_name"] == "Procédures qualité"
    assert s["business_domain_name"] == "Logistique"


def test_none_returns_empty():
    assert normalize_sources_for_audit(None) == []


def test_normalized_shape_and_rank():
    chunks = [
        {
            "chunk_id": "c1",
            "text": " hi ",
            "score": 0.5,
            "source": {"document_id": "d1", "filename": "f.txt", "page_number": 2},
        }
    ]
    out = normalize_sources_for_audit(chunks)
    s = out[0]
    assert s["rank"] == 1
    assert s["chunk_id"] == "c1"
    assert s["document_id"] == "d1"
    assert s["filename"] == "f.txt"
    assert s["page_number"] == 2
    assert s["score"] == 0.5
    assert s["snippet"] == "hi"
    assert s["schema_version"] == SOURCES_SCHEMA_VERSION


def test_snippet_truncation():
    chunks = [{"chunk_id": "c", "text": "hello", "score": 0.1, "source": {}}]
    out = normalize_sources_for_audit(chunks, max_snippet_chars=2)
    assert out[0]["snippet"] == "he"


def test_normalize_request_id_strips_and_truncates():
    assert normalize_request_id(None) == ""
    assert normalize_request_id("  abc  ") == "abc"
    assert len(normalize_request_id("x" * 5000)) <= 200

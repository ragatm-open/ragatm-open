"""Tests des contrôles déterministes de cohérence documentaire (P1-B)."""
import pytest

from apps.rag_core.benchmark import validation as v
from apps.rag_core.benchmark.dataset import BenchmarkCase


class FakeCorpus:
    """Résolveur de corpus en mémoire (implémente le protocole CorpusResolver)."""

    def __init__(self, *, documents, chunks):
        # documents: {rag_space_id: {filename}}
        # chunks: {chunk_id: ChunkRef}
        self.documents = documents
        self.chunks = chunks

    def find_document(self, *, rag_space_id, document_key):
        return document_key in self.documents.get(rag_space_id, set())

    def find_chunk(self, *, rag_space_id, chunk_id):
        return self.chunks.get(chunk_id)

    def document_chunk_texts(self, *, rag_space_id, document_key):
        return [
            c.text
            for c in self.chunks.values()
            if c.rag_space_id == rag_space_id and c.document_key == document_key
        ]


SPACE = "space-1"


def _corpus():
    chunks = {
        "chk-A": v.ChunkRef("chk-A", "A.pdf", SPACE, "AI Gateway production utilise le port 8100."),
        "chk-B": v.ChunkRef("chk-B", "B.pdf", SPACE, "AI Gateway staging utilise le port 8200."),
        "chk-other": v.ChunkRef("chk-other", "X.pdf", "space-2", "Autre espace."),
    }
    docs = {SPACE: {"A.pdf", "B.pdf"}}
    return FakeCorpus(documents=docs, chunks=chunks)


def _case(**kw):
    base = dict(
        id="q1", rag_space="s", question="Quel port ?", should_answer=True,
        expected_documents=("A.pdf",),
    )
    base.update(kw)
    return BenchmarkCase(**base)


def test_document_exists_valid():
    res = v.validate_case_against_corpus(_case(expected_answer_contains=("8100",)),
                                         rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_VALID
    assert not res.errors


def test_document_not_found_is_obsolete():
    res = v.validate_case_against_corpus(_case(expected_documents=("ABSENT.pdf",)),
                                         rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_OBSOLETE
    assert res.errors[0].code == v.CODE_EXPECTED_DOCUMENT_NOT_FOUND


def test_chunk_exists_valid():
    res = v.validate_case_against_corpus(
        _case(expected_documents=("A.pdf",), expected_chunk_ids=("chk-A",),
              expected_answer_contains=("8100",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_VALID


def test_chunk_not_found_is_obsolete():
    res = v.validate_case_against_corpus(
        _case(expected_chunk_ids=("chk-ghost",), expected_documents=()),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_OBSOLETE
    assert res.errors[0].code == v.CODE_EXPECTED_CHUNK_NOT_FOUND


def test_chunk_wrong_document_mismatch():
    res = v.validate_case_against_corpus(
        _case(expected_documents=("B.pdf",), expected_chunk_ids=("chk-A",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_OBSOLETE
    assert res.errors[0].code == v.CODE_CHUNK_DOCUMENT_MISMATCH


def test_chunk_wrong_ragspace_mismatch():
    res = v.validate_case_against_corpus(
        _case(expected_documents=(), expected_chunk_ids=("chk-other",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_OBSOLETE
    assert res.errors[0].code == v.CODE_CHUNK_RAGSPACE_MISMATCH


def test_reference_answer_present():
    res = v.validate_case_against_corpus(
        _case(expected_chunk_ids=("chk-A",), expected_documents=(),
              expected_answer_contains=("8100",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_VALID


def test_reference_answer_absent_atomic_is_invalid():
    res = v.validate_case_against_corpus(
        _case(expected_chunk_ids=("chk-A",), expected_documents=(),
              expected_answer_contains=("8000",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_INVALID
    assert any(e.code == v.CODE_REFERENCE_ANSWER_NOT_FOUND for e in res.errors)


def test_structured_number_absent_is_invalid():
    # "port 9999" : le terme atomique est absent ET c'est une valeur structurante.
    res = v.validate_case_against_corpus(
        _case(expected_chunk_ids=("chk-A",), expected_documents=(),
              expected_answer_contains=("9999",)),
        rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_INVALID
    codes = {e.code for e in res.errors}
    assert v.CODE_STRUCTURED_VALUE_NOT_FOUND in codes


def test_structured_number_present():
    assert v.extract_structured_values("port 8100") == ["8100"]


def test_extract_ip_version_date_filename_ref():
    assert v.extract_structured_values("198.51.100.7") == ["198.51.100.7"]
    assert v.extract_structured_values("v1.2.3") == ["1.2.3"] or \
        v.extract_structured_values("v1.2.3") == ["v1.2.3"]
    assert v.extract_structured_values("2026-07-15") == ["2026-07-15"]
    assert v.extract_structured_values("config.yaml") == ["config.yaml"]
    assert v.extract_structured_values("REF-2026-04") == ["REF-2026-04"]


def test_ip_present_in_chunk():
    corpus = FakeCorpus(
        documents={SPACE: {"net.pdf"}},
        chunks={"c": v.ChunkRef("c", "net.pdf", SPACE, "La gateway est en 198.51.100.7:8100.")},
    )
    ok = v.validate_case_against_corpus(
        _case(expected_documents=("net.pdf",), expected_chunk_ids=("c",),
              expected_answer_contains=("198.51.100.7",)),
        rag_space_id=SPACE, corpus=corpus)
    assert ok.status == v.STATUS_VALID
    ko = v.validate_case_against_corpus(
        _case(expected_documents=("net.pdf",), expected_chunk_ids=("c",),
              expected_answer_contains=("198.51.100.1",)),
        rag_space_id=SPACE, corpus=corpus)
    assert ko.status == v.STATUS_INVALID


def test_question_contains_answer_warning():
    res = v.validate_case_structure(
        _case(question="Quelle est la procédure de réinitialisation ?",
              expected_answer_contains=("réinitialisation",))
    )
    # warning (non bloquant), le statut reste valide
    assert res.status == v.STATUS_VALID
    assert any(w.code == v.CODE_QUESTION_CONTAINS_ANSWER for w in res.warnings)


def test_no_answer_case_not_subject_to_answer_check():
    case = BenchmarkCase(id="q99", rag_space="s", question="hors sujet ?", should_answer=False)
    res = v.validate_case_against_corpus(case, rag_space_id=SPACE, corpus=_corpus())
    assert res.status == v.STATUS_VALID
    assert not res.errors


def test_normalize_preserves_structuring_chars():
    assert v.normalize("  Port  8100 ") == "port 8100"
    assert "198.51.100.7" in v.normalize("IP: 198.51.100.7")
    assert "config.yaml" in v.normalize("Fichier CONFIG.yaml")



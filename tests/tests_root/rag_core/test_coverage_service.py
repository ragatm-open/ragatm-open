"""L0/L1 — service de couverture et statistiques (arbre legacy).

Couvre la même surface que tests/backend côté service : comptes, exclusions,
budget SQL, isolation, et le test bloquant sur les documents en échec.
"""
import pytest

from apps.rag_core.domain.coverage import (
    CoverageError,
    compute_corpus_fingerprint,
    coverage_to_dict,
    render_coverage_markdown,
)
from apps.rag_core.models import Chunk, Document, RagSpace
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.coverage_service import (
    build_corpus_coverage,
    compute_rag_space_statistics,
)

pytestmark = pytest.mark.django_db


def _doc(space, *, status=DocumentStatus.READY, name="d.txt", sha=""):
    return Document.objects.create(rag_space=space, filename=name, status=status, sha256=sha)


def _chunk(space, doc, *, text="abcd", page=1, idx=0):
    return Chunk.objects.create(
        rag_space=space, document=doc, chunk_index=idx, text=text, page_number=page
    )


def test_empty_space(rag_space):
    stats = compute_rag_space_statistics(rag_space.id)

    assert stats["documents"]["total"] == 0
    assert stats["content"]["chunks_total"] == 0
    assert stats["content"]["estimated_tokens_total"] == 0
    assert stats["ratios"]["ready"] == 0.0  # pas de division par zéro
    assert stats["coverage"].coverage_level == "A"
    assert "Aucun document à analyser" in render_coverage_markdown(stats["coverage"])


def test_counts_and_coverage(rag_space):
    d1 = _doc(rag_space, name="a.txt", sha="a")
    _chunk(rag_space, d1, text="12345678", page=1, idx=0)
    _chunk(rag_space, d1, text="1234", page=2, idx=1)
    d2 = _doc(rag_space, name="b.txt", sha="b")
    _chunk(rag_space, d2, text="123456", page=1, idx=0)

    stats = compute_rag_space_statistics(rag_space.id)
    cov = stats["coverage"]

    assert stats["content"]["chunks_total"] == 3
    assert stats["content"]["pages_numbered_total"] == 3
    assert stats["content"]["characters_total"] == 18
    assert stats["content"]["estimated_tokens_total"] == 5
    assert cov.documents_analyzed == 2
    assert cov.coverage_complete is True
    assert cov.coverage_level == "B"


def test_ready_without_chunk_is_excluded(rag_space):
    d = _doc(rag_space, name="ok.txt", sha="a")
    _chunk(rag_space, d)
    _doc(rag_space, name="vide.txt", sha="b")

    cov = compute_rag_space_statistics(rag_space.id)["coverage"]

    assert cov.documents_analyzed == 1
    assert cov.coverage_complete is False
    assert [(e.reason, e.count) for e in cov.documents_excluded] == [("no_text_content", 1)]


def test_coverage_never_claims_complete_when_documents_failed(rag_space):
    """TEST BLOQUANT (arbre legacy)."""
    for i in range(3):
        d = _doc(rag_space, name=f"ok{i}.txt", sha=f"s{i}")
        _chunk(rag_space, d, text="contenu textuel")
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")

    cov = compute_rag_space_statistics(rag_space.id)["coverage"]

    assert cov.coverage_complete is False
    assert any(e.reason == "extraction_failed" for e in cov.documents_excluded)
    assert cov.documents_analyzed < cov.documents_considered
    assert "exhaustif" not in render_coverage_markdown(cov).lower()


def test_unknown_status_is_flagged_not_swallowed(rag_space):
    """Un statut hors nomenclature est signalé (`unknown`), jamais absorbé."""
    d = _doc(rag_space, name="ok.txt", sha="a")
    _chunk(rag_space, d)
    weird = _doc(rag_space, name="weird.txt", sha="w")
    Document.objects.filter(pk=weird.pk).update(status="ARCHIVED")

    cov = compute_rag_space_statistics(rag_space.id)["coverage"]

    assert cov.documents_considered == 2  # toujours dans le dénominateur
    assert cov.documents_analyzed == 1
    assert [(e.reason, e.count) for e in cov.documents_excluded] == [("unknown", 1)]
    assert cov.coverage_complete is False


def test_isolation_between_spaces(rag_space, workspace):
    other = RagSpace.objects.create(name="autre", workspace=workspace)
    d_other = _doc(other, name="o.txt", sha="o")
    _chunk(other, d_other, text="x" * 50)

    d_mine = _doc(rag_space, name="m.txt", sha="m")
    _chunk(rag_space, d_mine, text="1234")

    stats = compute_rag_space_statistics(rag_space.id)
    assert stats["documents"]["total"] == 1
    assert stats["content"]["characters_total"] == 4
    assert (
        stats["coverage"].scope.corpus_fingerprint
        != compute_rag_space_statistics(other.id)["coverage"].scope.corpus_fingerprint
    )


def test_sql_budget(rag_space, django_assert_num_queries):
    for i in range(30):
        d = _doc(rag_space, name=f"d{i}.txt", sha=f"s{i}")
        for j in range(4):
            _chunk(rag_space, d, page=j, idx=j)

    with django_assert_num_queries(3):
        compute_rag_space_statistics(rag_space.id)


def test_fingerprint_follows_the_corpus(rag_space):
    d = _doc(rag_space, name="a.txt", sha="aaa")
    before = compute_rag_space_statistics(rag_space.id)["coverage"].scope.corpus_fingerprint

    # Même corpus, même empreinte (indépendance à l'ordre SQL).
    assert compute_rag_space_statistics(rag_space.id)["coverage"].scope.corpus_fingerprint == before

    Document.objects.filter(pk=d.pk).update(status=DocumentStatus.FAILED)
    after_status = compute_rag_space_statistics(rag_space.id)["coverage"].scope.corpus_fingerprint
    assert after_status != before

    Document.objects.filter(pk=d.pk).update(sha256="bbb")
    assert (
        compute_rag_space_statistics(rag_space.id)["coverage"].scope.corpus_fingerprint
        != after_status
    )


def test_fingerprint_is_order_independent():
    docs = [("d1", "a", "READY"), ("d2", "b", "FAILED")]
    assert compute_corpus_fingerprint(docs) == compute_corpus_fingerprint(list(reversed(docs)))


def test_build_corpus_coverage_rejects_impossible_facts(rag_space):
    """Le helper n'invente rien : des faits incohérents lèvent."""
    with pytest.raises(CoverageError):
        build_corpus_coverage(
            rag_space_id=rag_space.id,
            documents_total=5,
            documents_considered=5,
            documents_analyzed=9,  # > considered
            excluded={},
            chunks_analyzed=0,
            pages_analyzed=0,
            corpus_fingerprint="sha256:x",
        )


def test_serialized_coverage_hides_internal_reasons(rag_space):
    d = _doc(rag_space, name="ok.txt", sha="a")
    _chunk(rag_space, d)
    _doc(rag_space, name="ko.txt", status=DocumentStatus.FAILED, sha="")

    payload = coverage_to_dict(compute_rag_space_statistics(rag_space.id)["coverage"])

    assert payload["documents_excluded"] == [
        {"reason": "extraction_failed", "count": 1, "label": "échec d’extraction"}
    ]
    blob = str(payload).lower()
    assert "permission" not in blob
    assert "ok.txt" not in blob and "ko.txt" not in blob  # aucun nom de fichier

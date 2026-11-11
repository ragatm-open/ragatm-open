"""L0 — invariants, ratio, empreinte, sérialisation et rendu de la couverture.

Module pur : aucun accès base, aucun réseau, aucune IA. Ces tests verrouillent
les règles de docs/coverage-semantics.md.
"""
from datetime import datetime, timezone

import pytest

from apps.rag_core.domain.coverage import (
    REASON_EXTRACTION_FAILED,
    REASON_NO_TEXT_CONTENT,
    REASON_PERMISSION_FILTERED,
    Coverage,
    CoverageError,
    CoverageExclusion,
    CoverageScope,
    compute_corpus_fingerprint,
    compute_coverage_ratio,
    coverage_to_dict,
    estimate_tokens,
    render_coverage_markdown,
)

FIXED_TS = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _scope(**kw):
    return CoverageScope(
        rag_space_id=kw.get("rag_space_id", "11111111-1111-1111-1111-111111111111"),
        generated_at=kw.get("generated_at", FIXED_TS),
        corpus_fingerprint=kw.get("corpus_fingerprint", "sha256:deadbeef"),
        filters_applied=kw.get("filters_applied", ()),
    )


def _coverage(**kw):
    """Couverture valide par défaut ; chaque test n'écrase que ce qu'il teste."""
    defaults = dict(
        scope=_scope(),
        documents_total=10,
        documents_considered=10,
        documents_analyzed=10,
        documents_with_results=0,
        documents_excluded=(),
        pages_analyzed=20,
        chunks_analyzed=50,
        items_extracted=0,
        items_without_source=0,
        coverage_level="B",
        coverage_complete=True,
        coverage_ratio=1.0,
        limits_applied=(),
    )
    defaults.update(kw)
    return Coverage(**defaults)


# --------------------------------------------------------------------------- #
# U1 — invariants
# --------------------------------------------------------------------------- #

def test_analyzed_cannot_exceed_considered():
    with pytest.raises(CoverageError, match="documents_analyzed"):
        _coverage(documents_analyzed=11, documents_considered=10, coverage_complete=False)


def test_considered_cannot_exceed_total():
    with pytest.raises(CoverageError, match="documents_considered"):
        _coverage(documents_total=5, documents_considered=10, documents_analyzed=5,
                  coverage_complete=False)


def test_with_results_cannot_exceed_analyzed():
    with pytest.raises(CoverageError, match="documents_with_results"):
        _coverage(documents_with_results=11)


def test_negative_counter_is_rejected():
    with pytest.raises(CoverageError, match="ne peut pas être négatif"):
        _coverage(chunks_analyzed=-1)


@pytest.mark.parametrize("ratio", [-0.01, 1.01, 2.0])
def test_ratio_out_of_range_is_rejected(ratio):
    with pytest.raises(CoverageError, match="coverage_ratio"):
        _coverage(coverage_ratio=ratio)


def test_complete_is_incompatible_with_exclusions():
    """Le verrou central : on ne peut pas déclarer complet ET exclure des documents."""
    with pytest.raises(CoverageError, match="incompatible avec des exclusions"):
        _coverage(
            documents_excluded=(CoverageExclusion(REASON_EXTRACTION_FAILED, 3),),
            coverage_complete=True,
        )


def test_complete_is_incompatible_with_limits():
    with pytest.raises(CoverageError, match="incompatible avec limits_applied"):
        _coverage(limits_applied=("max_documents=100",), coverage_complete=True)


def test_complete_requires_analyzed_equals_considered():
    with pytest.raises(CoverageError, match="documents_analyzed == documents_considered"):
        _coverage(documents_analyzed=8, coverage_complete=True, coverage_ratio=0.8)


def test_unknown_exclusion_reason_is_rejected():
    with pytest.raises(CoverageError, match="raison d'exclusion inconnue"):
        _coverage(
            documents_excluded=(CoverageExclusion("because_i_said_so", 1),),
            coverage_complete=False,
            documents_analyzed=9,
            coverage_ratio=0.9,
        )


def test_unknown_coverage_level_is_rejected():
    with pytest.raises(CoverageError, match="coverage_level"):
        _coverage(coverage_level="Z")


# --------------------------------------------------------------------------- #
# U2 — ratio
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "analyzed,considered,expected",
    [(0, 0, 1.0), (0, 10, 0.0), (5, 10, 0.5), (10, 10, 1.0)],
)
def test_coverage_ratio(analyzed, considered, expected):
    assert compute_coverage_ratio(
        documents_analyzed=analyzed, documents_considered=considered
    ) == pytest.approx(expected)


def test_empty_scope_convention_is_documented_behaviour():
    """Périmètre vide : ratio 1.0 (rien à analyser, rien de manqué)…"""
    c = _coverage(
        documents_total=0, documents_considered=0, documents_analyzed=0,
        chunks_analyzed=0, pages_analyzed=0, coverage_level="A",
        coverage_complete=True, coverage_ratio=1.0,
    )
    assert c.coverage_complete is True

    # …mais le rendu ne parle JAMAIS de « couverture 100 % » sur un espace vide.
    md = render_coverage_markdown(c)
    assert "Aucun document à analyser" in md
    assert "100 %" not in md
    assert "exhaustif" not in md.lower()


# --------------------------------------------------------------------------- #
# U3 — empreinte de corpus
# --------------------------------------------------------------------------- #

_DOCS = [("d1", "aaa", "READY"), ("d2", "bbb", "READY"), ("d3", "ccc", "FAILED")]


def test_fingerprint_is_order_independent():
    assert compute_corpus_fingerprint(_DOCS) == compute_corpus_fingerprint(list(reversed(_DOCS)))


def test_fingerprint_is_deterministic():
    assert compute_corpus_fingerprint(_DOCS) == compute_corpus_fingerprint(_DOCS)


def test_fingerprint_changes_when_document_added():
    assert compute_corpus_fingerprint(_DOCS) != compute_corpus_fingerprint(
        _DOCS + [("d4", "ddd", "READY")]
    )


def test_fingerprint_changes_when_document_removed():
    assert compute_corpus_fingerprint(_DOCS) != compute_corpus_fingerprint(_DOCS[:-1])


def test_fingerprint_changes_when_content_changes():
    changed = [("d1", "ZZZ", "READY"), ("d2", "bbb", "READY"), ("d3", "ccc", "FAILED")]
    assert compute_corpus_fingerprint(_DOCS) != compute_corpus_fingerprint(changed)


def test_fingerprint_changes_when_status_changes():
    changed = [("d1", "aaa", "READY"), ("d2", "bbb", "READY"), ("d3", "ccc", "READY")]
    assert compute_corpus_fingerprint(_DOCS) != compute_corpus_fingerprint(changed)


def test_fingerprint_is_prefixed_and_leaks_nothing():
    fp = compute_corpus_fingerprint([("d1", "aaa", "READY")])
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 64
    # Ni identifiant, ni empreinte source, ni statut ne transparaissent en clair.
    assert "d1" not in fp[7:]
    assert "READY" not in fp


def test_estimate_tokens():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(-5) == 0
    assert estimate_tokens(4) == 1
    assert estimate_tokens(5) == 2  # ceil, jamais tronqué
    assert estimate_tokens(16_104_196) == 4_026_049


# --------------------------------------------------------------------------- #
# U4 — sérialisation
# --------------------------------------------------------------------------- #

def test_serialization_shape():
    c = _coverage(
        documents_analyzed=7,
        documents_excluded=(CoverageExclusion(REASON_EXTRACTION_FAILED, 3),),
        coverage_complete=False,
        coverage_ratio=0.7,
    )
    d = coverage_to_dict(c)

    assert d["scope"]["generated_at"] == "2026-07-13T12:00:00+00:00"
    assert d["scope"]["corpus_fingerprint"] == "sha256:deadbeef"
    assert isinstance(d["scope"]["filters_applied"], list)  # tuple → list
    assert isinstance(d["documents_excluded"], list)
    assert d["documents_excluded"][0] == {
        "reason": "extraction_failed",
        "count": 3,
        "label": "échec d’extraction",
    }
    assert d["coverage_complete"] is False
    assert d["coverage_ratio"] == 0.7
    assert d["coverage_level"] == "B"


def test_serialization_never_leaks_permission_filtered():
    """Un document inaccessible ne doit JAMAIS être révélé, même agrégé."""
    c = _coverage(
        documents_analyzed=8,
        documents_excluded=(
            CoverageExclusion(REASON_PERMISSION_FILTERED, 2),
        ),
        coverage_complete=False,
        coverage_ratio=0.8,
    )
    d = coverage_to_dict(c)
    assert d["documents_excluded"] == []
    assert "permission" not in str(d).lower()

    md = render_coverage_markdown(c)
    assert "permission" not in md.lower()


# --------------------------------------------------------------------------- #
# U5 / U6 — rendu
# --------------------------------------------------------------------------- #

def test_render_partial_coverage():
    c = _coverage(
        documents_total=216,
        documents_considered=216,
        documents_analyzed=203,
        documents_excluded=(CoverageExclusion(REASON_EXTRACTION_FAILED, 13),),
        chunks_analyzed=9478,
        pages_analyzed=6281,
        coverage_complete=False,
        coverage_ratio=203 / 216,
    )
    md = render_coverage_markdown(c)

    assert "216 documents considérés" in md
    assert "203 documents analysés" in md
    assert "13 documents non analysés" in md
    assert "9 478 extraits textuels parcourus" in md
    assert "94 %" in md
    assert "échec d’extraction" in md
    assert "exhaustif" not in md.lower()


def test_render_complete_coverage_still_never_promises_exhaustivity():
    c = _coverage(
        documents_total=42, documents_considered=42, documents_analyzed=42,
        chunks_analyzed=1358, coverage_complete=True, coverage_ratio=1.0,
    )
    md = render_coverage_markdown(c)

    assert "42 documents considérés" in md
    assert "1 358 extraits textuels parcourus" in md
    assert "100 %" in md
    assert "Documents non analysés" not in md
    assert (
        "Tous les documents et contenus textuels exploitables du périmètre ont été parcourus."
        in md
    )
    # Même complète, la couverture A/B ne promet RIEN sur les niveaux C et D.
    lowered = md.lower()
    for banned in (
        "exhaustif",
        "exhaustive",
        "intégralité du corpus",
        "tous les éléments ont été trouvés",
        "aucune information n’a été manquée",
    ):
        assert banned not in lowered


def test_render_no_text_content_exclusion_is_labelled():
    c = _coverage(
        documents_total=3, documents_considered=3, documents_analyzed=2,
        documents_excluded=(CoverageExclusion(REASON_NO_TEXT_CONTENT, 1),),
        chunks_analyzed=4, coverage_complete=False, coverage_ratio=2 / 3,
    )
    md = render_coverage_markdown(c)
    assert "1 document : aucun contenu textuel exploitable" in md
    assert "exhaustif" not in md.lower()


def test_render_exposes_no_technical_name():
    c = _coverage(
        documents_analyzed=9,
        documents_excluded=(CoverageExclusion(REASON_EXTRACTION_FAILED, 1),),
        coverage_complete=False, coverage_ratio=0.9,
    )
    md = render_coverage_markdown(c).lower()
    for banned in (
        "celery", "worker", "queue", "ollama", "openai", "gateway", "provider",
        "traceback", "exception", "/api/", "settings", "django", "postgres",
        "embedding", "chunk",
    ):
        assert banned not in md, f"nom technique exposé : {banned}"


def test_items_without_source_marks_result_invalid():
    ok = _coverage(items_without_source=0)
    assert ok.is_valid_result is True

    bad = _coverage(items_extracted=5, items_without_source=1)
    assert bad.is_valid_result is False

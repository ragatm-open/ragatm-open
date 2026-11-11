"""décision PAR RagSpace du rail tabulaire générique PDF.

Contrat : le drapeau global ne suffit plus. Le rail ne s'applique qu'à un document dont le RagSpace
(identité transmise par appel, jamais mémorisée) figure, en UUID complet, dans
DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS ; un espace du rail visuel structuré est toujours
exclu. Toute ambiguïté (liste vide, entrée douteuse, identité absente) donne rail OFF.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.extraction import config as C
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.pdf_extractor import PdfExtractor

from . import _pdf_table_fixtures as F

SPACE_A = "0a6e8f2c-31d4-4b8a-9c5e-7f1d2b3a4c50"  # autorisé
SPACE_B = "1b7f903d-42e5-4c9b-8d6f-802e3c4b5d61"  # non autorisé
SPACE_C = "2c80a14e-53f6-4dac-9e70-913f4d5c6e72"  # rail visuel structuré


@pytest.fixture
def scoped(settings):
    settings.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = True
    settings.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = [SPACE_A, SPACE_C]
    settings.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = [SPACE_C]
    return settings


# --------------------------------------------------------------------------- #
# Décision
# --------------------------------------------------------------------------- #
def test_global_flag_off_wins_over_allowlist(scoped):
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = False
    assert C.pdf_generic_tables_decision(SPACE_A) == (False, C.NATIVE_TABLES_GLOBAL_DISABLED)


def test_allowed_space_gets_the_rail(scoped):
    assert C.pdf_generic_tables_decision(SPACE_A) == (True, C.NATIVE_TABLES_ENABLED)


def test_registry_default_is_empty_and_permits_nothing(settings):
    from config import registry

    assert registry.get("DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS").default == []
    settings.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = True
    for empty in ([], None, "", " , "):
        settings.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = empty
        assert C.pdf_generic_tables_decision(SPACE_A) == (False, C.NATIVE_TABLES_SPACE_NOT_ALLOWED)


def test_missing_setting_permits_nothing(settings):
    settings.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = True
    del settings.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS
    assert C.pdf_generic_tables_decision(SPACE_A) == (False, C.NATIVE_TABLES_SPACE_NOT_ALLOWED)


@pytest.mark.parametrize("bad", [
    pytest.param("0a6e8f2c", id="prefix"),
    pytest.param("0a6e8f2c-31d4-4b8a-9c5e", id="truncated"),
    pytest.param("0a6e8f2c31d44b8a9c5e7f1d2b3a4c50", id="no-dashes"),
    pytest.param("{0a6e8f2c-31d4-4b8a-9c5e-7f1d2b3a4c50}", id="braces"),
    pytest.param("urn:uuid:0a6e8f2c-31d4-4b8a-9c5e-7f1d2b3a4c50", id="urn"),
    pytest.param("space-a", id="name"),
    pytest.param("*", id="wildcard"),
])
def test_one_invalid_entry_invalidates_the_whole_list(scoped, bad):
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = [SPACE_A, bad]
    assert C.pdf_generic_tables_decision(SPACE_A) == (False, C.NATIVE_TABLES_ALLOWLIST_INVALID)


def test_uppercase_full_uuid_is_canonicalised(scoped):
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = [SPACE_A.upper()]
    assert C.pdf_generic_tables_decision(SPACE_A)[0] is True


def test_comma_separated_string_is_accepted(scoped):
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = f" {SPACE_B} , {SPACE_A} "
    assert C.pdf_generic_tables_decision(SPACE_A)[0] is True
    assert C.pdf_generic_tables_decision(SPACE_B)[0] is True


@pytest.mark.parametrize("space,reason", [
    pytest.param(SPACE_B, C.NATIVE_TABLES_SPACE_NOT_ALLOWED, id="unlisted"),
    pytest.param("3d91b25f-6407-4ebd-8f81-a24f5e6d7f83", C.NATIVE_TABLES_SPACE_NOT_ALLOWED, id="unknown"),
    pytest.param(SPACE_C, C.NATIVE_TABLES_VISUAL_CONFLICT, id="visual-rail"),
    pytest.param(None, C.NATIVE_TABLES_MISSING_SPACE, id="none"),
    pytest.param("", C.NATIVE_TABLES_MISSING_SPACE, id="empty"),
    pytest.param("0a6e8f2c", C.NATIVE_TABLES_MISSING_SPACE, id="prefix-identity"),
])
def test_refusals(scoped, space, reason):
    assert C.pdf_generic_tables_decision(space) == (False, reason)


def test_several_allowed_spaces(scoped):
    scoped.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = []
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = [SPACE_A, SPACE_B, SPACE_C]
    assert all(C.pdf_generic_tables_decision(s)[0] for s in (SPACE_A, SPACE_B, SPACE_C))


def test_visual_rail_conflict_is_case_insensitive(scoped):
    scoped.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = SPACE_A.upper()
    assert C.pdf_generic_tables_decision(SPACE_A) == (False, C.NATIVE_TABLES_VISUAL_CONFLICT)


# --------------------------------------------------------------------------- #
# Extracteur : la décision suit le contexte de CHAQUE appel
# --------------------------------------------------------------------------- #
def _generic(space):
    res = PdfExtractor().extract(F.flat_three_column(), ExtractionContext(filename="f.pdf", rag_space_id=space))
    n = sum(1 for t in res.tables if str((t.get("meta") or {}).get("table_type", "")).startswith("generic"))
    return n, res.metadata.get("native_tables_decision")


@pytest.mark.parametrize("order", [
    pytest.param((SPACE_A, SPACE_B, SPACE_C, SPACE_A, None), id="A-B-C-A-none"),
    pytest.param((SPACE_C, SPACE_B, SPACE_A, None, SPACE_A), id="C-B-A-none-A"),
    pytest.param((SPACE_B, SPACE_A, SPACE_A, SPACE_C, SPACE_B), id="B-A-A-C-B"),
])
def test_no_contamination_between_spaces_in_the_same_process(scoped, order):
    expected = {SPACE_A: C.NATIVE_TABLES_ENABLED, SPACE_B: C.NATIVE_TABLES_SPACE_NOT_ALLOWED,
                SPACE_C: C.NATIVE_TABLES_VISUAL_CONFLICT, None: C.NATIVE_TABLES_MISSING_SPACE}
    for space in order:
        n, reason = _generic(space)
        assert reason == expected[space]
        assert (n > 0) is (space == SPACE_A)


def test_refused_space_is_identical_to_the_global_off_baseline(scoped):
    on_b = PdfExtractor().extract(F.flat_three_column(), ExtractionContext(filename="f.pdf", rag_space_id=SPACE_B))
    scoped.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = False
    off = PdfExtractor().extract(F.flat_three_column(), ExtractionContext(filename="f.pdf", rag_space_id=SPACE_B))
    assert on_b.text == off.text and on_b.pages == off.pages and on_b.tables == off.tables


def test_identity_crosses_the_cancellable_child_process(scoped):
    """`extract_document` extrait dans un processus enfant (fork) : l'identité y arrive par le contexte."""
    from apps.rag_core.services.ingest import process_isolation as PI
    from apps.rag_core.services.ingest.extraction import extract_document

    assert PI.fork_sans_danger() is True  # sinon ce test ne prouverait rien sur l'enfant
    for space, reason in ((SPACE_B, C.NATIVE_TABLES_SPACE_NOT_ALLOWED), (SPACE_A, C.NATIVE_TABLES_ENABLED),
                          (None, C.NATIVE_TABLES_MISSING_SPACE)):
        res = extract_document(F.flat_three_column(), filename="f.pdf", content_type="application/pdf",
                               rag_space_id=space)
        assert res.metadata["native_tables_decision"] == reason

"""Tests extracteur DOCX (+ sécurité : .docm, corrompu, zip bomb)."""
import pytest

from apps.rag_core.services.ingest.extraction import (
    CorruptedFile,
    ExtractionLimitExceeded,
    RejectedFile,
    extract_document,
)
from apps.rag_core.services.ingest.extraction.base import BlockType

from ._builders import make_docx, make_zip_bomb_docx


class TestDocxNominal:
    def test_text_and_titles(self):
        r = extract_document(make_docx(title="Mon Titre", paragraphs=("Para 1", "Para 2")),
                             filename="f.docx")
        assert r.status.value == "success"
        titles = [b for b in r.blocks if b.type == BlockType.TITLE]
        assert titles and titles[0].text == "Mon Titre"
        assert "Para 1" in r.text and "Para 2" in r.text

    def test_table_linearized(self):
        r = extract_document(make_docx(table=(("Nom", "Ville"), ("Alice", "Paris"))),
                             filename="f.docx")
        tables = [b for b in r.blocks if b.type == BlockType.TABLE]
        assert tables
        assert "Nom | Ville" in tables[0].text
        assert "Alice | Paris" in tables[0].text
        assert r.metadata["tables"] >= 1

    def test_list_extracted(self):
        r = extract_document(make_docx(list_items=("un", "deux")), filename="f.docx")
        lists = [b for b in r.blocks if b.type == BlockType.LIST]
        assert lists and "un" in lists[0].text


class TestDocxSecurity:
    def test_docm_refused(self):
        with pytest.raises(RejectedFile) as ei:
            extract_document(make_docx(macro=True), filename="f.docm")
        assert ei.value.code == "macro_enabled_document_refused"

    def test_corrupted_docx_fails_cleanly(self):
        # entête ZIP valide mais contenu cassé
        with pytest.raises((CorruptedFile, RejectedFile)):
            extract_document(b"PK\x03\x04" + b"\x00" * 40, filename="f.docx")

    def test_zip_bomb_rejected(self, settings):
        settings.DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB = 50
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(make_zip_bomb_docx(entry_uncompressed_mb=200), filename="f.docx")
        assert "docx" in ei.value.code

    def test_too_many_entries_rejected(self, settings):
        settings.DOCUMENT_DOCX_MAX_ZIP_ENTRIES = 1
        with pytest.raises(ExtractionLimitExceeded):
            extract_document(make_docx(), filename="f.docx")

"""Tests purs des extracteurs de texte et de l'extraction de date / sha."""
import datetime

import pytest

from apps.rag_core.services.ingest.extractors import extract_text_from_txt_bytes
from apps.rag_core.services.ingest.utils import (
    extract_document_date_from_pages,
    extract_document_date_from_text,
    sha256_bytes,
    sha256_text,
)


class TestTxtExtractor:
    def test_empty_bytes(self):
        assert extract_text_from_txt_bytes(b"") == ""
        assert extract_text_from_txt_bytes(None) == ""

    def test_utf8(self):
        assert extract_text_from_txt_bytes("café".encode("utf-8")) == "café"

    def test_latin1_fallback_does_not_raise(self):
        # octets non-utf8 -> ne lève jamais
        out = extract_text_from_txt_bytes(b"\xe9\xe8")
        assert isinstance(out, str)


class TestSha:
    def test_sha256_bytes_known_value(self):
        assert sha256_bytes(b"") == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_sha256_bytes_none_is_empty_hash(self):
        assert sha256_bytes(None) == sha256_bytes(b"")

    def test_sha256_text_deterministic(self):
        assert sha256_text("abc") == sha256_text("abc")

    def test_sha256_text_none_hashes_empty(self):
        # None est traité comme chaîne vide -> hash de b""
        assert sha256_text(None) == sha256_bytes(b"")


class TestDateExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2025-09-12", datetime.date(2025, 9, 12)),
            ("Publié le 12 septembre 2025", datetime.date(2025, 9, 12)),
            ("Date: 12 September 2025", datetime.date(2025, 9, 12)),
            ("blabla sans date", None),
            ("", None),
            (None, None),
        ],
    )
    def test_from_text(self, text, expected):
        assert extract_document_date_from_text(text) == expected

    def test_dmy_numeric_format(self):
        # JJ/MM/AAAA (DMY), pas MDY
        assert extract_document_date_from_text("01/02/2025") == datetime.date(2025, 2, 1)

    def test_from_pages_tuples(self):
        pages = [(1, "Publié le 2025-09-12")]
        assert extract_document_date_from_pages(pages) == datetime.date(2025, 9, 12)

    def test_from_pages_none(self):
        assert extract_document_date_from_pages(None) is None

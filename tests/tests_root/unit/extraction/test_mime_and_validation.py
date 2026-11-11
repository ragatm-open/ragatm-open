"""Tests transverses : détection MIME par contenu + validation/allowlist/refus."""
import pytest

from apps.rag_core.services.ingest.extraction import (
    RejectedFile,
    UnsupportedFormat,
    extract_document,
)
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.validation import (
    FMT_CSV,
    FMT_DOCX,
    FMT_HTML,
    FMT_MARKDOWN,
    FMT_PDF,
    FMT_TXT,
    validate_and_classify,
)

from ._builders import make_docx, make_pdf_with_text, make_png


def _ctx(name="f", ct="", ocr=None):
    return ExtractionContext(filename=name, declared_content_type=ct, ocr_enabled=ocr)


class TestMimeDetection:
    def test_pdf_signature(self):
        assert M.detect_mime(make_pdf_with_text(), _ctx("a.pdf")) == M.PDF

    def test_docx_signature_over_extension(self):
        # extension trompeuse .txt mais contenu DOCX réel -> détecté DOCX
        assert M.detect_mime(make_docx(), _ctx("trompeur.txt")) == M.DOCX

    def test_png_signature(self):
        assert M.detect_mime(make_png(), _ctx("x.png")) == M.PNG

    def test_executable_elf(self):
        assert M.detect_mime(b"\x7fELF\x02\x01\x01" + b"\x00" * 50, _ctx("a.bin")) == M.EXECUTABLE

    def test_executable_pe(self):
        assert M.detect_mime(b"MZ\x90\x00" + b"\x00" * 50, _ctx("a.exe")) == M.EXECUTABLE

    def test_html_by_content(self):
        assert M.detect_mime(b"<!DOCTYPE html><html><body>hi</body></html>", _ctx("x")) == M.HTML

    def test_csv_by_content(self):
        assert M.detect_mime(b"a,b,c\n1,2,3\n4,5,6\n", _ctx("x")) == M.CSV

    def test_plain_text(self):
        assert M.detect_mime(b"juste du texte sans structure", _ctx("notes")) == M.PLAIN


class TestValidation:
    def test_allowlist_formats(self):
        assert validate_and_classify(b"hello plain", _ctx("a.txt"))[1] == FMT_TXT
        assert validate_and_classify(b"# Titre\ntexte", _ctx("a.md"))[1] == FMT_MARKDOWN
        assert validate_and_classify(b"a,b\n1,2\n", _ctx("a.csv"))[1] == FMT_CSV
        assert validate_and_classify(b"<html><p>x</p></html>", _ctx("a.html"))[1] == FMT_HTML
        assert validate_and_classify(make_docx(), _ctx("a.docx"))[1] == FMT_DOCX
        assert validate_and_classify(make_pdf_with_text(), _ctx("a.pdf"))[1] == FMT_PDF

    def test_executable_rejected(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"\x7fELF\x02\x01\x01" + b"\x00" * 50, _ctx("a"))
        assert ei.value.code == "executable_refused"

    def test_docm_rejected(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_docx(macro=True), _ctx("a.docm"))
        assert ei.value.code == "macro_enabled_document_refused"

    def test_image_accepted_regardless_of_global_ocr(self, settings):
        # Lot 2 : la porte image est DÉCOUPLÉE de l'OCR PDF global. Une image est
        # acceptée même si OCR global off (elle sera OCRisée automatiquement).
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
        for ocr_flag in (False, True, None):
            _, fmt = validate_and_classify(make_png(), _ctx("a.png", ocr=ocr_flag))
            assert fmt == "image"

    def test_image_rejected_when_ingest_disabled(self, settings):
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = False
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_png(), _ctx("a.png"))
        assert ei.value.code == "image_ingest_disabled"

    def test_gif_unsupported_format(self, settings):
        # GIF hors allowlist MVP (jpeg/png/tiff/webp) -> image_unsupported_format.
        settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
        settings.DOCUMENT_IMAGE_FORMATS = "jpeg,png,tiff,webp"
        gif = b"GIF89a" + b"\x00" * 32
        with pytest.raises(UnsupportedFormat) as ei:
            validate_and_classify(gif, _ctx("a.gif"))
        assert ei.value.code == "image_unsupported_format"

    def test_empty_file_rejected(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"", _ctx("a.txt"))
        assert ei.value.code == "empty_file"

    def test_oversize_rejected(self, settings):
        settings.DOCUMENT_MAX_UPLOAD_SIZE_MB = 0.0001  # ~104 octets
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"x" * 5000, _ctx("a.txt"))
        assert ei.value.code == "file_too_large"

    def test_unknown_binary_unsupported(self):
        # binaire non reconnu (octets NUL) -> octet-stream -> unsupported
        with pytest.raises((UnsupportedFormat, RejectedFile)):
            validate_and_classify(b"\x00\x01\x02\x03garbage\x00", _ctx("a.dat"))


class TestSpoofedExtension:
    def test_exe_renamed_pdf_is_rejected(self):
        # contenu exécutable déguisé en .pdf -> on ne se fie pas à l'extension
        with pytest.raises(RejectedFile):
            extract_document(b"MZ\x90\x00" + b"\x00" * 80, filename="invoice.pdf")

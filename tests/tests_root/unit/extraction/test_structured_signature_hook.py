"""hook de détection de signatures STRUCTURÉES (registre interne).

Le hook est évalué APRÈS les signatures binaires/sécurité (exécutable, image, OLE2, PDF,
famille ZIP) et AVANT le repli texte générique (`text/plain`). Le registre est vide en
production dans ce lot : aucune signature réelle (`<?xml`, `ISO-10303-21`, DXF) n'est
enregistrée ici — ce sont les lots de format qui le feront.

Les symboles introduits par 1A sont résolus PARESSEUSEMENT (``getattr``) pour que la
ligne de base (avant implémentation) échoue par ASSERTION et non par erreur d'import.
"""
from __future__ import annotations

import pytest
from apps.rag_core.services.ingest.extraction import RejectedFile
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.validation import FMT_TXT, validate_and_classify

from ._builders import make_docx, make_pdf_with_text, make_png

TEST_MIME = "application/x-ragatm-test-structured"
OTHER_MIME = "application/x-ragatm-test-other"
MARKER = b"RAGATM-STRUCTURED-TEST\n"


def _ctx(name="f", ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


def _register(name, fn, **kw):
    reg = getattr(M, "register_structured_detector", None)
    assert reg is not None, "mime.register_structured_detector absent"
    reg(name, fn, **kw)


@pytest.fixture
def clean_registry():
    """Registre vide PENDANT le test, restauré à l'identique après (les détecteurs des
    lots de format — `xml` depuis une version antérieure — ne doivent pas fuir ni disparaître)."""
    clear = getattr(M, "_clear_structured_detectors", None)
    saved = list(getattr(M, "_STRUCTURED_DETECTORS", []))
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()
        M._STRUCTURED_DETECTORS.extend(saved)


# Échantillons dont la classification NE DOIT PAS dépendre du hook.
def _strong_samples():
    return [
        (b"\x7fELF\x02\x01\x01" + b"\x00" * 50, _ctx("a.bin"), M.EXECUTABLE),
        (b"MZ\x90\x00" + b"\x00" * 50, _ctx("a.exe"), M.EXECUTABLE),
        (make_png(), _ctx("x.png"), M.PNG),
        (make_pdf_with_text(), _ctx("a.pdf"), M.PDF),
        (make_docx(), _ctx("trompeur.txt"), M.DOCX),
        (b"<!DOCTYPE html><html><body>hi</body></html>", _ctx("x"), M.HTML),
        (b"a,b,c\n1,2,3\n4,5,6\n", _ctx("x"), M.CSV),
        (b"# Titre\ntexte", _ctx("a.md"), M.MARKDOWN),
        (b"juste du texte sans structure", _ctx("notes"), M.PLAIN),
        (b"\x00\x01\x02binaire inconnu" * 8, _ctx("x.bin"), M.OCTET),
    ]


# --------------------------------------------------------------------------- #
# S1 — ordre d'évaluation : après les signatures fortes, avant text/plain
# --------------------------------------------------------------------------- #
class TestS1HookOrder:
    def test_detector_wins_over_plain_text_fallback(self, clean_registry):
        seen = []

        def det(data, ctx):
            seen.append(ctx.filename)
            return TEST_MIME if data.startswith(MARKER) else None

        _register("test-structured", det)
        assert M.detect_mime(MARKER + b"payload texte lisible", _ctx("a.dat")) == TEST_MIME
        assert seen == ["a.dat"]

    def test_detector_is_not_consulted_for_strong_signatures(self, clean_registry):
        seen = []

        def det(data, ctx):
            seen.append(ctx.filename)
            return TEST_MIME

        _register("greedy", det)
        for data, ctx, expected in _strong_samples():
            if expected in (M.EXECUTABLE, M.PNG, M.PDF, M.DOCX):
                assert M.detect_mime(data, ctx) == expected
        assert seen == [], "le hook ne doit pas être consulté avant les signatures fortes"

    def test_detector_is_consulted_before_octet_and_text_fallbacks(self, clean_registry):
        seen = []

        def det(data, ctx):
            seen.append(ctx.filename)
            return None

        _register("observer", det)
        M.detect_mime(b"juste du texte sans structure", _ctx("notes"))
        M.detect_mime(b"\x00\x01\x02binaire inconnu" * 8, _ctx("x.bin"))
        assert seen == ["notes", "x.bin"]

    def test_collision_is_deterministic_first_priority_then_name(self, clean_registry):
        _register("zz-late", lambda d, c: OTHER_MIME, priority=50)
        _register("aa-early", lambda d, c: TEST_MIME, priority=50)
        assert M.detect_mime(MARKER, _ctx("a.dat")) == TEST_MIME  # égalité → nom
        clear = getattr(M, "_clear_structured_detectors")
        clear()
        _register("zz-prio", lambda d, c: OTHER_MIME, priority=10)
        _register("aa-name", lambda d, c: TEST_MIME, priority=50)
        assert M.detect_mime(MARKER, _ctx("a.dat")) == OTHER_MIME  # priorité d'abord

    def test_duplicate_name_is_refused(self, clean_registry):
        _register("dup", lambda d, c: None)
        with pytest.raises(ValueError):
            _register("dup", lambda d, c: None)


# --------------------------------------------------------------------------- #
# S2 — registre vide : classification strictement identique à l'existant
# --------------------------------------------------------------------------- #
class TestS2EmptyRegistryIsNeutral:
    def test_registry_holds_only_format_lot_detectors(self):
        """Le socle n'enregistre rien ; seuls les détecteurs de format le font (XML, DXF, IFC,
        EML — priorité 100, ordre par nom)."""
        listing = getattr(M, "structured_detectors", None)
        assert listing is not None, "mime.structured_detectors absent"
        assert listing() == ((100, "audio_transcript"), (100, "dwg"), (100, "dxf"), (100, "eml"), (100, "ifc"), (100, "xml"))

    def test_same_classification_with_empty_registry(self, clean_registry):
        for data, ctx, expected in _strong_samples():
            assert M.detect_mime(data, ctx) == expected

    def test_plain_text_still_txt_through_validation(self, clean_registry):
        assert validate_and_classify(b"hello plain", _ctx("a.txt"))[1] == FMT_TXT


# --------------------------------------------------------------------------- #
# S3 — un détecteur ne peut JAMAIS surclasser une signature forte
# --------------------------------------------------------------------------- #
class TestS3CannotOverrideStrongSignatures:
    def test_greedy_detector_does_not_change_strong_signatures(self, clean_registry):
        _register("greedy", lambda d, c: TEST_MIME)
        for data, ctx, expected in _strong_samples():
            if expected in (M.EXECUTABLE, M.PNG, M.PDF, M.DOCX):
                assert M.detect_mime(data, ctx) == expected

    def test_greedy_detector_does_change_text_and_octet_fallbacks(self, clean_registry):
        """Contre-épreuve : le hook agit bien sur ce qui n'a pas de signature forte."""
        _register("greedy", lambda d, c: TEST_MIME)
        assert M.detect_mime(b"juste du texte sans structure", _ctx("notes")) == TEST_MIME
        assert M.detect_mime(b"\x00\x01\x02binaire inconnu" * 8, _ctx("x.bin")) == TEST_MIME


# --------------------------------------------------------------------------- #
# S4 — un détecteur qui lève : échec FERMÉ (refus), jamais un text/plain par défaut
# --------------------------------------------------------------------------- #
class TestS4DetectorExceptionFailsClosed:
    def test_exception_yields_validation_rejection(self, clean_registry):
        def boom(data, ctx):
            raise RuntimeError("détecteur cassé")

        _register("broken", boom)
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"juste du texte sans structure", _ctx("a.txt"))
        assert ei.value.code == "structured_detection_failed"
        assert "détecteur cassé" not in ei.value.detail

    def test_detect_mime_itself_never_raises(self, clean_registry):
        def boom(data, ctx):
            raise RuntimeError("détecteur cassé")

        _register("broken", boom)
        out = M.detect_mime(b"juste du texte sans structure", _ctx("a.txt"))
        assert out == getattr(M, "STRUCTURED_DETECTION_FAILED", object())
        assert out != M.PLAIN

    def test_non_string_return_fails_closed(self, clean_registry):
        _register("weird", lambda d, c: 42)
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"juste du texte sans structure", _ctx("a.txt"))
        assert ei.value.code == "structured_detection_failed"

    def test_exception_does_not_touch_strong_signatures(self, clean_registry):
        def boom(data, ctx):
            raise RuntimeError("détecteur cassé")

        _register("broken", boom)
        assert M.detect_mime(make_pdf_with_text(), _ctx("a.pdf")) == M.PDF

    def test_detection_failure_is_non_retryable(self):
        from apps.rag_core.services.ingest import retry_strategy as rs

        assert "structured_detection_failed" in rs.NON_RETRYABLE_ERROR_CODES

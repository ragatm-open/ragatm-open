"""Lot E2 — sécurité de l'extracteur Excel.

Trois familles, toutes vérifiées AVANT qu'openpyxl ne touche l'archive :

1. les gardes du lot E0 (zip-bomb, DOCTYPE/ENTITY, macros) sont RÉUTILISÉES,
   pas réimplémentées ;
2. `sharedStrings` — la borne mémoire — est vérifiée sur la taille DÉCLARÉE dans le
   répertoire central du ZIP, donc sans décompresser un octet ;
3. rien de dangereux n'est jamais exécuté, ouvert ou suivi.
"""
import pytest

from apps.rag_core.services.ingest.extraction import (
    ExtractionLimitExceeded,
    RejectedFile,
    extract_document,
)
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.validation import (
    FMT_EXCEL,
    validate_and_classify,
)

from ._builders import (
    build_xlsx,
    make_xlsm_zip,
    make_xlsx_huge_shared_strings,
    make_xlsx_unsafe_xml,
    make_raw_xlsx,
    make_xlsx_zip_bomb,
    raw_sheet,
)


def _ctx(name="c.xlsx"):
    return ExtractionContext(filename=name)


# --------------------------------------------------------------------------- #
# sharedStrings — LA borne mémoire (spike E1)
# --------------------------------------------------------------------------- #
class TestSharedStrings:
    def test_oversized_table_is_refused(self, settings):
        settings.DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB = 1
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(make_xlsx_huge_shared_strings(), filename="gros.xlsx")
        assert ei.value.code == "excel_shared_strings_too_large"

    def test_refusal_happens_before_openpyxl_is_even_called(self, settings, monkeypatch):
        """La preuve : openpyxl ne doit JAMAIS être appelé sur un classeur hors limite.

        C'est tout l'intérêt de la borne — openpyxl chargerait la table entière en
        mémoire dès `load_workbook()`, et il serait alors trop tard.
        """
        settings.DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB = 1

        import openpyxl

        def _boom(*a, **kw):  # pragma: no cover - ne doit jamais être atteint
            raise AssertionError("openpyxl NE DOIT PAS être ouvert : la borne a échoué")

        monkeypatch.setattr(openpyxl, "load_workbook", _boom)

        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(make_xlsx_huge_shared_strings(), filename="gros.xlsx")
        assert ei.value.code == "excel_shared_strings_too_large"

    def test_reasonable_table_passes(self, settings):
        settings.DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB = 50
        r = extract_document(make_xlsx_huge_shared_strings(n=200), filename="ok.xlsx")
        assert r.status.value == "success"
        assert r.metadata["excel"]["shared_strings_mb"] >= 0


# --------------------------------------------------------------------------- #
# Gardes du lot E0, réutilisées
# --------------------------------------------------------------------------- #
class TestE0GuardsReused:
    def test_zip_bomb_rejected(self, settings):
        settings.DOCUMENT_EXCEL_MAX_UNCOMPRESSED_MB = 50
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(make_xlsx_zip_bomb(mb=200), filename="bombe.xlsx")
        assert ei.value.code.startswith("excel_")
        assert "uncompressed" in ei.value.code or "ratio" in ei.value.code

    def test_unsafe_xml_rejected(self):
        """DOCTYPE/ENTITY dans une feuille -> garde XML partagée (lot E0)."""
        with pytest.raises(RejectedFile) as ei:
            extract_document(make_xlsx_unsafe_xml(), filename="piege.xlsx")
        assert ei.value.code == "unsafe_xml"

    def test_unsafe_payload_never_reaches_the_xml_parser(self, monkeypatch):
        """Une bombe à entités n'explose QU'À L'INTÉRIEUR du parseur.

        On n'exige pas que le parseur ne tourne jamais (les parties SAINES — workbook,
        relations — sont bien parsées) : on exige qu'il ne voie JAMAIS d'octets
        porteurs d'une déclaration `<!DOCTYPE`/`<!ENTITY`.
        """
        from apps.rag_core.services.ingest.extraction import excel_workbook as XW

        seen = []
        original = XW.ET.fromstring

        def _spy(payload, *a, **kw):
            seen.append(payload)
            return original(payload, *a, **kw)

        monkeypatch.setattr(XW.ET, "fromstring", _spy)

        with pytest.raises(RejectedFile) as ei:
            extract_document(make_xlsx_unsafe_xml(), filename="piege.xlsx")
        assert ei.value.code == "unsafe_xml"

        for payload in seen:
            low = payload.lower() if isinstance(payload, bytes) else payload.encode().lower()
            assert b"<!doctype" not in low
            assert b"<!entity" not in low

    def test_xlsm_still_refused(self):
        """Une macro reste refusée — et n'est jamais lue."""
        with pytest.raises(RejectedFile) as ei:
            extract_document(make_xlsm_zip(), filename="classeur.xlsx")   # renommé !
        assert ei.value.code == "macro_enabled_workbook_refused"

    def test_xlsm_detected_by_content_not_by_name(self):
        assert M.detect_mime(make_xlsm_zip(), _ctx("innocent.xlsx")) == M.XLSM


# --------------------------------------------------------------------------- #
# Rien n'est exécuté, ouvert ou suivi
# --------------------------------------------------------------------------- #
class TestNothingIsExecuted:
    def test_no_network_access_during_extraction(self, monkeypatch):
        """Un classeur porte une formule `WEBSERVICE`. Aucune socket ne doit s'ouvrir."""
        import socket

        def _forbidden(*a, **kw):  # pragma: no cover
            raise AssertionError("l'extraction Excel ne doit ouvrir AUCUNE connexion")

        monkeypatch.setattr(socket.socket, "connect", _forbidden)
        monkeypatch.setattr(socket, "create_connection", _forbidden)

        body = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>url</t></is></c></row>'
            '<row r="2"><c r="A2"><f>WEBSERVICE("http://example.invalid/x")</f></c></row>'
        )
        r = extract_document(make_raw_xlsx(raw_sheet(body, dimension="A1:A2")),
                             filename="c.xlsx")
        # La formule est comptée, JAMAIS évaluée, JAMAIS indexée comme texte.
        assert r.metadata["excel"]["formula_count"] == 1
        assert "WEBSERVICE" not in (r.text or "")

    def test_formula_is_never_evaluated(self):
        """`=1+1` sans cache -> vide. Un moteur donnerait 2 ; openpyxl ne calcule pas."""
        body = (
            '<row r="1"><c r="A1" t="inlineStr"><is><t>calc</t></is></c></row>'
            '<row r="2"><c r="A2"><f>1+1</f></c></row>'
        )
        r = extract_document(make_raw_xlsx(raw_sheet(body, dimension="A1:A2")),
                             filename="c.xlsx")
        assert "2" not in (r.text or "")
        assert r.metadata["excel"]["formula_uncached_count"] == 1

    def test_no_temporary_file_is_created(self, tmp_path, monkeypatch):
        """Le pipeline travaille en `bytes` : aucun fichier temporaire (contrat E1)."""
        import tempfile

        # L'extraction s'exécute dans un processus enfant (HARDENING-03) : une liste en
        # mémoire y resterait confinée et cette assertion d'ABSENCE passerait à vide —
        # vraie parce qu'on n'observe plus rien, pas parce que rien n'a été créé.
        from tests_root.process_witness import TemoinPartage

        created = TemoinPartage(prefixe="xlsx-tempfiles-")
        orig = tempfile.NamedTemporaryFile

        def _spy(*a, **kw):  # pragma: no cover
            created.note()
            return orig(*a, **kw)

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", _spy)
        extract_document(build_xlsx({"S": [["a"], [1]]}), filename="c.xlsx")
        assert created.valeurs == []


# --------------------------------------------------------------------------- #
# Porte d'entrée : allowlist, flag, corpus
# --------------------------------------------------------------------------- #
class TestGate:
    def test_xlsx_is_classified_as_excel(self):
        mime, fmt = validate_and_classify(build_xlsx({"S": [["a"], [1]]}), _ctx())
        assert mime == M.XLSX
        assert fmt == FMT_EXCEL

    def test_disabled_falls_back_to_historic_refusal(self, settings):
        settings.DOCUMENT_EXCEL_ENABLED = False
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(build_xlsx({"S": [["a"], [1]]}), _ctx())
        assert ei.value.code == "unsupported_office_format"

    def test_corpus_path_refuses_excel(self):
        """🔒 L'ingestion corpus est SYNCHRONE : un classeur y bloquerait une requête HTTP."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(
                build_xlsx({"S": [["a"], [1]]}), _ctx(), allow_excel=False
            )
        assert ei.value.code == "excel_not_supported_here"

    def test_corpus_gate_also_applies_to_extract_document(self):
        with pytest.raises(RejectedFile) as ei:
            extract_document(
                build_xlsx({"S": [["a"], [1]]}), filename="c.xlsx", allow_excel=False
            )
        assert ei.value.code == "excel_not_supported_here"

    def test_other_office_formats_still_refused(self):
        from ._builders import make_legacy_ole_header, make_ods_zip

        for data, code in (
            (make_ods_zip(), "unsupported_spreadsheet_format"),
            (make_legacy_ole_header(), "legacy_office_format"),
        ):
            with pytest.raises(RejectedFile) as ei:
                validate_and_classify(data, _ctx())
            assert ei.value.code == code


# --------------------------------------------------------------------------- #
# Excel force l'asynchrone (comme l'OCR)
# --------------------------------------------------------------------------- #
class TestAsyncPolicy:
    def test_excel_forces_async_even_when_client_asks_sync(self, settings):
        from apps.rag_core.services.ingest.async_policy import resolve_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        # Un classeur de 500 k cellules prend ~7 s (E1) : jamais dans une requête HTTP.
        assert resolve_async(explicit="false", format_key=FMT_EXCEL) is True
        assert resolve_async(explicit=False, format_key=FMT_EXCEL) is True

    def test_other_formats_keep_their_behaviour(self, settings):
        from apps.rag_core.services.ingest.async_policy import resolve_async

        settings.RAG_INGEST_ASYNC_DEFAULT = False
        assert resolve_async(explicit="false", format_key="pdf") is False
        assert resolve_async(format_key="docx") is False

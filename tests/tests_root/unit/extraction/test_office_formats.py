"""Lot E0 — détection par CONTENU des familles Office et politique de refus.

Invariant central : la décision ne dépend JAMAIS du nom, de l'extension ou du
Content-Type déclaré par le client. Uniquement du contenu réel de l'archive.

Depuis le lot E2, `.xlsx` est ACCEPTÉ (extrait par openpyxl). Tous les autres
formats tableur — `.xlsm`, `.xls`, `.ods` — restent refusés, et correctement.

Depuis une version antérieure, `.pptx` est ACCEPTÉ (extrait par python-pptx). Les autres formats de
présentation — `.pptm`, `.ppt`, `.odp` — restent refusés, chacun avec son propre code.
"""
import pytest

from apps.rag_core.services.ingest.extraction import (
    RejectedFile,
    UnsupportedFormat,
    extract_document,
    supported_formats,
)
from apps.rag_core.services.ingest.extraction import mime as M
from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.validation import validate_and_classify

from ._builders import (
    build_pptx,
    make_docx,
    make_generic_zip,
    make_legacy_ole_header,
    make_odp_zip,
    make_ods_zip,
    make_odt_zip,
    make_ppt_media_only_zip,
    make_pptm_zip,
    make_pptm_zip_by_content_type,
    make_pptx_zip,
    make_xlsm_zip,
    make_xlsx_zip,
)


def _ctx(name="f", ct=""):
    return ExtractionContext(filename=name, declared_content_type=ct)


# --------------------------------------------------------------------------- #
# Détection MIME : XLSX vs XLSM
# --------------------------------------------------------------------------- #
class TestExcelMimeDetection:
    def test_xlsx_without_macro(self):
        assert M.detect_mime(make_xlsx_zip(), _ctx("budget.xlsx")) == M.XLSX

    def test_xlsm_via_vba_binary(self):
        data = make_xlsx_zip(macro_binary=True)
        assert M.detect_mime(data, _ctx("budget.xlsm")) == M.XLSM

    def test_xlsm_via_macro_content_type_only(self):
        # Pas de vbaProject.bin : seul le Content-Type déclare macroEnabled.
        data = make_xlsx_zip(macro_content_type=True)
        assert M.detect_mime(data, _ctx("budget.xlsm")) == M.XLSM

    def test_xlsm_renamed_xlsx_is_still_xlsm(self):
        """LE test qui motive tout le lot.

        Un vrai XLSM, nommé .xlsx, avec le Content-Type XLSX déclaré par le client :
        il doit rester un XLSM. Sinon le refus des macros serait contournable par un
        simple renommage le jour où XLSX sera accepté.
        """
        data = make_xlsm_zip()
        detected = M.detect_mime(
            data,
            _ctx(
                "budget.xlsx",
                ct="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        )
        assert detected == M.XLSM

    def test_xlsx_renamed_xlsm_is_still_xlsx(self):
        # Symétrique : le nom ne peut pas non plus INVENTER une macro.
        data = make_xlsx_zip()
        assert M.detect_mime(data, _ctx("innocent.xlsm")) == M.XLSX

    def test_incomplete_xl_folder_is_not_a_workbook(self):
        # Un ZIP contenant un dossier `xl/` sans `xl/workbook.xml` n'est PAS un classeur.
        data = make_xlsx_zip(workbook_part=False)
        assert M.detect_mime(data, _ctx("faux.xlsx")) == M.ZIP


class TestOtherOfficeMimeDetection:
    @pytest.mark.parametrize(
        ("payload_fn", "name", "expected"),
        [
            (make_ods_zip, "budget.ods", M.ODS),
            (make_ods_zip, "archive.zip", M.ODS),
            (make_legacy_ole_header, "budget.xls", M.LEGACY_OFFICE),
            (make_legacy_ole_header, "rapport.pdf", M.LEGACY_OFFICE),
        ],
        ids=["ods-by-mimetype-entry", "ods-renamed-zip", "legacy-ole2", "legacy-ole2-regardless-of-name"],
    )
    def test_detection_is_content_based_never_name_based(self, payload_fn, name, expected):
        assert M.detect_mime(payload_fn(), _ctx(name)) == expected


class TestZipFamilyNonRegression:
    """La modification de `_detect_zip_family` est la zone la plus risquée du lot."""

    def test_docx_still_docx(self):
        assert M.detect_mime(make_docx(), _ctx("a.docx")) == M.DOCX

    def test_docm_still_docm(self):
        assert M.detect_mime(make_docx(macro=True), _ctx("a.docm")) == M.DOCM

    def test_docm_renamed_docx_is_still_docm(self):
        assert M.detect_mime(make_docx(macro=True), _ctx("a.docx")) == M.DOCM

    def test_pptx_still_pptx(self):
        assert M.detect_mime(make_pptx_zip(), _ctx("a.pptx")) == M.PPTX

    def test_pptm_detected_by_vba_binary(self):
        """Lot P1 : PresentationML consulte enfin le marqueur macro, comme Word/Excel."""
        assert M.detect_mime(make_pptm_zip(), _ctx("a.pptm")) == M.PPTM

    def test_pptm_detected_by_content_type_alone(self):
        assert M.detect_mime(make_pptm_zip_by_content_type(), _ctx("a.pptm")) == M.PPTM

    def test_pptm_renamed_pptx_is_still_pptm(self):
        """LE test qui motive l'ajout de M.PPTM.

        Avant une version antérieure, `_detect_zip_family` renvoyait PPTX pour tout `ppt/` sans
        consulter `has_macro`. C'était sans effet tant que PPTX était refusé en bloc ;
        avec la porte ouverte, un `.pptm` renommé `.pptx` serait devenu indétectable et
        aurait contourné le refus des macros appliqué à `.docm` et `.xlsm`.
        """
        assert M.detect_mime(make_pptm_zip(), _ctx("a.pptx")) == M.PPTM

    def test_incomplete_ppt_folder_is_not_a_presentation(self):
        """Symétrique d'`xl/` incomplet : un dossier `ppt/` sans présentation = ZIP.

        Sans la partie canonique, l'archive serait routée vers l'extracteur PPTX, qui
        échouerait — autant la classer correctement dès la détection.
        """
        assert M.detect_mime(make_ppt_media_only_zip(), _ctx("a.pptx")) == M.ZIP

    def test_odp_detected_by_its_mimetype_entry(self):
        assert M.detect_mime(make_odp_zip(), _ctx("deck.odp")) == M.ODP

    def test_odp_detected_regardless_of_name(self):
        assert M.detect_mime(make_odp_zip(), _ctx("archive.zip")) == M.ODP

    def test_generic_zip_still_zip(self):
        assert M.detect_mime(make_generic_zip(), _ctx("a.zip")) == M.ZIP

    def test_corrupted_zip_falls_back_to_zip(self):
        # En-tête ZIP valide, contenu cassé : la détection ne doit jamais lever.
        assert M.detect_mime(b"PK\x03\x04" + b"\x00" * 40, _ctx("a.docx")) == M.ZIP


# --------------------------------------------------------------------------- #
# Politique de refus
# --------------------------------------------------------------------------- #
class TestOfficeRejection:
    def test_xlsm_refused(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_xlsm_zip(), _ctx("budget.xlsm"))
        assert ei.value.code == "macro_enabled_workbook_refused"
        assert "macro" in ei.value.detail.lower()

    def test_xlsm_renamed_xlsx_refused_with_macro_code(self):
        with pytest.raises(RejectedFile) as ei:
            extract_document(
                make_xlsm_zip(),
                filename="budget.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        assert ei.value.code == "macro_enabled_workbook_refused"

    def test_xlsx_is_now_accepted(self):
        """Depuis le lot E2, un .xlsx SANS macro est extrait (et non plus refusé).

        Le refus historique `unsupported_office_format` ne subsiste que si
        DOCUMENT_EXCEL_ENABLED=0 (cf. test_excel_security.TestGate).
        """
        _, fmt = validate_and_classify(make_xlsx_zip(), _ctx("budget.xlsx"))
        assert fmt == "excel"

    def test_registry_contents(self):
        assert supported_formats() == [
            "csv", "docx", "excel", "html", "image", "markdown", "pdf", "pptx", "txt"
        ]

    def test_pptx_is_now_accepted(self):
        """Depuis une version antérieure, un .pptx SANS macro est extrait (et non plus refusé).

        Le refus historique `unsupported_office_format` ne subsiste que si
        DOCUMENT_PPTX_ENABLED=0 (cf. TestPptxGate).
        """
        _, fmt = validate_and_classify(build_pptx([{"title": "T", "bullets": ["a"]}]),
                                       _ctx("deck.pptx"))
        assert fmt == "pptx"

    def test_pptm_refused_as_macro_not_as_unsupported(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_pptm_zip(), _ctx("deck.pptm"))
        assert ei.value.code == "macro_enabled_presentation_refused"
        assert "macro" in ei.value.detail.lower()

    def test_pptm_renamed_pptx_refused_with_macro_code(self):
        """L'extension ne doit JAMAIS ouvrir la porte : décision par contenu."""
        with pytest.raises(RejectedFile) as ei:
            extract_document(
                make_pptm_zip(),
                filename="deck.pptx",
                content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        assert ei.value.code == "macro_enabled_presentation_refused"

    def test_odp_refused_actionable_not_as_archive(self):
        """Régression visée : « envoyez les fichiers individuellement » pour un .odp."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_odp_zip(), _ctx("deck.odp"))
        assert ei.value.code == "unsupported_presentation_format"
        assert ei.value.code != "archive_not_supported"
        assert ".pptx" in ei.value.detail

    @pytest.mark.parametrize(("name", "hint"), [("deck.ppt", ".pptx"), ("budget.xls", ".xls")], ids=["ppt", "xls"])
    def test_legacy_office_refused_actionable(self, name, hint):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_legacy_ole_header(), _ctx(name))
        assert ei.value.code == "legacy_office_format"
        # Message spécialisé grâce à l'extension — mais l'extension n'autorise rien.
        assert hint in ei.value.detail

    def test_legacy_office_generic_message_without_xls_extension(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_legacy_ole_header(), _ctx("vieux.doc"))
        assert ei.value.code == "legacy_office_format"
        assert "moderne" in ei.value.detail.lower()

    def test_ods_refused_actionable_not_as_archive(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_ods_zip(), _ctx("budget.ods"))
        assert ei.value.code == "unsupported_spreadsheet_format"
        # Régression visée : ne PLUS dire « archive non supportée » pour un ODS valide.
        assert ei.value.code != "archive_not_supported"
        assert "csv" in ei.value.detail.lower()

    def test_docm_still_refused(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_docx(macro=True), _ctx("a.docm"))
        assert ei.value.code == "macro_enabled_document_refused"

    def test_generic_zip_still_archive_not_supported(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_generic_zip(), _ctx("a.zip"))
        assert ei.value.code == "archive_not_supported"


class TestDetectionOrder:
    def test_exe_renamed_xlsx_is_refused_as_executable(self):
        # L'ordre reste sûr : exécutable AVANT toute famille Office.
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(b"MZ\x90\x00" + b"\x00" * 80, _ctx("budget.xlsx"))
        assert ei.value.code == "executable_refused"

    def test_docx_still_accepted(self):
        # Non-régression : le format documentaire le plus proche reste accepté.
        _, fmt = validate_and_classify(make_docx(), _ctx("a.docx"))
        assert fmt == "docx"

    def test_no_office_refusal_raises_unsupported_format_415(self):
        """Les refus Office restent des RejectedFile (400), pas des UnsupportedFormat (415).

        Convention préexistante (DOCM) : on ne la change pas.
        (`.xlsx` n'est plus dans cette liste : il est ACCEPTÉ depuis le lot E2 ;
        `.pptx` non plus : il est ACCEPTÉ depuis une version antérieure.)
        """
        for data, name in (
            (make_xlsm_zip(), "a.xlsm"),
            (make_ods_zip(), "a.ods"),
            (make_legacy_ole_header(), "a.xls"),
            (make_pptm_zip(), "a.pptm"),
            (make_odp_zip(), "a.odp"),
        ):
            with pytest.raises(RejectedFile):
                validate_and_classify(data, _ctx(name))
            with pytest.raises(Exception) as ei:
                validate_and_classify(data, _ctx(name))
            assert not isinstance(ei.value, UnsupportedFormat)


class TestPptxGate:
    """Lot P1 — la porte `DOCUMENT_PPTX_ENABLED`, calquée sur celle d'Excel.

    Le refus historique `M.PPTX` est CONSERVÉ dans `_REJECT` : fermer le flag doit
    suffire à revenir au comportement d'avant le lot, sans toucher au code.
    """

    def test_gate_closed_falls_back_to_the_historical_refusal(self, settings):
        settings.DOCUMENT_PPTX_ENABLED = False
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(build_pptx([{"title": "T", "bullets": ["a"]}]),
                                  _ctx("deck.pptx"))
        assert ei.value.code == "unsupported_office_format"

    def test_gate_closed_still_refuses_macros_with_the_macro_code(self, settings):
        """Fermer la porte ne doit pas dégrader le diagnostic d'un fichier à macros."""
        settings.DOCUMENT_PPTX_ENABLED = False
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_pptm_zip(), _ctx("deck.pptm"))
        assert ei.value.code == "macro_enabled_presentation_refused"

    def test_corpus_refuses_pptx_because_it_is_synchronous(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(
                build_pptx([{"title": "T", "bullets": ["a"]}]),
                _ctx("deck.pptx"),
                allow_pptx=False,
            )
        assert ei.value.code == "pptx_not_supported_here"

    def test_corpus_gate_does_not_affect_the_default_path(self):
        _, fmt = validate_and_classify(
            build_pptx([{"title": "T", "bullets": ["a"]}]), _ctx("deck.pptx")
        )
        assert fmt == "pptx"


class TestOdtNamedNotArchive:
    """`.odt` — le format reste NON SUPPORTÉ ; seul son NOM est corrigé.

    Avant ce lot, un texte OpenDocument n'était reconnu par aucune entrée dédiée et
    retombait en ZIP générique : l'utilisateur lisait « Archive (.zip) non supportée :
    envoyez les fichiers individuellement ». Consigne absurde pour un traitement de
    texte — et refus correct pour une raison fausse.
    """

    def test_odt_detected_by_its_mimetype_entry(self):
        assert M.detect_mime(make_odt_zip(), _ctx("note.odt")) == M.ODT

    def test_odt_detected_regardless_of_name(self):
        # Doctrine inchangée : le nom n'autorise ni n'interdit jamais rien.
        assert M.detect_mime(make_odt_zip(), _ctx("archive.zip")) == M.ODT

    def test_odt_refused_actionable_not_as_archive(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_odt_zip(), _ctx("note.odt"))
        assert ei.value.code == "unsupported_text_document_format"
        assert ei.value.code != "archive_not_supported"

    def test_odt_message_names_the_format_and_gives_a_real_way_out(self):
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_odt_zip(), _ctx("note.odt"))
        detail = ei.value.detail
        assert ".odt" in detail
        assert ".docx" in detail
        # Jamais la consigne d'archive : c'est exactement le défaut corrigé.
        assert "individuellement" not in detail

    def test_odt_stays_unsupported(self):
        """Ce lot NOMME le format ; il ne l'implémente pas."""
        assert "odt" not in supported_formats()

    def test_ods_and_odp_untouched(self):
        """Non-régression : les deux autres familles ODF gardent leur code."""
        with pytest.raises(RejectedFile) as ods:
            validate_and_classify(make_ods_zip(), _ctx("budget.ods"))
        assert ods.value.code == "unsupported_spreadsheet_format"
        with pytest.raises(RejectedFile) as odp:
            validate_and_classify(make_odp_zip(), _ctx("deck.odp"))
        assert odp.value.code == "unsupported_presentation_format"

    def test_generic_zip_still_says_archive(self):
        """Une VRAIE archive garde le message d'archive — le correctif ne déteint pas."""
        with pytest.raises(RejectedFile) as ei:
            validate_and_classify(make_generic_zip(), _ctx("lot.zip"))
        assert ei.value.code == "archive_not_supported"

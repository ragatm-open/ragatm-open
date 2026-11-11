"""`preview_kind` — décision canonique du visualiseur à employer.

Le viewer `/doc/<id>` chargeait tout document dans une iframe PDF ; un `.xlsx` y
produisait une zone blanche. La correction commence ici : le backend dit quel
visualiseur employer, à partir du type MIME détecté PAR CONTENU à l'ingestion.
"""
import pytest

from api.v1.documents.preview_kind import (
    PREVIEW_DOCUMENT,
    PREVIEW_PRESENTATION,
    PREVIEW_PDF,
    PREVIEW_SPREADSHEET,
    PREVIEW_TEXT,
    PREVIEW_UNSUPPORTED,
    resolve_preview_kind,
)
from apps.rag_core.services.ingest.extraction import mime as M


class TestResolvePreviewKind:
    @pytest.mark.parametrize(
        "ctype,expected",
        [
            (M.PDF, PREVIEW_PDF),
            (M.XLSX, PREVIEW_SPREADSHEET),
            (M.PLAIN, PREVIEW_TEXT),
            (M.MARKDOWN, PREVIEW_TEXT),
        ],
    )
    def test_types_rendables(self, ctype, expected):
        assert resolve_preview_kind(content_type=ctype) == expected

    @pytest.mark.parametrize(
        "ctype",
        [M.CSV, M.PNG, M.JPEG, M.OUTLOOK_MSG, M.HTML, M.ZIP],
    )
    def test_types_non_rendables(self, ctype):
        assert resolve_preview_kind(content_type=ctype) == PREVIEW_UNSUPPORTED

    def test_pptx_route_vers_le_visualiseur_de_presentation(self):
        assert resolve_preview_kind(content_type=M.PPTX) == PREVIEW_PRESENTATION
        assert resolve_preview_kind(content_type="", filename="d.pptx") == PREVIEW_PRESENTATION

    def test_pptx_referme_par_le_drapeau(self, settings):
        settings.DOCUMENT_PPTX_PREVIEW_ENABLED = False
        assert resolve_preview_kind(content_type=M.PPTX) == PREVIEW_UNSUPPORTED
        # Les autres genres ne sont pas gouvernés par ce drapeau.
        assert resolve_preview_kind(content_type=M.DOCX) == PREVIEW_DOCUMENT
        assert resolve_preview_kind(content_type=M.PDF) == PREVIEW_PDF

    def test_pptm_macro_jamais_previsualise(self):
        # `.pptm` est refusé par CONTENU à l'ingestion, même renommé `.pptx`.
        assert resolve_preview_kind(content_type=M.PPTM, filename="m.pptm") == PREVIEW_UNSUPPORTED

    def test_docx_route_vers_le_visualiseur_documentaire(self):
        assert resolve_preview_kind(content_type=M.DOCX) == PREVIEW_DOCUMENT
        assert resolve_preview_kind(content_type="", filename="r.docx") == PREVIEW_DOCUMENT

    def test_docx_referme_par_le_drapeau(self, settings):
        # Drapeau fermé : le DOCX retombe sur l'état explicite (nom, format,
        # téléchargement) — jamais sur une zone blanche.
        settings.DOCUMENT_DOCX_PREVIEW_ENABLED = False
        assert resolve_preview_kind(content_type=M.DOCX) == PREVIEW_UNSUPPORTED
        assert resolve_preview_kind(content_type="", filename="r.docx") == PREVIEW_UNSUPPORTED
        # Les autres genres ne sont pas gouvernés par ce drapeau.
        assert resolve_preview_kind(content_type=M.PDF) == PREVIEW_PDF
        assert resolve_preview_kind(content_type=M.XLSX) == PREVIEW_SPREADSHEET

    def test_csv_n_est_pas_un_tableur(self):
        # Les chunks CSV passent par le chunker texte générique : ni feuille, ni
        # plage, ni ligne persistée. Le visualiseur tabulaire ne saurait pas où
        # surligner — l'annoncer comme pris en charge serait un mensonge.
        assert resolve_preview_kind(content_type=M.CSV, filename="a.csv") == PREVIEW_UNSUPPORTED
        assert resolve_preview_kind(content_type="", filename="a.csv") == PREVIEW_UNSUPPORTED

    def test_classeur_macro_jamais_previsualise(self):
        # `.xlsm` est refusé à l'ingestion par CONTENU ; il ne doit pas non plus
        # être routé vers le visualiseur tabulaire s'il en existe un en base.
        assert resolve_preview_kind(content_type=M.XLSM, filename="m.xlsm") == PREVIEW_UNSUPPORTED

    def test_le_type_prime_sur_l_extension(self):
        # Doctrine du dépôt : ne jamais faire confiance à l'extension. Un format
        # non rendable renommé `.pdf` ne doit pas atterrir dans le lecteur PDF,
        # et chaque format rendable va vers SON visualiseur, pas celui de
        # l'extension usurpée.
        assert resolve_preview_kind(content_type=M.HTML, filename="piege.pdf") == PREVIEW_UNSUPPORTED
        assert resolve_preview_kind(content_type=M.DOCX, filename="piege.pdf") == PREVIEW_DOCUMENT
        assert resolve_preview_kind(content_type=M.PPTX, filename="piege.pdf") == PREVIEW_PRESENTATION
        assert resolve_preview_kind(content_type=M.PDF, filename="piege.xlsx") == PREVIEW_PDF

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("a.pdf", PREVIEW_PDF),
            ("a.xlsx", PREVIEW_SPREADSHEET),
            ("A.XLSX", PREVIEW_SPREADSHEET),
            ("a.txt", PREVIEW_TEXT),
            ("a.md", PREVIEW_TEXT),
            ("a.markdown", PREVIEW_TEXT),
            ("a.csv", PREVIEW_UNSUPPORTED),
            ("a.bin", PREVIEW_UNSUPPORTED),
            ("sans_extension", PREVIEW_UNSUPPORTED),
            ("", PREVIEW_UNSUPPORTED),
        ],
    )
    def test_repli_extension_si_type_absent(self, filename, expected):
        assert resolve_preview_kind(content_type="", filename=filename) == expected

    def test_octet_stream_traite_comme_type_absent(self):
        # Certains clients envoient `application/octet-stream` : ce n'est pas une
        # information, on autorise donc le repli sur l'extension.
        assert resolve_preview_kind(content_type=M.OCTET, filename="a.xlsx") == PREVIEW_SPREADSHEET

    def test_parametres_de_charset_ignores(self):
        assert resolve_preview_kind(content_type="application/pdf; charset=binary") == PREVIEW_PDF
        assert resolve_preview_kind(content_type="  APPLICATION/PDF  ") == PREVIEW_PDF

    def test_ne_leve_jamais(self):
        assert resolve_preview_kind() == PREVIEW_UNSUPPORTED
        assert resolve_preview_kind(content_type=None, filename=None) == PREVIEW_UNSUPPORTED  # type: ignore[arg-type]


@pytest.mark.django_db
class TestSerialisation:
    def test_le_detail_document_expose_preview_kind(self, api_client, rag_space):
        from apps.rag_core.models.document import Document

        doc = Document.objects.create(
            rag_space=rag_space,
            filename="REFERENTIEL.xlsx",
            content_type=M.XLSX,
            status="READY",
        )
        res = api_client.get(f"/api/v1/documents/{doc.id}/")
        assert res.status_code == 200, res.data
        assert res.data["preview_kind"] == PREVIEW_SPREADSHEET
        # Les champs dont dépend le repli frontend restent présents.
        assert res.data["filename"] == "REFERENTIEL.xlsx"
        assert res.data["content_type"] == M.XLSX

    def test_pdf_conserve_son_genre(self, api_client, rag_space):
        from apps.rag_core.models.document import Document

        doc = Document.objects.create(
            rag_space=rag_space, filename="r.pdf", content_type=M.PDF, status="READY"
        )
        res = api_client.get(f"/api/v1/documents/{doc.id}/")
        assert res.status_code == 200
        assert res.data["preview_kind"] == PREVIEW_PDF

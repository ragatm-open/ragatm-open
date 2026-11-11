"""câblage du rail générique dans `PdfExtractor`.

Trois contrats sont vérifiés ici, qui ne peuvent l'être au niveau du moteur seul :

- **drapeau éteint** : le comportement PDF est celui de la ligne de base, au
  caractère près, et aucune analyse de mise en page n'est engagée ;
- **pas de doublon ambigu** : une région structurée quitte le texte de page, sinon
  la version aplatie concurrencerait la représentation sûre à la recherche ;
- **repli sans perte** : un candidat non structurable laisse le texte de page
  intact — on préfère du texte ambigu à du contenu disparu.
"""
from __future__ import annotations

import pytest

# RAGatm Open : moteur natif PyMuPDF = extra optionnel `pdf-mupdf` (AGPL), jamais installé par défaut.
fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.pdf_extractor import PdfExtractor

from . import _pdf_table_fixtures as F

# le rail ne s'applique qu'aux RagSpace listés ; ce contrat-ci porte sur l'extracteur.
SPACE_ID = "6f1c2a4e-0d3b-4c5e-9a7f-1b2c3d4e5f60"


@pytest.fixture
def generic_on(settings):
    settings.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = True
    settings.DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS = [SPACE_ID]
    return settings


@pytest.fixture
def generic_off(settings):
    settings.DOCUMENT_PDF_GENERIC_TABLES_ENABLED = False
    return settings


def _extract(data: bytes):
    return PdfExtractor().extract(data, ExtractionContext(filename="fixture.pdf", rag_space_id=SPACE_ID))


def _table_chunks(res):
    return [t for t in res.tables
            if str((t.get("meta") or {}).get("table_type", "")).startswith("generic")]


# --------------------------------------------------------------------------- #
# §5 / §100 — contrat « drapeau éteint »
# --------------------------------------------------------------------------- #
class TestFlagOff:
    def test_no_generic_table_is_produced(self, generic_off):
        res = _extract(F.flat_three_column())
        assert _table_chunks(res) == []

    def test_page_text_is_the_baseline_text(self, generic_off):
        res = _extract(F.prose_around_table())
        page_text = dict(res.pages)[1]
        assert F.PROSE_BEFORE in page_text
        assert "A distinctive cell sentence for duplication checks" in page_text

    def test_no_generic_metadata_is_added(self, generic_off):
        res = _extract(F.flat_three_column())
        assert "generic_tables" not in res.metadata


# --------------------------------------------------------------------------- #
# §50 / §54 / §95 — la région structurée quitte le texte de page
# --------------------------------------------------------------------------- #
class TestNoAmbiguousDuplicate:
    DISTINCTIVE = "A distinctive cell sentence for duplication checks"

    def test_cell_sentence_lives_in_the_table_chunk(self, generic_on):
        res = _extract(F.prose_around_table())
        chunks = _table_chunks(res)
        assert chunks, "aucun fragment structuré produit"
        assert any(self.DISTINCTIVE in c["markdown"] for c in chunks)

    def test_cell_sentence_left_the_page_text(self, generic_on):
        res = _extract(F.prose_around_table())
        assert self.DISTINCTIVE not in dict(res.pages)[1]

    def test_surrounding_prose_is_preserved(self, generic_on):
        """§119 : supprimer la région tabulaire ne doit emporter aucune prose."""
        res = _extract(F.prose_around_table())
        page_text = dict(res.pages)[1]
        assert F.PROSE_BEFORE in page_text
        assert F.PROSE_AFTER in page_text


# --------------------------------------------------------------------------- #
# §52 / §63 / §96 — repli sûr
# --------------------------------------------------------------------------- #
class TestFallbackKeepsContent:
    def test_unstructurable_candidate_keeps_its_page_text(self, generic_on):
        res = _extract(F.ambiguous_ragged())
        page_text = dict(res.pages)[1]
        assert "Ambiguous region follows." in page_text
        # Le contenu du candidat non structuré reste indexé sous forme de texte.
        assert "a b c d e f" in " ".join(page_text.split())

    def test_no_table_chunk_is_invented(self, generic_on):
        res = _extract(F.ambiguous_ragged())
        assert _table_chunks(res) == []

    def test_one_column_box_stays_page_text(self, generic_on):
        res = _extract(F.one_column_box())
        assert _table_chunks(res) == []
        assert "boxed paragraph" in dict(res.pages)[1]


# --------------------------------------------------------------------------- #
# §36 / §67 — provenance de page dans les métadonnées émises
# --------------------------------------------------------------------------- #
class TestEmittedProvenance:
    def test_each_fragment_cites_its_own_physical_page(self, generic_on):
        res = _extract(F.row_across_three_pages())
        chunks = _table_chunks(res)
        pages = sorted((c["meta"]["page_number"]) for c in chunks)
        assert pages == [1, 2, 3], pages

    def test_fragments_share_one_logical_row(self, generic_on):
        res = _extract(F.row_across_three_pages())
        ids = {c["meta"]["logical_row_id"] for c in _table_chunks(res)}
        assert len(ids) == 1, ids

    def test_logical_row_pages_are_recorded(self, generic_on):
        res = _extract(F.row_across_three_pages())
        for c in _table_chunks(res):
            assert c["meta"]["logical_row_pages"] == [1, 2, 3]

    def test_continuation_flag_marks_only_later_pages(self, generic_on):
        res = _extract(F.row_across_three_pages())
        by_page = {c["meta"]["page_number"]: c["meta"]["continuation"]
                   for c in _table_chunks(res)}
        assert by_page == {1: False, 2: True, 3: True}


# --------------------------------------------------------------------------- #
# §6 / §81 — le rail spécialisé garde la priorité
# --------------------------------------------------------------------------- #
class TestSpecializedPrecedence:
    def _pile_pdf(self) -> bytes:
        from io import BytesIO

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

        buf = BytesIO()
        data = [
            ["Noeud/Cas", "FX [T]", "FY [T]", "FZ [T]", "MX [Tm]", "MY [Tm]", "MZ [Tm]"],
            ["153/ELU+", "-11.4", "-3.1", "-160", "0", "0", "0"],
        ]
        t = Table(data)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        SimpleDocTemplate(buf, pagesize=A4).build([t])
        return buf.getvalue()

    def test_specialized_table_still_recognized_with_flag_on(self, generic_on):
        res = _extract(self._pile_pdf())
        kinds = [(t.get("meta") or {}).get("table_type") for t in res.tables]
        assert "pile_head_forces" in kinds, kinds

    def test_specialized_page_is_not_also_emitted_generically(self, generic_on):
        res = _extract(self._pile_pdf())
        assert _table_chunks(res) == [], "double représentation de la même page"


class TestUnverifiedTablePages:
    """NATIVE-TABLE-GUARD-1 : une grille détectée mais NON structurée laisse son texte aplati dans la
    page ; la page est signalée pour que ce texte ne serve jamais de preuve chiffrée."""

    def test_unstructurable_grid_marks_its_page(self, generic_on):
        res = _extract(F.ambiguous_ragged())
        assert res.metadata["generic_table_unverified_pages"] == [1]

    def test_structured_table_or_text_box_marks_nothing(self, generic_on):
        assert _extract(F.flat_three_column()).metadata["generic_table_unverified_pages"] == []
        assert _extract(F.one_column_box()).metadata["generic_table_unverified_pages"] == []

    def test_flag_off_adds_no_marking(self, generic_off):
        assert "generic_table_unverified_pages" not in _extract(F.ambiguous_ragged()).metadata

    def test_page_reread_by_the_fallback_engine_is_marked(self, generic_on):
        """NATIVE-TABLE-RECOVERY-2 : la page aux glyphes non droits garde son texte (relu par le moteur
        natif) mais ne prouve plus aucun chiffre ; les pages relues par pdfplumber restent inchangées."""
        from . import _pdf_header_band_fixtures as HB

        res = _extract(HB.band_previous_page(skew=True, straddle=True))
        assert res.metadata["generic_table_unverified_pages"] == [1]
        assert "Gizmo beta" in dict(res.pages)[1]
        assert _extract(HB.band_previous_page(straddle=True)).metadata["generic_table_unverified_pages"] == []

    def test_page_text_chunks_inherit_the_marking(self, generic_on):
        from types import SimpleNamespace

        from apps.rag_core.services.ingest.indexing import _build_chunks

        res = _extract(F.ambiguous_ragged())
        specs, _ = _build_chunks(doc=SimpleNamespace(id="d"), space=SimpleNamespace(id="s"), result=res,
                                 filename="fixture.pdf", chunk_size=800, overlap=0)
        assert specs and all(s.meta.get("native_table_unverified") is True for s in specs)
        clean = _extract(F.prose_around_table())
        specs, _ = _build_chunks(doc=SimpleNamespace(id="d"), space=SimpleNamespace(id="s"), result=clean,
                                 filename="fixture.pdf", chunk_size=800, overlap=0)
        assert not any(s.meta.get("native_table_unverified") for s in specs)

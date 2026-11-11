"""Tests extracteur CSV (séparateurs, encodage, limites, formula injection)."""
import pytest

from apps.rag_core.services.ingest.extraction import ExtractionLimitExceeded, extract_document
from apps.rag_core.services.ingest.extraction.base import BlockType


class TestCsvSeparators:
    def test_comma(self):
        r = extract_document(b"nom,ville\nAlice,Paris\nBob,Lyon\n", filename="d.csv")
        assert r.metadata["columns"] == 2
        assert r.metadata["lines"] == 2
        assert "nom: Alice" in r.text and "ville: Paris" in r.text

    def test_semicolon(self):
        r = extract_document(b"nom;ville\nAlice;Paris\n", filename="d.csv")
        assert r.metadata["csv_delimiter"] == ";"
        assert "nom: Alice" in r.text

    def test_tab(self):
        r = extract_document(b"nom\tville\nAlice\tParis\n", filename="d.tsv")
        assert r.metadata["csv_delimiter"] == "\t"

    def test_rows_are_blocks(self):
        r = extract_document(b"a,b\n1,2\n3,4\n", filename="d.csv")
        rows = [b for b in r.blocks if b.type == BlockType.CSV_ROW]
        assert len(rows) == 2


class TestCsvEncoding:
    def test_latin1(self):
        data = "nom,ville\nClément,Orléans\n".encode("latin-1")
        r = extract_document(data, filename="d.csv")
        assert "Orl" in r.text  # décodé sans crash


class TestCsvLimits:
    def test_too_many_rows(self, settings):
        settings.DOCUMENT_CSV_MAX_ROWS = 3
        body = "a,b\n" + "".join(f"{i},{i}\n" for i in range(10))
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(body.encode(), filename="d.csv")
        assert ei.value.code == "csv_too_many_rows"

    def test_too_many_columns(self, settings):
        settings.DOCUMENT_CSV_MAX_COLUMNS = 2
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(b"a,b,c,d\n1,2,3,4\n", filename="d.csv")
        assert ei.value.code == "csv_too_many_columns"

    def test_cell_too_long(self, settings):
        settings.DOCUMENT_CSV_MAX_CELL_CHARS = 10
        body = "a,b\nshort," + ("x" * 50) + "\n"
        with pytest.raises(ExtractionLimitExceeded) as ei:
            extract_document(body.encode(), filename="d.csv")
        assert ei.value.code == "csv_cell_too_long"


class TestCsvFormulaInjection:
    def test_dangerous_cells_neutralized_and_flagged(self):
        body = "nom,formule\nAlice,=SUM(A1:A9)\nBob,+1+1\nEve,@cmd\n"
        r = extract_document(body.encode(), filename="d.csv")
        assert "csv_formula_injection_neutralized" in r.warnings
        assert r.metadata["dangerous_cells"] == 3
        # neutralisé pour restitution : préfixe apostrophe, pas de "=SUM(" en tête de valeur
        assert "formule: '=SUM" in r.text

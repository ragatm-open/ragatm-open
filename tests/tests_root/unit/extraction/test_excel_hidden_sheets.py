"""Feuilles masquées : `hidden` est indexée, `veryHidden` reste exclue — et on le DIT.

La campagne AirWorks a trouvé une feuille entière absente de l'index : « Convertibles »,
un registre financier (taux d'intérêt, discounts, conversion prices), masquée dans le
classeur. Toute question la concernant était sans réponse possible, alors que la donnée
était dans le fichier.

Excel distingue **deux** états de masquage, et ils ne disent pas la même chose :

- `hidden` : l'auteur a rangé une feuille pour aérer son classeur. Le destinataire la
  réaffiche d'un clic droit. C'est une commodité de PRÉSENTATION — pas un contrôle
  d'accès, et sûrement pas un secret ;
- `veryHidden` : cet état n'est PAS modifiable depuis l'interface d'Excel (il exige du
  VBA). C'est une intention technique délibérée. On la respecte.

Le code savait déjà les distinguer (`SheetInfo.state`) ; c'est la politique qui les
confondait. Et indexer sans signaler serait exposer sans le dire : chaque chunk porte
donc `sheet_state`, projeté jusqu'à la citation.
"""
import pytest

from apps.rag_core.services.ingest.extraction import config
from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.query.source_meta import project_source_meta

from ._builders import build_xlsx


def _wb():
    return {
        "Visible": [["Projet", "Budget"], ["Atlas", 250]],
        "Convertibles": [["Noteholder", "Interest Rate"], ["2020-1", "8.00%"]],
        "Scratch": [["tmp", 1]],
    }


class TestThePolicy:
    def test_hidden_only_is_the_default(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "hidden_only"
        assert config.excel_hidden_sheets_policy() == "hidden_only"
        assert config.excel_sheet_is_indexable("visible") is True
        assert config.excel_sheet_is_indexable("hidden") is True
        assert config.excel_sheet_is_indexable("veryHidden") is False

    def test_none_excludes_every_hidden_sheet(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "none"
        assert config.excel_sheet_is_indexable("hidden") is False
        assert config.excel_sheet_is_indexable("veryHidden") is False
        assert config.excel_sheet_is_indexable("visible") is True

    def test_all_includes_veryhidden(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "all"
        assert config.excel_sheet_is_indexable("veryHidden") is True

    def test_the_deprecated_flag_still_works(self, settings):
        """Aucun environnement existant ne doit casser silencieusement."""
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = ""
        settings.DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS = "1"
        # L'ancien drapeau incluait TOUT : c'était sa sémantique, on la respecte.
        assert config.excel_hidden_sheets_policy() == "all"
        assert config.excel_sheet_is_indexable("veryHidden") is True

    def test_an_unknown_policy_falls_back_to_the_safe_default(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "n'importe quoi"
        settings.DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS = "0"
        assert config.excel_hidden_sheets_policy() == "hidden_only"


class TestExtraction:
    def test_a_hidden_sheet_is_indexed_and_flagged(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "hidden_only"
        r = extract_document(build_xlsx(_wb(), hidden=["Convertibles"]), filename="w.xlsx")

        sheets = {c["meta"]["sheet_name"] for c in r.excel_chunks}
        assert "Convertibles" in sheets

        conv = next(c for c in r.excel_chunks if c["meta"]["sheet_name"] == "Convertibles")
        assert conv["meta"]["sheet_state"] == "hidden"
        assert conv["meta"]["sheet_hidden"] is True
        assert "excel_hidden_sheet_indexed" in r.warnings

    def test_a_very_hidden_sheet_stays_out(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "hidden_only"
        r = extract_document(
            build_xlsx(_wb(), very_hidden=["Scratch"]), filename="w.xlsx",
        )
        sheets = {c["meta"]["sheet_name"] for c in r.excel_chunks}
        assert "Scratch" not in sheets
        assert "excel_veryhidden_sheet_skipped" in r.warnings
        assert r.metadata["excel"]["veryhidden_sheets_skipped"] == 1

    def test_a_visible_sheet_is_untouched(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "hidden_only"
        r = extract_document(build_xlsx(_wb(), hidden=["Convertibles"]), filename="w.xlsx")
        vis = next(c for c in r.excel_chunks if c["meta"]["sheet_name"] == "Visible")
        assert vis["meta"]["sheet_state"] == "visible"
        assert vis["meta"]["sheet_hidden"] is False

    def test_the_document_metadata_counts_both_states(self, settings):
        settings.DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY = "hidden_only"
        r = extract_document(
            build_xlsx(_wb(), hidden=["Convertibles"], very_hidden=["Scratch"]),
            filename="w.xlsx",
        )
        ex = r.metadata["excel"]
        assert ex["hidden_sheets_indexed"] == 1
        assert ex["hidden_sheets_indexed_names"] == ["Convertibles"]
        assert ex["veryhidden_sheets_skipped"] == 1


class TestTheStateReachesTheCitation:
    def test_sheet_state_is_projected_to_the_api(self):
        """Sans cette projection, on exposerait une feuille masquée SANS le dire."""
        meta = {"kind": "excel_table", "sheet_name": "Convertibles",
                "cell_range": "A2:Q8", "sheet_state": "hidden", "sheet_hidden": True}
        out = project_source_meta(meta)
        assert out["sheet_state"] == "hidden"
        assert out["sheet_hidden"] is True

    def test_a_visible_sheet_says_so_too(self):
        out = project_source_meta({"kind": "excel_table", "sheet_name": "S",
                                   "sheet_state": "visible", "sheet_hidden": False})
        assert out["sheet_state"] == "visible"
        assert out["sheet_hidden"] is False

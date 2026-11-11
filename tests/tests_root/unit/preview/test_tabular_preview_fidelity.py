"""TEST CARDINAL — l'aperçu affiche EXACTEMENT ce qui a été indexé.

Un visualiseur est incorrect s'il montre une valeur différente de celle sur
laquelle la citation a été produite. Or l'ingestion ne recopie pas les cellules
brutes : elle les NORMALISE (`excel_normalize.render_cell`). Un `0.1` au format
`0%` est indexé `"10%"` ; un aperçu qui relirait `cell.value` afficherait `0.1`
et mentirait sur la source.

L'oracle n'est pas une valeur écrite à la main dans le test — ce serait vérifier
le test contre lui-même. C'est `stable_source_resolver.extract_region_cells`, qui
reconstruit les cellules d'une région **depuis les chunks réellement indexés**.
La comparaison est faite caractère par caractère.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))

from _builders import build_xlsx  # noqa: E402

from apps.rag_core.benchmark import stable_ground_truth as gt  # noqa: E402
from apps.rag_core.benchmark.stable_source_resolver import (  # noqa: E402
    extract_region_cells,
)
from apps.rag_core.services.ingest.extraction.excel_extractor import (  # noqa: E402
    ExcelExtractor,
)
from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.preview import reader as R  # noqa: E402
from apps.rag_core.services.preview.locator import CellRect, parse_rect  # noqa: E402


# --------------------------------------------------------------------------- #
# Corpus de fidélité — un cas par famille de rendu délicate.
# --------------------------------------------------------------------------- #
FIDELITY_ROWS = [
    ["Code", "Libelle", "Montant", "Taux", "Date", "Duree", "Divers"],
    ["CRE 13101", "Texte simple", 250000, (0.1, "0%"), (44927, "yyyy-mm-dd"), None, ""],
    ["CRE 13102", "Décimal", 1234.56, (0.075, "0.0%"), (44928, "yyyy-mm-dd"), None, None],
    ["CRE 13103", "Zéros initiaux", "00123", (1.0, "0%"), None, None, "déjà du texte"],
    ["CRE 13104", "Unicode éàü ✓ 中文", -42, (0.5, "0.00%"), None, None, "fin"],
    ["CRE 13105", "Entier négatif", -1000000, None, None, None, "x" * 700],
    ["CRE 13106", "Booléen", True, None, None, None, False],
    ["CRE 13107", "Devise", (1284350.5, '#,##0.00 "€"'), None, None, None, ""],
    ["CRE 13108", "Formule dangereuse", "=SUM(A1:A9)", None, None, None, "+1+1"],
    ["CRE 13109", "Arobase", "@here", None, None, None, "-42"],
    ["CRE 13110", "Grand nombre", 9007199254740993, None, None, None, ""],
]


def _index_workbook(data: bytes):
    """Rejoue l'extraction RÉELLE et renvoie des pseudo-chunks (objets .meta/.text)."""
    res = ExcelExtractor().extract(data, ExtractionContext(filename="fidelite.xlsx"))
    chunks = []
    for ch in res.excel_chunks or []:
        chunks.append(
            type("C", (), {"meta": ch["meta"], "text": ch["text"], "id": "c"})()
        )
    return chunks


def _preview_cells(data: bytes, sheet: str, rect: CellRect) -> dict:
    """Cellules NON VIDES rendues par l'aperçu, sous la même forme que l'oracle."""
    wb = R.load_workbook_bounded(data)
    try:
        result = R.read_window(wb, sheet_name=sheet, display=rect)
    finally:
        wb.close()

    out = {}
    for row in result.rows:
        for cell in row.cells:
            norm = gt.normalize_benchmark_evidence_text(cell.value)
            if norm:
                out[(row.row_number, cell.column_index)] = norm
    return out


def _preview_raw(data: bytes, sheet: str, rect: CellRect) -> dict:
    """Cellules brutes (objets `PreviewCell`), pour inspecter les drapeaux."""
    wb = R.load_workbook_bounded(data)
    try:
        result = R.read_window(wb, sheet_name=sheet, display=rect)
    finally:
        wb.close()
    return {
        (row.row_number, cell.column_index): cell
        for row in result.rows
        for cell in row.cells
    }


@pytest.fixture(scope="module")
def workbook() -> bytes:
    return build_xlsx({"Fidelite": FIDELITY_ROWS})


class TestOracleDeFidelite:
    def test_apercu_identique_aux_chunks_indexes(self, workbook):
        """Chaque cellule de la région citée est identique à celle indexée."""
        chunks = _index_workbook(workbook)
        assert chunks, "l'extraction doit produire des chunks"

        table = [c for c in chunks if (c.meta or {}).get("kind") == "excel_table"]
        assert table, "un tableau doit être détecté"

        compared = 0
        for chunk in table:
            meta = chunk.meta
            rect = parse_rect(meta["cell_range"])

            locator = gt.ExcelRegionLocator(
                kind=gt.LOCATOR_KIND_XLSX,
                sheet_name=meta["sheet_name"],
                cell_range=gt.normalize_cell_range(meta["cell_range"]),
                row_start=meta["row_start"],
                row_end=meta["row_end"],
                column_start=meta["column_start"],
                column_end=meta["column_end"],
            )
            expected = extract_region_cells([chunk], locator, warnings=[]).cells
            got = _preview_cells(workbook, meta["sheet_name"], rect)
            raw = _preview_raw(workbook, meta["sheet_name"], rect)

            assert expected, f"l'oracle doit produire des cellules pour {meta['cell_range']}"
            for key, want in expected.items():
                ref = f"{gt.num_to_col(key[1])}{key[0]}"
                assert key in got, f"cellule {ref} absente de l'aperçu"

                cell = raw[key]
                if "truncated" in cell.flags:
                    # Troncature d'AFFICHAGE, bornée et DÉCLARÉE (drapeau +
                    # `full_length`). Ce n'est pas une divergence de valeur : le
                    # préfixe doit rester exactement celui indexé.
                    assert want.startswith(got[key]), (
                        f"DIVERGENCE de préfixe en {ref} : "
                        f"aperçu={got[key][:60]!r}… vs indexé={want[:60]!r}…"
                    )
                    assert cell.full_length == len(cell.value) or cell.full_length > len(
                        cell.value
                    ), f"{ref} : full_length absent alors que la valeur est tronquée"
                else:
                    # Comparaison caractère par caractère : toute divergence échoue.
                    assert got[key] == want, (
                        f"DIVERGENCE en {ref} : "
                        f"aperçu={got[key]!r} vs indexé={want!r}"
                    )
                compared += 1

        assert compared >= 40, f"corpus trop maigre pour conclure ({compared} cellules)"


class TestRenduParType:
    """Vérifie les familles de rendu, contre la normalisation canonique."""

    @pytest.fixture(scope="class")
    def rendered(self, workbook):
        return _preview_cells(workbook, "Fidelite", CellRect(1, 1, 7, 12))

    def test_pourcentage_honore_le_format(self, rendered):
        # 0.1 + format `0%` → "10%", jamais "0.1". C'est LA divergence que ce lot
        # existe pour empêcher.
        assert rendered[(2, 4)] == "10%"
        assert rendered[(3, 4)] == "7.5%"
        assert rendered[(4, 4)] == "100%"
        assert rendered[(5, 4)] == "50.00%"

    def test_zeros_initiaux_preserves(self, rendered):
        assert rendered[(4, 3)] == "00123"

    def test_entiers_sans_decimale_parasite(self, rendered):
        assert rendered[(2, 3)] == "250000"
        assert rendered[(6, 3)] == "-1000000"

    def test_grand_entier_rendu_comme_a_l_ingestion(self, rendered, workbook):
        # Au-delà de 2^53, le FORMAT xlsx lui-même stocke un double : la valeur
        # relue est 9007199254740992, pas ...993. Ce n'est pas une perte de
        # l'aperçu — l'ingestion lit exactement la même chose. Ce qui compte ici
        # n'est donc pas la valeur d'origine mais l'IDENTITÉ avec l'indexé.
        chunks = _index_workbook(workbook)
        indexed = {
            c.meta["sheet_name"]: c.text for c in chunks if "9007199254740" in (c.text or "")
        }
        assert indexed, "la valeur doit apparaître dans un chunk indexé"
        assert rendered[(11, 3)] in next(iter(indexed.values()))

    def test_unicode_preserve(self, rendered):
        assert rendered[(5, 2)] == "Unicode éàü ✓ 中文"

    def test_booleens(self, rendered):
        assert rendered[(7, 3)] == "TRUE"

    def test_texte_dangereux_neutralise(self, rendered):
        # Doctrine anti-injection déjà appliquée à l'ingestion : un TEXTE commençant
        # par `=`/`+`/`-`/`@` est préfixé d'une apostrophe, sans quoi il s'exécuterait
        # une fois collé dans Excel.
        assert rendered[(9, 7)].startswith("'+")     # "+1+1"
        assert rendered[(10, 3)].startswith("'@")    # "@here"
        assert rendered[(10, 7)].startswith("'-")    # "-42"

    def test_formule_sans_cache_n_est_pas_une_valeur(self, rendered):
        # `=SUM(A1:A9)` écrit par openpyxl n'a AUCUNE valeur en cache. Lu en
        # `data_only=True`, il rend vide — et l'aperçu ne fabrique rien. Il ne
        # doit surtout pas afficher le texte de la formule (hors périmètre MVP).
        assert (9, 3) not in rendered

    def test_cellules_vides_rendues_vides_jamais_none(self, workbook):
        wb = R.load_workbook_bounded(workbook)
        try:
            res = R.read_window(wb, sheet_name="Fidelite", display=CellRect(1, 2, 7, 2))
        finally:
            wb.close()
        cells = {c.column_index: c.value for c in res.rows[0].cells}
        # Une cellule vide rend la chaîne vide — jamais "None", jamais absente.
        assert cells[6] == ""
        assert cells[7] == ""
        assert all(isinstance(v, str) for v in cells.values())


class TestDrapeaux:
    def test_chaine_longue_tronquee_et_signalee(self, workbook):
        wb = R.load_workbook_bounded(workbook)
        try:
            res = R.read_window(wb, sheet_name="Fidelite", display=CellRect(7, 6, 7, 6))
        finally:
            wb.close()
        cell = res.rows[0].cells[0]
        assert len(cell.value) == 500          # PREVIEW_MAX_CELL_CHARS
        assert "truncated" in cell.flags       # jamais tronqué en silence
        assert cell.full_length == 700         # la longueur réelle est dite

    def test_valeur_neutralisee_porte_son_drapeau(self, workbook):
        wb = R.load_workbook_bounded(workbook)
        try:
            res = R.read_window(wb, sheet_name="Fidelite", display=CellRect(7, 9, 7, 9))
        finally:
            wb.close()
        assert "neutralized" in res.rows[0].cells[0].flags

    def test_chaque_ligne_porte_une_cellule_par_colonne_affichee(self, workbook):
        # openpyxl en read_only n'émet pas les cellules vides de fin de ligne. Sans
        # complétion, le client devrait deviner les colonnes manquantes — et une
        # valeur se retrouverait affichée sous le mauvais en-tête.
        wb = R.load_workbook_bounded(workbook)
        try:
            res = R.read_window(wb, sheet_name="Fidelite", display=CellRect(1, 1, 7, 12))
        finally:
            wb.close()
        for row in res.rows:
            assert [c.column_index for c in row.cells] == list(range(1, 8)), (
                f"ligne {row.row_number} : colonnes non contiguës"
            )
            assert all(isinstance(c.value, str) for c in row.cells)

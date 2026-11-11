"""Bornes de l'aperçu : deadline réelle, absence de matérialisation, projection.

Le coût d'openpyxl en `read_only` est LINÉAIRE dans l'index de la ligne visée :
atteindre la ligne 19 000 impose de parser les 18 999 précédentes. Une borne
vérifiée seulement APRÈS la lecture ne bornerait donc rien.
"""
import io
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))

from apps.rag_core.services.preview import errors as E  # noqa: E402
from apps.rag_core.services.preview import reader as R  # noqa: E402
from apps.rag_core.services.preview.locator import (  # noqa: E402
    CellRect,
    location_from_chunk_meta,
    assert_no_conflict,
)
from apps.rag_core.services.query.source_meta import project_source_meta  # noqa: E402


def _tall_workbook(n_rows: int = 8000, n_cols: int = 8) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Grande"
    ws.append([f"En-tête {c}" for c in range(1, n_cols + 1)])
    for r in range(2, n_rows + 1):
        ws.append([f"r{r}c{c}" for c in range(1, n_cols + 1)])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


@pytest.fixture(scope="module")
def tall() -> bytes:
    return _tall_workbook()


class TestDeadline:
    def test_budget_depasse_interrompt_la_lecture(self, tall):
        wb = R.load_workbook_bounded(tall)
        started = time.perf_counter()
        try:
            with pytest.raises(E.PreviewTimeout) as exc:
                R.read_window(
                    wb,
                    sheet_name="Grande",
                    display=CellRect(1, 7900, 8, 7950),
                    header_row=1,
                    budget_s=0.001,      # budget volontairement inatteignable
                )
        finally:
            wb.close()
        elapsed = time.perf_counter() - started

        assert exc.value.code == E.ERR_PREVIEW_TIMEOUT
        # L'interruption est RÉELLE : on n'a pas lu la feuille entière avant de
        # constater le dépassement.
        assert elapsed < 5.0, f"la lecture n'a pas été interrompue ({elapsed:.1f}s)"

    def test_budget_suffisant_rend_la_fenetre(self, tall):
        wb = R.load_workbook_bounded(tall)
        try:
            res = R.read_window(
                wb, sheet_name="Grande",
                display=CellRect(1, 100, 8, 120), header_row=1, budget_s=30.0,
            )
        finally:
            wb.close()
        assert [r.row_number for r in res.rows] == list(range(100, 121))
        assert res.header[0] == "En-tête 1"     # capté au vol, sans seconde lecture


class TestAbsenceDeMaterialisation:
    #: Budget de lecture LARGE, posé explicitement. Sans lui, `read_window` retombe
    #: sur `preview_config.effective_timeout_s()` — le délai de PRODUCTION (5 s) —
    #: et ce test mesurait donc, sans le dire, la vitesse de la machine autant que
    #: l'empreinte mémoire. Atteindre la ligne 7 000 coûte ~2,4 s à froid sur un
    #: poste au repos : moins d'un facteur 2 de marge, que la contention de quatre
    #: workers suffit à effacer (`PreviewTimeout` observé sous `-n 4`).
    #:
    #: Ce n'est pas un seuil relevé à l'aveugle : le respect de l'échéance a ses
    #: propres tests, juste au-dessus (`TestDeadline`), qui vérifient à la fois le
    #: budget dépassé et le budget suffisant. Celui-ci a un autre sujet — la
    #: NON-MATÉRIALISATION — et ne doit pas échouer pour une raison étrangère.
    BUDGET_GENEREUX_S = 120.0

    def test_la_feuille_entiere_n_est_jamais_chargee(self, tall):
        import tracemalloc

        wb = R.load_workbook_bounded(tall)
        tracemalloc.start()
        try:
            res = R.read_window(
                wb, sheet_name="Grande", display=CellRect(1, 7000, 8, 7020),
                header_row=1, budget_s=self.BUDGET_GENEREUX_S,
            )
        finally:
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            wb.close()

        assert len(res.rows) == 21
        # 8000 x 8 cellules matérialisées dépasseraient largement ce seuil ; le
        # streaming garde une empreinte quasi constante. C'est CE contrôle qui
        # porte le sujet du test, et il reste inchangé.
        assert peak < 12_000_000, f"pic mémoire trop élevé : {peak/1e6:.1f} Mo"


class TestClampFenetre:
    def test_lignes_plafonnees_et_declarees(self, settings):
        settings.PREVIEW_MAX_ROWS = 10
        rect, truncated, reason = R.clamp_display_rect(CellRect(1, 1, 5, 500))
        assert truncated is True and reason == "max_rows"
        assert rect.row_end - rect.row_start + 1 == 10

    def test_borne_cardinale_prime_et_rogne_les_lignes(self, settings):
        # On rogne les LIGNES, jamais les colonnes : perdre une colonne casserait
        # l'alignement valeur/en-tête, perdre une ligne ne casse rien.
        settings.PREVIEW_MAX_CELLS = 40
        settings.PREVIEW_MAX_ROWS = 1000
        settings.PREVIEW_MAX_COLUMNS = 100
        rect, truncated, reason = R.clamp_display_rect(CellRect(1, 1, 10, 100))
        assert truncated is True and reason == "max_cells"
        assert rect.col_end == 10                       # colonnes intactes
        assert rect.cell_count <= 40

    def test_fenetre_dans_les_bornes_non_tronquee(self):
        rect, truncated, reason = R.clamp_display_rect(CellRect(1, 1, 5, 20))
        assert truncated is False and reason is None
        assert rect == CellRect(1, 1, 5, 20)


class TestLocator:
    def _meta(self, **over):
        base = {
            "kind": "excel_table",
            "sheet_name": "Données",
            "sheet_index": 2,
            "cell_range": "C10:D10",
            "row_start": 10, "row_end": 10,
            "column_start": "C", "column_end": "D",
        }
        base.update(over)
        return base

    def test_data_ranges_prime_sur_cell_range(self):
        loc = location_from_chunk_meta(
            self._meta(data_ranges=["A10:A10", "C10:D10"], chunk_axis="columns")
        )
        assert [r.a1 for r in loc.ranges] == ["A10:A10", "C10:D10"]
        # Le rectangle de CADRAGE couvre tout, mais il ne sert pas au surlignage.
        assert loc.bounding_rect.a1 == "A10:D10"

    def test_repli_sur_cell_range_sans_data_ranges(self):
        loc = location_from_chunk_meta(self._meta())
        assert [r.a1 for r in loc.ranges] == ["C10:D10"]

    def test_index_0_based_refuse(self):
        # La convention du dépôt est 1-based (`SheetInfo.index`). Un 0 signalerait
        # une méta écrite par un autre pipeline : on refuse plutôt que de décaler.
        with pytest.raises(E.PreviewInvalidRequest) as exc:
            location_from_chunk_meta(self._meta(sheet_index=0))
        assert exc.value.code == E.ERR_CHUNK_LOCATION_INVALID

    def test_chunk_non_tabulaire_refuse(self):
        with pytest.raises(E.PreviewUnsupported) as exc:
            location_from_chunk_meta({"kind": "pptx_slide", "slide_number": 3})
        assert exc.value.code == E.ERR_CHUNK_NOT_TABULAR

    def test_plage_absente_refuse(self):
        with pytest.raises(E.PreviewInvalidRequest):
            location_from_chunk_meta(self._meta(cell_range=None, data_range=None))

    def test_nom_normalise_nfc_et_trim(self):
        loc = location_from_chunk_meta(self._meta(sheet_name="  Données  "))
        assert loc.sheet_name == "Données"

    def test_conflit_index_leve(self):
        loc = location_from_chunk_meta(self._meta())
        with pytest.raises(E.PreviewInvalidRequest) as exc:
            assert_no_conflict(loc, requested_sheet_index=1, requested_sheet_name=None)
        assert exc.value.code == E.ERR_SHEET_REFERENCE_CONFLICT

    def test_accord_index_et_nom_accepte(self):
        loc = location_from_chunk_meta(self._meta())
        assert_no_conflict(loc, requested_sheet_index=2, requested_sheet_name="Données")


class TestProjectionSourceMeta:
    """`data_ranges` doit VOYAGER jusqu'au client, sinon le surlignage est faux."""

    def test_data_ranges_projete(self):
        out = project_source_meta({
            "kind": "excel_table",
            "sheet_name": "S", "sheet_index": 2,
            "cell_range": "C10:D10",
            "data_ranges": ["A10:A10", "C10:D10"],
            "identity_columns": ["A"],
            "identity_range": "A10:A10",
            "chunk_axis": "columns",
            "column_part_index": 1, "column_part_count": 3,
        })
        assert out["data_ranges"] == ["A10:A10", "C10:D10"]
        assert out["identity_columns"] == ["A"]
        assert out["chunk_axis"] == "columns"
        assert out["column_part_index"] == 1

    def test_cle_hors_allowlist_toujours_ecartee(self):
        out = project_source_meta({
            "kind": "excel_table", "cell_range": "A1:B2",
            "entities": {"montants": ["1", "2"]},     # bruit d'implémentation
            "char_start": 42,
        })
        assert "entities" not in out and "char_start" not in out

    def test_liste_malformee_ignoree_sans_lever(self):
        out = project_source_meta({"kind": "excel_table", "data_ranges": "pas-une-liste"})
        assert "data_ranges" not in out

    def test_liste_bornee(self):
        out = project_source_meta({
            "kind": "excel_table",
            "data_ranges": [f"A{i}:B{i}" for i in range(1, 40)],
        })
        assert len(out["data_ranges"]) <= 8

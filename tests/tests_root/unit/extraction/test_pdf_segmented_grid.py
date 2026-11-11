"""NATIVE-TABLE-GEOMETRY-12 — certification des grilles à filets de colonnes SEGMENTÉS.

Constat réel (tableau de surfaces, texte natif) : filets en rectangles fins (deux bords par filet), filets
intérieurs interrompus à des hauteurs différentes par une rangée de total qui fusionne deux colonnes, rangées de
section, filets horizontaux partiels. `_column_grids` n'y voit aucune grille (coupures non communes) et le
fragment de l'analyseur, rejeté (`single_semantic_column`), masquait la région. Chaque certification est
vérifiée cellule par cellule ; chaque refus, en particulier toute FUSION de grilles, est une barrière.
Données SYNTHÉTIQUES ; la vérité terrain est la position de dessin, jamais les valeurs.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.extraction import pdf_tables as P

from . import _pdf_header_band_fixtures as B
from . import _pdf_segmented_fixtures as S

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")


def _segmented(data: bytes):
    rep = P.extract_generic_tables(data)
    return rep, [t for t in rep.tables if t.structure == P.STRUCTURE_SEGMENTED_RULED_COLUMNS]


def _rows(table):
    return [r for r in table.rows if r.row_kind == P.ROW_KIND_DATA]


def _cells(table):
    return [r.fragments[0].cells for r in _rows(table)]


def _expected(rows, schema):
    return [dict(zip(schema, r)) for r in rows]


class TestBaselineDefect:
    def test_column_grid_signature_misses_segmented_rules(self):
        """Reproduction : la signature historique (coupures communes, filets uniques) ne voit aucune grille."""
        import io

        import pdfplumber
        with pdfplumber.open(io.BytesIO(S.page())) as pdf:
            geometry, outcomes = {}, []
            P._collect_fragments(pdf, None, P.TableLimits(), outcomes, geometry)
        assert P._column_grids(geometry[1]) == []
        assert len(P._segmented_column_grids(geometry[1])) == 1


class TestCertification:
    def test_first_section_rows_are_certified_cell_by_cell(self):
        rows = S.data_rows(5)
        rep, [table] = _segmented(S.page(sections=(("Shared rooms", rows),)))
        assert table.schema == S.HEADER[0]
        assert _cells(table) == _expected(rows, S.HEADER[0])
        assert {r.context_label for r in _rows(table)} == {"Shared rooms"}
        out = next(o for o in rep.outcomes if o.get("reason") == "segmented_grid")
        assert out["rows_certified"] == 5

    def test_each_value_box_lies_inside_its_column(self):
        _rep, [table] = _segmented(S.page())
        for r in _rows(table):
            for j, name in enumerate(table.schema):
                x0, top, x1, bottom = r.fragments[0].cell_boxes[name]
                assert S.EDGES[j] - 1 <= x0 < x1 <= S.EDGES[j + 1] + 1 and top < bottom

    def test_provenance_reaches_the_fragment_meta(self):
        _rep, [table] = _segmented(S.page())
        row = _rows(table)[2]
        meta = P.fragment_to_meta(table, row, row.fragments[0])
        assert meta["structure"] == P.STRUCTURE_SEGMENTED_RULED_COLUMNS and meta["source_page"] == 1
        assert meta["header_source"]["relation"] == "same_grid"

    def test_certified_rows_leave_the_page_text_and_the_rest_stays(self):
        rows = S.data_rows(5)
        rep, [_table] = _segmented(S.page(sections=(("Shared rooms", rows),)))
        residual = rep.residual_page_text[1]
        assert rows[3][1] not in residual and "Total Shared rooms" in residual

    def test_plain_line_rules_are_also_certified(self):
        rows = S.data_rows(4)
        _rep, [table] = _segmented(S.page(thin=False, sections=(("Shared rooms", rows),)))
        assert _cells(table) == _expected(rows, S.HEADER[0])

    def test_multi_line_header(self):
        header = [["Code", "Designation", "Count", "Mean\nusable area", "Total\narea"]]
        _rep, [table] = _segmented(S.page(header=header))
        assert table.schema == ["Code", "Designation", "Count", "Mean usable area", "Total area"]

    def test_three_column_grid(self):
        edges = [60.0, 250.0, 330.0, 420.0]
        rows = [["Room A", "3", "12,50"], ["Room B", "1", "7,25"], ["Room C", "2", "9,75"]]
        header = [["Designation", "Count", "Total area"]]
        _rep, [table] = _segmented(S.page(edges=edges, header=header, sections=(("Shared rooms", rows),)))
        assert _cells(table) == _expected(rows, header[0])

    def test_small_offset_between_band_and_body_rules(self):
        _rep, [table] = _segmented(S.page(body_shift=0.6))
        assert len(_rows(table)) == 5

    def test_permuted_values_follow_geometry_not_order(self):
        """Adversarial : valeurs échangées entre colonnes — chaque valeur reste dans SA colonne dessinée."""
        rows = [[r[0], r[1], r[2], r[4], r[3]] for r in S.data_rows(4)]
        _rep, [table] = _segmented(S.page(sections=(("Shared rooms", rows),)))
        assert _cells(table) == _expected(rows, S.HEADER[0])

    def test_close_labels_stay_distinct_rows(self):
        rows = S.data_rows(3, labels=["Pump A", "Pump AB", "Pump B"])
        _rep, [table] = _segmented(S.page(sections=(("Shared rooms", rows),)))
        assert [c["Designation"] for c in _cells(table)] == ["Pump A", "Pump AB", "Pump B"]


class TestRefusal:
    def test_rows_after_a_total_row_are_never_attached(self):
        """Rangée de total (texte sur plusieurs colonnes, nombre) : la grille s'arrête, la section suivante n'est
        pas certifiée sous ces en-têtes."""
        sections = (("Shared rooms", S.data_rows(3)), ("Bedrooms", S.data_rows(3, start=10)))
        _rep, [table] = _segmented(S.page(sections=sections))
        assert [c["Code"] for c in _cells(table)] == ["0", "1", "2"]

    @pytest.mark.parametrize("gap", [0.0, 6.0])
    def test_two_stacked_tables_are_never_merged(self, gap):
        rep, tables = _segmented(S.two_tables(gap=gap))
        for t in tables:
            codes = {c.get("Code") or c.get("Ref") for c in _cells(t)}
            assert not ({"0", "1"} & codes and {"20", "21"} & codes)
            for c in _cells(t):
                if c.get("Code") in {"20", "21", "22", "23"}:
                    pytest.fail("rangée du second tableau certifiée sous l'en-tête du premier")

    def test_cartouche_and_side_legend_stay_outside(self):
        _rep, [table] = _segmented(S.with_cartouche_and_legend())
        assert table.schema == S.HEADER[0]
        assert all("Legend" not in v and "Project" not in v for c in _cells(table) for v in c.values())

    def test_label_crossing_a_column_rule_refuses_the_segment(self):
        rep, tables = _segmented(S.page(straddle_row=1))
        assert not tables or all(c["Code"] != "1" for t in tables for c in _cells(t))

    def test_value_between_two_columns_refuses_its_row(self):
        rep, tables = _segmented(S.page(between_value_row=2))
        assert not tables or all(c["Code"] != "2" for t in tables for c in _cells(t))

    def test_inner_rule_missing_from_the_header_band_is_no_grid(self):
        _rep, tables = _segmented(S.page(header_rule_missing=1))
        assert not tables

    def test_region_without_outer_border_is_no_grid(self):
        _rep, tables = _segmented(S.page(border_end=0.0))
        assert not tables

    def test_foreign_rule_crossing_the_region_is_no_grid(self):
        """Deux découpages de colonnes possibles (filet supplémentaire absent du bandeau) : rejet."""
        _rep, tables = _segmented(S.page(foreign_rule=200.0))
        assert not tables

    def test_numeric_header_band_is_refused(self):
        header = [["Code", "Designation", "2026", "Mean area", "Total area"]]
        _rep, tables = _segmented(S.page(header=header))
        assert not tables

    def test_single_row_is_not_enough(self):
        _rep, tables = _segmented(S.page(sections=(("Shared rooms", S.data_rows(1)),)))
        assert not tables

    def test_unit_is_never_invented(self):
        """En-têtes sans unité : aucune unité n'apparaît dans le schéma ni dans les cellules."""
        _rep, [table] = _segmented(S.page())
        assert not any("m²" in h or "m2" in h for h in table.schema)
        assert not any("m²" in v for c in _cells(table) for v in c.values())

    def test_existing_ruleless_and_header_band_fixtures_unchanged(self):
        """Non-régression : les fixtures historiques ne produisent aucune grille segmentée."""
        from . import _pdf_ruleless_fixtures as F
        for data in (F.grid(F.numeric_rows(4), header=[B.SCHEMA]), F.adjacent_grids(), F.aligned_prose(),
                     F.cartouche_only(), B.band_same_page(), B.adjacent_tables()):
            assert not _segmented(data)[1]


class TestAnalyserInteraction:
    def test_rejected_fragment_no_longer_masks_its_region(self):
        """Un fragment REJETÉ (`single_semantic_column`) recouvrant la grille ne la masque plus ; un fragment
        structuré la masque toujours (aucune double certification)."""
        import io

        import pdfplumber
        data = S.page()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            geometry, outcomes = {}, []
            P._collect_fragments(pdf, None, P.TableLimits(), outcomes, geometry)
            words = {1: pdf.pages[0].extract_words()}
        from types import SimpleNamespace
        fake = SimpleNamespace(page_number=1, bbox=(S.EDGES[0], 100.0, S.EDGES[-1], 400.0))

        def run(rejected: bool):
            rep = P.TableExtractionReport()
            if rejected:
                rep.rejected_fragment_ids.add(id(fake))
            P._ruleless_grids(rep, [fake], geometry, lambda pn: words.get(pn), digest="t", limits=P.TableLimits())
            return [t for t in rep.tables if t.structure == P.STRUCTURE_SEGMENTED_RULED_COLUMNS]

        assert len(run(rejected=True)) == 1
        assert run(rejected=False) == []

    def test_partial_label_rules_form_a_structured_subgrid_that_keeps_masking(self):
        """Limite documentée : des filets horizontaux partiels ferment un sous-tableau (colonnes de libellé) que
        l'analyseur STRUCTURE ; il masque la région — aucune grille segmentée par-dessus, aucune double preuve."""
        rep, tables = _segmented(S.page(partial_hrules=True))
        assert not tables and any(o.get("outcome") == P.OUTCOME_STRUCTURED for o in rep.outcomes)

    def test_full_width_backgrounds_are_structured_by_the_existing_analyser(self):
        rep, tables = _segmented(S.page(backgrounds=True))
        assert not tables and any(t.schema == S.HEADER[0] for t in rep.tables)


class TestNumericEvidenceOnSegmentedRows:
    """Rail + contrôle numérique : une cellule certifiée prouve SA valeur sous SA colonne, jamais une autre."""

    def _sources(self):
        from apps.rag_core.services.query import numeric_evidence as NE
        rows = S.data_rows(5)
        _rep, [table] = _segmented(S.page(sections=(("Shared rooms", rows),)))
        r = _rows(table)[3]
        meta = P.fragment_to_meta(table, r, r.fragments[0])
        chunk = {"chunk_id": "seg-3", "text": P.fragment_to_text(table, r, r.fragments[0]),
                 "source": {"document_id": "d", "page_number": 1, "filename": "Synth.pdf", "meta": meta}}
        ocr = {"chunk_id": "ocr", "text": "vignette", "source": {"document_id": "d", "page_number": 2, "filename": "Synth.pdf",
               "meta": {"source_is_ocr": True, "requires_visual_check": False, "page_type": "text"}}}
        return NE, rows[3], NE.annotate_numeric_evidence([chunk, ocr], table_pages_by_doc={})

    def test_value_under_its_own_column_is_proven(self):
        NE, row, src = self._sources()
        q = f"Quelle est la valeur de « Total area » pour la ligne « {row[1]} » ?"
        answer = f"La valeur de « Total area » pour « {row[1]} » est de {row[4]} [1]."
        d = NE.guard_answer(answer, question=q, sources=src)
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_value_of_the_neighbouring_column_is_refused(self):
        NE, row, src = self._sources()
        q = f"Quelle est la valeur de « Total area » pour la ligne « {row[1]} » ?"
        d = NE.guard_answer(f"La valeur de « Total area » pour « {row[1]} » est de {row[3]} [1].", question=q, sources=src)
        assert d.verdict != NE.VERDICT_PASS and row[3] not in d.answer

    def test_value_of_another_row_is_refused(self):
        NE, row, src = self._sources()
        other = S.data_rows(5)[1]
        q = f"Quelle est la valeur de « Total area » pour la ligne « {other[1]} » ?"
        d = NE.guard_answer(f"La valeur de « Total area » pour « {other[1]} » est de {row[4]} [1].", question=q, sources=src)
        assert d.verdict != NE.VERDICT_PASS and row[4] not in d.answer

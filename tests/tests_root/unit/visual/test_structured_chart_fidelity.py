"""identité et sémantique des valeurs d'un graphique.

Fixtures SYNTHÉTIQUES (aucune donnée du corpus) au format réel de `structured-extract/1` :
OCR du moteur (`raw_output.ocr` : textes, scores, boîtes), séries relevées aux catégories.
  * titre et identifiants lus dans l'OCR du moteur, au-dessus de l'axe des valeurs ;
  * jamais d'identifiant inventé, ni de date ou d'année prise pour un identifiant ;
  * extrema PARMI LES POINTS RELEVÉS, jamais présentés comme ceux de la courbe ;
  * valeurs hors de l'échelle lue signalées ; aucune valeur nouvelle produite.
"""
from __future__ import annotations

from apps.rag_core.services.ingest.visual import structured as S


def _ocr(*rows):
    """rows : (texte, score, [x0, y0, x1, y1])."""
    return {"rec_texts": [r[0] for r in rows], "rec_scores": [r[1] for r in rows],
            "rec_boxes": [r[2] for r in rows]}


Y_TICKS = [("40", 0.98, [8, 40, 30, 52]), ("30", 0.98, [8, 80, 30, 92]),
           ("20", 0.98, [8, 120, 30, 132]), ("10", 0.98, [8, 160, 30, 172]), ("0", 0.98, [16, 200, 30, 212])]
X_TICKS = [("T1", 0.99, [40, 220, 70, 230]), ("T2", 0.99, [120, 220, 150, 230]), ("T3", 0.99, [200, 220, 230, 230])]


def _chart(*, title_rows=(), series=None, categories=("T1", "T2", "T3"), extra_ocr=(), region=(0, 0, 400, 260)):
    series = series if series is not None else [{"name": "Débit", "unit_hint": "m3/h",
                                                 "values": [12.0, 31.5, 18.0], "raw_values": ["12", "31.5", "18"]}]
    item = {"region_bbox": list(region), "corner_label": "Période", "categories": list(categories),
            "series": series, "native_text": ""}
    data = {"title_candidates": [], "raw_output": {"ocr": _ocr(*title_rows, *Y_TICKS, *X_TICKS, *extra_ocr)}}
    return item, data


def _text(item, data):
    return S.chart_text(item, data, filename="rapport.pdf", page=3)


class TestIdentity:
    def test_title_and_identifier_are_read_from_engine_ocr(self):
        item, data = _chart(title_rows=[("Débit mesuré poste", 0.97, [60, 10, 220, 24]),
                                        ("PX0042117", 0.99, [225, 11, 300, 23])])
        identity = S.chart_identity(item, data)
        assert identity["title"] == "Débit mesuré poste PX0042117"
        assert identity["identifiers"] == ["PX0042117"]
        text = _text(item, data)
        assert "Titre imprimé (lecture OCR brute du moteur, espaces et caractères possiblement altérés) : Débit mesuré poste PX0042117" in text
        assert "Identifiants lus dans le titre (OCR) : PX0042117 — à comparer caractère par caractère" in text

    def test_chart_without_title_invents_nothing(self):
        item, data = _chart()
        identity = S.chart_identity(item, data)
        assert identity["title"] is None and identity["identifiers"] == []
        assert "Titre imprimé" not in _text(item, data) and "Identifiants" not in _text(item, data)

    def test_neighbouring_identifiers_stay_with_their_own_chart(self):
        a = _chart(title_rows=[("Probabilités AB0000012345", 0.98, [60, 10, 300, 24])])
        b = _chart(title_rows=[("Probabilités AB0000012346", 0.98, [60, 10, 300, 24])],
                   series=[{"name": "Débit", "values": [3.0, 4.0, 5.0], "raw_values": ["3", "4", "5"]}])
        ta, tb = _text(*a), _text(*b)
        assert "AB0000012345" in ta and "AB0000012346" not in ta
        assert "AB0000012346" in tb and "AB0000012345" not in tb
        assert "31.5" in ta and "31.5" not in tb, "aucune valeur d'un graphique dans l'autre"

    def test_garbled_ocr_identifier_is_declared_uncertain_never_corrected(self):
        item, data = _chart(title_rows=[("Probas Scooo0o08766", 0.97, [60, 10, 300, 24])])
        text = _text(item, data)
        assert "Identifiant de lecture OCR incertaine : Scooo0o08766" in text
        assert "Identifiants lus dans le titre" not in text
        assert "SC0000008766" not in text, "jamais de correction devinée"

    def test_digits_in_title_are_not_identifiers(self):
        item, data = _chart(title_rows=[("Production 2024 (MWh) au 2025-03-31", 0.97, [60, 10, 320, 24])])
        assert S.chart_identity(item, data)["identifiers"] == []

    def test_unreadable_and_out_of_region_text_is_ignored(self):
        item, data = _chart(title_rows=[("Tit#e ill1sible", 0.41, [60, 10, 200, 24]),
                                        ("Légende hors graphique ZZ998877", 0.99, [500, 10, 700, 24])])
        assert S.chart_identity(item, data)["title"] is None

    def test_legend_and_axis_labels_are_not_title(self):
        item, data = _chart(title_rows=[("Débit horaire", 0.95, [60, 10, 200, 24])],
                            extra_ocr=[("Débit", 0.97, [150, 245, 190, 255])])
        assert S.chart_identity(item, data)["title"] == "Débit horaire"


class TestValueSemantics:
    def test_sampled_maximum_is_never_presented_as_the_curve_maximum(self):
        item, data = _chart()
        text = _text(item, data)
        assert "maximum parmi les points relevés = 31.5 (T2)" in text
        assert "le maximum réel de la courbe n'est pas déterminable" in text
        assert "Débit » : maximum = " not in text

    def test_peak_between_ticks_produces_no_new_value(self):
        # Les points relevés sont tout ce que le moteur fournit : aucune valeur hors de ces points.
        item, data = _chart(series=[{"name": "Niveau", "values": [10.0, 12.0, 11.0],
                                     "raw_values": ["10", "12", "11"]}])
        reading = S.series_reading(item, S.chart_identity(item, data))[0]
        assert (reading["sampled_max"], reading["sampled_min"]) == ({"value": "12", "at": "T2"},
                                                                    {"value": "10", "at": "T1"})
        line = next(l for l in _text(item, data).splitlines() if "maximum parmi les points relevés" in l)
        assert line == ("Série « Niveau » : maximum parmi les points relevés = 12 (T2) ; "
                        "minimum parmi les points relevés = 10 (T1)")

    def test_single_point_has_no_extremum(self):
        item, data = _chart(series=[{"name": "Seul", "values": [7.0], "raw_values": ["7"]}], categories=("T1",))
        reading = S.series_reading(item, S.chart_identity(item, data))
        assert reading[0]["sampled_max"] is None
        assert "maximum parmi les points relevés" not in _text(item, data)

    def test_multi_series_extrema_are_per_series(self):
        item, data = _chart(series=[{"name": "A", "values": [1.0, 9.0, 4.0], "raw_values": ["1", "9", "4"]},
                                    {"name": "B", "values": [8.0, 2.0, 3.0], "raw_values": ["8", "2", "3"]}])
        text = _text(item, data)
        assert "Série « A » : maximum parmi les points relevés = 9 (T2) ; minimum parmi les points relevés = 1 (T1)" in text
        assert "Série « B » : maximum parmi les points relevés = 8 (T1) ; minimum parmi les points relevés = 2 (T2)" in text

    def test_units_and_raw_rendering_are_kept(self):
        text = _text(*_chart())
        assert "Série « Débit » [unité : m3/h] :" in text and "- T2 : 31.5" in text

    def test_values_outside_the_read_axis_scale_are_flagged(self):
        item, data = _chart(series=[{"name": "Dérive", "values": [12.0, 55.0, 18.0],
                                     "raw_values": ["12", "55", "18"]}])
        text = _text(item, data)
        assert "Échelle de l'axe des valeurs lue : de 0 à 40" in text
        assert "1 valeur(s) hors de l'échelle lue, non fiables : 55 (T2)" in text

    def test_secondary_axis_disables_the_scale_check(self):
        item, data = _chart(series=[{"name": "Dérive", "values": [12.0, 55.0, 18.0], "raw_values": ["12", "55", "18"]}],
                            extra_ocr=[("100", 0.98, [380, 40, 398, 52])])
        assert S.chart_identity(item, data)["value_axis"] is None
        assert "hors de l'échelle" not in _text(item, data)

    def test_non_numeric_points_are_partially_illegible_not_zero(self):
        item, data = _chart(series=[{"name": "Flou", "values": [None, 5.0, None], "raw_values": ["?", "5", ""]}])
        reading = S.series_reading(item, S.chart_identity(item, data))[0]
        assert reading["points"] == 1 and reading["sampled_max"] is None


class TestProvenanceMeta:
    def test_chart_unit_meta_carries_identity_and_value_semantics(self):
        class _Asset:
            content_hash, ordinal, source_page, source_locator = "c" * 64, 0, 3, None

        item, data = _chart(title_rows=[("Débit PX0042117", 0.97, [60, 10, 220, 24])])
        data.update({"items": [item], "engine": {"name": "PP-StructureV3"}, "warnings": []})
        out = S.normalize_response(data, asset=_Asset(), task="chart", filename="rapport.pdf", response_sha256="d" * 64)
        structured = out.units[0].meta["visual"][S.STRUCTURED_KEY]
        assert structured["identity"] == {"title": "Débit PX0042117", "identifiers": ["PX0042117"],
                                          "uncertain_identifiers": [], "source": "engine_ocr"}
        assert structured["value_semantics"] == "points_at_categories"
        assert structured["value_axis"]["max"] == 40.0 and structured["outside_axis_values"] == 0
        assert out.units[0].meta["visual"]["asset_hash"] == "c" * 64


class TestLongChartParts:
    def _long(self):
        cats = [f"2024-{m:02d}-{d:02d}" for m in range(1, 13) for d in (1, 15)]
        series = [{"name": f"S{k}", "values": [float(i) for i in range(len(cats))],
                   "raw_values": [str(i) for i in range(len(cats))]} for k in range(4)]
        native = "Date | " + " | ".join(f"S{k}" for k in range(4)) + "\n" + "\n".join(
            f"{c} | " + " | ".join(str(i) for _ in range(4)) for i, c in enumerate(cats))
        item, data = _chart(title_rows=[("Suivi PX0042117", 0.98, [60, 10, 220, 24])], series=series,
                            categories=cats)
        item["native_text"] = native
        return item, data

    def test_every_part_fits_and_repeats_the_chart_identity(self):
        item, data = self._long()
        parts = S.chart_texts(item, data, filename="rapport.pdf", page=3)
        assert len(parts) > 1
        for i, text in enumerate(parts, start=1):
            assert len(text) <= S.TABLE_PART_MAX_CHARS
            assert "Identifiants lus dans le titre (OCR) : PX0042117 — à comparer caractère par caractère" in text
            assert f"Partie {i}/{len(parts)}" in text and "Source : rapport.pdf, page 3" in text

    def test_no_value_is_lost_or_detached_from_its_series(self):
        item, data = self._long()
        parts = S.chart_texts(item, data, filename="rapport.pdf", page=3)
        full = S.chart_text(item, data, filename="rapport.pdf", page=3)
        data_lines = [l for l in full.splitlines() if l.startswith("- ") or " | " in l]
        split_lines = [l for p in parts for l in p.splitlines() if l.startswith("- ") or " | " in l]
        assert sorted(set(split_lines)) == sorted(set(data_lines))
        for text in parts:
            lines = text.splitlines()
            first_value = next((i for i, l in enumerate(lines) if l.startswith("- ")), None)
            if first_value is not None:
                assert lines[first_value - 1].startswith("Série « ") or lines[first_value - 1].startswith("- ")

    def test_short_chart_stays_one_unit(self):
        item, data = _chart(title_rows=[("Débit PX0042117", 0.97, [60, 10, 220, 24])])
        assert S.chart_texts(item, data, filename="rapport.pdf", page=3) == [
            S.chart_text(item, data, filename="rapport.pdf", page=3)]

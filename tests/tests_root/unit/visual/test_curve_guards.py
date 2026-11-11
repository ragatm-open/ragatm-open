"""garde-fous de sécurité numérique du shadow des courbes.

Graphiques SYNTHÉTIQUES (``_curve_charts``). Prouve :
  * second axe Y à droite -> UNSUPPORTED pour tout le graphique, jamais l'échelle gauche ;
    deux graduations seulement -> NOT_DETERMINABLE ; nombre isolé, légende, étiquettes X -> rien ;
  * série neutre dont l'extremum tombe sur un axe/une grille de même couleur -> UNRELIABLE, valeur
    seulement diagnostique ; série grise distincte et séries colorées -> mesurées comme avant ;
  * le moteur figé n'est pas modifié (empreinte vérifiée par test_curve_shadow).
"""
from __future__ import annotations

import io

import numpy as np

from apps.rag_core.services.ingest.visual import curve_digitization as CD
from apps.rag_core.services.ingest.visual import curve_guards as G
from apps.rag_core.services.ingest.visual import curve_shadow as CS
from tests_root.unit.visual import _curve_charts as K

GREY = (165, 165, 165)


class _Doc:
    id = "doc-16"
    rag_space_id = "space"


def _run(chart):
    return CS.run_for_asset(doc=_Doc(), asset=K.Asset(chart.png()), data=chart.response())


def _one(results, series="Série A", extremum="max"):
    return next(r for r in results if r.series_id == series and r.extremum_type == extremum)


def _right_ticks(chart, values, scale=100.0):
    for v in values:
        chart.text(f"{v:.0f}", K.X1 + 8, K.y_of(v / scale) - 5, K.GREY)


class TestRightYAxis:
    def test_dual_axis_is_unsupported_for_the_whole_chart(self):
        c = K.simple_chart(0.7)
        _right_ticks(c, [0, 20, 40, 60, 80, 100])
        out = _run(c)
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("UNSUPPORTED", G.DUAL_AXIS_REASON)]
        assert all(r.extremum_value is None and r.diagnostic_value is None for r in out)

    def test_two_aligned_right_numbers_cannot_rule_out_an_axis(self):
        c = K.simple_chart(0.7)
        _right_ticks(c, [20, 80])
        out = _run(c)
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("NOT_DETERMINABLE", G.POSSIBLE_DUAL_AXIS_REASON)]

    def test_single_number_on_the_right_is_not_an_axis(self):
        c = K.simple_chart(0.7)
        c.text("0,95", K.X1 + 8, K.y_of(0.5) - 5, K.GREY)
        assert _one(_run(c)).processing_status == "MEASURED_FROM_PIXELS"

    def test_text_legend_on_the_right_is_not_an_axis(self):
        c = K.simple_chart(0.7)
        for i, name in enumerate(("Nord", "Sud", "Est")):
            c.text(name, K.X1 + 8, 60 + 20 * i, K.GREY)
        assert G.right_y_axis(c.response()) is None

    def test_numbers_increasing_downwards_are_not_an_axis(self):
        c = K.simple_chart(0.7)
        for i, v in enumerate((2021, 2022, 2023)):
            c.text(str(v), K.X1 + 8, 60 + 20 * i, K.GREY)
        assert G.right_y_axis(c.response()) is None

    def test_left_axis_chart_is_unchanged(self):
        assert G.right_y_axis(K.simple_chart(0.7).response()) is None
        assert _one(_run(K.simple_chart(0.7))).processing_status == "MEASURED_FROM_PIXELS"


class TestNeutralAxisGrid:
    def _grey_on_grey_axis(self):
        c = K.Chart()
        c.draw.line([(K.X0 - 30, K.y_of(0.0)), (K.X1, K.y_of(0.0))], fill=(185, 185, 185), width=1)
        c.curve("Série A", GREY, K.bump(0.72), legend_x=200)
        return c

    def test_engine_alone_measures_the_axis_line(self):
        c = self._grey_on_grey_axis()
        a = CD.digitize_extremum(io.BytesIO(c.png()), c.response(), "Série A", "min")
        assert a.status == "MEASURED_FROM_PIXELS" and a.value < 0.05, "le défaut du lot 15 est bien reproduit"

    def test_grey_minimum_on_a_grey_axis_line_is_rejected_and_stays_diagnostic(self):
        r = _one(_run(self._grey_on_grey_axis()), extremum="min")
        assert (r.processing_status, r.abstention_reason) == ("UNRELIABLE", G.NEUTRAL_REASON)
        assert r.extremum_value is None and r.extremum_interval is None and r.diagnostic_value < 0.05

    def test_grey_maximum_away_from_the_axis_stays_measured(self):
        r = _one(_run(self._grey_on_grey_axis()))
        assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - 0.72) < 0.01

    def test_grey_series_distinct_from_the_grid_stays_measured(self):
        c = K.Chart()
        c.curve("Série A", (100, 100, 100), K.bump(0.72), legend_x=200)
        r = _one(_run(c))
        assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - 0.72) < 0.01

    def test_colored_series_is_never_concerned(self):
        c = K.simple_chart(0.83)
        c.draw.line([(K.X0, K.y_of(0.0)), (K.X1, K.y_of(0.0))], fill=(128, 128, 128), width=1)
        for e, v in (("max", 0.83), ("min", 0.2)):
            r = _one(_run(c), extremum=e)
            assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - v) < 0.01

    def test_structural_lines_are_found(self):
        img = np.full((50, 100, 3), 255, dtype=np.uint8)
        img[40, :, :] = 170
        img[:, 5, :] = 0
        rows, cols = G.structural_lines(img, [0, 0, 100, 50])
        assert [r for r, _ in rows] == [40] and [k for k, _ in cols] == [5]

    def test_neutral_series_near_white_is_not_determinable(self):
        res = CD.Result("MEASURED_FROM_PIXELS", value=0.5, interval=(0.49, 0.51), x_pixel=10, y_pixel=10,
                        diagnostics={"color": [230.0, 230.0, 230.0], "plot_box": [0, 0, 50, 50], "line_thickness_px": 2})
        out = G.neutral_axis_grid(res, np.full((60, 60, 3), 255, dtype=np.uint8))
        assert (out.status, out.reason.split(" ")[0]) == ("NOT_DETERMINABLE", G.BACKGROUND_REASON)

    def test_guard_leaves_abstentions_untouched(self):
        res = CD.Result("NOT_DETERMINABLE", "x")
        assert G.neutral_axis_grid(res, np.zeros((5, 5, 3), dtype=np.uint8)) is res


class TestNonBlocking:
    def test_guard_failure_is_contained(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("panne")

        monkeypatch.setattr(G, "neutral_axis_grid", boom)
        out = _run(K.simple_chart())
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("ERROR", "shadow_exception")]

    def test_reason_codes_are_stable(self):
        assert CS.reason_code(f"{G.NEUTRAL_REASON} : x") == G.NEUTRAL_REASON
        assert CS.reason_code(f"{G.BACKGROUND_REASON} : x") == G.BACKGROUND_REASON


class TestC2Order:
    """Lot 18 : C2 (graduations Y strictement dupliquées) ne passe qu'APRÈS le garde double axe."""

    def _with_dup_zero(self):
        c = K.simple_chart(0.8)
        c.text("0", 34, 352, K.GREY)  # « 0 » de l'axe X dans la colonne des graduations Y
        return c

    def test_x_axis_zero_no_longer_breaks_the_calibration(self):
        out = _run(self._with_dup_zero())
        for e, v in (("max", 0.8), ("min", 0.2)):
            r = _one(out, extremum=e)
            assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - v) < 0.02

    def test_same_values_on_a_right_axis_still_abstain(self):
        c = self._with_dup_zero()
        for v in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            c.text(f"{v:.1f}".replace(".", ","), K.X1 + 8, K.y_of(v) - 5, K.GREY)
        assert [(r.processing_status, r.abstention_reason) for r in _run(c)] == [("UNSUPPORTED", G.DUAL_AXIS_REASON)]

"""numérisation des courbes en mode SHADOW.

Graphiques SYNTHÉTIQUES (``_curve_charts``), aucune donnée du corpus. Prouve :
  * le prototype figé du lot 13B est intégré sans changement algorithmique (empreinte
    reconstituée, paramètres, substitut ``ndimage`` équivalent à scipy) ;
  * les garde-fous hérités : frange de texte coloré, trait de légende dans le tracé,
    série cachée, axe non pris en charge, extrapolation, lacune au pic -> abstention ;
  * la portée : éteint par défaut, refus en production drapeaux allumés, allowlist ;
  * la robustesse : tout échec devient ``ERROR`` ou une abstention, jamais une exception ;
  * l'isolation : fragments identiques shadow allumé / éteint, aucune valeur journalisée.
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from apps.rag_core.services.ingest.visual import curve_digitization as CD
from apps.rag_core.services.ingest.visual import curve_ndimage as ND
from apps.rag_core.services.ingest.visual import curve_shadow as CS
from apps.rag_core.services.ingest.visual import structured as S
from tests_root.unit.visual import _curve_charts as K

SPACE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class _Doc:
    id = "doc-1"
    rag_space_id = SPACE
    sha256 = "d" * 64
    filename = "courbes.pdf"


def _shadow(chart, *, series_doc=_Doc(), **kw):
    return CS.run_for_asset(doc=series_doc, asset=K.Asset(chart.png()), data=chart.response(), **kw)


def _one(results, series="Série A", extremum="max"):
    return next(r for r in results if r.series_id == series and r.extremum_type == extremum)


def _reliable_wrong(r, expected, tol=0.02):
    return r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - expected) > tol


@pytest.fixture
def shadow_on(settings):
    settings.DJANGO_ENV = "staging"
    settings.STRUCTURED_VISUAL_ENABLED = True
    settings.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = [SPACE, OTHER]
    settings.STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED = True
    settings.STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST = [SPACE]
    return settings


# =========================================================================== #
# A. Prototype figé
# =========================================================================== #
class TestFrozenPrototype:
    #: Correction C2 (lots 15/17, intégrée au lot 18) : SEULE modification algorithmique autorisée du
    #: fichier figé, en plus de l'import de ``ndimage``. Texte exact, vérifié octet pour octet.
    C2_BLOCK = (
        "    # C2 (lots 15/17) : appliqué réellement — une valeur, une boîte, la plus à gauche\n"
        "    leftmost = {}\n"
        "    for c in cands:\n"
        "        if c[0] not in leftmost or c[2][0] < leftmost[c[0]][2][0]:\n"
        "            leftmost[c[0]] = c\n"
        "    cands = [c for c in cands if leftmost[c[0]] is c]\n"
    )

    def test_module_is_the_frozen_13b_file_but_the_import_and_c2(self):
        src = Path(CD.__file__).read_text(encoding="utf-8")
        assert src.count(self.C2_BLOCK) == 1, "C2 présent une fois, au texte exact du lot 17"
        anchor = "    # Une même valeur lue deux fois (ex. axe X) : on garde la plus à gauche\n"
        assert src.index(anchor) + len(anchor) == src.index(self.C2_BLOCK), "C2 à sa place dans calibrate_y"
        lines = src.replace(self.C2_BLOCK, "", 1).splitlines(keepends=True)
        changed = [i for i, line in enumerate(lines) if "curve_ndimage as ndimage" in line]
        assert len(changed) == 1
        lines[changed[0]] = "from scipy import ndimage\n"
        assert hashlib.sha256("".join(lines).encode("utf-8")).hexdigest() == CS.FROZEN_SHA256

    def test_c2_keeps_the_leftmost_of_strictly_equal_ticks_only(self):
        lines = [("0,0", 0.99, [20, 335, 40, 345]), ("0,5", 0.99, [20, 185, 40, 195]), ("1,0", 0.99, [20, 35, 40, 45]),
                 ("0,5", 0.99, [28, 260, 41, 270])]
        cal = CD.calibrate_y(lines, [0, 0, 640, 420])
        assert cal["ok"] and cal["n_ticks"] == 3 and abs(cal["a"] * 0.5 + cal["b"] - 190) < 1
        distinct = [("0,0", 0.99, [20, 335, 40, 345]), ("0,5", 0.99, [20, 185, 40, 195]), ("1,0", 0.99, [20, 35, 40, 45])]
        assert CD.calibrate_y(distinct, [0, 0, 640, 420]) == {**cal}, "sans doublon : résultat identique"

    def test_frozen_parameters_are_unchanged(self):
        assert (CD.OCR_MIN_SCORE, CD.MIN_Y_TICKS, CD.MAX_CALIB_RESIDUAL_FRAC, CD.COLOR_TOL, CD.SAME_COLOR_TOL,
                CD.MIN_COVERAGE, CD.MIN_TRACE_WIDTH_FRAC, CD.TEXT_MASK_MARGIN_FRAC,
                CD.CURVE_MIN_COMPONENT_WIDTH_FRAC, CD.LEGEND_SWATCH_FRAC, CD.GAP_CLEARANCE_PX,
                CD.TEXT_EXTREMUM_CLEARANCE_PX, CD.MIN_PLOT_W, CD.MIN_PLOT_H, CD.MAX_SHARED_FRAC,
                CD.LEGEND_MATCH_MIN, CD.SINGLE_SERIES_MIN_SAT, CD.SINGLE_SERIES_DOMINANCE) == (
            0.8, 3, 0.01, 60.0, 45.0, 0.5, 0.3, 0.35, 0.05, 4.0, 6, 4, 40, 20, 0.7, 0.6, 60, 0.85)

    def test_label_is_8_connected_in_raster_order(self):
        mask = np.array([[1, 0, 0, 1],
                         [0, 1, 0, 1],
                         [0, 0, 0, 0],
                         [1, 1, 0, 0]], dtype=bool)
        labels, n = ND.label(mask, structure=np.ones((3, 3)))
        assert n == 3
        assert labels.tolist() == [[1, 0, 0, 2], [0, 1, 0, 2], [0, 0, 0, 0], [3, 3, 0, 0]]
        assert ND.find_objects(labels) == [(slice(0, 2), slice(0, 2)), (slice(0, 2), slice(3, 4)),
                                           (slice(3, 4), slice(0, 2))]

    def test_u_shape_merges_late(self):
        mask = np.array([[1, 0, 1], [1, 0, 1], [1, 1, 1]], dtype=bool)
        labels, n = ND.label(mask, structure=np.ones((3, 3)))
        assert n == 1 and set(labels[mask].tolist()) == {1}

    def test_other_structures_are_refused(self):
        with pytest.raises(ValueError):
            ND.label(np.ones((2, 2), dtype=bool), structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))

    def test_empty_mask(self):
        labels, n = ND.label(np.zeros((3, 4), dtype=bool), structure=np.ones((3, 3)))
        assert n == 0 and ND.find_objects(labels) == []

    def test_matches_scipy_when_available(self):
        nd = pytest.importorskip("scipy.ndimage")
        rng = np.random.default_rng(14)
        for _ in range(300):
            mask = rng.random((int(rng.integers(1, 30)), int(rng.integers(1, 40)))) < rng.uniform(0.05, 0.7)
            a, na = nd.label(mask, structure=np.ones((3, 3)))
            b, nb = ND.label(mask, structure=np.ones((3, 3)))
            assert na == nb and np.array_equal(a, b)
            assert nd.find_objects(a) == ND.find_objects(b)


# =========================================================================== #
# F1. Garde-fous hérités du lot 13B
# =========================================================================== #
class TestInheritedSafety:
    def test_clean_chart_is_measured_with_interval(self):
        r = _one(_shadow(K.simple_chart(0.83)))
        assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - 0.83) < 0.01
        assert r.extremum_interval[0] <= r.extremum_value <= r.extremum_interval[1]
        assert (r.axis_calibration_status, r.series_identity_status, r.trace_continuity_status) == (
            "PASS", "PASS", "PASS")
        assert r.abstention_reason is None and r.algorithm_provenance["frozen_sha256"] == CS.FROZEN_SHA256

    def test_min_is_measured_too(self):
        r = _one(_shadow(K.simple_chart(0.83)), extremum="min")
        assert r.processing_status == "MEASURED_FROM_PIXELS" and abs(r.extremum_value - 0.2) < 0.01

    def test_colored_title_above_plot_is_not_a_maximum(self):
        c = K.simple_chart(0.6)
        c.text("TITRE 0,99 Série A", 200, 18, K.BLUE)
        r = _one(_shadow(c))
        assert not _reliable_wrong(r, 0.6)

    def test_colored_text_inside_plot_is_masked(self):
        c = K.simple_chart(0.6)
        c.text("NOTE 0,95 Serie A", 250, 52, K.BLUE)
        r = _one(_shadow(c))
        assert not _reliable_wrong(r, 0.6)
        assert r.text_interference_status in ("MASKED", "AT_EXTREMUM")

    def test_legend_swatch_inside_plot_is_masked(self):
        c = K.simple_chart(0.6)
        c.legend("Série A", K.BLUE, 470, 60)
        assert not _reliable_wrong(_one(_shadow(c)), 0.6)

    def test_hidden_series_abstains(self):
        c = K.Chart()
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200)
        c.curve("Série B", K.ORANGE, K.bump(0.7), legend_x=380, width=4)
        r = _one(_shadow(c))
        assert r.processing_status == "NOT_DETERMINABLE" and r.extremum_value is None
        assert r.trace_continuity_status == "FAIL"

    def test_right_axis_is_unsupported_and_abstains(self):
        c = K.Chart(tick_side="right")
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200)
        out = _shadow(c)
        # Lot 16 : un axe Y à droite est détecté AVANT la mesure -> tout le graphique est non pris en charge.
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("UNSUPPORTED", "dual_y_axis_unsupported")]

    def test_log_axis_abstains(self):
        c = K.Chart(ticks=[])
        for label, v in (("1", 0.0), ("10", 0.25), ("100", 0.5), ("1000", 0.75), ("10000", 1.0)):
            c.text(label, 22, K.y_of(v) - 5, K.GREY)
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200)
        r = _one(_shadow(c))
        assert (r.processing_status, r.abstention_reason) == ("NOT_DETERMINABLE", "axis_unsupported_scale")

    def test_extrapolation_is_rejected_and_value_stays_diagnostic(self):
        c = K.Chart(ticks=[0.0, 0.2, 0.4, 0.6, 0.8])
        c.curve("Série A", K.BLUE, K.bump(0.9), legend_x=200)
        r = _one(_shadow(c))
        assert (r.processing_status, r.abstention_reason) == ("UNRELIABLE", "extrapolation_refused")
        assert r.extremum_value is None and r.extremum_interval is None and r.diagnostic_value is not None

    def test_trace_gap_near_peak_abstains(self):
        c = K.Chart()
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200, gap=(366, 378))
        r = _one(_shadow(c))
        assert (r.processing_status, r.abstention_reason) == ("NOT_DETERMINABLE", "trace_gap_at_extremum")
        assert r.extremum_value is None

    def test_indistinguishable_colors_abstain(self):
        c = K.Chart()
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200)
        c.curve("Série B", (36, 125, 186), K.bump(0.5, at=0.3), legend_x=380)
        r = _one(_shadow(c))
        assert (r.processing_status, r.abstention_reason) == ("NOT_DETERMINABLE", "series_color_ambiguous")
        assert r.series_identity_status == "FAIL"

    def test_two_distinct_series_are_each_measured(self):
        c = K.Chart()
        c.curve("Série A", K.BLUE, K.bump(0.7), legend_x=200)
        c.curve("Série B", K.ORANGE, K.bump(0.5, at=0.25), legend_x=380)
        out = _shadow(c)
        assert abs(_one(out).extremum_value - 0.7) < 0.01
        assert abs(_one(out, "Série B").extremum_value - 0.5) < 0.01
        assert len(out) == 4, "max et min de chaque série"


# =========================================================================== #
# E. Robustesse : jamais d'exception, toujours ERROR ou abstention
# =========================================================================== #
class TestRobustness:
    def test_invalid_image(self):
        c = K.simple_chart()
        out = CS.run_for_asset(doc=_Doc(), asset=K.Asset(b"pas une image"), data=c.response())
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("ERROR", "invalid_image")]

    def test_missing_ocr(self):
        c = K.simple_chart()
        data = c.response()
        data["raw_output"] = {}
        out = CS.run_for_asset(doc=_Doc(), asset=K.Asset(c.png()), data=data)
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("NOT_DETERMINABLE", "missing_ocr")]

    def test_missing_legend(self):
        c = K.Chart()
        c.curve("Série A", K.BLUE, K.bump(0.7))
        c.curve("Série B", K.ORANGE, K.bump(0.5, at=0.3))
        r = _one(_shadow(c))
        assert (r.processing_status, r.abstention_reason) == ("NOT_DETERMINABLE", "series_not_in_legend")

    def test_numerical_calibration_failure(self):
        c = K.simple_chart()
        data = c.response()
        texts = data["raw_output"]["ocr"]["rec_texts"]
        data["raw_output"]["ocr"]["rec_texts"] = ["0,9" if t == "0,2" else t for t in texts]
        r = _one(CS.run_for_asset(doc=_Doc(), asset=K.Asset(c.png()), data=data))
        assert r.processing_status == "NOT_DETERMINABLE" and r.axis_calibration_status == "FAIL"
        assert r.abstention_reason == "axis_calibration_nonlinear"

    def test_prototype_exception(self):
        def boom(*a, **k):
            raise RuntimeError("panne")

        out = _shadow(K.simple_chart(), digitize=boom)
        assert {(r.processing_status, r.abstention_reason) for r in out} == {("ERROR", "prototype_exception")}

    def test_memory_limit_exceeded(self):
        def oom(*a, **k):
            raise MemoryError()

        out = _shadow(K.simple_chart(), digitize=oom)
        assert {(r.processing_status, r.abstention_reason) for r in out} == {("ERROR", "memory_limit")}

    def test_slow_call_is_discarded_as_timeout(self):
        ticks = iter(range(0, 10_000_000, 600))  # chaque lecture d'horloge avance de 0,6 s

        def clock():
            return next(ticks) / 1000.0

        def slow(*a, **k):
            clock()
            clock()
            return CD.Result("MEASURED_FROM_PIXELS", value=0.5, interval=(0.49, 0.51))

        out = _shadow(K.simple_chart(), clock=clock, digitize=slow)
        assert out[0].processing_status == "ERROR" and out[0].abstention_reason == "timeout"
        assert out[0].extremum_value is None
        assert {r.abstention_reason for r in out} <= {"timeout", "budget_exhausted"}

    def test_budget_exhausted_skips_remaining_calls(self):
        ticks = iter(range(0, 10_000_000, 2500))
        calls = []

        def counted(*a, **k):
            calls.append(1)
            return CD.Result("NOT_DETERMINABLE", "x")

        out = _shadow(K.simple_chart(), clock=lambda: next(ticks) / 1000.0, digitize=counted)
        assert calls == [] and {r.abstention_reason for r in out} == {"budget_exhausted"}

    def test_image_too_large_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(CS, "MAX_PIXELS", 1000)
        out = _shadow(K.simple_chart())
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("UNSUPPORTED", "image_too_large")]

    def test_too_many_series_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(CS, "MAX_SERIES", 0)
        out = _shadow(K.simple_chart())
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("UNSUPPORTED", "too_many_series")]

    def test_multiple_chart_items_are_unsupported(self):
        c = K.simple_chart()
        data = c.response()
        data["items"].append(dict(data["items"][0]))
        out = CS.run_for_asset(doc=_Doc(), asset=K.Asset(c.png()), data=data)
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("UNSUPPORTED", "multiple_chart_items")]

    def test_chart_without_series_yields_nothing(self):
        c = K.Chart()
        assert CS.run_for_asset(doc=_Doc(), asset=K.Asset(c.png()), data=c.response()) == []

    def test_unexpected_failure_is_contained(self):
        out = CS.run_for_asset(doc=_Doc(), asset=None, data={"items": [{"series": [{"values": [1]}]}]})
        assert [(r.processing_status, r.abstention_reason) for r in out] == [("ERROR", "shadow_exception")]

    def test_response_is_not_mutated(self):
        c = K.simple_chart()
        data = c.response()
        before = repr(data)
        CS.run_for_asset(doc=_Doc(), asset=K.Asset(c.png()), data=data)
        assert repr(data) == before


# =========================================================================== #
# B/F2. Portée
# =========================================================================== #
class TestScope:
    def test_registry_default_is_off_and_empty(self):
        from config import registry

        assert registry.require("STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED").default is False
        assert registry.require("STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST").default == []

    def test_default_settings_refuse(self, settings):
        settings.DJANGO_ENV = "staging"
        assert CS.shadow_refusal(SPACE) is not None

    def test_allowlisted_space_runs(self, shadow_on):
        assert CS.shadow_refusal(SPACE) is None

    def test_unauthorized_space_is_refused(self, shadow_on):
        assert CS.shadow_refusal(OTHER) == "curve_shadow_space_not_allowed"

    def test_shadow_allowlist_alone_is_not_enough(self, shadow_on):
        shadow_on.STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST = [OTHER]
        assert CS.shadow_refusal(SPACE) == "structured_visual_space_not_allowed"

    def test_flag_off_is_refused(self, shadow_on):
        shadow_on.STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED = False
        assert CS.shadow_refusal(SPACE) == "curve_shadow_disabled"

    def test_structured_off_is_refused(self, shadow_on):
        shadow_on.STRUCTURED_VISUAL_ENABLED = False
        assert CS.shadow_refusal(SPACE) == "structured_visual_disabled"

    @pytest.mark.parametrize("env", ["prod", "production", " PROD "])
    def test_production_is_refused_with_every_flag_on(self, shadow_on, env):
        shadow_on.DJANGO_ENV = env
        assert CS.shadow_refusal(SPACE) == "curve_shadow_production_refused"


# =========================================================================== #
# D/F4. Isolation : sortie canonique identique, aucune valeur exposée
# =========================================================================== #
class _Result:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def extract(self, **kwargs):
        self.calls += 1
        return _Result(self.data)


def _extract(chart, **kw):
    import copy

    client = _Client(copy.deepcopy(chart.response()))
    out = S.extract_asset(doc=_Doc(), asset=K.Asset(chart.png()), task="chart", client=client,
                          do_publish=False, **kw)
    return out, client


_REAL_NORMALIZE = S.normalize_response


def _canonical(monkeypatch, chart):
    """Extraction à blanc ; rend aussi la sortie CANONIQUE (unités texte + méta, tableaux)."""
    captured = []

    def spy(*a, **k):
        norm = _REAL_NORMALIZE(*a, **k)
        captured.append(([(u.text, u.meta) for u in norm.units], norm.tables))
        return norm

    monkeypatch.setattr(S, "normalize_response", spy)
    out, client = _extract(chart)
    return out, client, captured


class TestIsolation:
    def test_shadow_on_and_off_produce_identical_canonical_output(self, shadow_on, monkeypatch):
        chart = K.simple_chart(0.83)
        shadow_on.STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED = False
        off, client_off, canon_off = _canonical(monkeypatch, chart)
        shadow_on.STRUCTURED_VISUAL_CURVE_SHADOW_ENABLED = True
        on, client_on, canon_on = _canonical(monkeypatch, chart)

        assert off.curve_shadow == [] and off.curve_shadow_status == "off:curve_shadow_disabled"
        assert on.curve_shadow_status == "ran" and on.curve_shadow
        assert canon_on == canon_off
        assert (on.units_text, on.table_texts, on.status, on.units) == (
            off.units_text, off.table_texts, off.status, off.units)
        assert (client_off.calls, client_on.calls) == (1, 1), "aucun appel Gateway supplémentaire"

    def test_measured_value_never_reaches_fragments(self, shadow_on, monkeypatch):
        out, _, canon = _canonical(monkeypatch, K.simple_chart(0.83))
        measured = [r for r in out.curve_shadow if r.processing_status == "MEASURED_FROM_PIXELS"]
        assert measured
        blob = repr(canon) + repr(out.units_text)
        for r in measured:
            for shown in (f"{r.extremum_value:.4g}", f"{r.extremum_value:.3f}".replace(".", ","),
                          f"{r.extremum_value:.3f}"):
                assert shown not in blob
        assert "curve_shadow" not in blob and "MEASURED_FROM_PIXELS" not in blob

    def test_production_runs_nothing_even_when_called_directly(self, shadow_on, monkeypatch):
        shadow_on.DJANGO_ENV = "production"
        ran = []
        monkeypatch.setattr(CS, "run_for_asset", lambda **kw: ran.append(1) or [])
        out, _ = _extract(K.simple_chart())
        # STRUCTVIS-PRODUCTION-GATE-1 : `chart` hors du plafond de production, refus avant l'appel.
        assert ran == [] and out.status == "refused" and out.gateway_calls == 0
        # La garde propre du shadow tient seule, dérogation structurée comprise.
        shadow_on.STRUCTURED_VISUAL_PRODUCTION_ALLOWED = True
        assert CS.shadow_refusal(SPACE) == "curve_shadow_production_refused"

    def test_unauthorized_space_runs_nothing(self, shadow_on, monkeypatch):
        shadow_on.STRUCTURED_VISUAL_CURVE_SHADOW_RAGSPACE_ALLOWLIST = [OTHER]
        out, _ = _extract(K.simple_chart())
        assert out.curve_shadow == [] and out.curve_shadow_status == "off:curve_shadow_space_not_allowed"

    def test_engine_crash_does_not_change_extraction(self, shadow_on, monkeypatch):
        ok, _ = _extract(K.simple_chart())

        def crash(**kw):
            raise RuntimeError("panne")

        monkeypatch.setattr(CS, "run_for_asset", crash)
        broken, _ = _extract(K.simple_chart())
        assert broken.curve_shadow_status == "error" and broken.curve_shadow == []
        assert (broken.status, broken.units_text) == (ok.status, ok.units_text)

    def test_table_task_never_runs_shadow(self, shadow_on, monkeypatch):
        ran = []
        monkeypatch.setattr(CS, "run_for_asset", lambda **kw: ran.append(1) or [])
        c = K.simple_chart()
        data = c.response()
        data["task"] = "table"
        data["items"] = []
        S.extract_asset(doc=_Doc(), asset=K.Asset(c.png()), task="table", client=_Client(data), do_publish=False)
        assert ran == []

    def test_logs_carry_counts_never_values_or_names(self, shadow_on, caplog):
        caplog.set_level(logging.DEBUG)
        out, _ = _extract(K.simple_chart(0.83))
        values = [r.extremum_value for r in out.curve_shadow if r.extremum_value is not None]
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "curve shadow" in text and "measured=" in text
        assert "Série A" not in text
        for v in values:
            assert f"{v:.3f}" not in text and f"{v:.2f}" not in text

    def test_outcome_repr_hides_shadow_results(self, shadow_on):
        out, _ = _extract(K.simple_chart(0.83))
        assert "MEASURED" not in repr(out)


def test_synthetic_png_is_valid():
    assert Image.open(io.BytesIO(K.simple_chart().png())).size == (K.W, K.H)

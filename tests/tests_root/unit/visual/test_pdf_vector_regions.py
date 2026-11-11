"""régions VECTORIELLES rendues d'un PDF.

Contrat :
- drapeau éteint (``vector_regions=False``) : AUCUN code nouveau exécuté (ni ``get_drawings`` ni ``get_pixmap``),
  sortie identique ;
- allumé : graphiques, courbes, schémas de blocs et dessins techniques tracés en primitives deviennent UN
  ``VisualAsset`` ``origin="rendered"`` par figure, rendu de la SEULE région ;
- négatifs : image raster seule, grille de tableau à texte, décor répété, texte seul, page trop complexe ;
- géométrie : bbox dans le repère des images intégrées (page non tournée, relatif à la CropBox), rendu orienté
  comme affiché ;
- budget visuel COMMUN avec les images intégrées, bornes de pixels, candidats, temps.
Les verdicts de décision sont portés par la variante ``val`` (jamais utilisée pour régler les seuils).
"""
from __future__ import annotations

import io

import pytest

fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest.extraction import config as C  # noqa: E402
from apps.rag_core.services.ingest.extraction import pdf_vector as PV  # noqa: E402
from apps.rag_core.services.ingest.extraction import pdf_visual as P  # noqa: E402
from apps.rag_core.services.ingest.extraction.pdf_visual import PdfVisualLimits, discover_pdf_visual_assets  # noqa: E402

from . import _pdf_vector_fixtures as F  # noqa: E402

VARIANTS = ("dev", "val")


def _disc(data, **kw):
    return discover_pdf_visual_assets(data, vector_regions=kw.pop("vector_regions", True), **kw)


def _img(asset):
    from PIL import Image
    return Image.open(io.BytesIO(asset.content)).convert("RGB")


# --------------------------------------------------------------------------- #
# Détection utile
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("family", sorted(F.FAMILIES))
def test_each_vector_family_yields_one_rendered_asset(family, variant):
    d = _disc(F.FAMILIES[family](variant))
    assert [a.origin for a in d.assets] == ["rendered"]
    a = d.assets[0]
    assert a.source_page == 1 and a.source_kind == "pdf" and a.render_dpi and a.render_dpi <= 150
    assert a.source_locator["region"] == "vector" and a.source_locator["page"] == 1
    assert a.width * a.height <= PdfVisualLimits().max_pixels
    x0, y0, x1, y1 = a.source_bbox
    assert 0 <= x0 < x1 <= F.A4[0] and 0 <= y0 < y1 <= F.A4[1]
    assert _img(a).size == (a.width, a.height)
    assert d.stats["vector_admitted"] == 1


@pytest.mark.parametrize("variant", VARIANTS)
def test_render_includes_axis_labels_and_title(variant):
    """Les libellés proches (titre, graduations) sont dans la région rendue, pas seulement les tracés."""
    data = F.bar_chart(variant)
    a = _disc(data).assets[0]
    page = fitz.open(stream=data)[0]
    words = {w[4]: w[:4] for w in page.get_text("words")}
    x0, y0, x1, y1 = a.source_bbox
    for label in ("200", "S1"):
        wx0, wy0, wx1, wy1 = words[label]
        assert x0 <= wx0 and wx1 <= x1 and y0 <= wy0 and wy1 <= y1, label


# --------------------------------------------------------------------------- #
# Négatifs et non-duplication
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("negative,reasons", [
    ("raster_only", ()), ("native_table", ("vector_omitted_table_like",)),
    ("decorative", ("vector_omitted_decorative", "vector_page_frames", "vector_omitted_small")),
    ("text_only", ()), ("framed_page", ())])
def test_negatives_produce_no_rendered_asset(negative, reasons, variant):
    d = _disc(F.NEGATIVES[negative](variant))
    assert all(a.origin == "embedded" for a in d.assets)
    assert d.stats["vector_admitted"] == 0
    if reasons:
        assert sum(d.stats.get(r, 0) for r in reasons) >= 1


def test_text_only_and_raster_only_never_render(monkeypatch):
    calls = []
    real = fitz.Page.get_pixmap
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    for data in (F.text_only(), F.raster_only()):
        _disc(data)
    assert calls == []


@pytest.mark.parametrize("variant", VARIANTS)
def test_mixed_keeps_both_kinds(variant):
    d = _disc(F.mixed(variant))
    assert sorted((a.origin, a.source_page) for a in d.assets) == [("embedded", 1), ("rendered", 2)]


@pytest.mark.parametrize("variant", VARIANTS)
def test_composite_figure_supersedes_its_inner_raster(variant):
    d = _disc(F.composite(variant))
    assert [a.origin for a in d.assets] == ["rendered"] and d.stats["vector_raster_superseded"] == 1


@pytest.mark.parametrize("variant", VARIANTS)
def test_page_frame_never_turns_the_page_into_a_figure(variant):
    """Constat de qualification : un cadre de page reliait tout. Seul le graphique est rendu."""
    a, = _disc(F.chart_on_framed_page(variant)).assets
    x0, y0, x1, y1 = a.source_bbox
    assert (x1 - x0) * (y1 - y0) < 0.5 * F.A4[0] * F.A4[1]


def test_native_table_region_wins_over_vector():
    data = F.bar_chart("dev")
    d = _disc(data, suppressed_regions={1: [(60, 200, 560, 560)]})
    assert d.assets == [] and (d.stats["vector_omitted_table_overlap"] + d.stats["omitted_table_overlap"]) >= 1


def test_identical_drawing_repeated_is_decorative_but_same_template_with_new_data_is_not():
    assert _disc(F.repeated_identical_chart(5)).stats["vector_omitted_decorative"] == 5
    assert len(_disc(F.many_charts(6)).assets) == 6


# --------------------------------------------------------------------------- #
# Géométrie
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("variant", VARIANTS)
def test_rotation_keeps_unrotated_bbox_and_renders_as_displayed(variant):
    base = _disc(F.rotated(0, variant)).assets[0]
    for rot in (90, 180, 270):
        a = _disc(F.rotated(rot, variant)).assets[0]
        assert a.source_bbox == base.source_bbox, rot
        assert a.source_locator["rotation"] == rot
        expected = (base.height, base.width) if rot in (90, 270) else (base.width, base.height)
        # ±1 px : arrondi de MuPDF sur un rectangle transformé (même zone, même échelle).
        assert abs(a.width - expected[0]) <= 1 and abs(a.height - expected[1]) <= 1, rot
        # Barres bleues présentes : la bonne zone a été rendue.
        assert any(p[2] > 150 and p[0] < 100 for p in _img(a).resize((64, 64)).getdata()), rot


@pytest.mark.parametrize("variant", VARIANTS)
def test_cropbox_bbox_is_relative_to_the_cropbox(variant):
    full = _disc(F.rotated(0, variant)).assets[0].source_bbox
    crop = _disc(F.cropped(variant)).assets[0].source_bbox
    assert crop[0] == pytest.approx(max(full[0] - 40, 0), abs=0.6) and crop[1] == pytest.approx(full[1] - 60, abs=0.6)


def test_bbox_convention_matches_embedded_images():
    """Repère commun : la bbox d'une image intégrée et d'un tracé posés au même endroit coïncident."""
    for rot in (0, 90):
        d = fitz.open(); p = d.new_page(width=600, height=800)
        p.insert_image(fitz.Rect(100, 100, 300, 200), stream=F._png(200, 100, (200, 0, 0)))
        p.draw_rect(fitz.Rect(100, 300, 300, 400), color=(0, 0, 0))
        p.set_rotation(rot)
        assert tuple(round(v) for v in p.get_image_rects(p.get_images()[0][0])[0]) == (100, 100, 300, 200)
        assert tuple(round(v) for v in p.get_drawings()[0]["rect"]) == (100, 300, 300, 400)


# --------------------------------------------------------------------------- #
# Budgets et résilience
# --------------------------------------------------------------------------- #
def test_shared_budget_never_exceeds_max_assets():
    d = _disc(F.many_charts(10), limits=PdfVisualLimits(max_assets=4))
    assert len(d.assets) == 4 and d.stats["admitted"] == 4


def test_max_candidates_bounds_detection(settings):
    settings.DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES = 3
    d = _disc(F.many_charts(10))
    assert d.stats["vector_regions_detected"] == 3 and d.stats["vector_omitted_limit"] == 7


def test_pixel_cap_lowers_render_resolution():
    d = _disc(F.bar_chart("dev"), limits=PdfVisualLimits(max_pixels=100_000))
    a = d.assets[0]
    assert a.width * a.height <= 100_000 and a.render_dpi < 150


def test_page_too_complex_is_skipped_without_render():
    d = _disc(F.complex_page(6000))
    assert d.assets == [] and d.stats["vector_page_too_complex"] == 1


def test_time_budget_stops_inspection(monkeypatch):
    ticks = iter([0.0] + [PV.TIME_BUDGET_S + 1] * 50)
    monkeypatch.setattr(PV.time, "monotonic", lambda: next(ticks))
    d = _disc(F.many_charts(5))
    assert d.stats["vector_time_budget_exhausted"] == 1 and d.assets == []


@pytest.mark.parametrize("data", [b"", b"%PDF-1.7 garbage", b"not a pdf"])
def test_malformed_input_is_empty_not_an_error(data):
    assert _disc(data).assets == []


def test_detection_failure_keeps_embedded_images(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("hostile")
    monkeypatch.setattr(PV, "detect_vector_regions", boom)
    d = _disc(F.mixed("dev"))
    assert [a.origin for a in d.assets] == ["embedded"] and d.stats["vector_errors"] == 1


def test_render_failure_is_counted_and_skipped(monkeypatch):
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = _disc(F.bar_chart("dev"))
    assert d.assets == [] and d.stats["omitted_unreadable"] >= 1


def test_deterministic_pixels():
    hashes = {tuple(a.content_hash for a in _disc(F.block_diagram("val")).assets) for _ in range(3)}
    assert len(hashes) == 1


# --------------------------------------------------------------------------- #
# Drapeau éteint : chemin historique strictement inchangé
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("make", [F.bar_chart, F.mixed, F.raster_only, F.composite, F.text_only])
def test_off_runs_no_new_code_and_matches_history(make, monkeypatch):
    seen = []
    real_draw, real_pix = fitz.Page.get_drawings, fitz.Page.get_pixmap
    monkeypatch.setattr(fitz.Page, "get_drawings", lambda self, *a, **k: seen.append("d") or real_draw(self, *a, **k))
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda self, *a, **k: seen.append("p") or real_pix(self, *a, **k))
    data = make("dev")
    off = discover_pdf_visual_assets(data)
    assert seen == [] and not any(k.startswith("vector") for k in off.stats)
    assert all(a.origin == "embedded" and a.render_dpi is None for a in off.assets)
    explicit = discover_pdf_visual_assets(data, vector_regions=False)
    assert [a.content_hash for a in explicit.assets] == [a.content_hash for a in off.assets]
    assert explicit.stats == off.stats


# --------------------------------------------------------------------------- #
# Porte de configuration
# --------------------------------------------------------------------------- #
SPACE = "4d1c2b3a-1111-4222-8333-944455566677"


@pytest.mark.parametrize("enabled,ids,space,expected", [
    (False, [SPACE], SPACE, False), (True, [], SPACE, False), (True, [SPACE], SPACE, True),
    (True, [SPACE], None, False), (True, [SPACE[:8]], SPACE, False), (True, [SPACE, "x"], SPACE, False),
    (True, [SPACE.upper()], SPACE, True), (True, [SPACE], "5e2d3c4b-1111-4222-8333-944455566677", False),
])
def test_enabled_for_space(settings, enabled, ids, space, expected):
    settings.DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED = enabled
    settings.DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS = ids
    assert C.pdf_vector_regions_enabled_for(space) is expected


def test_registry_defaults_are_off(settings):
    from config import registry

    assert registry.get("DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED").default is False
    assert registry.get("DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS").default == []


def test_extractor_wiring_follows_the_space(settings):
    from apps.rag_core.services.ingest.extraction.base import ExtractionContext
    from apps.rag_core.services.ingest.extraction.pdf_extractor import PdfExtractor

    settings.DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED = True
    settings.DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS = [SPACE]
    data = F.bar_chart("val")

    def run(space):
        ctx = ExtractionContext(filename="f.pdf", rag_space_id=space, visual_sources=frozenset({"pdf"}))
        return [a.origin for a in PdfExtractor().extract(data, ctx).visual_assets]

    assert run(SPACE) == ["rendered"] and run("5e2d3c4b-1111-4222-8333-944455566677") == [] and run(None) == []

"""Contrat de rendu ``ragatm-pdfium/1`` (CV-3C) — PDF 100 % synthétiques.

Les vecteurs ``fixtures/raster_vectors/vectors.json`` ont été mesurés par CV-3A sur deux
machines (Intel, AMD) et conteneurs : un écart de pixels n'est JAMAIS corrigé en mettant
la valeur attendue à jour. Il impose une évaluation et, normalement, ``ragatm-pdfium/2``.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest

from apps.rag_core.services.ingest.extraction.pdf_geometry import read_pdf_page_geometry
from apps.rag_core.services.raster import contract as C

VECTORS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "raster_vectors"
VECTORS = json.loads((VECTORS_DIR / "vectors.json").read_text())

# RAGatm Open : les 13 PDF de ce contrat sont des binaires non livrés par l'export
# (revue humaine en attente). Les vecteurs `vectors.json` sont le contrat
# `ragatm-pdfium/1` mesuré sur ces PDF exacts : les régénérer changerait les empreintes et
# imposerait `ragatm-pdfium/2`. Tant que les PDF ne sont pas réintroduits (approbation ou
# régénération + nouveau contrat), la suite est SAUTÉE explicitement, jamais contournée.
_MISSING = sorted(n for n in VECTORS["pdf_sha256"] if not (VECTORS_DIR / n).exists())
if _MISSING:
    pytest.skip(f"fixtures PDF du contrat ragatm-pdfium/1 absentes ({len(_MISSING)}/13) : "
                "revue binaire en attente", allow_module_level=True)


def _raw(name: str) -> bytes:
    data = (VECTORS_DIR / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == VECTORS["pdf_sha256"][name], f"fixture altérée : {name}"
    return data


def _render(name: str, dpi: int) -> C.RenderedRaster:
    raw = _raw(name)
    g = read_pdf_page_geometry(raw)[0]
    return C.render_pdf_page(raw, page_number=1, cropbox=g.cropbox, rotate=g.rotate, dpi=dpi)


def _locate(rgb, color, tol=40):
    m = (np.abs(rgb.astype(int) - np.array(color)).sum(axis=2) < tol)
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return round(float(xs.mean()) / rgb.shape[1], 2), round(float(ys.mean()) / rgb.shape[0], 2)


# -- paramètres et hashes ------------------------------------------------------------
def test_canonical_params_are_stable_and_complete():
    p = C.rasterizer_params(200)
    assert C.RASTERIZER_ID == "ragatm-pdfium/1"
    assert p == {
        "box": "cropbox", "page_rotation": "applied", "extra_rotation": 0, "dpi": 200,
        "size_rule": "ceil(box_pt_after_rotation * dpi / 72)", "user_unit": "ignored",
        "color": "RGB8", "background": "FFFFFF", "draw_annots": False, "forms": False,
        "antialias": "pdfium_default", "font_warmup": "process-v1",
        "render_isolation": "fresh-interpreter-per-page",
    }
    assert C.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")
    # Valeur figée : tout changement de paramètre DOIT changer params_sha256.
    assert C.params_sha256(p) == hashlib.sha256(C.canonical_json(p)).hexdigest()
    assert C.params_sha256(p) == C.params_sha256(dict(reversed(list(p.items()))))
    assert len({C.params_sha256(C.rasterizer_params(d)) for d in (150, 200, 300)}) == 3
    changed = dict(p, draw_annots=True)
    assert C.params_sha256(changed) != C.params_sha256(p)


@pytest.mark.parametrize("dpi", [72, 144, 199, 600])
def test_unsupported_dpi_rejected(dpi):
    with pytest.raises(C.UnsupportedRasterProfile):
        C.rasterizer_params(dpi)


def test_pixels_sha256_definition_and_buffer_guard():
    rgb = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    expected = hashlib.sha256(struct.pack(">II", 3, 2) + rgb.tobytes()).hexdigest()
    assert C.pixels_sha256(3, 2, rgb) == expected
    with pytest.raises(C.RasterContractMismatch):
        C.pixels_sha256(4, 2, rgb)


def test_expected_size_uses_ceil_and_rotation_swap():
    assert C.expected_raster_size((0, 0, 595, 842), 0, 200) == (1653, 2339)
    assert C.expected_raster_size((0, 0, 595, 842), 90, 200) == (2339, 1653)
    assert C.expected_raster_size((50, 100, 545, 742), 0, 200) == (1375, 1784)
    assert C.expected_raster_size((0, 0, 595, 842), 0, 72 * 2) == (1190, 1684)


# -- plafond mémoire ----------------------------------------------------------------
def test_memory_ceiling_accepts_a1_300_and_refuses_a0_300_before_any_render(monkeypatch):
    monkeypatch.setattr(C, "_run_render_child", lambda *a, **k: pytest.fail("rendu lancé"))
    a1 = C.expected_raster_size((0, 0, 1683.78, 2383.94), 0, 300)
    C.check_raster_budget(*a1)                                   # 69,7 MP < 80 MP
    with pytest.raises(C.RasterTooLarge):
        C.render_pdf_page(b"%PDF", page_number=1, cropbox=(0, 0, 2383.94, 3370.39), rotate=0, dpi=300)


# -- vecteurs -------------------------------------------------------------------------
@pytest.mark.parametrize("vec", VECTORS["vectors"], ids=lambda v: f"{v['fixture']}@{v['dpi']}")
def test_raster_vectors(vec):
    r = _render(vec["fixture"], vec["dpi"])
    assert (r.width, r.height) == (vec["width"], vec["height"])
    assert r.pixels_sha256 == vec["pixels_sha256"], (
        "pixels divergents : NE PAS mettre à jour vectors.json — évaluer et passer à ragatm-pdfium/2")
    assert r.rasterizer_id == VECTORS["rasterizer_id"]
    assert r.rgb.dtype == np.uint8 and r.rgb.shape == (r.height, r.width, 3) and r.rgb.flags.c_contiguous


def test_same_page_rendered_twice_in_separate_processes_is_identical():
    a, b = _render("F01_a4_text_vector.pdf", 200), _render("F01_a4_text_vector.pdf", 200)
    assert a.pixels_sha256 == b.pixels_sha256 and np.array_equal(a.rgb, b.rgb)


# -- préchauffage F13 ----------------------------------------------------------------
def test_font_warmup_is_required_and_isolates_from_process_history():
    """F13 (police substituée) : sans préchauffage → état FROID ; un historique pollué du
    processus appelant donnerait encore d'autres pixels ; le contrat donne l'état chaud."""
    import pypdfium2 as pdfium

    raw = _raw("F13_nonembedded_font.pdf")
    warm = next(v for v in VECTORS["vectors"]
                if v["fixture"] == "F13_nonembedded_font.pdf" and v["dpi"] == 150)
    cold = C._run_render_child(raw, page_number=1, dpi=150, width=1240, height=1755, warmup=False)
    assert C.pixels_sha256(1240, 1755, cold) == VECTORS["f13_cold_first_render_150dpi_pixels_sha256"]
    assert VECTORS["f13_cold_first_render_150dpi_pixels_sha256"] != warm["pixels_sha256"]

    # Pollue le processus de test par un rendu à police substituée.
    polluter = raw.replace(b"/BaseFont /DejaVuSans-Bold", b"/BaseFont /Calibri-Bold")
    doc = pdfium.PdfDocument(polluter)
    doc[0].render(scale=150 / 72, draw_annots=False, may_draw_forms=False).close()
    doc.close()

    assert _render("F13_nonembedded_font.pdf", 150).pixels_sha256 == warm["pixels_sha256"]


# -- géométrie -------------------------------------------------------------------------
@pytest.mark.parametrize("name,size,red", [
    ("F01_a4_text_vector.pdf", (1653, 2339), (0.07, 0.05)),
    ("F07_rotate90.pdf", (2339, 1653), (0.95, 0.07)),   # horaire : haut-gauche → haut-droite
    ("F08_rotate180.pdf", (1653, 2339), (0.93, 0.95)),
    ("F09_rotate270.pdf", (2339, 1653), (0.05, 0.93)),
])
def test_page_rotation_applied_once_by_pdfium(name, size, red):
    r = _render(name, 200)
    assert (r.width, r.height) == size
    assert _locate(r.rgb, (255, 0, 0)) == red


def test_cropbox_is_rendered_and_guarded():
    r = _render("F06_cropbox.pdf", 200)
    assert (r.width, r.height) == (1375, 1784)
    assert _locate(r.rgb, (255, 255, 0)) is None          # bande hors CropBox absente
    with pytest.raises(C.RasterContractMismatch):         # géométrie déclarée fausse → refus
        C.render_pdf_page(_raw("F06_cropbox.pdf"), page_number=1, cropbox=(0, 0, 595, 842),
                          rotate=0, dpi=200)


def test_annotations_and_forms_are_not_drawn_explicitly():
    import pypdfium2 as pdfium

    r = _render("F10_annots_forms.pdf", 200)
    assert _locate(r.rgb, (0, 255, 255)) is None           # annotation Square (cyan)
    assert _locate(r.rgb, (255, 204, 204), tol=20) is None  # widget AcroForm (rose)
    # Les défauts de pypdfium2 dessinent les annotations : le contrat ne s'y fie pas.
    doc = pdfium.PdfDocument(_raw("F10_annots_forms.pdf"))
    default = np.ascontiguousarray(doc[0].render(scale=200 / 72).to_numpy()[:, :, ::-1])
    doc.close()
    assert _locate(default, (0, 255, 255)) is not None


def test_user_unit_is_ignored():
    """/UserUnit 2 : PDFium ne l'applique pas — taille = ceil(pt × dpi/72)."""
    content = b"BT /F1 18 Tf 72 700 Td (UserUnit test page synthetique) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /UserUnit 2 "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.7\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % x
    r = C.render_pdf_page(out, page_number=1, cropbox=(0, 0, 300, 400), rotate=0, dpi=200)
    assert (r.width, r.height) == (834, 1112)


# -- échecs ----------------------------------------------------------------------------
def test_unreadable_pdf_fails_with_stable_error():
    with pytest.raises(C.RasterError) as e:
        C.render_pdf_page(b"%PDF-1.7\nTRUNCATED", page_number=1, cropbox=(0, 0, 100, 100),
                          rotate=0, dpi=150)
    assert "raster_render_failed" in str(e.value)


def test_absent_page_is_a_contract_mismatch():
    with pytest.raises(C.RasterContractMismatch):
        C.render_pdf_page(_raw("F01_a4_text_vector.pdf"), page_number=2, cropbox=(0, 0, 595, 842),
                          rotate=0, dpi=150)


def test_render_timeout_kills_the_child():
    with pytest.raises(C.RasterError):
        C.render_pdf_page(_raw("F11_a3.pdf"), page_number=1, cropbox=(0, 0, 842, 1191), rotate=0,
                          dpi=300, timeout_s=0.01)


def test_implementation_diagnostics_are_not_identity():
    diag = C.implementation_diagnostics()
    assert {"pypdfium2", "pdfium", "pdfium_build", "pillow"} <= set(diag)
    assert not set(diag) & set(C.rasterizer_params(200))

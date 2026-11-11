"""Géométrie DÉCLARÉE des pages PDF (CV-3B) — lecture seule, aucun raster.

PDF 100 % synthétiques, construits en mémoire.
"""
from __future__ import annotations

import pytest

# RAGatm Open : moteur natif PyMuPDF = extra optionnel `pdf-mupdf` (AGPL), jamais installé par défaut.
fitz = pytest.importorskip("fitz")

from apps.rag_core.services.ingest.extraction.pdf_geometry import read_pdf_page_geometry


def _three_page_pdf(*, labels: bool = False) -> bytes:
    doc = fitz.open()
    doc.new_page(width=595, height=842)                        # A4 portrait
    p2 = doc.new_page(width=842, height=595)                   # paysage, CropBox réduite
    p2.set_cropbox(fitz.Rect(50, 40, 792, 555))
    p3 = doc.new_page(width=842, height=1191)                  # A3, /Rotate 90
    p3.set_rotation(90)
    for p in doc:
        p.insert_text((72, 72), f"Page synthetique {p.number + 1} : texte natif suffisant pour l'extraction.",
                      fontname="helv", fontsize=10)
    if labels:
        doc.set_page_labels([
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
        ])
    return doc.tobytes()


def _raw_pdf(pages_dict: bytes, kids: list[bytes]) -> bytes:
    """PDF minimal écrit à la main (xref exacte) pour tester l'HÉRITAGE d'attributs."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", pages_dict] + kids
    out = b"%PDF-1.7\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_reads_mediabox_cropbox_rotate_per_page_one_based():
    geo = read_pdf_page_geometry(_three_page_pdf())
    assert [g.page_number for g in geo] == [1, 2, 3]
    assert geo[0].mediabox == (0, 0, 595, 842) and geo[0].cropbox == (0, 0, 595, 842)
    assert geo[0].rotate == 0
    # CropBox déclarée ≠ MediaBox, en coordonnées PDF (origine bas-gauche).
    assert geo[1].mediabox == (0, 0, 842, 595)
    assert geo[1].cropbox == (50, 40, 792, 555)
    assert geo[2].mediabox == (0, 0, 842, 1191) and geo[2].rotate == 90


def test_page_labels_only_when_declared():
    assert [g.page_label for g in read_pdf_page_geometry(_three_page_pdf())] == [None, None, None]
    labelled = read_pdf_page_geometry(_three_page_pdf(labels=True))
    assert [g.page_label for g in labelled] == ["i", "ii", "1"]
    # Le libellé n'altère jamais l'ordre physique.
    assert [g.page_number for g in labelled] == [1, 2, 3]


def test_inherited_attributes_and_normalized_corners_and_rotate_modulo():
    pdf = _raw_pdf(
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 612 792] "
        b"/CropBox [10 20 600 780] /Rotate -90 >>",
        [b"<< /Type /Page /Parent 2 0 R >>",
         # Coins inversés + décimales : normalisés, valeurs exactes conservées.
         b"<< /Type /Page /Parent 2 0 R /MediaBox [595.276 841.89 0 0] /Rotate 180 >>"],
    )
    geo = read_pdf_page_geometry(pdf)
    assert geo[0].mediabox == (0, 0, 612, 792)
    assert geo[0].cropbox == (10, 20, 600, 780)          # héritée du nœud /Pages
    assert geo[0].rotate == 270                          # -90 ≡ 270
    assert geo[1].mediabox == (0, 0, 595.276, 841.89)
    assert geo[1].rotate == 180


@pytest.mark.parametrize("bad", [
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 612 792] /Rotate 45 >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 0 792] >>",
])
def test_invalid_geometry_makes_document_geometry_unavailable(bad):
    assert read_pdf_page_geometry(_raw_pdf(bad, [b"<< /Type /Page /Parent 2 0 R >>"])) is None


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.7\nTRUNCATED"])
def test_unreadable_input_returns_none(data):
    assert read_pdf_page_geometry(data) is None


def test_pdf_extractor_exposes_transient_geometry_not_in_metadata():
    from apps.rag_core.services.ingest.extraction import extract_document

    result = extract_document(_three_page_pdf(), filename="geo.pdf", content_type="application/pdf")
    assert [g.page_number for g in result.page_geometry] == [1, 2, 3]
    assert "page_geometry" not in (result.metadata or {})

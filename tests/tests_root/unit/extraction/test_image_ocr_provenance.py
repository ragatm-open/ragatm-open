"""Le texte d'une IMAGE est de l'OCR : il doit être déclaré comme tel.

Défaut préexistant mis au jour lors de la qualification : `ImageExtractor` rendait
`pages=[(1, texte)]` sans `ocr_pages`. Le rail paginé de l'indexation posait alors
`source_is_ocr=False`, et la politique de preuve numérique classait ce texte comme
« texte natif » — donc probant —, contrairement à la règle « OCR = jamais probant ».
"""
import io
import shutil

import pytest

from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.query import numeric_evidence as NE

pytest.importorskip("pytesseract")


def _png(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((20, 60), text, fill="black", font=ImageFont.load_default(size=56))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="binaire tesseract absent (runner CI)")
def test_la_page_d_une_image_est_declaree_ocr():
    res = extract_document(_png("MONTANT 125000"), filename="scan.png")
    assert res.pages and res.metadata.get("ocr_used") is True
    assert res.metadata.get("ocr_pages") == [1]


def test_un_fragment_ocr_d_image_n_est_jamais_probant():
    status, _ = NE.classify({"meta": {"source_is_ocr": True, "page_number": 1}}, {})
    assert status in NE.UNVERIFIED

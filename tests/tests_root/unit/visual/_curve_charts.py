"""Graphiques en courbes SYNTHÉTIQUES pour la numérisation shadow (lot 14).

Aucune image du corpus : chaque graphique est dessiné ici, et la réponse
``structured-extract/1`` l'accompagne avec un OCR (textes, scores, boîtes) dont les boîtes
couvrent exactement les textes dessinés — la forme réelle de ``raw_output.ocr``.

Géométrie : graduations GAUCHES 0,0 … 1,0 ; pixel_y = 340 - 300 × valeur ; tracé de
x = 70 à x = 620 ; étiquettes X sous le tracé ; légende (trait + libellé) sous les étiquettes.
"""
from __future__ import annotations

import io
import math
from typing import List, Optional

from PIL import Image, ImageDraw

W, H = 640, 420
X0, X1 = 70, 620
TICKS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
BLUE = (31, 119, 180)
ORANGE = (237, 125, 49)
GREY = (90, 90, 90)


def y_of(value: float) -> float:
    return 340.0 - 300.0 * value


def bump(peak: float, at: float = 0.55, base: float = 0.2, width: float = 0.18):
    """Courbe en cloche : ``base`` aux bords, ``peak`` à la fraction ``at`` du tracé."""
    return lambda f: base + (peak - base) * math.exp(-((f - at) / width) ** 2)


class Chart:
    def __init__(self, ticks=TICKS, tick_side: str = "left"):
        self.img = Image.new("RGB", (W, H), "white")
        self.draw = ImageDraw.Draw(self.img)
        self.ocr: List[tuple] = []
        self.series: List[dict] = []
        for v in ticks:
            label = f"{v:.1f}".replace(".", ",")
            y = y_of(v) if tick_side == "left" else y_of(v)
            x = 22 if tick_side == "left" else X1 + 6
            self.text(label, x, y - 5, GREY)
        for i, year in enumerate(("2021", "2022", "2023", "2024")):
            self.text(year, X0 + 20 + i * 150, 352, GREY)

    def text(self, label: str, x: float, y: float, color, *, ocr: bool = True) -> None:
        self.draw.text((x, y), label, fill=color)
        if ocr:
            x0, y0, x1, y1 = self.draw.textbbox((x, y), label)
            self.ocr.append((label, 0.99, [float(x0), float(y0), float(x1), float(y1)]))

    def curve(self, name: str, color, fn, *, legend_x: Optional[float] = None, width: int = 2,
              gap: Optional[tuple] = None) -> None:
        pts = [(x, y_of(fn((x - X0) / (X1 - X0)))) for x in range(X0, X1 + 1, 2)]
        for a, b in zip(pts, pts[1:]):
            if gap and (gap[0] <= a[0] <= gap[1] or gap[0] <= b[0] <= gap[1]):
                continue
            self.draw.line([a, b], fill=color, width=width)
        if legend_x is not None:
            self.legend(name, color, legend_x, 385)
        self.series.append({"name": name, "unit_hint": None, "values": [0.2, 0.5, 0.3],
                            "raw_values": ["0.2", "0.5", "0.3"]})

    def legend(self, name: str, color, x: float, y: float) -> None:
        self.draw.line([(x - 34, y + 5), (x - 6, y + 5)], fill=color, width=3)
        self.text(name, x, y, GREY)

    def png(self) -> bytes:
        out = io.BytesIO()
        self.img.save(out, format="PNG")
        return out.getvalue()

    def response(self, region=(0, 0, W, H)) -> dict:
        return {
            "status": "ok", "contract": "structured-extract/1", "task": "chart", "extraction_status": "success",
            "items": [{"region_bbox": list(region), "corner_label": "", "categories": ["2021", "2022", "2023"],
                       "series": self.series, "native_text": ""}],
            "title_candidates": [], "warnings": [],
            "raw_output": {"ocr": {"rec_texts": [o[0] for o in self.ocr], "rec_scores": [o[1] for o in self.ocr],
                                   "rec_boxes": [o[2] for o in self.ocr]}},
        }


class Asset:
    def __init__(self, content: bytes, content_hash: str = "c" * 64, source_page: int = 3):
        self.content = content
        self.content_hash = content_hash
        self.source_page = source_page
        self.mime_type = "image/png"
        self.ordinal = 0
        self.source_locator = None


def simple_chart(peak: float = 0.83) -> Chart:
    """Une série bleue légendée, maximum ``peak`` au milieu, minimum 0,2 aux bords."""
    c = Chart()
    c.curve("Série A", BLUE, bump(peak), legend_x=200)
    return c

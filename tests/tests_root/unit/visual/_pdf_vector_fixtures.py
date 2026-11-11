"""PDF SYNTHÉTIQUES déterministes pour les régions vectorielles (aucun fichier binaire commité).

Deux variantes par famille : ``dev`` (mise au point des seuils) et ``val`` (positions, tailles, valeurs et
mises en page DIFFÉRENTES, réservée à la décision). Les graphiques sont tracés en primitives PDF (aucune image).
"""
from __future__ import annotations

import io

import fitz

A4 = (595, 842)
V = {"dev": dict(ox=100, oy=500, vals=(120, 180, 90, 210), dx=0, dy=0, w=50, title="Consommation par site"),
     "val": dict(ox=150, oy=620, vals=(60, 140, 200, 75, 160), dx=40, dy=90, w=38, title="Effectifs par service")}


def _doc():
    return fitz.open()


def _bar_chart(p, v, *, x0=None, y0=None):
    ox, oy = (x0 if x0 is not None else v["ox"]), (y0 if y0 is not None else v["oy"])
    p.insert_text((ox, oy - 260), v["title"], fontsize=11)
    p.draw_line((ox, oy), (ox + 420, oy))
    p.draw_line((ox, oy), (ox, oy - 240))
    for t in (0, 100, 200):
        p.draw_line((ox - 5, oy - t), (ox, oy - t))
        p.insert_text((ox - 28, oy - t + 3), str(t), fontsize=8)
    for i, val in enumerate(v["vals"]):
        x = ox + 25 + i * (v["w"] + 35)
        p.draw_rect(fitz.Rect(x, oy - val, x + v["w"], oy), color=(0, 0, 0), fill=(0.2, 0.4, 0.8))
        p.insert_text((x + 6, oy + 14), f"S{i + 1}", fontsize=8)


def bar_chart(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); v = V[variant]
    p.insert_text((72, 72), "Rapport annuel. Le graphique ci-dessous présente les données.", fontsize=10)
    _bar_chart(p, v)
    return d.tobytes()


def line_chart(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); v = V[variant]
    ox, oy = 90 + v["dx"], 460 + v["dy"]
    p.insert_text((ox, oy - 250), "Évolution mensuelle de la température", fontsize=11)
    p.draw_line((ox, oy), (ox + 400, oy)); p.draw_line((ox, oy), (ox, oy - 230))
    pts = [(ox + 20 + i * 32, oy - 40 - 120 * abs(((i * 7 + (3 if variant == "val" else 0)) % 12) - 6) / 6) for i in range(12)]
    p.draw_polyline(pts, color=(0.8, 0.1, 0.1), width=1.5)
    for x, y in pts:
        p.draw_circle((x, y), 2.5, color=(0.8, 0.1, 0.1), fill=(0.8, 0.1, 0.1))
    p.draw_line((ox + 300, oy - 220), (ox + 330, oy - 220), color=(0.8, 0.1, 0.1))
    p.insert_text((ox + 335, oy - 217), "Site A", fontsize=8)
    return d.tobytes()


def block_diagram(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); v = V[variant]
    names = ["Capteur", "Passerelle", "Serveur", "Base"] if variant == "dev" else ["Client", "Proxy", "API", "File", "Worker"]
    x, y = 70 + v["dx"], 300 + v["dy"]
    for i, n in enumerate(names):
        bx = x + i * (480 // len(names))
        p.draw_rect(fitz.Rect(bx, y, bx + 80, y + 45), color=(0, 0, 0), width=1.2)
        p.insert_text((bx + 8, y + 27), n, fontsize=9)
        if i:
            px = bx - (480 // len(names)) + 80
            p.draw_line((px, y + 22), (bx, y + 22))
            p.draw_polyline([(bx - 6, y + 18), (bx, y + 22), (bx - 6, y + 26)], color=(0, 0, 0), closePath=True, fill=(0, 0, 0))
    p.draw_line((x + 40, y + 45), (x + 40, y + 110)); p.draw_rect(fitz.Rect(x, y + 110, x + 160, y + 150), color=(0, 0, 0))
    p.insert_text((x + 8, y + 135), "Supervision", fontsize=9)
    return d.tobytes()


def technical_drawing(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); v = V[variant]
    cx, cy = 280 + v["dx"], 400 + v["dy"]
    p.draw_circle((cx, cy), 90, color=(0, 0, 0), width=1.5)
    p.draw_circle((cx, cy), 30 + (10 if variant == "val" else 0), color=(0, 0, 0))
    for k in range(4):
        dx, dy = [(60, 0), (0, 60), (-60, 0), (0, -60)][k]
        p.draw_circle((cx + dx, cy + dy), 8, color=(0, 0, 0))
    p.draw_line((cx - 90, cy + 120), (cx + 90, cy + 120)); p.draw_line((cx - 90, cy + 112), (cx - 90, cy + 128))
    p.draw_line((cx + 90, cy + 112), (cx + 90, cy + 128))
    p.insert_text((cx - 20, cy + 135), "Ø 180", fontsize=8)
    p.draw_line((cx + 60, cy - 60), (cx + 130, cy - 130)); p.insert_text((cx + 132, cy - 132), "4 x Ø16", fontsize=8)
    return d.tobytes()


def _png(w, h, color):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), color)
    dr = ImageDraw.Draw(img)
    for i in range(0, w, 17):
        dr.line([(i, 0), (w - i, h)], fill=(255 - color[0], 120, 30), width=3)
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def raster_only(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    p.insert_text((72, 72), "Photo du site.", fontsize=10)
    p.insert_image(fitz.Rect(80, 120, 480, 420), stream=_png(400, 300, (40, 90, 160) if variant == "dev" else (90, 40, 30)))
    return d.tobytes()


def mixed(variant="dev") -> bytes:
    """Page 1 : photo raster ; page 2 : histogramme vectoriel."""
    d = _doc(); v = V[variant]
    p1 = d.new_page(width=A4[0], height=A4[1])
    p1.insert_image(fitz.Rect(80, 120, 480, 420), stream=_png(400, 300, (40, 90, 160)))
    p2 = d.new_page(width=A4[0], height=A4[1]); _bar_chart(p2, v)
    return d.tobytes()


def composite(variant="dev") -> bytes:
    """Une image raster ENTOURÉE d'axes et de graduations vectoriels : une seule figure."""
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    p.insert_image(fitz.Rect(120, 200, 420, 420), stream=_png(300, 220, (30, 140, 60)))
    p.draw_line((110, 430), (430, 430)); p.draw_line((110, 430), (110, 190))
    for i in range(6):
        p.draw_line((110 + i * 60, 430), (110 + i * 60, 436)); p.insert_text((106 + i * 60, 448), str(i * 10), fontsize=7)
        p.draw_line((104, 430 - i * 40), (110, 430 - i * 40))
    return d.tobytes()


def native_table(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    rows, cols = (6, 4) if variant == "dev" else (8, 3)
    x0, y0, cw, rh = 80, 150, 110, 24
    for r in range(rows + 1):
        p.draw_line((x0, y0 + r * rh), (x0 + cols * cw, y0 + r * rh))
    for c in range(cols + 1):
        p.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + rows * rh))
    for r in range(rows):
        for c in range(cols):
            p.insert_text((x0 + c * cw + 6, y0 + r * rh + 16), f"Cellule {r}.{c}", fontsize=8)
    return d.tobytes()


def decorative(variant="dev") -> bytes:
    d = _doc()
    for _ in range(4 if variant == "dev" else 5):
        p = d.new_page(width=A4[0], height=A4[1])
        p.draw_line((50, 60), (545, 60)); p.draw_line((50, 790), (545, 790))
        p.draw_rect(fitz.Rect(30, 30, 565, 812), color=(0.5, 0.5, 0.5))
        p.draw_rect(fitz.Rect(500, 20, 545, 50), color=(0, 0, 0), fill=(0.1, 0.3, 0.6))
        p.draw_line((505, 35), (540, 35), color=(1, 1, 1)); p.draw_line((522, 22), (522, 48), color=(1, 1, 1))
        p.draw_circle((522, 35), 8, color=(1, 1, 1)); p.draw_circle((522, 35), 4, color=(1, 1, 1))
        p.insert_text((72, 120), "Texte courant de la page. " * 6, fontsize=9)
        p.draw_line((72, 140), (300, 140))
    return d.tobytes()


def text_only(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    for i in range(20):
        p.insert_text((72, 80 + i * 16), f"Ligne {i} de texte natif sans figure.", fontsize=10)
    d.new_page(width=A4[0], height=A4[1])
    return d.tobytes()


def rotated(rotation: int, variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); _bar_chart(p, V[variant]); p.set_rotation(rotation)
    return d.tobytes()


def cropped(variant="dev") -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1]); _bar_chart(p, V[variant])
    p.set_cropbox(fitz.Rect(40, 60, 560, 800))
    return d.tobytes()


def complex_page(n=6000) -> bytes:
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    for i in range(n):
        x = 50 + (i % 100) * 5; y = 50 + (i // 100) * 6
        p.draw_line((x, y), (x + 3, y + 3))
    return d.tobytes()


def many_charts(pages=10, variant="dev") -> bytes:
    """Même GABARIT de graphique sur chaque page, données différentes (rapport mensuel)."""
    d = _doc()
    for k in range(pages):
        p = d.new_page(width=A4[0], height=A4[1])
        v = dict(V[variant], vals=tuple(20 + (k * 37 + i * 53) % 200 for i in range(len(V[variant]["vals"]))))
        _bar_chart(p, v)
    return d.tobytes()


def repeated_identical_chart(pages=5) -> bytes:
    """Le MÊME dessin à l'identique sur chaque page (gabarit décoratif) : écarté comme décor."""
    d = _doc()
    for _ in range(pages):
        _bar_chart(d.new_page(width=A4[0], height=A4[1]), V["dev"])
    return d.tobytes()


def framed_page(variant="dev") -> bytes:
    """UNE page : cadre de page, filets d'en-tête et de pied, petit logo, texte — aucune figure."""
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    inset = 30 if variant == "dev" else 24
    p.draw_rect(fitz.Rect(inset, inset, A4[0] - inset, A4[1] - inset), color=(0.5, 0.5, 0.5))
    p.draw_line((50, 60), (545, 60)); p.draw_line((50, 790), (545, 790))
    p.draw_rect(fitz.Rect(500, 20, 545, 50), color=(0, 0, 0), fill=(0.1, 0.3, 0.6))
    p.draw_circle((522, 35), 8, color=(1, 1, 1))
    for i in range(12):
        p.insert_text((72, 120 + i * 14), "Texte courant de la page encadrée. " * 3, fontsize=9)
    return d.tobytes()


def chart_on_framed_page(variant="dev") -> bytes:
    """Cadre de page + filets + UN vrai graphique : seul le graphique est rendu, pas la page."""
    d = _doc(); p = d.new_page(width=A4[0], height=A4[1])
    p.draw_rect(fitz.Rect(30, 30, 565, 812), color=(0.5, 0.5, 0.5))
    p.draw_line((50, 60), (545, 60)); p.draw_line((50, 790), (545, 790))
    _bar_chart(p, V[variant])
    return d.tobytes()


FAMILIES = {"bar_chart": bar_chart, "line_chart": line_chart, "block_diagram": block_diagram,
            "technical_drawing": technical_drawing}
NEGATIVES = {"raster_only": raster_only, "native_table": native_table, "decorative": decorative,
             "text_only": text_only, "framed_page": framed_page}

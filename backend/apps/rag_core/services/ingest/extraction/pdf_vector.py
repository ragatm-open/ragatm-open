# backend/apps/rag_core/services/ingest/extraction/pdf_vector.py
"""
Régions VECTORIELLES d'un PDF.

Un graphique ou un schéma tracé directement en primitives PDF (lignes, rectangles,
courbes) n'est pas une image intégrée : ``pdf_visual`` ne le voyait pas. Ce module
repère, dans le document DÉJÀ ouvert par ``pdf_visual``, des régions de tracés
cohérentes ; ``pdf_visual`` les rend ensuite UNE PAR UNE (``get_pixmap(clip=…)``), jamais
la page entière, et les fait entrer dans le MÊME vivier, les MÊMES filtres et le MÊME
budget que les images intégrées.

Déterministe, borné, sans contenu métier :

* primitives = chemins de ``page.get_drawings()`` ; page au-delà de
  ``MAX_PRIMITIVES_PER_PAGE`` : ignorée et comptée (jamais tronquée en silence) ;
* regroupement par proximité (``MERGE_GAP_PT``) via une grille, jamais une paire à paire
  quadratique sur toute la page ;
* rejets EXPLICITES et comptés : séparateurs et filets (région trop mince), cadres vides
  (aucune primitive intérieure), grilles de tableau à texte (règles pleine largeur ou pleine
  hauteur + mots), régions répétées sur plusieurs pages (en-tête, pied de page),
  régions contenues dans une image intégrée, régions couvrant un tableau natif ;
* repère : bbox dans le repère de la page NON tournée, relatif à la CropBox — le même que
  ``get_image_rects`` pour les images intégrées ; le rendu applique ``rotation_matrix``.

Une incertitude conduit à NE PAS produire de région : une omission comptée vaut mieux
qu'un appel vision inutile.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

Bbox = Tuple[float, float, float, float]

#: Pages inspectées au plus par document (au-delà : non inspectées, comptées).
MAX_PAGES_INSPECTED = 200
#: Chemins vectoriels au plus par page (au-delà : page ignorée, comptée).
MAX_PRIMITIVES_PER_PAGE = 4000
#: Chemins vectoriels au plus par document.
MAX_PRIMITIVES_PER_DOCUMENT = 40000
#: Budget de temps de la détection pour un document (secondes).
TIME_BUDGET_S = 20.0
#: Deux primitives à moins de cet écart (points PDF) appartiennent à la même région.
MERGE_GAP_PT = 10.0
#: Petit groupe (axes, graduations, légende : moins de MIN_PRIMITIVES chemins) rattaché au groupe principal
#: le plus proche à moins de cet écart (points PDF).
ATTACH_GAP_PT = 40.0
#: Cellule de la grille d'accélération (points PDF).
GRID_CELL_PT = 48.0
#: Mots à moins de cette distance du tracé (libellés d'axes, légende, titre proche) rejoignent la région rendue.
LABEL_REACH_PT = 40.0
#: Marge ajoutée autour de la région rendue (tracés + libellés).
RENDER_MARGIN_PT = 6.0
#: Une région utile porte au moins ce nombre de chemins.
MIN_PRIMITIVES = 4
#: Plus petit côté et plus petite part de page d'une région utile.
MIN_SIDE_PT = 48.0
MIN_AREA_FRACTION = 0.015
#: Chemin couvrant au moins cette part de la page : fond de page, ignoré.
PAGE_FRAME_FRACTION = 0.85
#: Contour NON rempli couvrant au moins cette part de la page : cadre de page, ignoré (sinon il relie tout).
PAGE_OUTLINE_FRACTION = 0.5
#: Filet d'en-tête / de pied de page : trait d'au moins cette part de la largeur dans la bande haute ou basse.
BAND_FRACTION = 0.12
BAND_RULE_FRACTION = 0.5
#: Grille de tableau : part de règles pleine largeur / pleine hauteur et mots contenus.
TABLE_RULE_SPAN = 0.9
TABLE_RULE_RATIO = 0.5
TABLE_MIN_WORDS = 8
#: Région identique (bbox arrondie, nombre de chemins) sur PLUS de pages : décorative (même règle que les
#: images intégrées, DOCUMENT_PDF_VISUAL_MAX_REPEATS = 3 par défaut).
DECORATIVE_REPEATS = 3
#: Part d'une région couverte par une image intégrée au-delà de laquelle elle n'en est que le cadre ou une
#: surcharge. En deçà (axes, graduations, légende autour d'une image) : figure COMPOSITE, conservée.
INSIDE_RASTER_RATIO = 0.95


@dataclass(frozen=True)
class VectorRegion:
    page: int                 # 1-based
    bbox: Bbox                # repère page non tournée, relatif à la CropBox, marge incluse
    primitives: int
    has_fill: bool
    has_curves: bool
    words: int
    #: Empreinte de la géométrie COMPLÈTE des tracés (décor = dessin strictement identique répété).
    shape: int = 0


@dataclass
class _Prim:
    rect: Bbox
    fill: bool
    curve: bool
    diagonal: bool
    horizontal: bool
    vertical: bool


def new_stats() -> Dict:
    return {"vector_pages_inspected": 0, "vector_primitives_seen": 0, "vector_regions_detected": 0,
            "vector_omitted_thin": 0, "vector_omitted_frame": 0, "vector_omitted_table_like": 0,
            "vector_omitted_small": 0, "vector_omitted_decorative": 0, "vector_omitted_inside_raster": 0,
            "vector_omitted_table_overlap": 0, "vector_omitted_limit": 0, "vector_page_too_complex": 0,
            "vector_pages_not_inspected": 0, "vector_time_budget_exhausted": 0, "vector_errors": 0}


def _overlap(inner: Bbox, outer: Bbox) -> float:
    w = min(inner[2], outer[2]) - max(inner[0], outer[0])
    h = min(inner[3], outer[3]) - max(inner[1], outer[1])
    if w <= 0 or h <= 0:
        return 0.0
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return (w * h) / area if area > 0 else 0.0


def _prims(paths, page_area: float, stats: Dict, page_w: float = 0.0, page_h: float = 0.0) -> List[_Prim]:
    out = []
    for p in paths:
        r = p.get("rect")
        if r is None:
            continue
        x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            continue
        w, h = x1 - x0, y1 - y0
        if w <= 0.5 and h <= 0.5:
            continue
        if page_area > 0 and (w * h) >= PAGE_FRAME_FRACTION * page_area:
            continue
        items = p.get("items") or ()
        kinds = {it[0] for it in items if it}
        fill = p.get("fill")
        filled = fill is not None and tuple(round(c, 3) for c in fill) not in ((1.0, 1.0, 1.0), (1.0,))
        if not filled and page_area > 0 and (w * h) >= PAGE_OUTLINE_FRACTION * page_area:
            stats["vector_page_frames"] = stats.get("vector_page_frames", 0) + 1
            continue
        if page_h > 0 and h <= 1.5 and page_w > 0 and w >= BAND_RULE_FRACTION * page_w and (
                y1 <= BAND_FRACTION * page_h or y0 >= (1 - BAND_FRACTION) * page_h):
            stats["vector_band_rules"] = stats.get("vector_band_rules", 0) + 1
            continue
        diagonal = any(it[0] == "l" and abs(it[1].x - it[2].x) > 0.5 and abs(it[1].y - it[2].y) > 0.5
                       for it in items if it)
        out.append(_Prim(rect=(x0, y0, x1, y1), fill=filled, curve="c" in kinds or "qu" in kinds,
                         diagonal=diagonal, horizontal=h <= 1.5 and w > 1.5, vertical=w <= 1.5 and h > 1.5))
    stats["vector_primitives_seen"] += len(out)
    return out


def _cluster(prims: Sequence[_Prim]) -> List[List[int]]:
    """Composantes connexes par proximité (écart ``MERGE_GAP_PT``), grille d'accélération."""
    parent = list(range(len(prims)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: Dict[Tuple[int, int], List[int]] = {}
    g = MERGE_GAP_PT
    for i, p in enumerate(prims):
        x0, y0, x1, y1 = p.rect
        cells = [(cx, cy) for cx in range(int((x0 - g) // GRID_CELL_PT), int((x1 + g) // GRID_CELL_PT) + 1)
                 for cy in range(int((y0 - g) // GRID_CELL_PT), int((y1 + g) // GRID_CELL_PT) + 1)]
        seen = set()
        for cell in cells:
            for j in grid.get(cell, ()):
                if j in seen:
                    continue
                seen.add(j)
                q = prims[j].rect
                if x0 - g <= q[2] and q[0] <= x1 + g and y0 - g <= q[3] and q[1] <= y1 + g:
                    a, b = find(i), find(j)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
            grid.setdefault(cell, []).append(i)
    groups: Dict[int, List[int]] = {}
    for i in range(len(prims)):
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def _gap(a: Bbox, b: Bbox) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return max(dx, dy)


def _attach_small(groups: List[List[int]], prims: Sequence[_Prim]) -> List[List[int]]:
    """Rattache chaque petit groupe au groupe principal le plus proche (écart ≤ ATTACH_GAP_PT), sinon le garde."""
    boxes = [_union([prims[i].rect for i in g]) for g in groups]
    main = [k for k, g in enumerate(groups) if len(g) >= MIN_PRIMITIVES]
    merged = {k: list(groups[k]) for k in main}
    for k, g in enumerate(groups):
        if k in merged:
            continue
        best = min(main, key=lambda m: (_gap(boxes[k], boxes[m]), m), default=None)
        if best is not None and _gap(boxes[k], boxes[best]) <= ATTACH_GAP_PT:
            merged[best].extend(g)
    return [sorted(merged[k]) for k in sorted(merged)]


def _union(rects: Sequence[Bbox]) -> Bbox:
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def _words_inside(words, bbox: Bbox) -> int:
    n = 0
    for w in words:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]:
            n += 1
    return n


def _table_like(members: Sequence[_Prim], bbox: Bbox, words: int) -> bool:
    """Grille de tableau : uniquement des traits axiaux, majorité de règles pleine largeur/hauteur, du texte."""
    if any(m.curve or m.diagonal for m in members) or words < TABLE_MIN_WORDS:
        return False
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    rules = [m for m in members if m.horizontal or m.vertical
             or (m.rect[3] - m.rect[1]) <= 1.5 or (m.rect[2] - m.rect[0]) <= 1.5]
    long_rules = [m for m in members
                  if ((m.rect[2] - m.rect[0]) >= TABLE_RULE_SPAN * bw and (m.rect[3] - m.rect[1]) < 0.5 * bh)
                  or ((m.rect[3] - m.rect[1]) >= TABLE_RULE_SPAN * bh and (m.rect[2] - m.rect[0]) < 0.5 * bw)]
    filled_shapes = [m for m in members if m.fill and m not in long_rules
                     and (m.rect[2] - m.rect[0]) > 1.5 and (m.rect[3] - m.rect[1]) > 1.5]
    return bool(rules) and len(long_rules) >= TABLE_RULE_RATIO * len(members) and not filled_shapes


def _interior(members: Sequence[_Prim], bbox: Bbox) -> int:
    """Primitives de CONTENU : ni trait fin couché le long d'un bord parallèle, ni contour égal à la région.

    Un cadre vide (encadré, bordure de page) n'en a aucune ; des barres posées sur un axe, des
    graduations, une courbe en ont."""
    e = 2.0
    n = 0
    for m in members:
        x0, y0, x1, y1 = m.rect
        on_h_edge = m.horizontal and (abs(y0 - bbox[1]) <= e or abs(y1 - bbox[3]) <= e)
        on_v_edge = m.vertical and (abs(x0 - bbox[0]) <= e or abs(x1 - bbox[2]) <= e)
        outline = (not m.fill and abs(x0 - bbox[0]) <= e and abs(y0 - bbox[1]) <= e
                   and abs(x1 - bbox[2]) <= e and abs(y1 - bbox[3]) <= e)
        if not (on_h_edge or on_v_edge or outline):
            n += 1
    return n


def detect_vector_regions(doc, *, stats: Dict, max_pages: int = 0,
                          suppressed_regions: Optional[Dict[int, List[Bbox]]] = None,
                          raster_rects: Optional[Dict[int, List[Bbox]]] = None,
                          table_overlap_ratio: float = 0.6, max_candidates: int = 24,
                          clock=None) -> List[VectorRegion]:
    """Régions vectorielles candidates, dans l'ordre documentaire (page, haut, gauche). Sans exception."""
    clock = clock or time.monotonic
    deadline = clock() + TIME_BUDGET_S
    pages = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
    if pages > MAX_PAGES_INSPECTED:
        stats["vector_pages_not_inspected"] += pages - MAX_PAGES_INSPECTED
        pages = MAX_PAGES_INSPECTED
    found: List[VectorRegion] = []
    budget_prims = MAX_PRIMITIVES_PER_DOCUMENT
    for pno in range(pages):
        if clock() > deadline:
            stats["vector_time_budget_exhausted"] += 1
            stats["vector_pages_not_inspected"] += pages - pno
            break
        page = doc[pno]
        try:
            r = page.rect
            page_area = float(r.width) * float(r.height)
            paths = page.get_drawings()
        except Exception:  # noqa: BLE001 - un PDF hostile ne casse jamais l'ingestion
            stats["vector_errors"] += 1
            continue
        stats["vector_pages_inspected"] += 1
        if not paths:
            continue
        if len(paths) > MAX_PRIMITIVES_PER_PAGE or len(paths) > budget_prims:
            stats["vector_page_too_complex"] += 1
            continue
        budget_prims -= len(paths)
        rot90 = page.rotation % 180 != 0
        prims = _prims(paths, page_area, stats, float(r.height) if rot90 else float(r.width),
                       float(r.width) if rot90 else float(r.height))
        tables = (suppressed_regions or {}).get(pno + 1, ())
        if tables:
            prims = [p for p in prims if not any(_overlap(p.rect, t) >= 0.9 for t in tables)]
        if len(prims) < MIN_PRIMITIVES:
            continue
        words = None
        for group in _attach_small(_cluster(prims), prims):
            members = [prims[i] for i in group]
            core = _union([m.rect for m in members])
            bw, bh = core[2] - core[0], core[3] - core[1]
            if len(members) < MIN_PRIMITIVES:
                continue
            if min(bw, bh) < 6.0:
                stats["vector_omitted_thin"] += 1
                continue
            if min(bw, bh) < MIN_SIDE_PT or (page_area > 0 and (bw * bh) / page_area < MIN_AREA_FRACTION):
                stats["vector_omitted_small"] += 1
                continue
            if _interior(members, core) == 0:
                stats["vector_omitted_frame"] += 1
                continue
            if words is None:
                try:
                    words = page.get_text("words")
                except Exception:  # noqa: BLE001
                    words = []
            n_words = _words_inside(words, core)
            if _table_like(members, core, n_words):
                stats["vector_omitted_table_like"] += 1
                continue
            if any(_overlap(core, t) >= table_overlap_ratio for t in tables):
                stats["vector_omitted_table_overlap"] += 1
                continue
            if any(_overlap(core, rr) >= INSIDE_RASTER_RATIO for rr in (raster_rects or {}).get(pno + 1, ())):
                stats["vector_omitted_inside_raster"] += 1
                continue
            # Libellés voisins (repère des mots = repère des tracés : page non tournée).
            reach = (core[0] - LABEL_REACH_PT, core[1] - LABEL_REACH_PT, core[2] + LABEL_REACH_PT, core[3] + LABEL_REACH_PT)
            near = [(w[0], w[1], w[2], w[3]) for w in words
                    if w[0] < reach[2] and w[2] > reach[0] and w[1] < reach[3] and w[3] > reach[1]]
            full = _union([core] + near)
            m = RENDER_MARGIN_PT
            width_u = float(r.width) if page.rotation % 180 == 0 else float(r.height)
            height_u = float(r.height) if page.rotation % 180 == 0 else float(r.width)
            bbox = (round(max(full[0] - m, 0.0), 4), round(max(full[1] - m, 0.0), 4),
                    round(min(full[2] + m, width_u), 4), round(min(full[3] + m, height_u), 4))
            shape = hash(tuple(sorted(tuple(round(v, 1) for v in mm.rect) for mm in members)))
            found.append(VectorRegion(page=pno + 1, bbox=bbox, primitives=len(members),
                                      has_fill=any(p.fill for p in members),
                                      has_curves=any(p.curve or p.diagonal for p in members), words=n_words,
                                      shape=shape))
    # Décoratif : MÊME dessin (géométrie complète des tracés) sur trop de pages — un même gabarit de graphique
    # aux données différentes n'est PAS décoratif.
    signature: Dict[int, int] = {}
    for reg in found:
        signature[reg.shape] = signature.get(reg.shape, 0) + 1
    kept = []
    for reg in found:
        if signature[reg.shape] > DECORATIVE_REPEATS:
            stats["vector_omitted_decorative"] += 1
            continue
        kept.append(reg)
    kept.sort(key=lambda reg: (reg.page, reg.bbox[1], reg.bbox[0]))
    if len(kept) > max_candidates:
        stats["vector_omitted_limit"] += len(kept) - max_candidates
        kept = kept[:max(max_candidates, 0)]
    stats["vector_regions_detected"] += len(kept)
    return kept

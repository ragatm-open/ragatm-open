"""Prototype CORRIGÉ de numérisation de courbe (copie révisée du prototype isolé).

Correctifs de sécurité seulement : A1 zone de tracé stricte (domaine des graduations), A2 masquage
des boîtes OCR dilatées (franges ClearType), A3 aucune extrapolation, A4 continuité (composantes
connexes larges, extremum jamais adjacent à du texte masqué), A5 statut fiable = toutes barrières.

prototype ISOLÉ de numérisation de courbe (évaluation seule).

Entrées : l'image du graphique (le visuel déjà découvert) et la réponse `structured-extract/1`
déjà obtenue à l'ingestion (OCR PP-OCR : textes, scores, boîtes). Aucune nouvelle inférence.

Étapes : graduations Y (OCR) -> étalonnage linéaire pixel/valeur (résidu contrôlé) -> légende
(OCR) -> couleur de la série demandée -> masque couleur dans la zone de tracé -> trace par
colonne -> extremum au CENTRE du trait -> intervalle (résidu d'étalonnage + demi-épaisseur du
trait + 1 pixel). Toute condition non remplie produit une ABSTENTION motivée, jamais une valeur.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image
from . import curve_ndimage as ndimage  # lot 14 : seule ligne modifiée (scipy absent des images)

#: Paramètres FIGÉS après réglage sur le jeu DEV (voir README).
OCR_MIN_SCORE = 0.8
MIN_Y_TICKS = 3
MAX_CALIB_RESIDUAL_FRAC = 0.01      # résidu max = 1 % de l'étendue lue
COLOR_TOL = 60.0                    # distance RVB à la couleur de légende
SAME_COLOR_TOL = 45.0               # deux légendes plus proches que ceci = indiscernables
MIN_COVERAGE = 0.5                  # part des colonnes [x_min, x_max] de la trace portant la série
MIN_TRACE_WIDTH_FRAC = 0.3          # largeur de trace minimale / largeur de tracé
TEXT_MASK_MARGIN_FRAC = 0.35        # A2 : dilatation = 35 % de la hauteur de la boîte OCR (min 2 px)
CURVE_MIN_COMPONENT_WIDTH_FRAC = 0.05  # A4 : composante connexe retenue si largeur >= 5 % du tracé
LEGEND_SWATCH_FRAC = 4.0            # A2 : bande d'échantillon de légende = 4 × hauteur du libellé
GAP_CLEARANCE_PX = 6                # A4 : aucune colonne manquante à ±6 px de l'extremum
TEXT_EXTREMUM_CLEARANCE_PX = 4      # A4 : extremum à moins de 4 px d'un texte masqué = abstention
MIN_PLOT_W, MIN_PLOT_H = 40, 20     # A1 : zone de tracé minimale (px)
MAX_SHARED_FRAC = 0.7               # colonnes où une autre série couvre le même pixel
LEGEND_MATCH_MIN = 0.6
SINGLE_SERIES_MIN_SAT = 60           # écart max-min des canaux : pixel « coloré »
SINGLE_SERIES_DOMINANCE = 0.85      # part des pixels colorés dans la teinte dominante

_NUM_RE = re.compile(r"^[−\-+]?\d[\d\s]*(?:[.,]\d+)?(?:[eE][−\-+]?\d+)?%?$")


def parse_tick(text: str) -> Optional[float]:
    t = (text or "").strip().replace(" ", "").replace(" ", "").replace("−", "-")
    if not _NUM_RE.match(t):
        return None
    pct = t.endswith("%")
    t = t.rstrip("%").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v / 100.0 if pct else v


@dataclass
class Result:
    status: str                       # MEASURED_FROM_PIXELS / UNRELIABLE / NOT_DETERMINABLE (13B : plus d'ESTIMATED)
    reason: str = ""
    value: Optional[float] = None
    interval: Optional[tuple] = None
    x_pixel: Optional[int] = None
    y_pixel: Optional[float] = None
    diagnostics: dict = field(default_factory=dict)


def _ocr(response: dict) -> list:
    o = (response.get("raw_output") or {}).get("ocr") or {}
    out = []
    for t, s, b in zip(o.get("rec_texts") or [], o.get("rec_scores") or [], o.get("rec_boxes") or []):
        if isinstance(t, str) and t.strip() and isinstance(b, (list, tuple)) and len(b) == 4:
            out.append((t.strip(), float(s or 0.0), [float(v) for v in b]))
    return out


def calibrate_y(lines: list, region: list) -> dict:
    """Graduations numériques de l'axe GAUCHE -> droite pixel_y = a * valeur + b."""
    left, top, right, bottom = region
    width = max(right - left, 1.0)
    cands = [(parse_tick(t), (b[1] + b[3]) / 2, b) for t, s, b in lines
             if s >= OCR_MIN_SCORE and parse_tick(t) is not None and b[2] <= left + 0.25 * width]
    if len(cands) < MIN_Y_TICKS:
        return {"ok": False, "reason": f"graduations Y lisibles insuffisantes ({len(cands)})"}
    # Colonne d'étiquettes : bord droit dominant (tolérance 12 px)
    rights = np.array([c[2][2] for c in cands])
    med = np.median(rights)
    cands = [c for c in cands if abs(c[2][2] - med) <= 12]
    # Une même valeur lue deux fois (ex. axe X) : on garde la plus à gauche
    # C2 (lots 15/17) : appliqué réellement — une valeur, une boîte, la plus à gauche
    leftmost = {}
    for c in cands:
        if c[0] not in leftmost or c[2][0] < leftmost[c[0]][2][0]:
            leftmost[c[0]] = c
    cands = [c for c in cands if leftmost[c[0]] is c]
    vals = np.array([c[0] for c in cands]); ys = np.array([c[1] for c in cands])
    if len(cands) < MIN_Y_TICKS or len(set(vals.tolist())) < MIN_Y_TICKS:
        return {"ok": False, "reason": "colonne de graduations Y incohérente"}
    a, b = np.polyfit(vals, ys, 1)
    if a >= 0:
        return {"ok": False, "reason": "axe Y non croissant vers le haut"}
    pred = a * vals + b
    resid_px = float(np.sqrt(np.mean((ys - pred) ** 2)))
    span = float(vals.max() - vals.min())
    resid_val = resid_px / abs(a)
    # Contrôle logarithmique grossier : pas de valeurs strictement positives en progression géométrique
    sv = np.sort(vals)
    steps = np.diff(sv)
    geometric = bool(np.all(sv > 0) and len(sv) >= 3 and np.std(steps) / max(np.mean(steps), 1e-12) > 0.5
                     and np.std(np.diff(np.log(sv))) < 0.1)
    if geometric:
        return {"ok": False, "reason": "axe probablement logarithmique : non pris en charge"}
    if resid_val > MAX_CALIB_RESIDUAL_FRAC * span:
        return {"ok": False, "reason": f"étalonnage non linéaire (résidu {resid_val:.4g} > 1 % de l'étendue)"}
    return {"ok": True, "a": float(a), "b": float(b), "vmin": float(vals.min()), "vmax": float(vals.max()),
            "span": span, "resid_val": float(resid_val), "n_ticks": int(len(vals)),
            "tick_right": float(med), "y_top": float(ys.min()), "y_bottom": float(ys.max())}


def legend_colors(img: np.ndarray, lines: list, plot_bottom: float) -> dict:
    """Entrée de légende (texte OCR SOUS le tracé) -> couleur échantillonnée à gauche du texte."""
    out = {}
    h, w, _ = img.shape
    for t, s, b in lines:
        if b[1] < plot_bottom or parse_tick(t) is not None:
            continue
        y0, y1 = int(max(b[1], 0)), int(min(b[3], h - 1))
        x0, x1 = int(max(b[0] - 45, 0)), int(max(b[0] - 2, 0))
        patch = img[y0:y1 + 1, x0:x1 + 1].reshape(-1, 3).astype(float)
        if not len(patch):
            continue
        nonwhite = patch[np.linalg.norm(255 - patch, axis=1) > 60]
        if len(nonwhite) < 4:
            continue
        out[t] = np.median(nonwhite, axis=0)
    return out


def match_series(name: str, legends: dict) -> Optional[str]:
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    best, score = None, 0.0
    for k in legends:
        r = difflib.SequenceMatcher(None, norm(name), norm(k)).ratio()
        if r > score:
            best, score = k, r
    return best if score >= LEGEND_MATCH_MIN else None


def digitize_extremum(image_path: str, response: dict, series: str, extremum: str = "max") -> Result:
    img = np.asarray(Image.open(image_path).convert("RGB"))
    h, w, _ = img.shape
    lines = _ocr(response)
    item = (response.get("items") or [{}])[0]
    region = item.get("region_bbox") or [0, 0, w, h]
    cal = calibrate_y(lines, region)
    diag = {"calibration": cal}
    if not cal["ok"]:
        return Result("NOT_DETERMINABLE", cal["reason"], diagnostics=diag)
    # Zone de tracé : à droite des étiquettes Y, bornée en bas par les étiquettes X / la légende
    x_left = int(cal["tick_right"] + 6)
    x_right = int(min(region[2], w - 1))
    below = [b[1] for t, s, b in lines if b[1] > cal["y_bottom"] + 4]
    legend_floor = int(min(below) - 2) if below else int(region[3])
    # A1 : zone de tracé = domaine ÉTALONNÉ (centre de la plus haute à la plus basse graduation),
    # jamais le titre ni les étiquettes ; ±2 px pour le trait posé sur une graduation extrême.
    y_top = int(round(cal["y_top"] - 2))
    y_bottom = int(round(min(cal["y_bottom"] + 2, legend_floor)))
    diag["plot_region"] = [x_left, y_top, x_right, y_bottom]
    if x_right - x_left < MIN_PLOT_W or y_bottom - y_top < MIN_PLOT_H:
        return Result("NOT_DETERMINABLE", "PLOT_REGION = UNKNOWN (zone de tracé dégénérée)", diagnostics=diag)
    legends = legend_colors(img, lines, legend_floor)
    diag["legends"] = {k: [round(float(c), 1) for c in v] for k, v in legends.items()}
    key = match_series(series, legends)
    if key is None and not legends:
        # Graphique à série UNIQUE sans légende : couleur saturée dominante, si elle est unique.
        region_px = img[y_top:y_bottom, int(cal["tick_right"] + 6):int(min(region[2], w - 1))].reshape(-1, 3).astype(int)
        sat = region_px[(region_px.max(axis=1) - region_px.min(axis=1)) > SINGLE_SERIES_MIN_SAT]
        if len(sat):
            bins, counts = np.unique(sat // 32, axis=0, return_counts=True)
            order = np.argsort(-counts)
            if counts[order[0]] / counts.sum() >= SINGLE_SERIES_DOMINANCE:
                dom = bins[order[0]]
                legends = {"<série unique>": np.median(sat[(sat // 32 == dom).all(axis=1)], axis=0)}
                key = "<série unique>"
                diag["single_series_inferred"] = True
    if key is None:
        return Result("NOT_DETERMINABLE", "série introuvable dans la légende", diagnostics=diag)
    color = legends[key]
    others = {k: v for k, v in legends.items() if k != key}
    twins = [k for k, v in others.items() if np.linalg.norm(v - color) < SAME_COLOR_TOL]
    if twins:
        return Result("NOT_DETERMINABLE", f"couleur indiscernable d'une autre série ({twins[0]})", diagnostics=diag)
    plot = img[y_top:y_bottom, x_left:x_right].astype(float)
    dist = np.linalg.norm(plot - color, axis=2)
    mask = dist < COLOR_TOL
    other_masks = [np.linalg.norm(plot - v, axis=2) < COLOR_TOL for v in others.values()]
    # A2 : masque de TOUTES les boîtes OCR, dilatées en proportion de la hauteur des caractères
    text_mask = np.zeros(mask.shape, dtype=bool)
    for t, s_, b in lines:
        m = max(2, int(round(TEXT_MASK_MARGIN_FRAC * (b[3] - b[1]))))
        x0, x1 = int(b[0]) - m - x_left, int(b[2]) + m - x_left
        y0, y1 = int(b[1]) - m - y_top, int(b[3]) + m - y_top
        text_mask[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)] = True
        # Échantillon de trait d'une légende : à GAUCHE du libellé, sur sa bande verticale
        sw = int(round(LEGEND_SWATCH_FRAC * (b[3] - b[1])))
        text_mask[max(y0, 0):max(y1, 0), max(x0 - sw, 0):max(x0, 0)] = True
    masked_before = int(mask.sum())
    mask &= ~text_mask
    other_masks = [om & ~text_mask for om in other_masks]
    # A4 : seules les composantes connexes LARGES sont une courbe ; un îlot coloré est ignoré
    labels, n_comp = ndimage.label(mask, structure=np.ones((3, 3)))
    keep = np.zeros(mask.shape, dtype=bool)
    if n_comp:
        for k, sl in enumerate(ndimage.find_objects(labels), start=1):
            if (sl[1].stop - sl[1].start) >= CURVE_MIN_COMPONENT_WIDTH_FRAC * mask.shape[1]:
                keep |= labels == k
    diag["text_masking"] = {"series_pixels_before": masked_before, "removed_by_text_mask": int(masked_before - mask.sum()),
                            "components": int(n_comp), "series_pixels_kept_in_curve_components": int(keep.sum())}
    mask = keep
    cols = np.where(mask.any(axis=0))[0]
    if not len(cols):
        return Result("NOT_DETERMINABLE", "aucun pixel de la série dans la zone de tracé", diagnostics=diag)
    span_cols = cols.max() - cols.min() + 1
    coverage = len(cols) / span_cols
    width_frac = span_cols / max(plot.shape[1], 1)
    diag.update({"series_key": key, "color": [round(float(c), 1) for c in color], "coverage": round(float(coverage), 3),
                 "width_frac": round(float(width_frac), 3), "plot_box": [x_left, y_top, x_right, y_bottom]})
    if coverage < MIN_COVERAGE or width_frac < MIN_TRACE_WIDTH_FRAC:
        return Result("NOT_DETERMINABLE", "trace trop lacunaire (série masquée ou absente)", diagnostics=diag)
    # Trace : centre du segment de pixels le plus haut (max) ou le plus bas (min) par colonne
    trace, thick = {}, []
    for c in cols:
        ys = np.where(mask[:, c])[0]
        runs = np.split(ys, np.where(np.diff(ys) > 1)[0] + 1)
        run = runs[0] if extremum == "max" else runs[-1]
        trace[int(c)] = float(run.mean())
        thick.append(len(run))
    shared = 0
    for c in cols:
        yc = int(round(trace[int(c)]))
        if any(m[max(yc - 1, 0):yc + 2, c].any() for m in other_masks):
            shared += 1
    diag["shared_frac"] = round(shared / len(cols), 3)
    if diag["shared_frac"] > MAX_SHARED_FRAC:
        return Result("NOT_DETERMINABLE", "série superposée à une autre sur l'essentiel du tracé", diagnostics=diag)
    xs = np.array(sorted(trace)); ys = np.array([trace[x] for x in xs])
    i = int(np.argmin(ys) if extremum == "max" else np.argmax(ys))
    y_ext = float(ys[i]); x_ext = int(xs[i] + x_left); y_abs = y_ext + y_top
    value = (y_abs - cal["b"]) / cal["a"]
    half_thick = float(np.median(thick)) / 2.0
    px_val = 1.0 / abs(cal["a"])
    unc = 2 * cal["resid_val"] + (half_thick + 1.0) * px_val
    diag.update({"line_thickness_px": float(np.median(thick)), "value_per_px": px_val,
                 "uncertainty_components": {"calibration_2sigma": 2 * cal["resid_val"], "half_thickness": half_thick * px_val, "pixel": px_val}})
    # Cohérence d'échelle : extremum au-delà d'une graduation hors de l'échelle lue
    step_val = cal["span"] / max(cal["n_ticks"] - 1, 1)
    if value > cal["vmax"] + step_val or value < cal["vmin"] - step_val:
        return Result("UNRELIABLE", "extremum hors de l'échelle lue", value=value, diagnostics=diag)
    status = "MEASURED_FROM_PIXELS"
    # A3 : aucune extrapolation — hors du domaine étalonné (à un pixel près), jamais fiable
    if value > cal["vmax"] + px_val or value < cal["vmin"] - px_val:
        return Result("UNRELIABLE", "extremum hors du domaine étalonné (extrapolation refusée)", value=value, diagnostics=diag)
    # A4 : un extremum au bord d'une LACUNE de la trace (courbe interrompue) peut être tronqué
    xe, ye = int(xs[i]), int(round(y_ext))
    present = set(int(x) for x in xs)
    first, last = int(xs.min()), int(xs.max())
    near = [x for x in range(xe - GAP_CLEARANCE_PX, xe + GAP_CLEARANCE_PX + 1) if first <= x <= last]
    if any(x not in present for x in near):
        return Result("NOT_DETERMINABLE", "TRACE_COMPLETENESS = INSUFFICIENT (lacune de trace à l'extremum)", value=value, diagnostics=diag)
    # A4 : un extremum jouxtant un texte masqué peut être tronqué -> non déterminable
    c = TEXT_EXTREMUM_CLEARANCE_PX
    if text_mask[max(ye - c, 0):ye + c + 1, max(xe - c, 0):xe + c + 1].any():
        return Result("NOT_DETERMINABLE", "TRACE_COMPLETENESS = INSUFFICIENT (extremum contre un texte masqué)", value=value, diagnostics=diag)
    diag["gates"] = {"VALID_PLOT_REGION": "PASS", "VALID_AXIS_CALIBRATION": "PASS", "VALID_SERIES_IDENTITY": "PASS",
                     "TEXT_INTERFERENCE": "REJECTED", "VALID_TRACE": "PASS", "EXTREMUM_WITHIN_CALIBRATED_DOMAIN": "PASS"}
    if 2 * unc > 0.04 * cal["span"]:
        return Result("UNRELIABLE", "intervalle trop large (> 4 % de l'étendue)", value=value, diagnostics=diag)
    return Result(status, "", value=float(value), interval=(float(value - unc), float(value + unc)),
                  x_pixel=x_ext, y_pixel=y_abs, diagnostics=diag)

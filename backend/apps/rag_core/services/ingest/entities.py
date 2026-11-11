# backend/apps/rag_core/services/ingest/entities.py
from __future__ import annotations

"""
Extraction d'entités métier pour documents techniques (plans de coffrage /
ferraillage, notes de calcul) — P2.

Module PUR (aucun import Django) : prend du TEXTE (natif ou OCR) et renvoie des
entités structurées + un type de page. Tolérant à l'OCR (tirets/espaces perdus).

ATTENTION RAG : ces entités sont des INDICES extraits automatiquement (regex /
heuristiques), à VÉRIFIER sur la page source. On ne calcule aucune quantité, on
n'invente rien : on liste ce qui apparaît textuellement.
"""

import re
from typing import Dict, List

# Bornage : on ne stocke jamais des listes géantes dans Chunk.meta / Document.meta.
_MAX_PER_KIND = 40


# ---------------------------------------------------------------------
# Regex (insensibles à la casse, tolérantes OCR)
# ---------------------------------------------------------------------
# Massif : "MASSIF M01", "M-06", "Massif N°04"
_RE_MASSIF_M = re.compile(r"\bM[\s\-]?0*([1-9]\d?)\b")
_RE_MASSIF_N = re.compile(r"MASSIF\s+N[°ºo]?\s*0*([1-9]\d?)", re.IGNORECASE)

# Vues / coupes / détails
_RE_VUE_PLAN = re.compile(r"VUE\s+EN\s+PLAN", re.IGNORECASE)
_RE_COUPE = re.compile(r"COUPE\s+([A-H])[\s\-]?\1\b", re.IGNORECASE)  # COUPE A-A, COUPE A A
_RE_DETAIL = re.compile(r"D[ÉE]TAIL\s+N[°ºo]?\s*([1-9]\d?)", re.IGNORECASE)

# Nappes
_RE_NAPPE_INF = re.compile(r"NAPPE\s+INF[ÉE]?RIEURE", re.IGNORECASE)
_RE_NAPPE_SUP = re.compile(r"NAPPE\s+SUP[ÉE]?RIEURE", re.IGNORECASE)

# Armatures : "8 HA20", "HA14". 1-2 chiffres => exclut la nuance acier "HA 500".
_RE_REBAR = re.compile(r"\b(\d{1,3}\s*)?HA[\s\-]?(\d{1,2})\b", re.IGNORECASE)

# Espacements : "e=0.14", "e = 0,15"
_RE_SPACING = re.compile(r"\be\s*[=:]\s*0[.,]\d{1,2}\b", re.IGNORECASE)

# Pieux / platine
_RE_PIEU = re.compile(r"[ØøΦØ]\s*(\d{2,4})")
_RE_PLATINE = re.compile(r"\b(\d{3,4})\s*[xX]\s*(\d{3,4})\b")

# Types de page (cartouche / nomenclature)
_RE_NOMENCLATURE = re.compile(
    r"NOMENCLATURE|R[ÉE]CAPITULATIF\s+DES\s+ACIERS|LISTE\s+DES\s+ACIERS",
    re.IGNORECASE,
)
_RE_CARTOUCHE = re.compile(
    r"\b(ECHELLE|ÉCHELLE|FORMAT|INDICE|EMETTEUR|ÉMETTEUR|MA[IÎ]TRE\s+D.OUVRAGE|"
    r"PROPRI[ÉE]T[ÉE]\s+DE|AUTORITE\s+DELEGANTE)\b",
    re.IGNORECASE,
)


def _dedup_limit(seq: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
        if len(out) >= _MAX_PER_KIND:
            break
    return out


def extract_plan_entities(text: str) -> Dict[str, object]:
    """Extrait les entités métier d'un texte de page (natif ou OCR).

    Renvoie un dict sérialisable :
      page_type, massifs[], views[], nappes[], rebar[], spacing[], piles[], plates[]
    Listes vides si rien trouvé. Aucune exception (best-effort)."""
    t = text or ""
    if not t.strip():
        return {
            "page_type": "empty",
            "massifs": [], "views": [], "nappes": [],
            "rebar": [], "spacing": [], "piles": [], "plates": [],
        }

    massifs = [f"M{int(m):02d}" for m in _RE_MASSIF_M.findall(t)]
    massifs += [f"M{int(m):02d}" for m in _RE_MASSIF_N.findall(t)]

    views: List[str] = []
    if _RE_VUE_PLAN.search(t):
        views.append("vue_en_plan")
    for c in _RE_COUPE.findall(t):
        views.append(f"coupe_{c.upper()}-{c.upper()}")
    for d in _RE_DETAIL.findall(t):
        views.append(f"detail_{int(d)}")

    nappes: List[str] = []
    if _RE_NAPPE_INF.search(t):
        nappes.append("inferieure")
    if _RE_NAPPE_SUP.search(t):
        nappes.append("superieure")

    rebar: List[str] = []
    for qty, dia in _RE_REBAR.findall(t):
        qty = (qty or "").strip()
        rebar.append((f"{qty} HA{dia}").strip())

    spacing = [re.sub(r"\s+", "", s) for s in _RE_SPACING.findall(t)]
    piles = [f"Ø{p}" for p in _RE_PIEU.findall(t)]
    plates = [f"{a}x{b}" for a, b in _RE_PLATINE.findall(t)]

    # Type de page (priorité : nomenclature > plan > cartouche > texte).
    if _RE_NOMENCLATURE.search(t):
        page_type = "nomenclature"
    elif views or nappes or rebar or piles:
        page_type = "plan"
    elif _RE_CARTOUCHE.search(t):
        page_type = "cartouche"
    else:
        page_type = "text"

    return {
        "page_type": page_type,
        "massifs": _dedup_limit(massifs),
        "views": _dedup_limit(views),
        "nappes": _dedup_limit(nappes),
        "rebar": _dedup_limit(rebar),
        "spacing": _dedup_limit(spacing),
        "piles": _dedup_limit(piles),
        "plates": _dedup_limit(plates),
    }


def has_business_entities(ent: Dict[str, object]) -> bool:
    """True si des entités métier exploitables ont été détectées."""
    return any(
        ent.get(k) for k in ("massifs", "views", "nappes", "rebar", "piles", "plates")
    )


def aggregate_page_entities(per_page: List[Dict[str, object]]) -> Dict[str, object]:
    """Agrège des entités page-par-page en métadonnées document-level."""
    agg = {k: [] for k in ("massifs", "views", "nappes", "rebar", "spacing", "piles", "plates")}
    pages_by_type: Dict[str, int] = {}
    for ent in per_page:
        pt = str(ent.get("page_type", "text"))
        pages_by_type[pt] = pages_by_type.get(pt, 0) + 1
        for k in agg:
            agg[k].extend(ent.get(k) or [])  # type: ignore[arg-type]
    return {
        "detected_massifs": _dedup_limit(sorted(set(agg["massifs"]))),
        "detected_views": _dedup_limit(sorted(set(agg["views"]))),
        "detected_nappes": _dedup_limit(sorted(set(agg["nappes"]))),
        "detected_rebar_mentions": _dedup_limit(agg["rebar"]),
        "detected_spacing_mentions": _dedup_limit(sorted(set(agg["spacing"]))),
        "detected_piles": _dedup_limit(sorted(set(agg["piles"]))),
        "detected_plates": _dedup_limit(sorted(set(agg["plates"]))),
        "pages_by_type": pages_by_type,
    }

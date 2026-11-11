# backend/apps/rag_core/services/ingest/extraction/table_extractor.py
from __future__ import annotations

"""
Extraction tabulaire STRUCTURÉE (P3-a1) — tableaux à couche texte native.

Module PUR (aucun accès DB, aucun import Django). NON branché au pipeline : il
fournit des fonctions réutilisables que P3-a2 viendra connecter au chunking.

Cible prioritaire P3-a1 : `pile_head_forces` (efforts en têtes de pieux), au
format réel des notes de calcul — la 1re colonne combine souvent Nœud et Cas
(`153/ELU+`). Les diamètres d'armatures ont seulement un helper (`normalize_
rebar_diameter`) ; la nomenclature complète est repoussée à P3-a3/a4.

Garanties anti-hallucination :
- aucune valeur calculée ni inférée : une cellule vide/illisible -> ``None`` ;
- on n'extrait QUE les tableaux dont le type est reconnu ; sinon on les ignore
  (pas de faux tableau) ;
- ``pdfplumber`` est import-gardé : absent -> ``[]`` (jamais de crash) ;
- chaque ligne conserve ``source_text`` ; ``requires_visual_check`` par défaut
  dès que la confiance n'est pas ``high``.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, List, Literal, Optional

logger = logging.getLogger(__name__)

Confidence = Literal["low", "medium", "high"]

_FORCE_KEYS = ("fx", "fy", "fz", "mx", "my", "mz")


@dataclass
class ExtractedTable:
    table_type: str
    title: Optional[str]
    page_number: int
    units: dict
    columns: List[str]
    rows: List[dict]
    confidence: Confidence
    requires_visual_check: bool
    source: str = "pdfplumber"

    def as_dict(self) -> dict:
        return table_to_meta(self)


# ---------------------------------------------------------------------
# Helpers purs : nombres, diamètres, en-têtes, unités
# ---------------------------------------------------------------------
def parse_number_fr(value: Any) -> Optional[float]:
    """Parse un nombre au format FR/EN. Renvoie None si non numérique.

    Gère : espaces (y compris insécables) comme séparateurs de milliers,
    virgule OU point décimal, signe +/-. Ne lève jamais. JAMAIS d'inférence."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Retire tous les espaces (normaux ET insécables) = séparateurs de milliers.
    s = re.sub(r"\s+", "", s)
    if "," in s and "." in s:
        # ',' = milliers, '.' = décimal (peu courant en FR mais sûr)
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_rebar_diameter(value: Any) -> Optional[str]:
    """`HA 14`/`ha12` -> `HA14`/`HA12` ; `HA 500` (nuance acier) -> None.

    Règle : 1 à 2 chiffres uniquement (diamètre de barre), pas une nuance."""
    if not value:
        return None
    s = str(value).strip().upper().replace(" ", "")
    m = re.fullmatch(r"HA0*(\d{1,2})", s)
    return f"HA{int(m.group(1))}" if m else None


def _strip_accents(s: str) -> str:
    s = (s or "").replace("œ", "oe").replace("Œ", "OE")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _canon_header(h: Any) -> str:
    """Canonicalise un en-tête : retire les unités, accents, casse."""
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", str(h or ""))  # retire [T]/(Tm)
    s = _strip_accents(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    if s in ("noeud/cas", "noeud / cas", "node/case", "nud/cas"):
        return "node_case"
    if s in ("noeud", "node", "nud"):
        return "node"
    if s in ("cas", "combinaison", "case"):
        return "case"
    if s in _FORCE_KEYS:
        return s
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "col"


def normalize_headers(headers: List[Any]) -> List[str]:
    """Renvoie les clés canoniques (même longueur que `headers`)."""
    return [_canon_header(h) for h in (headers or [])]


def _unit_in(h: Any) -> Optional[str]:
    m = re.search(r"\[([^\]]+)\]|\(([^)]+)\)", str(h or ""))
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip()


def _norm_unit(u: Optional[str]) -> Optional[str]:
    low = (u or "").strip().lower().replace(".", "").replace("·", "")
    return {"t": "T", "tonne": "T", "tonnes": "T", "kn": "kN", "tm": "Tm", "knm": "kNm"}.get(low, u or None)


def _detect_units(headers: List[Any]) -> dict:
    """Unités forces/moments lues dans les en-têtes (ex. `FZ [T]`, `MZ [Tm]`)."""
    force_u = moment_u = None
    for h in headers or []:
        key = _canon_header(h)
        u = _norm_unit(_unit_in(h))
        if key in ("fx", "fy", "fz") and u and not force_u:
            force_u = u
        if key in ("mx", "my", "mz") and u and not moment_u:
            moment_u = u
    return {"force": force_u, "moment": moment_u}


def detect_table_type(headers: List[Any]) -> Optional[str]:
    """Type de tableau d'après les en-têtes. None si non reconnu/ambigu.

    P3-a1 : seul `pile_head_forces` (nœud + FZ + ≥2 colonnes d'efforts)."""
    cols = set(normalize_headers(headers))
    has_node = "node" in cols or "node_case" in cols
    if has_node and "fz" in cols and len(set(_FORCE_KEYS) & cols) >= 2:
        return "pile_head_forces"
    return None


# ---------------------------------------------------------------------
# Construction d'un tableau à partir de lignes brutes (PUR, sans pdfplumber)
# ---------------------------------------------------------------------
def _forces_confidence(cols: List[str]) -> Confidence:
    s = set(cols)
    has_node = "node" in s or "node_case" in s
    if has_node and set(_FORCE_KEYS).issubset(s):
        return "high"
    return "medium"


def _build_force_row(cells: List[Any], cols: List[str], confidence: Confidence) -> Optional[dict]:
    by = {}
    for idx, key in enumerate(cols):
        by[key] = (str(cells[idx]).strip() if idx < len(cells) and cells[idx] is not None else "")
    node = case = ""
    if "node_case" in by:
        nc = by["node_case"]
        if "/" in nc:
            node, case = nc.split("/", 1)
        else:
            node = nc
    if by.get("node"):
        node = by["node"]
    if by.get("case"):
        case = by["case"]
    node, case = node.strip(), case.strip()
    if not node and not case:
        return None  # ligne vide / séparateur : ignorée (jamais inventée)
    forces = {k: parse_number_fr(by.get(k, "")) for k in _FORCE_KEYS}
    src = " | ".join(str(c).strip() for c in cells if c is not None and str(c).strip())
    return {
        "node": node or None,
        "case": case or None,
        **forces,
        "source_text": src,
        "confidence": confidence,
        "requires_visual_check": confidence != "high",
    }


def build_table_from_rows(
    raw_rows: List[List[Any]], *, page_number: int, title: Optional[str] = None, source: str = "pdfplumber"
) -> Optional[ExtractedTable]:
    """Construit un ExtractedTable depuis des lignes brutes (1re = en-têtes).

    Renvoie None si <2 lignes, type non reconnu, ou aucune ligne exploitable."""
    rows = [[(c if c is not None else "") for c in r] for r in (raw_rows or []) if r]
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        return None

    headers = rows[0]
    ttype = detect_table_type(headers)
    if ttype != "pile_head_forces":  # P3-a1 : on ignore tout le reste
        return None

    cols = normalize_headers(headers)
    confidence = _forces_confidence(cols)
    data_rows = [r for r in (_build_force_row(c, cols, confidence) for c in rows[1:]) if r]
    if not data_rows:
        return None

    return ExtractedTable(
        table_type="pile_head_forces",
        title=title or "Efforts en têtes de pieux",
        page_number=page_number,
        units=_detect_units(headers),
        columns=["node", "case", *_FORCE_KEYS],
        rows=data_rows,
        confidence=confidence,
        requires_visual_check=confidence != "high",
        source=source,
    )


# ---------------------------------------------------------------------
# Extraction depuis un PDF (pdfplumber import-gardé)
# ---------------------------------------------------------------------
def extract_tables_from_pdf_bytes(data: bytes, pages: Optional[List[int]] = None) -> List[ExtractedTable]:
    """Extrait les tableaux RECONNUS d'un PDF à couche texte (pdfplumber).

    `pages` : numéros 1-based à traiter (toutes si None). pdfplumber absent ou
    erreur -> ``[]`` (jamais de crash : l'ingestion future ne doit pas échouer)."""
    data = data or b""
    if not data:
        return []
    try:
        import pdfplumber  # import-gardé (extra [tables])
    except Exception:
        logger.info("pdfplumber indisponible : extraction tabulaire ignorée (extra [tables]).")
        return []

    wanted = set(pages) if pages else None
    out: List[ExtractedTable] = []
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                if wanted is not None and i not in wanted:
                    continue
                try:
                    raw_tables = page.extract_tables() or []
                except Exception:
                    raw_tables = []
                for raw in raw_tables:
                    t = build_table_from_rows(raw, page_number=i)
                    if t:
                        out.append(t)
    except Exception as e:  # PDF illisible / pdfplumber interne : best-effort
        logger.warning("table extraction failed: %s", e.__class__.__name__)
    return out


# ---------------------------------------------------------------------
# Reconstruction depuis l'OCR (P3-b) — pages image-only/scannées
# ---------------------------------------------------------------------
_UNIT_TOKEN_RE = re.compile(r"^[\[(].*[\])]$")


def reconstruct_rows_from_ocr_words(words: List[dict], *, row_tol: int = 8) -> List[List[str]]:
    """Reconstruit des lignes/cellules à partir de mots OCR positionnés (bbox).

    Chaque mot : ``{text, left, top, line_num?, block_num?, par_num?}``. On groupe
    par ligne (line_num si dispo, sinon proximité verticale), on trie par x, et on
    fusionne un token d'unité isolé (``[T]``/``(Tm)``) dans la cellule précédente.

    PUR et déterministe (testable sans tesseract). Best-effort, ne lève jamais."""
    if not words:
        return []

    def _line_key(w: dict):
        if w.get("line_num") is not None and w.get("block_num") is not None:
            return (int(w.get("block_num", 0)), int(w.get("par_num", 0)), int(w.get("line_num", 0)))
        return ("y", round(int(w.get("top", 0)) / max(1, row_tol)))

    lines: dict = {}
    for w in words:
        if not (w.get("text") or "").strip():
            continue
        lines.setdefault(_line_key(w), []).append(w)

    rows: List[tuple] = []
    for ws in lines.values():
        ws = sorted(ws, key=lambda w: int(w.get("left", 0)))
        cells: List[str] = []
        for w in ws:
            tok = (w.get("text") or "").strip()
            if _UNIT_TOKEN_RE.match(tok) and cells:
                cells[-1] = f"{cells[-1]} {tok}".strip()  # unité rattachée à l'en-tête
            else:
                cells.append(tok)
        top = min(int(w.get("top", 0)) for w in ws)
        rows.append((top, cells))

    rows.sort(key=lambda r: r[0])
    return [cells for _, cells in rows]


def extract_ocr_tables_from_pages(page_to_words: dict) -> List[ExtractedTable]:
    """Construit des ExtractedTable depuis des mots OCR par page (P3-b).

    Mêmes détection/normalisation que le text-native, MAIS confiance forcée à
    ``low`` et ``requires_visual_check=True`` (extraction OCR = fragile)."""
    out: List[ExtractedTable] = []
    for page in sorted(page_to_words or {}):
        raw = reconstruct_rows_from_ocr_words(page_to_words.get(page) or [])
        t = build_table_from_rows(raw, page_number=int(page), source="ocr")
        if not t:
            continue
        t.confidence = "low"
        t.requires_visual_check = True
        for r in t.rows:
            r["confidence"] = "low"
            r["requires_visual_check"] = True
        out.append(t)
    return out


# ---------------------------------------------------------------------
# Rendu : Markdown embeddable + meta JSON
# ---------------------------------------------------------------------
def table_to_markdown(table: ExtractedTable) -> str:
    u = table.units or {}
    units_line = "Unités : forces = {f} ; moments = {m}".format(
        f=u.get("force") or "non précisé", m=u.get("moment") or "non précisé"
    )
    head = [
        f"Tableau détecté : {table.title or table.table_type}",
        f"Page : {table.page_number}",
        units_line,
        f"Confiance : {table.confidence}",
    ]
    if table.requires_visual_check:
        head.append("⚠️ Valeurs extraites automatiquement — à vérifier sur le fichier source.")

    cols = table.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in table.rows:
        cells = []
        for c in cols:
            v = row.get(c)
            cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(head) + "\n\n" + "\n".join(lines)


def table_to_meta(table: ExtractedTable) -> dict:
    return {
        "table_type": table.table_type,
        "title": table.title,
        "page_number": table.page_number,
        "units": table.units,
        "columns": table.columns,
        "rows": table.rows,
        "confidence": table.confidence,
        "requires_visual_check": table.requires_visual_check,
        "source": table.source,
    }

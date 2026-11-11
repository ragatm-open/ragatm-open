# backend/apps/rag_core/services/ingest/docvision/normalize.py
from __future__ import annotations

"""
`middle_json` MinerU → contrat RAGatm.

Deux points méritent d'être écrits noir sur blanc, parce qu'ils ont coûté cher à
établir :

1. **Espace de coordonnées.** `middle_json` exprime ses boîtes dans l'espace AFFICHÉ
   (page pivotée), alors que le contrat RAGatm et les vérités terrain sont en MEDIABOX
   (page non pivotée). Sur les pages pivotées, confondre les deux ne décale pas un peu
   la zone : il la met ailleurs. La conversion est explicite et n'a lieu que si la page
   déclare une rotation.

2. **Aucune boîte synthétique.** Si le moteur ne fournit pas de géométrie pour un bloc,
   `bbox` reste `None`. On ne l'estime pas, on ne la retrouve pas par appariement de
   texte, on ne la déduit pas d'un rendu.
"""

import html
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..extraction.base import Block, BlockType

#: Types de blocs `middle_json` rattachés au vocabulaire RAGatm.
_TYPES = {
    "title": BlockType.TITLE, "header": BlockType.TITLE,
    "text": BlockType.PARAGRAPH, "plain_text": BlockType.PARAGRAPH,
    "list": BlockType.LIST,
    "table": BlockType.TABLE, "table_body": BlockType.TABLE,
    "footer": BlockType.PARAGRAPH, "page_number": BlockType.PARAGRAPH,
    "image": BlockType.SECTION, "figure": BlockType.SECTION,
}

_BALISES = re.compile(r"<[^>]+>")


def vers_mediabox(bbox: Sequence[float], rotation: int) -> Optional[List[float]]:
    """Boîte normalisée de l'espace AFFICHÉ vers l'espace MEDIABOX."""
    if bbox is None or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if rotation == 90:
        n = [y0, 1.0 - x1, y1, 1.0 - x0]
    elif rotation == 180:
        n = [1.0 - x1, 1.0 - y1, 1.0 - x0, 1.0 - y0]
    elif rotation == 270:
        n = [1.0 - y1, x0, 1.0 - y0, x1]
    else:
        n = [x0, y0, x1, y1]
    out = [round(min(1.0, max(0.0, v)), 5) for v in n]
    if out[0] > out[2] or out[1] > out[3]:
        return None
    return out


def table_html_en_lignes(h: str) -> List[List[str]]:
    """Tableau HTML → matrice rectangulaire.

    Les cellules fusionnées sont RÉPLIQUÉES selon `colspan`/`rowspan`. Sans cela une
    fusion décale toutes les colonnes suivantes, et une cellule correcte est déclarée
    fausse parce qu'elle a glissé d'un rang.
    """
    if not h:
        return []
    lignes = re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S | re.I)
    grille: List[List[str]] = []
    differe: Dict[Tuple[int, int], str] = {}
    for i, ligne_html in enumerate(lignes):
        cellules = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", ligne_html, re.S | re.I)
        attrs = re.findall(r"<t[dh]([^>]*)>", ligne_html, re.I)
        ligne: List[str] = []
        j = 0
        for k, c in enumerate(cellules):
            while (i, j) in differe:
                ligne.append(differe.pop((i, j)))
                j += 1
            txt = html.unescape(_BALISES.sub(" ", c)).strip()
            a = attrs[k] if k < len(attrs) else ""
            cs = int((re.search(r'colspan\s*=\s*"?(\d+)', a) or [0, 1])[1])
            rs = int((re.search(r'rowspan\s*=\s*"?(\d+)', a) or [0, 1])[1])
            for dc in range(cs):
                ligne.append(txt)
                for dr in range(1, rs):
                    differe[(i + dr, j + dc)] = txt
            j += cs
        while (i, j) in differe:
            ligne.append(differe.pop((i, j)))
            j += 1
        grille.append(ligne)
    return grille


def _texte(noeud: Any) -> str:
    if isinstance(noeud, str):
        return noeud
    if isinstance(noeud, dict):
        c = noeud.get("content")
        if isinstance(c, str):
            return "" if "<table" in c.lower() else c
        if isinstance(c, list):
            return " ".join(x for x in (_texte(v) for v in c) if x)
    if isinstance(noeud, list):
        return " ".join(x for x in (_texte(v) for v in noeud) if x)
    return ""


def _table_du_bloc(bloc: dict) -> Tuple[List[List[str]], Optional[Sequence[float]], str]:
    for c in bloc.get("content") or []:
        if isinstance(c, dict) and isinstance(c.get("content"), str) \
                and "<table" in c["content"].lower():
            return table_html_en_lignes(c["content"]), c.get("bbox"), c["content"]
    return [], None, ""


def lignes_en_markdown(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    larg = max(len(r) for r in rows)
    out = []
    for i, r in enumerate(rows):
        cells = list(r) + [""] * (larg - len(r))
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        if i == 0:
            out.append("|" + "|".join([" --- "] * larg) + "|")
    return "\n".join(out)


def blocs_depuis_middle_json(
    mid: dict, *, page_number: int, rotation: int, provenance: dict
) -> Tuple[List[Block], List[dict]]:
    """Rend (blocs, tables). Les tables sortent à part : elles deviennent des unités
    atomiques, et leur texte est retiré du flux de page pour ne pas exister deux fois."""
    blocs: List[Block] = []
    tables: List[dict] = []
    for page in mid.get("pages") or []:
        for b in page.get("blocks") or []:
            brut = (b.get("type") or "text").lower()
            bbox = vers_mediabox(b.get("bbox"), rotation)
            index = b.get("index")
            prov = dict(provenance)
            prov.update({"source_block_type": brut, "source_block_index": index,
                         "bbox_engine": list(b.get("bbox") or []) or None})
            if brut in ("table", "table_body"):
                rows, bb_t, html_brut = _table_du_bloc(b)
                if rows:
                    tables.append({
                        "markdown": lignes_en_markdown(rows),
                        "meta": {
                            "rows": len(rows),
                            "columns": max((len(r) for r in rows), default=0),
                            "bbox": vers_mediabox(bb_t, rotation) or bbox,
                            "page_number": page_number,
                            "source_engine": "mineru",
                            "html": html_brut,
                            "provenance": prov,
                            "requires_visual_check": True,
                        },
                    })
                    continue
            texte = _texte(b).strip()
            if not texte:
                continue
            blocs.append(Block(
                type=_TYPES.get(brut, BlockType.PARAGRAPH), text=texte,
                page_number=page_number, bbox=tuple(bbox) if bbox else None,
                reading_order=index if isinstance(index, int) else None,
                source_engine="mineru", provenance=prov,
            ))
    return blocs, tables

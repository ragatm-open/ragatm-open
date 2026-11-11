# backend/apps/rag_core/services/ingest/extraction/excel_blocks.py
from __future__ import annotations

"""
Découpage d'une feuille en BLOCS. Module PUR (aucun Django, aucun openpyxl).

Une feuille n'est pas un tableau : c'est une toile où l'auteur a posé un ou plusieurs
tableaux, parfois des titres, parfois de la prose. Ce module reconstruit cette
structure à partir de la seule géométrie des cellules non vides.

Stratégie P1 — délibérément simple et prévisible :
- séparation sur les **lignes entièrement vides** (jamais sur les colonnes vides
  intérieures : cela romprait la contiguïté de la plage citée) ;
- rognage des colonnes vides **aux bords seulement** ;
- détection d'en-tête heuristique, sinon `col1..colN` (convention de l'extracteur CSV) ;
- une ligne à cellule unique juste au-dessus d'un tableau devient son **titre** ;
- une feuille majoritairement mono-cellule est **narrative**, pas tabulaire.

On ne CALCULE aucun total (c'est le problème de P2). Mais depuis P1.2 on RATTACHE un
bloc `TOTAL`/`SUBTOTAL` isolé par une ligne vide au tableau qui le précède : sans cela
son chiffre est indexé sous `col2`, sémantiquement orphelin, et le total du classeur
devient introuvable — ce qui pousse le modèle à rabattre sur la première ligne venue.
L'héritage est strictement conditionné (cf. `_inherit_total_headers`) : en cas de
doute, on retombe sur `col1..colN`.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .excel_headers import (
    CONFIDENCE_FALLBACK,
    DEFAULT_MAX_HEADER_ROWS,
    STRATEGY_INFERRED,
    MergedHeaderMap,
    build_headers,
    detect_header_rows,
)

BLOCK_TABLE = "table"
BLOCK_NARRATIVE = "narrative"

#: Stratégie d'en-tête d'un bloc de totaux rattaché au tableau qui le précède.
STRATEGY_INHERITED_TOTAL = "inherited_total_block"

#: Un bloc de totaux tient en quelques lignes (TOTAL, dont TVA, TOTAL GÉNÉRAL).
MAX_TOTAL_BLOCK_ROWS = 3

#: Libellés de totaux reconnus. La cellule doit être ENTIÈREMENT ce libellé : « total »
#: perdu au milieu d'une phrase narrative ne déclenche rien.
_TOTAL_LABEL = re.compile(
    r"^(?:"
    r"sous[- ]?total|subtotal|"
    r"grand[- ]?total|"
    r"total\s+general|total\s+cumule|"
    r"total|totaux|totals|"
    r"net total|overall total|somme|cumul"
    r")\b[\s:.\-–—*]*$"
)


def _norm_label(text: str) -> str:
    """Minuscules, accents retirés — « SOUS-TOTAL » et « Sous-total » sont un seul mot."""
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


@dataclass
class Cell:
    """Une cellule normalisée (texte déjà rendu par `excel_normalize`)."""

    text: str = ""
    is_numeric: bool = False       # nombre / date / booléen -> indice de « ligne de données »

    @property
    def empty(self) -> bool:
        return not self.text


@dataclass
class Block:
    """Un bloc contigu de lignes non vides, avec ses coordonnées RÉELLES."""

    kind: str                       # table | narrative
    row_start: int                  # 1-based, coordonnées Excel
    row_end: int
    col_start: int                  # 1-based
    col_end: int
    rows: List[List[str]] = field(default_factory=list)   # lignes de DONNÉES (hors en-tête)
    headers: List[str] = field(default_factory=list)
    headers_inferred: bool = False
    title: Optional[str] = None
    index: int = 0                  # n° du bloc dans la feuille (1-based)

    # -- En-têtes multi-lignes (P1.1) ------------------------------------
    #: Lignes d'en-tête, en coordonnées EXCEL (absolues).
    header_rows: List[int] = field(default_factory=list)
    header_strategy: str = STRATEGY_INFERRED
    header_confidence: str = CONFIDENCE_FALLBACK
    #: Fusions ayant réellement servi à reconstruire l'en-tête (`B1:D1`).
    merged_header_ranges: List[str] = field(default_factory=list)

    # -- Héritage des en-têtes par un bloc de totaux (P1.2) ---------------
    headers_inherited: bool = False
    inherited_from_table_index: Optional[int] = None
    #: Plage d'en-tête du tableau SOURCE (`A1:S2`) — la preuve de l'héritage.
    source_header_range: Optional[str] = None

    @property
    def header_depth(self) -> int:
        return len(self.header_rows)

    @property
    def data_row_start(self) -> int:
        """1re ligne de DONNÉES, en coordonnées Excel.

        Se déduit des lignes d'en-tête RÉELLES : un tableau financier en empile
        couramment deux. Toute constante (`row_start + 1`) décalerait la citation —
        c'est exactement le défaut que ce lot corrige.
        """
        return (max(self.header_rows) + 1) if self.header_rows else self.row_start

    @property
    def header_range(self) -> Optional[str]:
        """Plage des lignes d'en-tête (`A1:S2`), ou celle du tableau dont on hérite."""
        if not self.header_rows:
            return self.source_header_range
        return _range_ref((
            self.col_start, min(self.header_rows),
            self.col_end, max(self.header_rows),
        ))

    @property
    def n_cols(self) -> int:
        return self.col_end - self.col_start + 1

    @property
    def n_rows(self) -> int:
        return len(self.rows)


def _row_is_empty(row: List[Cell]) -> bool:
    return all(c.empty for c in row)


def _range_ref(box: tuple) -> str:
    """`(col_min, row_min, col_max, row_max)` -> `B1:D1`."""
    from .excel_workbook import col_letter

    c1, r1, c2, r2 = box
    return f"{col_letter(c1)}{r1}:{col_letter(c2)}{r2}"


def _is_narrative(rows: List[List[Cell]]) -> bool:
    """Feuille/bloc de prose : la plupart des lignes n'ont qu'UNE cellule remplie."""
    if not rows:
        return False
    single = sum(1 for r in rows if sum(1 for c in r if not c.empty) == 1)
    return single >= max(1, int(len(rows) * 0.7))


def _trim_columns(rows: List[List[Cell]]) -> tuple:
    """Rogne les colonnes vides AUX BORDS uniquement.

    Les colonnes vides INTÉRIEURES sont conservées : les supprimer rendrait la plage
    citée (`A12:F28`) fausse, puisqu'elle ne serait plus contiguë.
    """
    if not rows:
        return 0, -1
    width = max(len(r) for r in rows)
    first, last = None, None
    for c in range(width):
        if any(c < len(r) and not r[c].empty for r in rows):
            if first is None:
                first = c
            last = c
    if first is None:
        return 0, -1
    return first, last


def _inherit_total_headers(block: Block, previous: List[Block]) -> bool:
    """Rattache un bloc `TOTAL`/`SUBTOTAL` au tableau qui le précède. Renvoie True si oui.

    Un auteur de tableur aère : il saute une ligne avant la ligne de total. Le découpage
    par lignes vides en fait donc un bloc distinct, sans en-tête — et son chiffre part à
    l'index sous `col2`, coupé de « Shares Outstanding ». Le total du classeur devient
    alors introuvable, et c'est précisément ce qui a conduit le modèle à présenter la
    première catégorie venue comme le total de l'entreprise.

    L'héritage est REFUSÉ dès qu'un doute subsiste — toutes les conditions ci-dessous
    sont nécessaires. En cas de refus : `col1..colN`, comme avant. Aucune valeur n'est
    déplacée, aucune coordonnée n'est modifiée : seuls les INTITULÉS sont empruntés.
    """
    if block.kind != BLOCK_TABLE or not block.headers_inferred or not block.rows:
        return False
    # Un titre s'est intercalé entre les deux blocs : ce n'est plus la continuité
    # du même tableau, c'est une nouvelle section.
    if block.title:
        return False
    if len(block.rows) > MAX_TOTAL_BLOCK_ROWS:
        return False

    prev = previous[-1] if previous else None
    if prev is None or prev.kind != BLOCK_TABLE:
        return False
    # Un `col1..colN` n'a rien à transmettre : la chaîne s'arrête là.
    if prev.headers_inferred or not prev.headers:
        return False
    # `TOTAL` puis, une ligne plus bas, `GRAND TOTAL` : le second hérite lui aussi — mais
    # du TABLEAU d'origine, jamais du bloc intermédiaire. Les intitulés sont les mêmes ;
    # seule la provenance citée doit rester exacte.
    source_index = prev.inherited_from_table_index if prev.headers_inherited else prev.index

    # Exactement UNE ligne vide de séparation. Deux lignes plus bas, c'est autre chose.
    if block.row_start != prev.row_end + 2:
        return False

    # Même colonne de départ, et jamais plus large que le tableau : les colonnes sont
    # alignées par leur coordonnée ABSOLUE, jamais réalignées par position.
    if block.col_start != prev.col_start or block.col_end > prev.col_end:
        return False

    # Toute ligne du bloc doit être une ligne de total, libellée en PREMIÈRE colonne.
    if not all(_TOTAL_LABEL.match(_norm_label(r[0] if r else "")) for r in block.rows):
        return False

    block.headers = list(prev.headers[:block.n_cols])
    block.headers_inferred = False
    block.headers_inherited = True
    block.inherited_from_table_index = source_index
    block.source_header_range = prev.header_range
    block.header_strategy = STRATEGY_INHERITED_TOTAL
    block.header_confidence = prev.header_confidence
    block.header_rows = []          # le bloc n'a AUCUNE ligne d'en-tête à lui
    return True


def split_sheet_into_blocks(
    grid: List[List[Cell]],
    *,
    max_blocks: int,
    first_row: int = 1,
    first_col: int = 1,
    merged_ranges: Sequence[tuple] = (),
    max_header_rows: int = DEFAULT_MAX_HEADER_ROWS,
) -> tuple:
    """Découpe une grille en blocs. Renvoie ``(blocks, truncated, warnings)``.

    ``grid`` est indexée à partir de ``(first_row, first_col)`` en coordonnées Excel.
    ``merged_ranges`` : plages fusionnées de la FEUILLE, en coordonnées absolues
    ``(col_min, row_min, col_max, row_max)``. Elles servent — et servent SEULEMENT — à
    reconstruire les en-têtes multi-lignes (P1.1) : jamais à propager quoi que ce soit
    dans les données.
    """
    blocks: List[Block] = []
    truncated = False
    warnings: List[str] = []

    run: List[List[Cell]] = []
    run_start = first_row
    pending_title: Optional[str] = None

    def flush(end_row: int) -> None:
        nonlocal run, pending_title, truncated
        if not run:
            return
        if len(blocks) >= max_blocks:
            truncated = True
            run = []
            pending_title = None
            return

        # Un « bloc » d'une seule ligne à cellule unique n'est pas un tableau :
        # c'est un titre pour ce qui suit (ou une note isolée).
        if len(run) == 1 and sum(1 for c in run[0] if not c.empty) == 1:
            pending_title = next((c.text for c in run[0] if not c.empty), None)
            run = []
            return

        c0, c1 = _trim_columns(run)
        if c1 < c0:
            run = []
            return

        rows = [[c.text for c in r[c0:c1 + 1]] + [""] * max(0, (c1 - c0 + 1) - len(r[c0:c1 + 1]))
                for r in run]
        cells = [[Cell(text=r[i].text if i < len(r) else "",
                       is_numeric=r[i].is_numeric if i < len(r) else False)
                  for i in range(c0, c1 + 1)] for r in run]

        block = Block(
            kind=BLOCK_NARRATIVE if _is_narrative(cells) else BLOCK_TABLE,
            row_start=run_start,
            row_end=end_row,
            col_start=first_col + c0,
            col_end=first_col + c1,
            index=len(blocks) + 1,
            title=pending_title,
        )

        if block.kind == BLOCK_TABLE:
            width = len(rows[0]) if rows else 0
            abs_col = first_col + c0

            # La carte des fusions n'est construite QUE sur la fenêtre candidate à
            # l'en-tête (les premières lignes du bloc) : une feuille peut porter des
            # centaines de fusions dans ses données, elles ne nous concernent pas.
            merged = MergedHeaderMap(
                merged_ranges,
                row_min=run_start,
                row_max=run_start + min(len(cells), max_header_rows + 1) - 1,
            )

            det = detect_header_rows(
                cells, merged=merged, first_row=run_start, first_col=abs_col,
                width=width, max_depth=max_header_rows,
            )
            for w in det.warnings:
                if w not in warnings:
                    warnings.append(w)

            # Un bandeau qui couvre TOUTE la largeur ne discrimine aucune colonne :
            # c'est un titre, pas un niveau. On ne le préfixe donc pas partout.
            if det.title_row_index is not None:
                banner = next((t for t in rows[det.title_row_index] if t), None)
                if banner and not block.title:
                    block.title = banner

            headers, hw = build_headers(
                cells, det, merged=merged, first_row=run_start, first_col=abs_col,
                width=width,
            )
            for w in hw:
                if w not in warnings:
                    warnings.append(w)

            block.headers = headers
            block.headers_inferred = det.headers_inferred
            block.header_strategy = det.strategy
            block.header_confidence = det.confidence

            if det.headers_inferred:
                # Aucun en-tête fiable -> `col1..colN`, TOUTES les lignes en données.
                block.rows = rows
                block.header_rows = []
            else:
                block.header_rows = [run_start + i for i in det.header_row_indices]
                consumed = max(det.header_row_indices) + 1
                block.rows = rows[consumed:]
                block.merged_header_ranges = [
                    _range_ref(box) for box in merged.ranges
                    if any(box[1] <= run_start + i <= box[3] for i in det.header_row_indices)
                ][:20]
        else:
            block.rows = rows

        if _inherit_total_headers(block, blocks):
            if "excel_total_headers_inherited" not in warnings:
                warnings.append("excel_total_headers_inherited")

        if block.rows or block.kind == BLOCK_NARRATIVE:
            blocks.append(block)

        run = []
        pending_title = None

    for i, row in enumerate(grid):
        r = first_row + i
        if _row_is_empty(row):
            flush(r - 1)
            run_start = r + 1
        else:
            if not run:
                run_start = r
            run.append(row)

    flush(first_row + len(grid) - 1)
    return blocks, truncated, warnings

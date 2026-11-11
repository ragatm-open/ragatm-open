# backend/apps/rag_core/services/preview/reader.py
from __future__ import annotations

"""Lecture BORNÉE d'un classeur — fidèle à l'ingestion, jamais exhaustive.

Deux propriétés gouvernent ce module, et elles sont en tension :

**Fidélité.** L'aperçu doit afficher exactement la chaîne qui a été indexée. Or
l'ingestion ne recopie pas les cellules brutes : elle les NORMALISE
(`excel_normalize.render_cell`) — un `0.1` au format `0%` devient ``"10%"``, une
date devient de l'ISO, un texte dangereux est neutralisé. Relire le fichier et
afficher `cell.value` montrerait `0.1` là où la citation dit `10%`. On rejoue donc
la normalisation canonique, avec la MÊME limite de caractères qu'à l'ingestion.
Aucun second formateur n'est introduit ici.

**Bornes.** Le coût d'openpyxl en `read_only` est linéaire dans l'index de la ligne
visée : atteindre la ligne 19 000 impose de parser les 18 999 précédentes. La
fenêtre est donc bornée, le budget de temps vérifié PENDANT l'itération, et la
grille n'est jamais matérialisée en entier.
"""

import io
import time
from dataclasses import dataclass, field
from typing import Iterable

from apps.rag_core.services.ingest.extraction import config as ingest_config
from apps.rag_core.services.ingest.extraction.excel_normalize import (
    CellFlags,
    render_cell,
)

from . import config as preview_config
from . import errors as E
from .locator import CellRect, num_to_col

#: Vérifier l'horloge à chaque ligne coûterait plus que la lecture elle-même sur
#: une feuille large ; toutes les 256 lignes suffit à borner le dépassement.
_DEADLINE_CHECK_EVERY = 256

#: Drapeau propre à l'aperçu : la cellule PORTE une formule dont le classeur ne
#: contient aucune valeur en cache. `data_only=True` la rend indiscernable d'une
#: cellule vide ; l'ingestion, elle, l'a repérée par un scan XML et l'a persistée.
FLAG_UNCACHED_FORMULA = "uncached_formula"


@dataclass
class PreviewCell:
    column_index: int
    column_letter: str
    value: str
    highlighted: bool = False
    flags: list[str] = field(default_factory=list)
    #: Longueur de la valeur CANONIQUE (celle qui a été indexée), avant troncature
    #: d'affichage. Renseignée uniquement quand `value` est tronquée : le client
    #: peut alors dire « 700 caractères, 500 affichés » au lieu de laisser croire
    #: que la cellule s'arrête là. Une troncature muette mentirait sur la donnée.
    full_length: int | None = None


@dataclass
class PreviewRow:
    row_number: int
    cells: list[PreviewCell]


@dataclass
class SheetEntry:
    index: int          # 1-based, ordre du classeur
    name: str
    state: str          # visible | hidden | veryHidden
    previewable: bool
    reason: str | None = None


@dataclass
class ReadResult:
    rows: list[PreviewRow]
    header: list[str]
    display: CellRect
    truncated: bool
    truncation_reason: str | None
    #: Lignes réellement présentes dans la feuille à l'intérieur de la fenêtre.
    matched_rows: int


def _deadline_guard(started: float, budget: float) -> None:
    if (time.perf_counter() - started) > budget:
        raise E.PreviewTimeout(
            E.ERR_PREVIEW_TIMEOUT,
            "la lecture du classeur a dépassé le temps imparti",
        )


def load_workbook_bounded(data: bytes):
    """Ouvre le classeur avec EXACTEMENT les gardes de l'ingestion.

    `read_only=True` (8,5× moins de mémoire à 1 M de cellules, spike E1),
    `data_only=True` (valeurs en cache ; **jamais** le texte des formules, jamais de
    recalcul), `keep_links=False` (aucun lien externe résolu, aucune I/O réseau).
    """
    from openpyxl import load_workbook

    try:
        return load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
            keep_links=False,
            rich_text=False,
        )
    except Exception as exc:  # openpyxl lève des types variés sur fichier corrompu
        raise E.PreviewUnreadable(
            E.ERR_WORKBOOK_UNREADABLE, "classeur illisible ou corrompu"
        ) from exc


def list_sheets(wb, *, include_hidden: bool = True) -> list[SheetEntry]:
    """Inventaire des feuilles **selon la politique d'ingestion**.

    Cette liste n'est PAS `wb.sheetnames`. Une feuille `veryHidden` n'est pas
    indexée par la politique par défaut (`hidden_only`) : l'exposer dans l'aperçu
    ferait apparaître, dans l'interface RAG, un contenu que le RAG a délibérément
    écarté. Une feuille-graphique n'est pas une grille et n'a rien à afficher.
    """
    out: list[SheetEntry] = []
    for i, name in enumerate(wb.sheetnames, start=1):   # 1-based, comme SheetInfo.index
        state = "visible"
        previewable, reason = True, None
        try:
            ws = wb[name]
            state = str(getattr(ws, "sheet_state", "visible") or "visible")
        except Exception:
            previewable, reason = False, "sheet_unreadable"
            ws = None

        if previewable and not ingest_config.excel_sheet_is_indexable(state):
            previewable, reason = False, "sheet_not_indexed"
        elif previewable and ws is not None and not hasattr(ws, "iter_rows"):
            # Feuille-GRAPHIQUE : ni cellule, ni `iter_rows`.
            previewable, reason = False, "chart_sheet"

        out.append(
            SheetEntry(index=i, name=name, state=state, previewable=previewable, reason=reason)
        )
    return out


def resolve_sheet(sheets: Iterable[SheetEntry], sheet_index: int) -> SheetEntry:
    for s in sheets:
        if s.index == sheet_index:
            if not s.previewable:
                raise E.PreviewUnsupported(
                    E.ERR_SHEET_NOT_PREVIEWABLE,
                    "cette feuille n'est pas consultable dans l'aperçu",
                )
            return s
    raise E.PreviewNotFound(E.ERR_SHEET_NOT_FOUND, "feuille inconnue dans ce classeur")


def compute_display_rect(
    focus: CellRect, *, context_rows: int, context_columns: int
) -> CellRect:
    """Fenêtre affichée = plage cadrée + contexte, bornée par la grille Excel."""
    return CellRect(
        col_start=max(1, focus.col_start - context_columns),
        row_start=max(1, focus.row_start - context_rows),
        col_end=focus.col_end + context_columns,
        row_end=focus.row_end + context_rows,
    )


def clamp_display_rect(rect: CellRect) -> tuple[CellRect, bool, str | None]:
    """Applique les bornes de réponse. Renvoie `(fenêtre, tronqué, motif)`.

    La troncature est TOUJOURS déclarée : un aperçu réduit en silence ment sur le
    contexte réellement montré.
    """
    max_r, max_c, max_cells = (
        preview_config.max_rows(),
        preview_config.max_columns(),
        preview_config.max_cells(),
    )
    truncated, reason = False, None

    row_end, col_end = rect.row_end, rect.col_end
    if (row_end - rect.row_start + 1) > max_r:
        row_end = rect.row_start + max_r - 1
        truncated, reason = True, "max_rows"
    if (col_end - rect.col_start + 1) > max_c:
        col_end = rect.col_start + max_c - 1
        truncated, reason = True, "max_columns"

    # Borne CARDINALE : elle prime. On rogne les lignes, pas les colonnes — perdre
    # une colonne casse l'alignement valeur/en-tête, perdre une ligne ne casse rien.
    n_cols = col_end - rect.col_start + 1
    if n_cols > 0:
        allowed_rows = max(1, max_cells // n_cols)
        if (row_end - rect.row_start + 1) > allowed_rows:
            row_end = rect.row_start + allowed_rows - 1
            truncated, reason = True, "max_cells"

    return (
        CellRect(
            col_start=rect.col_start,
            row_start=rect.row_start,
            col_end=col_end,
            row_end=row_end,
        ),
        truncated,
        reason,
    )


def read_window(
    wb,
    *,
    sheet_name: str,
    display: CellRect,
    highlight: tuple[CellRect, ...] = (),
    header_row: int | None = None,
    budget_s: float | None = None,
    uncached_formula_cells: frozenset[str] = frozenset(),
) -> ReadResult:
    """Lit la fenêtre demandée en UN SEUL passage, en normalisant comme l'ingestion.

    Passage unique et non deux lectures : openpyxl en `read_only` ne sait pas
    revenir en arrière sans re-parser le fichier. Comme il faut de toute façon
    traverser les lignes 1..`display.row_start`, on en profite pour capter la ligne
    d'en-tête au vol — mesuré à +134 ms sur une feuille de 20 000 lignes, contre le
    double coût d'une seconde ouverture.
    """
    budget = budget_s if budget_s is not None else preview_config.effective_timeout_s()
    started = time.perf_counter()
    max_chars_display = preview_config.max_cell_chars()
    # ⚠️ La NORMALISATION utilise la limite d'INGESTION, pas celle d'affichage :
    # c'est ce qui garantit que la chaîne produite ici est celle qui a été indexée.
    max_chars_canonical = ingest_config.excel_max_cell_chars()

    try:
        ws = wb[sheet_name]
    except KeyError as exc:
        raise E.PreviewNotFound(
            E.ERR_SHEET_NOT_FOUND, "feuille inconnue dans ce classeur"
        ) from exc

    if not hasattr(ws, "iter_rows"):
        raise E.PreviewUnsupported(
            E.ERR_SHEET_NOT_PREVIEWABLE, "cette feuille n'est pas une grille"
        )

    # ⚠️ OBLIGATOIRE — une dimension déclarée trop PETITE fait perdre des cellules
    # en silence (même piège qu'à l'ingestion, E1 §10). Ne jamais retirer.
    if hasattr(ws, "reset_dimensions"):
        ws.reset_dimensions()

    def in_highlight(row: int, col: int) -> bool:
        return any(
            r.row_start <= row <= r.row_end and r.col_start <= col <= r.col_end
            for r in highlight
        )

    # On streame depuis la ligne 1 quand un en-tête est attendu au-dessus de la
    # fenêtre ; sinon on démarre directement à la fenêtre.
    want_header = header_row is not None and header_row < display.row_start
    scan_from = 1 if want_header else display.row_start

    rows: list[PreviewRow] = []
    header: list[str] = []
    seen = 0

    try:
        for raw in ws.iter_rows(
            min_row=scan_from,
            max_row=display.row_end,
            min_col=display.col_start,
            max_col=display.col_end,
        ):
            seen += 1
            if seen % _DEADLINE_CHECK_EVERY == 0:
                _deadline_guard(started, budget)

            if not raw:
                continue
            rn = getattr(raw[0], "row", None)
            if rn is None:
                continue

            is_header = want_header and rn == header_row
            in_window = display.row_start <= rn <= display.row_end
            if not (is_header or in_window):
                continue

            # openpyxl en `read_only` n'émet PAS les cellules vides de fin de ligne :
            # une ligne rendue telle quelle aurait des trous et le client devrait
            # deviner les colonnes manquantes. On indexe puis on complète, pour que
            # CHAQUE ligne porte une cellule par colonne affichée.
            by_col: dict[int, PreviewCell] = {}
            for c in raw:
                col = getattr(c, "column", None)
                if col is None:
                    continue
                text, flags = render_cell(
                    getattr(c, "value", None),
                    number_format=getattr(c, "number_format", "") or "",
                    data_type=getattr(c, "data_type", "") or "",
                    max_chars=max_chars_canonical,
                )
                display_text = text
                out_flags = list(flags)
                full_length = None
                if len(display_text) > max_chars_display:
                    full_length = len(display_text)
                    display_text = display_text[:max_chars_display]
                    out_flags.append(CellFlags.TRUNCATED)
                # Une formule SANS valeur en cache rend `None`, exactement comme une
                # cellule vide : sans ce drapeau, l'aperçu afficherait un blanc et
                # laisserait croire à une absence de donnée. La liste vient des
                # métadonnées d'ingestion — aucune seconde ouverture du classeur.
                if not text and f"{num_to_col(col)}{rn}" in uncached_formula_cells:
                    out_flags.append(FLAG_UNCACHED_FORMULA)
                by_col[col] = PreviewCell(
                    column_index=col,
                    column_letter=num_to_col(col),
                    value=display_text,
                    highlighted=in_window and in_highlight(rn, col),
                    flags=out_flags,
                    full_length=full_length,
                )

            cells = [
                by_col.get(col)
                or PreviewCell(
                    column_index=col,
                    column_letter=num_to_col(col),
                    value="",
                    highlighted=in_window and in_highlight(rn, col),
                    flags=(
                        [FLAG_UNCACHED_FORMULA]
                        if f"{num_to_col(col)}{rn}" in uncached_formula_cells
                        else []
                    ),
                )
                for col in range(display.col_start, display.col_end + 1)
            ]

            if is_header:
                header = [c.value for c in cells]
            if in_window:
                rows.append(PreviewRow(row_number=rn, cells=cells))
    except E.PreviewError:
        raise
    except Exception as exc:
        raise E.PreviewUnreadable(
            E.ERR_WORKBOOK_UNREADABLE, "lecture du classeur interrompue"
        ) from exc

    _deadline_guard(started, budget)

    return ReadResult(
        rows=rows,
        header=header,
        display=display,
        truncated=False,
        truncation_reason=None,
        matched_rows=len(rows),
    )

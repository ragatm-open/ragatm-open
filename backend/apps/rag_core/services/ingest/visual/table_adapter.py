"""``TablePayload`` Gateway V1 -> rail tabulaire générique EXISTANT.

Aucun second découpeur : le résultat est un ``pdf_tables.LogicalTable`` dont les
fragments passent par ``fragment_to_text`` / ``fragment_to_meta``, comme un tableau
PDF natif. Seuls ``row``/``col``/spans/``role``/``state``/``certainty`` décident —
jamais le texte d'une cellule.

Partition (générique, sans vocabulaire) :

* ``h`` = plus long préfixe de rangées portant une ancre ``column_header``/``corner``
  et aucune donnée remplie (une rangée sans ancre en hérite si toutes ses cellules
  couvrantes sont d'en-tête) ;
* ``v`` = plus long préfixe de colonnes qui, sous ``h``, portent un ``row_header`` et
  aucune ``data`` ;
* chemin d'une colonne feuille = ancres ``column_header`` qui la couvrent, de haut en
  bas, dédoublonnées par ancre ; illisible -> ``[unreadable header]`` ; vide ->
  ``Column N`` ; chemins non uniques -> repli plat neutre (jamais de hiérarchie
  inventée) ;
* libellé de ligne = ancres ``row_header`` couvrant ``0..v-1`` ; coin = contexte ;
* une donnée fusionnée est émise UNE fois, à son ancre.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from apps.llm_router.gateway import vision_contract as V1
from apps.rag_core.services.ingest.extraction import pdf_tables as P

UNREADABLE_HEADER = "[unreadable header]"
_HEADER_ROLES = frozenset({"column_header", "corner"})


@dataclass
class VisualTable:
    table: P.LogicalTable
    header_rows: int
    row_header_columns: int
    #: chemins d'en-tête non uniques -> colonnes neutres.
    flat_fallback: bool
    #: indicateur Gateway du visuel/région, reporté tel quel pour 1B.
    requires_visual_check: bool
    caption: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    #: étiquette de colonne -> statut d'en-tête (seulement ``unreadable``).
    header_states: Dict[str, str] = field(default_factory=dict)
    #: données fusionnées : {logical_row_id, column, row_span, col_span}.
    merged_cells: List[dict] = field(default_factory=list)
    #: métadonnées visuelles BORNÉES de l'artefact source : identité
    #: modèle/pipeline, empreinte, région — recopiées sur chaque fragment indexé.
    visual_meta: dict = field(default_factory=dict)


def _label(cell: dict) -> Optional[str]:
    if cell["state"] == "unreadable":
        return UNREADABLE_HEADER
    if cell["state"] == "filled" and cell["text"] and cell["text"].strip():
        return cell["text"].strip()
    return None


def _header_depth(cells: list, grid: dict, n_rows: int, n_cols: int) -> int:
    h = 0
    while h < n_rows:
        anchored = [c for c in cells if c["row"] == h]
        if anchored:
            is_header = (any(c["role"] in _HEADER_ROLES for c in anchored)
                         and not any(c["role"] == "data" and c["state"] == "filled"
                                     for c in anchored))
        else:
            is_header = all(cells[grid[(h, k)]]["role"] in _HEADER_ROLES for k in range(n_cols))
        if not is_header:
            break
        h += 1
    return h


def _row_header_width(cells: list, grid: dict, h: int, n_rows: int, n_cols: int) -> int:
    v = 0
    while v < n_cols and h < n_rows:
        roles = {cells[grid[(r, v)]]["role"] for r in range(h, n_rows)}
        if "row_header" not in roles or "data" in roles:
            break
        v += 1
    return v


def adapt_table_payload(payload: dict, *, source_digest: str, source_page: Optional[int] = None,
                        occurrence: int = 0, requires_visual_check: bool = False) -> VisualTable:
    """Payload VALIDÉ -> ``VisualTable``. Revérifie la couverture de grille."""
    try:
        grid = V1.expand_table_grid(payload)
    except V1.ContractViolation as exc:
        raise ValueError(exc.reason) from None
    cells, n_rows, n_cols = payload["cells"], payload["n_rows"], payload["n_cols"]
    h = _header_depth(cells, grid, n_rows, n_cols)
    v = _row_header_width(cells, grid, h, n_rows, n_cols)

    paths: List[List[str]] = []
    header_unreadable: List[bool] = []
    for k in range(v, n_cols):
        parts, seen, unreadable = [], set(), False
        for r in range(h):
            idx = grid[(r, k)]
            if idx in seen or cells[idx]["role"] != "column_header":
                continue
            seen.add(idx)
            label = _label(cells[idx])
            if label is not None:
                parts.append(label)
                unreadable = unreadable or label == UNREADABLE_HEADER
        paths.append(parts or [P.NEUTRAL_COLUMN.format(k - v + 1)])
        header_unreadable.append(unreadable)

    schema = [P.PATH_SEP.join(p) for p in paths]
    flat = len(set(schema)) != len(schema)
    if flat:
        paths = [[P.NEUTRAL_COLUMN.format(i + 1)] for i in range(len(paths))]
        schema = [p[0] for p in paths]

    corner_parts = [_label(c) for c in sorted(cells, key=lambda c: (c["row"], c["col"]))
                    if c["role"] == "corner"]
    context_label = P.PATH_SEP.join(p for p in corner_parts if p) or None

    matrix = not flat and (v > 0 or any(len(p) > 1 for p in paths))
    table_id = P._table_id(source_digest, source_page or 0, schema, occurrence)
    table = P.LogicalTable(
        table_id=table_id,
        table_type=P.TABLE_TYPE_MATRIX if matrix else P.TABLE_TYPE_GENERIC,
        schema=schema,
        column_paths=paths,
        source_pages=[source_page] if source_page is not None else [],
    )
    result = VisualTable(
        table=table, header_rows=h, row_header_columns=v, flat_fallback=flat,
        requires_visual_check=bool(requires_visual_check), caption=payload["caption"],
        notes=list(payload["notes"]),
        header_states={schema[i]: P.CELL_STATE_UNREADABLE
                       for i, bad in enumerate(header_unreadable) if bad and not flat},
    )

    for r in range(h, n_rows):
        label_parts, seen = [], set()
        for k in range(v):
            idx = grid[(r, k)]
            if idx in seen or cells[idx]["role"] != "row_header":
                continue
            seen.add(idx)
            label = _label(cells[idx])
            if label is not None:
                label_parts.append(label)

        values: Dict[str, str] = {}
        states: Dict[str, str] = {}
        merged: List[dict] = []
        for pos, k in enumerate(range(v, n_cols)):
            cell = cells[grid[(r, k)]]
            if (cell["row"], cell["col"]) != (r, k):
                continue  # couverte par une ancre fusionnée : émise une seule fois
            if cell["role"] == "note":
                if cell["state"] == "filled" and cell["text"]:
                    result.notes.append(cell["text"])
                continue
            column = schema[pos]
            if cell["state"] == "unreadable":
                states[column] = P.CELL_STATE_UNREADABLE
            elif cell["state"] == "filled":
                values[column] = cell["text"]
                if cell["certainty"] in (P.CELL_STATE_APPROXIMATE, P.CELL_STATE_AMBIGUOUS):
                    states[column] = cell["certainty"]
            else:
                continue  # blank : absence, sémantique inchangée du rail
            if cell["row_span"] > 1 or cell["col_span"] > 1:
                merged.append({"column": column, "row_span": cell["row_span"],
                               "col_span": cell["col_span"]})

        if not label_parts and not values and not states:
            continue
        row = P.LogicalRow(
            logical_row_id=P._logical_row_id(table_id, len(table.rows)),
            row_kind=P.ROW_KIND_DATA,
            row_label=P.PATH_SEP.join(label_parts) or None,
            context_label=context_label,
        )
        row.fragments.append(P.SourceFragment(page_number=source_page, physical_row_index=r,
                                              cells=values, cell_states=states))
        table.rows.append(row)
        result.merged_cells.extend({"logical_row_id": row.logical_row_id, **m} for m in merged)
    return result

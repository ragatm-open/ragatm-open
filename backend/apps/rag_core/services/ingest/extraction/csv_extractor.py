# backend/apps/rag_core/services/ingest/extraction/csv_extractor.py
from __future__ import annotations

"""
Extracteur CSV (stdlib `csv`).

- détection du séparateur (",", ";", "\\t") via csv.Sniffer + repli heuristique ;
- limites strictes : lignes, colonnes, taille de cellule (anti-DoS mémoire) ;
- représentation auto-descriptive "col: valeur" par ligne (meilleur retrieval
  qu'une table brute géante) ; un bloc CSV_ROW par ligne (groupable) ;
- détection CSV formula injection : les cellules à risque (= + - @ et tab/CR)
  sont NEUTRALISÉES pour toute restitution (préfixe apostrophe) et signalées.
"""

import csv
import io
from typing import List, Optional

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    CorruptedFile,
    ExtractionContext,
    ExtractionLimitExceeded,
    ExtractionResult,
    decode_best_effort,
)

# Préfixes dangereux pour CSV/formula injection (tableurs).
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_FORMULA_CONTROL = ("\t", "\r")


def _sniff_dialect(sample: str) -> "csv.Dialect | type[csv.Dialect]":
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        # repli : compte les séparateurs sur les 1res lignes
        lines = [ln for ln in sample.splitlines() if ln.strip()][:10]
        best, best_count = ",", -1
        for sep in (",", ";", "\t"):
            c = sum(ln.count(sep) for ln in lines)
            if c > best_count:
                best, best_count = sep, c

        class _D(csv.Dialect):
            delimiter = best
            quotechar = '"'
            doublequote = True
            skipinitialspace = False
            lineterminator = "\r\n"
            quoting = csv.QUOTE_MINIMAL

        return _D


def _is_dangerous(cell: str) -> bool:
    if not cell:
        return False
    c = cell.lstrip()
    if c[:1] in _FORMULA_PREFIXES:
        return True
    if cell[:1] in _FORMULA_CONTROL:
        return True
    return False


def _neutralize(cell: str) -> str:
    """Neutralise une cellule à risque pour toute restitution/export (préfixe ')."""
    return "'" + cell if cell else cell


class CsvExtractor:
    name = "csv"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.CSV

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        text, warn = decode_best_effort(data, declared=ctx.declared_content_type)
        res = ExtractionResult()
        if warn:
            res.add_warning(warn)

        if not text.strip():
            return res.finalize(fmt="csv", mime=M.CSV, method="csv")

        dialect = _sniff_dialect(text[:8192])
        max_rows = config.csv_max_rows()
        max_cols = config.csv_max_columns()
        max_cell = config.csv_max_cell_chars()

        reader = csv.reader(io.StringIO(text), dialect)
        try:
            rows = []
            for r in reader:
                rows.append(r)
                if len(rows) > max_rows + 1:  # +1 pour l'en-tête
                    raise ExtractionLimitExceeded(
                        f"CSV trop volumineux : plus de {max_rows} lignes. "
                        "Ajustez DOCUMENT_CSV_MAX_ROWS si nécessaire.",
                        code="csv_too_many_rows",
                    )
        except csv.Error as e:
            raise CorruptedFile(f"CSV illisible : {e.__class__.__name__}.", code="csv_parse_error")

        if not rows:
            return res.finalize(fmt="csv", mime=M.CSV, method="csv")

        header = rows[0]
        if len(header) > max_cols:
            raise ExtractionLimitExceeded(
                f"CSV : trop de colonnes ({len(header)} > {max_cols}). "
                "Ajustez DOCUMENT_CSV_MAX_COLUMNS si nécessaire.",
                code="csv_too_many_columns",
            )

        # cellule trop longue => rejet (anti-DoS) sur n'importe quelle cellule
        for r in rows:
            for cell in r:
                if cell is not None and len(cell) > max_cell:
                    raise ExtractionLimitExceeded(
                        f"CSV : cellule trop longue (> {max_cell} caractères). "
                        "Ajustez DOCUMENT_CSV_MAX_CELL_CHARS si nécessaire.",
                        code="csv_cell_too_long",
                    )

        header = [(_clean(h) or f"col{i+1}") for i, h in enumerate(header)]
        data_rows = rows[1:]

        blocks: List[Block] = []
        dangerous_cells = 0
        lines_out: List[str] = []

        for row_idx, row in enumerate(data_rows):
            parts: List[str] = []
            row_danger: List[int] = []
            for col_idx, value in enumerate(row):
                col_name = header[col_idx] if col_idx < len(header) else f"col{col_idx+1}"
                value = value if value is not None else ""
                if _is_dangerous(value):
                    dangerous_cells += 1
                    row_danger.append(col_idx)
                    value = _neutralize(value)
                parts.append(f"{col_name}: {_clean(value)}")
            line_text = " ; ".join(parts).strip()
            if not line_text:
                continue
            lines_out.append(line_text)
            meta = {"row": row_idx + 1}
            if row_danger:
                meta["dangerous_columns"] = row_danger
            blocks.append(Block(type=BlockType.CSV_ROW, text=line_text, meta=meta))

        res.blocks = blocks
        res.text = "\n".join(lines_out).strip()
        res.metadata["lines"] = len(data_rows)
        res.metadata["columns"] = len(header)
        res.metadata["csv_delimiter"] = getattr(dialect, "delimiter", ",")
        if dangerous_cells:
            res.metadata["dangerous_cells"] = dangerous_cells
            res.add_warning("csv_formula_injection_neutralized")
        return res.finalize(fmt="csv", mime=M.CSV, method="csv")


def _clean(s: Optional[str]) -> str:
    if not s:
        return ""
    return " ".join(str(s).split())

# backend/apps/rag_core/services/ingest/extraction/excel_extractor.py
from __future__ import annotations

"""
Extracteur Excel `.xlsx` — ingestion DOCUMENTAIRE (P1).

Ce que cet extracteur fait : rendre un classeur *consultable et citable* par le RAG
(« quel est le budget du projet Atlas ? », « résume la feuille Risques »).

Ce qu'il ne fait PAS, et ne doit pas faire : calculer. Aucune somme, aucune moyenne,
aucun total. Le chiffré exact est le problème de P2 (moteur tabulaire déterministe),
et il exige la table ENTIÈRE — pas des fenêtres de 40 lignes. Ce sont deux systèmes.

Trois décisions du spike E1 sont ici **non négociables** (chacune a son test) :

1. `read_only=True` — à 1 M de cellules, le mode normal consomme 448 Mo contre 53 Mo
   (mesuré). Ce n'est pas une optimisation, c'est une condition.
2. `ws.reset_dimensions()` avant TOUTE itération — sans lui, une feuille déclarant
   `A1:A1` mais contenant 50 cellules n'en rend qu'UNE. **49 cellules disparaissent
   sans erreur, sans warning, sans exception.** C'est le défaut le plus dangereux
   trouvé, parce qu'il est totalement muet.
3. Le cache de formule vient du **scan XML** de la pré-passe, jamais d'une seconde
   ouverture openpyxl (+19 % contre +108 %, mesuré).

Aucune macro n'est lue (`.xlsm` est refusé en amont). Aucune formule n'est évaluée —
openpyxl ne le peut structurellement pas. Aucun lien externe n'est suivi
(`keep_links=False`). Aucun fichier temporaire n'est créé.
"""

import io
import logging
from typing import List

from . import config
from . import mime as M
from .base import (
    Block,
    BlockType,
    CorruptedFile,
    ExtractionContext,
    ExtractionLimitExceeded,
    ExtractionResult,
    RejectedFile,
)
from .excel_blocks import BLOCK_NARRATIVE, Cell, split_sheet_into_blocks
from .excel_normalize import CellFlags, currency_symbol, render_cell
from .excel_render import render_block
from . import excel_workbook as xw
from .excel_workbook import SheetInfo, WorkbookInventory, inspect_workbook, parse_range

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "1"
CHUNKING_VERSION = "1"


class ExcelExtractor:
    name = "excel"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.XLSX

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        if not config.excel_enabled():
            raise RejectedFile(
                "L'ingestion des classeurs Excel est désactivée sur cet environnement "
                "(DOCUMENT_EXCEL_ENABLED=0).",
                code="excel_disabled",
            )

        res = ExtractionResult()

        # 1) Pré-passe STDLIB : gardes E0 + borne sharedStrings + structure + formules.
        #    Tout ce qui peut refuser le classeur le fait ICI, avant qu'openpyxl ne
        #    touche l'archive.
        inv = inspect_workbook(data)
        for w in inv.warnings:
            res.add_warning(w)

        # 2) openpyxl, en streaming.
        wb = self._load(data)
        try:
            state = _SheetState(inv)
            for sheet in inv.sheets:
                if not config.excel_sheet_is_indexable(sheet.state):
                    # `veryHidden` n'est pas modifiable depuis l'interface d'Excel :
                    # c'est une intention délibérée, on la respecte.
                    if sheet.state == "veryHidden":
                        state.veryhidden_skipped.append(sheet.name)
                    else:
                        state.hidden_skipped.append(sheet.name)
                    continue
                if sheet.hidden:
                    state.hidden_indexed.append(sheet.name)
                self._extract_sheet(wb, sheet, res, state, inv)
                if state.chunks_full:
                    break
        finally:
            try:
                wb.close()
            except Exception:  # pragma: no cover - fermeture best-effort
                logger.debug("excel: workbook close failed")

        return self._finalize(res, inv, state)

    # -----------------------------------------------------------------
    def _load(self, data: bytes):
        from openpyxl import load_workbook

        try:
            return load_workbook(
                io.BytesIO(data),
                read_only=True,     # E1 : 8,5× moins de mémoire à 1 M de cellules
                data_only=True,     # valeurs en cache ; JAMAIS le texte des formules
                keep_links=False,   # aucun lien externe résolu, aucune I/O réseau
                rich_text=False,
            )
        except (RejectedFile, ExtractionLimitExceeded):
            raise
        except Exception as e:
            raise CorruptedFile(
                "Classeur Excel illisible ou corrompu.", code="excel_corrupted"
            ) from e

    # -----------------------------------------------------------------
    def _extract_sheet(self, wb, sheet: SheetInfo, res: ExtractionResult,
                       state: "_SheetState", inv: WorkbookInventory) -> None:
        try:
            ws = wb[sheet.name]
        except KeyError as e:  # pragma: no cover - incohérence workbook.xml / openpyxl
            # Feuille déclarée dans workbook.xml mais introuvable côté openpyxl. C'est
            # une PERTE de contenu : elle doit porter un code, comme toutes les autres
            # catégories de saut — `partial=True` seul ne dit pas ce qui a disparu.
            state.unreadable.append(sheet.name)
            logger.warning("excel: feuille %r déclarée mais illisible (%s)", sheet.name, e)
            return

        # Une feuille-GRAPHIQUE (`Chartsheet`) n'est pas une grille : elle n'a ni cellule,
        # ni `reset_dimensions`, ni `iter_rows`. On la saute proprement plutôt que de
        # laisser une AttributeError condamner tout le classeur. (Un classeur de reporting
        # mêle couramment feuilles de données et feuilles de graphiques.)
        if not hasattr(ws, "reset_dimensions") or not hasattr(ws, "iter_rows"):
            state.chart_sheets.append(sheet.name)
            return

        # ⚠️ OBLIGATOIRE. Sans cet appel, une dimension déclarée trop PETITE fait
        # perdre des cellules EN SILENCE (E1 §10). Ne jamais retirer.
        ws.reset_dimensions()

        max_cols = config.excel_max_columns()
        max_cell_chars = config.excel_max_cell_chars()

        grid: List[List[Cell]] = []
        real_rows = 0
        real_cols = 0
        for row in ws.iter_rows():
            cells: List[Cell] = []
            for c in row[:max_cols]:
                value = getattr(c, "value", None)
                nf = getattr(c, "number_format", "") or ""
                dt = getattr(c, "data_type", "") or ""
                text, flags = render_cell(
                    value, number_format=nf, data_type=dt, max_chars=max_cell_chars
                )
                state.note_flags(flags, nf)
                cells.append(Cell(
                    text=text,
                    is_numeric=bool(text) and dt in ("n", "d", "b") and CellFlags.ERROR_CELL not in flags,
                ))
                state.cells_read += 1

            if len(row) > max_cols:
                state.column_overflow = True

            if any(not c.empty for c in cells):
                real_rows = len(grid) + 1
                for i, c in enumerate(cells, start=1):
                    if not c.empty:
                        real_cols = max(real_cols, i)

            grid.append(cells)

            if state.cells_read > config.excel_max_cells():
                raise ExtractionLimitExceeded(
                    f"Classeur Excel trop volumineux : plus de {config.excel_max_cells()} "
                    "cellules. Ajustez DOCUMENT_EXCEL_MAX_CELLS si nécessaire.",
                    code="excel_too_many_cells",
                )

        # La dimension DÉCLARÉE fait-elle autorité ? Non — et on le constate ici.
        # Plus PETITE que le contenu réel, elle aurait fait perdre des cellules sans le
        # moindre avertissement (E1 §10) ; `reset_dimensions()` l'a neutralisée.
        box = parse_range(sheet.declared_dimension or "")
        if box and real_rows and (box[3] < real_rows or box[2] < real_cols):
            state.dimension_mismatch = True

        if not any(not c.empty for r in grid for c in r):
            state.empty_sheets.append(sheet.name)
            return

        # Plages fusionnées de la feuille, en coordonnées absolues. Elles ne servent
        # QU'À reconstruire les en-têtes (P1.1) : jamais à propager une valeur dans les
        # données. Cf. excel_headers.MergedHeaderMap.
        # Une plage non parsable est ÉCARTÉE — mais signalée : la colonne concernée
        # retombera sur `colN` sans que rien ne l'explique autrement (M2).
        merged_boxes = []
        for ref in sheet.merged_ranges:
            box = parse_range(ref)
            if box:
                merged_boxes.append(box)
            else:
                inv.mark_invalid_merged_range(sheet.name, ref)

        blocks, block_truncated, header_warnings = split_sheet_into_blocks(
            grid,
            max_blocks=max(0, config.excel_max_blocks() - state.blocks),
            merged_ranges=merged_boxes,
            max_header_rows=config.excel_max_header_rows(),
        )
        if block_truncated:
            state.blocks_full = True
        for w in header_warnings:
            res.add_warning(w)

        has_formulas = sheet.formula_count > 0
        for block in blocks:
            state.blocks += 1
            state.note_block_headers(block)
            chunks, warns = render_block(
                block,
                sheet_name=sheet.name,
                sheet_index=sheet.index,
                sheet_hidden=sheet.hidden,
                sheet_state=sheet.state,
                rows_per_chunk=config.excel_rows_per_chunk(),
                chunk_max_chars=config.excel_chunk_max_chars(),
                max_columns_markdown=config.excel_max_columns_markdown(),
                wide_threshold=config.excel_wide_table_threshold(),
                max_columns_per_chunk=config.excel_max_columns_per_chunk(),
                max_chunks=config.excel_max_chunks_per_sheet(),
                has_formulas=has_formulas,
            )
            for w in warns:
                res.add_warning(w)

            # Observabilité : un bloc surdimensionné a été découpé pour tenir sous le
            # plafond. Découpage NORMAL, pas une erreur — on journalise la structure
            # (feuille, bloc, nb de parts), jamais le contenu.
            _split_markers = {
                "excel_narrative_split", "excel_wide_row_split", "excel_block_hard_cap_split",
            }.intersection(warns)
            if _split_markers:
                logger.info(
                    "excel_block_split sheet=%r block=%d parts=%d max_chars=%d markers=%s",
                    sheet.name, block.index, len(chunks),
                    config.excel_chunk_max_chars(), sorted(_split_markers),
                )

            for ch in chunks:
                if len(res.excel_chunks) >= config.excel_max_chunks():
                    state.chunks_full = True
                    return
                res.excel_chunks.append(ch)
                res.blocks.append(Block(
                    type=BlockType.PARAGRAPH if block.kind == BLOCK_NARRATIVE else BlockType.TABLE,
                    text=ch["text"],
                    meta={k: ch["meta"][k] for k in ("sheet_name", "cell_range")},
                ))
                state.rows_indexed += (ch["meta"]["row_end"] - ch["meta"]["row_start"] + 1)

        state.sheets_indexed.append(sheet.name)

    # -----------------------------------------------------------------
    def _finalize(self, res: ExtractionResult, inv: WorkbookInventory,
                  state: "_SheetState") -> ExtractionResult:
        if state.hidden_skipped:
            res.add_warning("excel_hidden_sheets_skipped")
        if state.hidden_indexed:
            # Une feuille masquée INDEXÉE n'est pas une erreur — mais l'utilisateur doit
            # le savoir : elle n'apparaît pas dans les onglets de son classeur.
            res.add_warning("excel_hidden_sheet_indexed")
        if state.veryhidden_skipped:
            res.add_warning("excel_veryhidden_sheet_skipped")
        if state.empty_sheets:
            res.add_warning("excel_empty_sheets_skipped")
        if state.chart_sheets:
            res.add_warning("excel_chart_sheets_skipped")
        if state.blocks_full:
            res.add_warning("excel_max_blocks_reached")
        if state.chunks_full:
            res.add_warning("excel_max_chunks_reached")
        if state.column_overflow:
            res.add_warning("excel_too_many_columns")
        if state.truncated_cells:
            res.add_warning("excel_cell_truncated")
        if state.error_cells:
            res.add_warning("excel_error_cells")
        if state.neutralized_cells:
            res.add_warning("excel_formula_injection_neutralized")
        if state.duration_cells:
            res.add_warning("excel_duration_format_unsupported")
        if state.unreadable:
            # Feuille déclarée mais illisible : contenu perdu, code dédié (M2).
            res.add_warning(xw.WARN_SHEET_UNREADABLE)
        # -- dégradations structurelles remontées par la pré-passe (M2) --------
        # Ces pertes n'empêchent pas openpyxl de lire le fichier : sans ce relais,
        # l'extraction paraîtrait complète alors qu'elle a perdu formules, fusions
        # ou dimensions. On les propage telles quelles, avec leur code stable.
        for code in inv.warnings:
            res.add_warning(code)
        if inv.structurally_degraded:
            # Connaissance structurelle incomplète ⇒ l'extraction n'est PAS complète.
            res.partial = True
        if state.dimension_mismatch:
            # La dimension DÉCLARÉE contredisait le contenu réel. `reset_dimensions()`
            # a rétabli la vérité — sans lui, des cellules auraient disparu en silence.
            # On ne lève ce warning que sur divergence RÉELLE : tout classeur porte une
            # dimension déclarée, en signaler la simple présence serait du bruit.
            res.add_warning("excel_dimension_reset")

        # Le texte global sert au FTS et à la garde « document vide » ; les chunks
        # indexés viennent de `excel_chunks` (voir indexing._build_excel_specs).
        res.text = "\n\n".join(c["text"] for c in res.excel_chunks).strip()

        md = res.metadata
        md["type"] = "excel"
        md["excel"] = {
            "sheets_total": len(inv.sheets),
            "sheets_indexed": len(state.sheets_indexed),
            "sheets_empty": len(state.empty_sheets),
            "sheets_hidden": len(state.hidden_skipped),
            "sheets_hidden_names": state.hidden_skipped[:20],
            "hidden_sheets_indexed": len(state.hidden_indexed),
            "hidden_sheets_indexed_names": state.hidden_indexed[:20],
            "veryhidden_sheets_skipped": len(state.veryhidden_skipped),
            "sheets_empty_names": state.empty_sheets[:20],
            "blocks": state.blocks,
            "rows_indexed": state.rows_indexed,
            "cells_read": state.cells_read,
            "chunks": len(res.excel_chunks),
            "formula_count": inv.formula_count,
            "formula_cached_count": inv.formula_cached_count,
            "formula_uncached_count": inv.formula_uncached_count,
            "requires_recalculation": bool(inv.formula_uncached_count),
            "uncached_formula_cells": inv.uncached_formula_cells,
            "merged_ranges": inv.merged_ranges,
            "defined_names": inv.defined_names,
            "declared_tables": [t["name"] for t in inv.tables][:20],
            "shared_strings_mb": round(inv.shared_strings_bytes / (1024 * 1024), 2),
            "uncompressed_mb": round(inv.uncompressed_bytes / (1024 * 1024), 2),
            "charts": inv.charts,
            "images": inv.images,
            "comments": inv.comments,
            "external_links": inv.external_links,
            "ole_objects": inv.ole_objects,
            "error_cells": state.error_cells,
            "truncated_cells": state.truncated_cells,
            "currencies": sorted(state.currencies),
            # En-têtes multi-lignes (P1.1)
            "multirow_header_blocks": state.multirow_header_blocks,
            "max_header_depth": state.max_header_depth,
            "header_fallback_blocks": state.header_fallback_blocks,
            "inherited_header_blocks": state.inherited_header_blocks,
            "truncated": bool(state.chunks_full or state.blocks_full),
        }
        md["sheets"] = len(inv.sheets)
        md["tables"] = state.blocks
        # Versions d'extracteur / de chunking : permettent de détecter, plus tard, des
        # chunks périmés après évolution du pipeline. Aucune migration (meta est libre).
        md["extractor"] = "excel"
        md["extractor_version"] = EXTRACTOR_VERSION
        md["chunking_version"] = CHUNKING_VERSION

        # PARTIEL : une part significative du classeur n'a pas été indexée. On ne
        # présente jamais une extraction tronquée comme un succès.
        if state.chunks_full or state.blocks_full or state.unreadable:
            res.partial = True

        return res.finalize(fmt="excel", mime=M.XLSX, method="excel:openpyxl")


class _SheetState:
    """Compteurs transverses au classeur (agrégés dans les métadonnées)."""

    def __init__(self, inv: WorkbookInventory):
        self.inv = inv
        self.cells_read = 0
        self.rows_indexed = 0
        self.blocks = 0
        self.sheets_indexed: List[str] = []
        self.hidden_skipped: List[str] = []
        self.hidden_indexed: List[str] = []
        self.veryhidden_skipped: List[str] = []
        self.empty_sheets: List[str] = []
        self.unreadable: List[str] = []
        self.chart_sheets: List[str] = []
        self.error_cells = 0
        self.truncated_cells = 0
        self.neutralized_cells = 0
        self.duration_cells = 0
        self.currencies: set = set()
        self.column_overflow = False
        self.dimension_mismatch = False
        # En-têtes multi-lignes (P1.1)
        self.multirow_header_blocks = 0
        self.max_header_depth = 0
        self.header_fallback_blocks = 0
        self.inherited_header_blocks = 0
        self.blocks_full = False
        self.chunks_full = False

    def note_block_headers(self, block) -> None:
        depth = len(getattr(block, "header_rows", []) or [])
        if depth > 1:
            self.multirow_header_blocks += 1
        self.max_header_depth = max(self.max_header_depth, depth)
        if getattr(block, "headers_inferred", False):
            self.header_fallback_blocks += 1
        if getattr(block, "headers_inherited", False):
            self.inherited_header_blocks += 1

    def note_flags(self, flags: list, number_format: str) -> None:
        for f in flags:
            if f == CellFlags.ERROR_CELL:
                self.error_cells += 1
            elif f == CellFlags.TRUNCATED:
                self.truncated_cells += 1
            elif f == CellFlags.NEUTRALIZED:
                self.neutralized_cells += 1
            elif f == CellFlags.DURATION_UNSUPPORTED:
                self.duration_cells += 1
        cur = currency_symbol(number_format)
        if cur:
            self.currencies.add(cur)

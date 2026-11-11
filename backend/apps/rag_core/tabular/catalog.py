"""Catalogue tabulaire — inventaire des tables d'un document, et chargement des lignes.

Deux responsabilités, volontairement dans le même module parce qu'elles partagent la
même vérité : **la structure et les données viennent du fichier source, jamais des
chunks.** Un chunk peut tronquer des lignes, répéter un en-tête ou fragmenter un
tableau ; une agrégation calculée dessus mesurerait le découpage, pas le document.

Le catalogue est lié au `content_sha256` du document. Si le fichier est remplacé, le
hash change, le catalogue est périmé (`catalog_stale`) et doit être reconstruit — il
n'est jamais réutilisé « au cas où ».

Aucun chemin ne vient de l'utilisateur : le document est résolu par identifiant, ses
octets sont lus via le stockage Django, et le moteur ne reçoit que des lignes typées.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import types as T

logger = logging.getLogger(__name__)

#: Espace de noms des identifiants de catalogue. Les UUID sont DÉRIVÉS (uuid5), jamais
#: tirés au hasard : un `uuid4()` produirait des identifiants différents à chaque
#: reconstruction du catalogue, donc un `plan_hash` différent pour le même plan
#: sémantique — les plans ne seraient ni reproductibles ni rejouables, et la propriété
#: de déterminisme exigée du moteur serait fausse.
#:
#: La dérivation inclut le hash du document : deux versions d'un même fichier
#: produisent des identifiants distincts, ce qui rend l'invalidation automatique.
_NS = uuid5(NAMESPACE_URL, "ragatm/tabular/catalog/v1")


def _stable_id(*parts: str) -> UUID:
    """Identifiant stable entre reconstructions, machines et exécutions."""
    return uuid5(_NS, "|".join(str(p) for p in parts))

#: Lignes échantillonnées pour inférer le type d'une colonne. Assez pour décider,
#: assez peu pour ne pas matérialiser la table entière au moment de l'inventaire.
TYPE_SAMPLE_ROWS = 200

#: Libellés de lignes de synthèse. Une ligne TOTAL agrège déjà les lignes du dessus :
#: l'inclure dans une somme compterait deux fois les mêmes montants.
SUMMARY_LABELS = frozenset({
    "total", "totaux", "sous-total", "sous total", "soustotal", "subtotal",
    "cumul", "moyenne", "somme", "grand total", "general", "général",
})


@dataclass(frozen=True)
class CatalogColumn:
    column_id: UUID
    original_name: str
    normalized_name: str
    position: int
    inferred_type: str
    unit: str = ""
    null_ratio: float = 0.0


@dataclass(frozen=True)
class CatalogTable:
    table_id: UUID
    sheet_id: UUID
    sheet_name: str
    data_range: str
    header_range: str
    row_count: int
    column_count: int
    columns: tuple[CatalogColumn, ...]
    #: Vrai si au moins une cellule de la table porte une formule sans valeur en cache.
    has_uncached_formula: bool = False
    #: Vrai si la table porte des formules (toutes n'ont pas forcément de cache).
    has_formulas: bool = False

    def column_by_id(self, column_id: UUID) -> CatalogColumn | None:
        return next((c for c in self.columns if c.column_id == column_id), None)

    def positions(self) -> dict[UUID, int]:
        return {c.column_id: c.position for c in self.columns}


@dataclass
class DocumentCatalog:
    document_id: UUID
    content_sha256: str
    filename: str
    tables: list[CatalogTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table_by_id(self, table_id: UUID) -> CatalogTable | None:
        return next((t for t in self.tables if t.table_id == table_id), None)


def _is_summary_row(cells: list[Any]) -> bool:
    for c in cells[:2]:
        if isinstance(c, str) and c.strip().lower().strip(":") in SUMMARY_LABELS:
            return True
    return False


def _column_letter(i: int) -> str:
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _build_columns(headers: list[str], rows: list[list[Any]],
                   formats: list[str], *, scope: str) -> tuple[CatalogColumn, ...]:
    out = []
    for pos, raw in enumerate(headers):
        sample = [r[pos] if pos < len(r) else None for r in rows[:TYPE_SAMPLE_ROWS]]
        ctype, unit = T.infer_column_type(
            sample, number_format=formats[pos] if pos < len(formats) else "")
        non_empty = sum(1 for v in sample if not T.is_empty(v))
        out.append(CatalogColumn(
            column_id=_stable_id(scope, "col", str(pos), T.normalize_header(str(raw))),
            original_name=str(raw).strip(),
            normalized_name=T.normalize_header(str(raw)),
            position=pos,
            inferred_type=ctype,
            unit=unit,
            null_ratio=round(1.0 - (non_empty / len(sample)), 4) if sample else 0.0,
        ))
    return tuple(out)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def build_csv_catalog(data: bytes, *, document_id: UUID, content_sha256: str,
                      filename: str) -> DocumentCatalog:
    """Un CSV = une feuille, une table. Le délimiteur est détecté, jamais supposé."""
    text, warnings = _decode(data)
    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    all_rows = [r for r in reader]
    if not all_rows:
        raise E.TabularError(E.ERR_TABLE_NOT_FOUND, "fichier CSV vide")

    headers = [h.strip() for h in all_rows[0]]
    body = [r for r in all_rows[1:] if any(str(c).strip() for c in r)]
    body = [r for r in body if not _is_summary_row(r)]

    scope = f"{content_sha256}|{filename}"
    sheet_id = _stable_id(scope, "sheet", filename)
    columns = _build_columns(headers, body, [""] * len(headers), scope=scope)
    last = _column_letter(max(0, len(headers) - 1))
    table = CatalogTable(
        table_id=_stable_id(scope, "table", filename, "A1"),
        sheet_id=sheet_id, sheet_name=filename,
        data_range=f"A2:{last}{len(body) + 1}",
        header_range=f"A1:{last}1",
        row_count=len(body), column_count=len(headers), columns=columns,
    )
    return DocumentCatalog(document_id=document_id, content_sha256=content_sha256,
                           filename=filename, tables=[table], warnings=warnings)


def _decode(data: bytes) -> tuple[str, list[str]]:
    """Décode en préservant l'information : un BOM et un CP1252 sont signalés."""
    warnings: list[str] = []
    if data.startswith(b"\xef\xbb\xbf"):
        warnings.append("csv_utf8_bom")
        return data[3:].decode("utf-8", errors="replace"), warnings
    try:
        return data.decode("utf-8"), warnings
    except UnicodeDecodeError:
        warnings.append("csv_encoding_fallback_cp1252")
        return data.decode("cp1252", errors="replace"), warnings


def _sniff_delimiter(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        # Repli déterministe : le séparateur le plus fréquent sur l'en-tête.
        first = head.splitlines()[0] if head.splitlines() else ""
        return max(",;\t|", key=first.count)


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #
def build_xlsx_catalog(data: bytes, *, document_id: UUID, content_sha256: str,
                       filename: str) -> DocumentCatalog:
    """Inventorie les tables d'un classeur, sans matérialiser les données.

    Double lecture assumée : `data_only=True` donne les valeurs mises en cache,
    `data_only=False` révèle les cellules PORTEUSES de formule. Sans la seconde, une
    formule non recalculée serait indiscernable d'une cellule vide et fausserait
    silencieusement toute somme.
    """
    from openpyxl import load_workbook

    wb_values = load_workbook(io.BytesIO(data), read_only=True, data_only=True,
                              keep_links=False, rich_text=False)
    wb_formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False,
                                keep_links=False, rich_text=False)
    try:
        tables: list[CatalogTable] = []
        for name in wb_values.sheetnames:
            ws = wb_values[name]
            if not hasattr(ws, "iter_rows"):      # feuille-graphique
                continue
            ws.reset_dimensions()
            grid = [list(r) for r in ws.iter_rows(values_only=True)]
            if not any(any(c is not None for c in r) for r in grid):
                continue

            header_idx = _find_header_row(grid)
            if header_idx is None:
                continue
            headers = [str(c).strip() if c is not None else ""
                       for c in grid[header_idx]]
            width = max((i + 1 for i, h in enumerate(headers) if h), default=0)
            if width == 0:
                continue
            headers = headers[:width]

            body = []
            for row in grid[header_idx + 1:]:
                cells = list(row[:width])
                if not any(c is not None and str(c).strip() for c in cells):
                    continue
                if _is_summary_row(cells):
                    continue
                body.append(cells)
            if not body:
                continue

            has_f, has_uncached = _scan_formulas(wb_formulas, name, header_idx, width)

            scope = f"{content_sha256}|{name}|{header_idx}"
            sheet_id = _stable_id(content_sha256, "sheet", name)
            last = _column_letter(width - 1)
            tables.append(CatalogTable(
                table_id=_stable_id(scope, "table"),
                sheet_id=sheet_id, sheet_name=name,
                data_range=f"A{header_idx + 2}:{last}{header_idx + 1 + len(body)}",
                header_range=f"A{header_idx + 1}:{last}{header_idx + 1}",
                row_count=len(body), column_count=width,
                columns=_build_columns(headers, body, [""] * width, scope=scope),
                has_formulas=has_f, has_uncached_formula=has_uncached,
            ))

        if not tables:
            raise E.TabularError(
                E.ERR_TABLE_NOT_FOUND, "aucune table exploitable dans ce classeur")
        return DocumentCatalog(document_id=document_id, content_sha256=content_sha256,
                               filename=filename, tables=tables)
    finally:
        for wb in (wb_values, wb_formulas):
            try:
                wb.close()
            except Exception:  # pragma: no cover - fermeture best-effort
                logger.debug("tabular: fermeture de classeur en échec")


def _find_header_row(grid: list[list[Any]]) -> int | None:
    """Première ligne majoritairement textuelle suivie d'au moins une ligne de données."""
    for i, row in enumerate(grid[:20]):
        filled = [c for c in row if c is not None and str(c).strip()]
        if len(filled) < 2:
            continue
        textual = sum(1 for c in filled if isinstance(c, str))
        if textual >= max(1, int(len(filled) * 0.6)) and i + 1 < len(grid):
            return i
    return None


def _scan_formulas(wb_formulas, sheet_name: str, header_idx: int,
                   width: int) -> tuple[bool, bool]:
    """(porte des formules, porte une formule SANS valeur en cache) pour la table."""
    try:
        ws = wb_formulas[sheet_name]
    except KeyError:  # pragma: no cover
        return False, False
    if not hasattr(ws, "iter_rows"):
        return False, False
    ws.reset_dimensions()
    has_formula = False
    for r_i, row in enumerate(ws.iter_rows(values_only=True)):
        if r_i <= header_idx:
            continue
        for c in list(row[:width]):
            if isinstance(c, str) and c.startswith("="):
                has_formula = True
                break
        if has_formula:
            break
    return has_formula, has_formula


def load_rows(data: bytes, table: CatalogTable, *, is_csv: bool,
              max_rows: int, deadline=None) -> list[tuple[Any, ...]]:
    """Charge et TYPE les lignes de la table. Lève plutôt que de convertir au hasard.

    C'est le seul endroit où les données brutes deviennent des valeurs typées. Une
    formule sans valeur en cache y est détectée : elle vaut `None` côté openpyxl, donc
    indiscernable d'une cellule vide — l'accepter fausserait la somme en silence.
    """
    if is_csv:
        text, _ = _decode(data)
        reader = csv.reader(io.StringIO(text), delimiter=_sniff_delimiter(text))
        raw = [r for r in reader][1:]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True,
                           keep_links=False, rich_text=False)
        try:
            ws = wb[table.sheet_name]
            ws.reset_dimensions()
            header_row = int(table.header_range.split(":")[1].lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
            raw = [list(r) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)
                   if i > header_row]
        finally:
            try:
                wb.close()
            except Exception:  # pragma: no cover
                logger.debug("tabular: fermeture de classeur en échec")

    width = table.column_count
    out: list[tuple[Any, ...]] = []
    for cells in raw:
        cells = list(cells[:width]) + [None] * max(0, width - len(cells))
        if not any(c is not None and str(c).strip() for c in cells):
            continue
        if _is_summary_row(cells):
            continue
        if len(out) >= max_rows:
            raise E.TabularLimitExceeded(
                E.ERR_TOO_MANY_ROWS,
                f"la table dépasse {max_rows} lignes exploitables")
        typed = []
        for col in table.columns:
            typed.append(T.coerce(cells[col.position], col.inferred_type))
        out.append(tuple(typed))
        # Le typage est une boucle Python sur chaque cellule : sans contrôle, un gros
        # fichier consommerait tout le budget avant même d'atteindre DuckDB.
        if deadline is not None and len(out) % 20_000 == 0:
            deadline.raise_if_expired(phase="read_and_type")
    return out

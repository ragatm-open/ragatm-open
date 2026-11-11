"""Exécuteur tabulaire de **référence** — calcule la vérité terrain d'agrégation.

C'est l'organe critique du benchmark : si ce module se trompe, tous les verdicts sont
faux. Il est donc construit sur trois partis pris.

**1. Le calcul est pur, le chargement est isolé.** ``execute()`` ne connaît qu'une
``LoadedTable`` en mémoire ; ``load_table()`` seul touche openpyxl. Les opérations sont
donc testables exhaustivement sans classeur, et la vérité terrain peut être recalculée
par un second chemin indépendant (double calcul, §41).

**2. La table vient du classeur, jamais des chunks.** Un chunk peut tronquer des lignes,
répéter un en-tête, perdre un type ou fragmenter un tableau. Une agrégation calculée sur
des chunks serait une agrégation sur un artefact du découpage. ``load_table`` lit les
octets du fichier source ; la vérité terrain est ainsi **indépendante du chunking**.

**3. Toute ambiguïté est un refus.** Deux colonnes homonymes, une colonne de type mixte,
une égalité en tête d'un ``argmax`` : aucun de ces cas n'est tranché par un ``first()``
ou un ``[0]``. Le cas est rejeté avec un code stable. Un benchmark qui devine produit
des mesures indéfendables.

Aucun LLM, aucun retrieval, aucun score vectoriel, aucun accès base.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.rag_core.benchmark import tabular_aggregation_schema as sch
from apps.rag_core.benchmark import tabular_types as tt
from apps.rag_core.benchmark.stable_ground_truth import parse_cell_range, sha256_hex

#: Sentinelle : cellule portant une formule dont Excel n'a pas mis la valeur en cache.
#: Indiscernable d'une cellule vide au niveau valeur — d'où la double lecture (§20).
FORMULA_NO_CACHE = object()


class TabularExecutionError(ValueError):
    """Échec d'exécution de référence. Porte un ``code`` stable et exposable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Représentation en mémoire
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableRow:
    """Une ligne de données, indexée par en-tête normalisé."""

    row_number: int
    values: dict[str, Any]

    def get(self, normalized_header: str) -> Any:
        return self.values.get(normalized_header)


@dataclass
class LoadedTable:
    """Table structurée, prête au calcul et **détachée de toute source**."""

    sheet_name: str
    headers: tuple[str, ...]                      # en-têtes source, ordre des colonnes
    rows: list[TableRow] = field(default_factory=list)
    column_index: dict[str, int] = field(default_factory=dict)   # normalisé -> index Excel
    number_formats: dict[str, str] = field(default_factory=dict)  # normalisé -> format

    @property
    def normalized_headers(self) -> list[str]:
        return [sch.normalize_header(h) for h in self.headers]

    def duplicated_headers(self) -> set[str]:
        seen, dupes = set(), set()
        for h in self.normalized_headers:
            (dupes if h in seen else seen).add(h)
        return dupes


@dataclass(frozen=True)
class AggregationResult:
    value: Any
    result_type: str
    selected_row_count: int
    evidence: sch.AggregationEvidence
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Résolution de colonnes — aucune sélection arbitraire (§52)
# --------------------------------------------------------------------------- #
def resolve_column(table: LoadedTable, column: sch.ColumnRef, *, code: str) -> str:
    """Renvoie l'en-tête normalisé d'une colonne, ou lève si absente/ambiguë."""
    key = column.normalized_header
    if key in table.duplicated_headers():
        raise TabularExecutionError(
            code, f"en-tête {column.source_header!r} présent plusieurs fois — jamais arbitré")
    if key not in table.normalized_headers:
        raise TabularExecutionError(
            code, f"colonne {column.source_header!r} absente de la table")
    return key


def infer_column_type(table: LoadedTable, normalized_header: str) -> str:
    """Type d'une colonne, déduit de ses cellules non vides.

    Un mélange numérique (entiers + décimaux) est unifié en ``decimal``. Tout autre
    mélange rend la colonne inexploitable : ``ambiguous_column_type``.
    """
    fmt = table.number_formats.get(normalized_header, "")
    seen: set[str] = set()
    for row in table.rows:
        value = row.get(normalized_header)
        if value is FORMULA_NO_CACHE:
            raise TabularExecutionError(
                sch.ERR_FORMULA_RESULT_UNAVAILABLE,
                f"ligne {row.row_number} : formule sans valeur en cache")
        if tt.is_empty(value):
            continue
        seen.add(tt.infer_cell_type(value, number_format=fmt))
    if not seen:
        return tt.TYPE_EMPTY
    if len(seen) == 1:
        return seen.pop()
    if seen <= {tt.TYPE_INTEGER, tt.TYPE_DECIMAL}:
        return tt.TYPE_DECIMAL
    if seen <= {tt.TYPE_DATE, tt.TYPE_DATETIME}:
        return tt.TYPE_DATETIME
    raise TabularExecutionError(
        sch.ERR_UNSUPPORTED_CELL_TYPE,
        f"colonne {normalized_header!r} de types mixtes : {sorted(seen)}")


def column_types(table: LoadedTable) -> dict[str, str]:
    return {h: infer_column_type(table, h) for h in table.normalized_headers}


# --------------------------------------------------------------------------- #
# Population de lignes (§12)
# --------------------------------------------------------------------------- #
def build_population(table: LoadedTable, locator: sch.TableLocator,
                     policy: sch.RowPopulationPolicy) -> list[TableRow]:
    """Lignes réellement agrégées : bornes du locator, moins les exclusions.

    Les lignes de synthèse (TOTAL, SOUS-TOTAL…) sont retirées : les inclure
    **compterait deux fois** les mêmes montants.
    """
    excluded = set(locator.excluded_rows) | set(locator.footer_rows)
    population: list[TableRow] = []
    for row in table.rows:
        if not locator.data_row_start <= row.row_number <= locator.data_row_end:
            continue
        if row.row_number in excluded:
            continue
        values = list(row.values.values())
        if policy.ignore_empty_rows and all(tt.is_empty(v) for v in values):
            continue
        if any(not tt.is_empty(v) and policy.is_excluded_label(v) for v in values):
            continue
        population.append(row)
    return population


# --------------------------------------------------------------------------- #
# Filtres (§13)
# --------------------------------------------------------------------------- #
def _comparable(value: Any, column_type: str, locale: str | None):
    """Valeur ordonnable pour une comparaison, ou ``None`` si non comparable."""
    if tt.is_empty(value):
        return None
    if column_type in tt.NUMERIC_TYPES:
        return tt.normalize_decimal(value)
    if column_type in (tt.TYPE_DATE, tt.TYPE_DATETIME):
        return tt.normalize_date(value, locale=locale)   # ISO -> ordre lexical = ordre temporel
    # Texte : repli de casse, par cohérence avec `contains` et l'exclusion des libellés
    # de synthèse. « En cours » et « EN COURS » désignent le même statut métier.
    return tt.fold_for_match(value)


def _matches(row: TableRow, f: sch.Filter, *, key: str, column_type: str,
             locale: str | None) -> bool:
    raw = row.get(key)
    op = f.operator
    if op == sch.F_IS_EMPTY:
        return tt.is_empty(raw)
    if op == sch.F_IS_NOT_EMPTY:
        return not tt.is_empty(raw)
    if tt.is_empty(raw):
        return False        # une cellule vide ne satisfait aucun prédicat de valeur

    if op in (sch.F_CONTAINS, sch.F_NOT_CONTAINS):
        hit = tt.fold_for_match(f.value) in tt.fold_for_match(raw)
        return hit if op == sch.F_CONTAINS else not hit

    left = _comparable(raw, column_type, locale)
    if op in (sch.F_EQ, sch.F_NEQ):
        right = _comparable(f.value, column_type, locale)
        hit = left == right
        return hit if op == sch.F_EQ else not hit
    if op in (sch.F_IN, sch.F_NOT_IN):
        rights = {_comparable(v, column_type, locale) for v in f.values}
        hit = left in rights
        return hit if op == sch.F_IN else not hit
    if op == sch.F_BETWEEN:
        lo, hi = (_comparable(v, column_type, locale) for v in f.values)
        if lo > hi:
            raise TabularExecutionError(sch.ERR_INVALID_FILTER, "between : bornes inversées")
        return lo <= left <= hi

    right = _comparable(f.value, column_type, locale)
    if op in (sch.F_LT, sch.F_BEFORE):
        return left < right
    if op in (sch.F_GT, sch.F_AFTER):
        return left > right
    if op == sch.F_LTE:
        return left <= right
    if op == sch.F_GTE:
        return left >= right
    raise TabularExecutionError(sch.ERR_UNSUPPORTED_FILTER_OPERATOR, op)


def apply_filters(table: LoadedTable, population: list[TableRow],
                  filters: tuple[sch.Filter, ...], *, types: dict[str, str],
                  locale: str | None) -> list[TableRow]:
    """Conjonction des prédicats : une ligne est retenue si **tous** matchent."""
    selected = population
    for f in filters:
        key = resolve_column(table, f.column, code=sch.ERR_AMBIGUOUS_FILTER_COLUMN)
        ctype = types[key]
        f.validate(column_type=ctype)
        selected = [r for r in selected
                    if _matches(r, f, key=key, column_type=ctype, locale=locale)]
    return selected


# --------------------------------------------------------------------------- #
# Agrégation (§6)
# --------------------------------------------------------------------------- #
def _numeric_values(rows: list[TableRow], key: str, policy: sch.MissingValuePolicy):
    """Valeurs numériques exploitables, selon la politique de valeurs manquantes."""
    values: list[Decimal] = []
    for row in rows:
        raw = row.get(key)
        if raw is FORMULA_NO_CACHE:
            raise TabularExecutionError(
                sch.ERR_FORMULA_RESULT_UNAVAILABLE, f"ligne {row.row_number}")
        if tt.is_empty(raw):
            if policy.on_missing == sch.MISSING_REJECT:
                raise TabularExecutionError(
                    sch.ERR_UNSUPPORTED_CELL_TYPE,
                    f"ligne {row.row_number} : valeur manquante et politique = reject")
            continue
        values.append(tt.normalize_decimal(raw))
    return values


def _extreme(rows: list[TableRow], key: str, policy: sch.MissingValuePolicy, *,
             largest: bool) -> tuple[Decimal, list[TableRow]]:
    """Valeur extrême **et** lignes qui la portent — pour détecter les ex aequo.

    La garde sur les formules sans valeur en cache duplique celle du typage, qui lève
    déjà en amont. C'est délibéré : ``min``/``max``/``argmin``/``argmax`` sont un chemin
    de vérité terrain, et un marqueur qui y parviendrait serait converti en nombre par
    ``normalize_decimal`` — donc signalé sous un code trompeur au lieu du bon.
    """
    best: Decimal | None = None
    holders: list[TableRow] = []
    for row in rows:
        raw = row.get(key)
        if raw is FORMULA_NO_CACHE:
            raise TabularExecutionError(
                sch.ERR_FORMULA_RESULT_UNAVAILABLE,
                f"ligne {row.row_number} : formule sans valeur en cache")
        if tt.is_empty(raw):
            if policy.on_missing == sch.MISSING_REJECT:
                raise TabularExecutionError(
                    sch.ERR_UNSUPPORTED_CELL_TYPE, f"ligne {row.row_number} : valeur manquante")
            continue
        val = tt.normalize_decimal(raw)
        if best is None or (val > best if largest else val < best):
            best, holders = val, [row]
        elif val == best:
            holders.append(row)
    if best is None:
        raise TabularExecutionError(sch.ERR_EMPTY_SELECTION, "aucune valeur numérique")
    return best, holders


def execute(table: LoadedTable, gt: sch.TabularAggregationGroundTruth) -> AggregationResult:
    """Calcule le résultat de référence. **Pur** : aucune I/O, aucun aléa."""
    types = column_types(table)
    population = build_population(table, gt.table_locator, gt.row_population_policy)
    selected = apply_filters(table, population, gt.filters, types=types, locale=gt.locale)

    op = gt.aggregation.function
    policy = gt.missing_value_policy
    warnings: list[str] = []

    if op in (sch.OP_COUNT_ROWS, sch.OP_FILTERED_COUNT):
        if not policy.count_empty_as_row and gt.aggregation.target_column is not None:
            key = resolve_column(table, gt.aggregation.target_column,
                                 code=sch.ERR_AMBIGUOUS_TARGET_COLUMN)
            selected = [r for r in selected if not tt.is_empty(r.get(key))]
        value: Any = len(selected)
        result_type = sch.RESULT_INTEGER
        used: list[Any] = []
    else:
        if not selected:
            raise TabularExecutionError(
                sch.ERR_EMPTY_SELECTION, "sélection vide : aucune ligne à agréger")
        key = resolve_column(table, gt.aggregation.target_column,
                             code=sch.ERR_AMBIGUOUS_TARGET_COLUMN)
        ctype = types[key]
        if ctype not in tt.NUMERIC_TYPES:
            raise TabularExecutionError(
                sch.ERR_UNSUPPORTED_CELL_TYPE,
                f"colonne cible {key!r} de type {ctype} : agrégation arithmétique impossible")

        if op in (sch.OP_SUM, sch.OP_FILTERED_SUM):
            used = _numeric_values(selected, key, policy)
            if not used:
                raise TabularExecutionError(sch.ERR_EMPTY_SELECTION, "aucune valeur à sommer")
            value, result_type = sum(used, Decimal(0)), sch.RESULT_DECIMAL
        elif op == sch.OP_AVERAGE:
            used = _numeric_values(selected, key, policy)
            if not used:
                raise TabularExecutionError(sch.ERR_EMPTY_SELECTION, "aucune valeur à moyenner")
            # Dénominateur = valeurs numériques valides, jamais le nombre de lignes brut.
            value = sum(used, Decimal(0)) / Decimal(len(used))
            result_type = sch.RESULT_DECIMAL
            if len(used) != len(selected):
                warnings.append(f"moyenne sur {len(used)}/{len(selected)} lignes non vides")
        elif op in (sch.OP_MINIMUM, sch.OP_MAXIMUM):
            value, holders = _extreme(selected, key, policy, largest=(op == sch.OP_MAXIMUM))
            used = [value]
            result_type = sch.RESULT_DECIMAL
            if len(holders) > 1:
                warnings.append(f"{len(holders)} lignes portent la valeur extrême")
        elif op in (sch.OP_ARGMIN, sch.OP_ARGMAX):
            extreme, holders = _extreme(selected, key, policy, largest=(op == sch.OP_ARGMAX))
            if len(holders) > 1:
                # Ex aequo : « quelle ligne » n'a pas de réponse unique. Refus (§52).
                raise TabularExecutionError(
                    sch.ERR_AMBIGUOUS_ROW_POPULATION,
                    f"{len(holders)} lignes ex aequo à {extreme} : argmin/argmax indéterminé")
            row = holders[0]
            ident: dict[str, Any] = {}
            for col in gt.aggregation.identity_columns:
                ikey = resolve_column(table, col, code=sch.ERR_MISSING_IDENTITY_COLUMN)
                ident[col.source_header] = tt.normalize_text(row.get(ikey) or "")
            value, result_type, used = ident, sch.RESULT_ROW_IDENTIFIER, [extreme]
        else:
            raise TabularExecutionError(sch.ERR_UNSUPPORTED_OPERATION, op)

    return AggregationResult(
        value=value, result_type=result_type, selected_row_count=len(selected),
        evidence=build_evidence(selected, used, value),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Preuve d'exécution (§22)
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return json.dumps({k: _canonical(v) for k, v in sorted(value.items())},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def build_evidence(selected: list[TableRow], used_values: list[Any],
                   result: Any) -> sch.AggregationEvidence:
    """Hachages permettant de rejouer le calcul sans stocker les valeurs sensibles."""
    keys = ",".join(str(r.row_number) for r in selected)
    vals = ",".join(_canonical(v) for v in used_values)
    return sch.AggregationEvidence(
        selected_row_count=len(selected),
        selected_row_keys_sha256=sha256_hex(keys),
        normalized_input_values_sha256=sha256_hex(vals),
        result_sha256=sha256_hex(_canonical(result)),
    )


# --------------------------------------------------------------------------- #
# Chargement depuis le classeur (§18-19) — seule partie qui fait de l'I/O
# --------------------------------------------------------------------------- #
class WorkbookCache:
    """Garde ouverts les classeurs récemment lus (§53).

    Un document porte souvent des dizaines de tables ; sans cache, chacune rouvrirait
    **deux fois** le même fichier — jusqu'à plusieurs mégaoctets. Le cache est borné et
    évince le plus ancien : un corpus entier ne tient pas en mémoire.
    """

    def __init__(self, max_documents: int = 2):
        self.max_documents = max_documents
        self._open: dict[str, tuple[Any, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, path: Path) -> tuple[Any, Any]:
        from openpyxl import load_workbook

        key = str(path)
        if key in self._open:
            self.hits += 1
            return self._open[key]
        self.misses += 1
        while len(self._open) >= self.max_documents:
            old_key = next(iter(self._open))
            self.close(old_key)
        pair = (load_workbook(path, data_only=True, read_only=False),
                load_workbook(path, data_only=False, read_only=False))
        self._open[key] = pair
        return pair

    def close(self, key: str) -> None:
        for wb in self._open.pop(key, ()):
            try:
                wb.close()
            except Exception:       # noqa: BLE001 — fermer au mieux, ne jamais masquer un résultat
                pass

    def close_all(self) -> None:
        for key in list(self._open):
            self.close(key)


def load_table(path: str | Path, locator: sch.TableLocator,
               cache: WorkbookCache | None = None) -> LoadedTable:
    """Construit une ``LoadedTable`` depuis les **octets** du classeur.

    Double lecture assumée : ``data_only=True`` donne les valeurs mises en cache par
    Excel, ``data_only=False`` révèle quelles cellules portent une formule. Sans cette
    seconde passe, une formule non recalculée serait indiscernable d'une cellule vide
    et produirait une vérité terrain silencieusement fausse.

    ``read_only`` est volontairement évité : il ne donne ni cellules fusionnées ni
    formats de nombre, dont dépendent la détection des pourcentages et la population.

    Passer un ``cache`` évite de rouvrir le même classeur pour chacune de ses tables.
    """
    from openpyxl import load_workbook

    path = Path(path)
    c1, r1, c2, r2 = parse_cell_range(locator.table_range)
    owned = cache is None

    if cache is not None:
        wb_values, wb_formulas = cache.get(path)
    else:
        wb_values = load_workbook(path, data_only=True, read_only=False)
        wb_formulas = load_workbook(path, data_only=False, read_only=False)
    try:
        if locator.sheet_name not in wb_values.sheetnames:
            raise TabularExecutionError(
                sch.ERR_INVALID_TABLE_LOCATOR, f"feuille {locator.sheet_name!r} absente")
        ws = wb_values[locator.sheet_name]
        headers, formats = _read_headers(ws, locator, c1, c2)
        rows = _read_rows(ws, locator, headers, c1, c2)
        _mark_uncached_formulas(wb_formulas, locator, headers, rows, c1)
    finally:
        if owned:
            wb_values.close()
            wb_formulas.close()

    # Les en-têtes homonymes ne sont pas fatals en soi : ils ne le deviennent que si le
    # cas référence l'un d'eux (`resolve_column` refuse alors, sans jamais arbitrer).
    return LoadedTable(
        sheet_name=locator.sheet_name,
        headers=tuple(headers),
        rows=rows,
        column_index={sch.normalize_header(h): c1 + i for i, h in enumerate(headers) if h},
        number_formats=formats,
    )


def _read_headers(ws, locator: sch.TableLocator, c1: int, c2: int) -> tuple[list[str], dict]:
    """En-têtes concaténés sur toutes les ``header_rows`` (en-têtes multi-lignes)."""
    parts: dict[int, list[str]] = {c: [] for c in range(c1, c2 + 1)}
    for hr in locator.header_rows:
        for c in range(c1, c2 + 1):
            val = ws.cell(row=hr, column=c).value
            if not tt.is_empty(val):
                parts[c].append(tt.normalize_text(val))
    headers = [" ".join(parts[c]).strip() for c in range(c1, c2 + 1)]
    formats: dict[str, str] = {}
    probe_row = min(locator.data_row_start, locator.data_row_end)
    for i, h in enumerate(headers):
        if h:
            formats[sch.normalize_header(h)] = ws.cell(row=probe_row, column=c1 + i).number_format or ""
    return headers, formats


def _read_rows(ws, locator: sch.TableLocator, headers: list[str],
               c1: int, c2: int) -> list[TableRow]:
    keys = [sch.normalize_header(h) for h in headers]
    rows: list[TableRow] = []
    for r in range(locator.data_row_start, locator.data_row_end + 1):
        values: dict[str, Any] = {}
        for i, key in enumerate(keys):
            if not key:
                continue
            values[key] = ws.cell(row=r, column=c1 + i).value
        rows.append(TableRow(row_number=r, values=values))
    return rows


def _mark_uncached_formulas(wb_formulas: Any, locator: sch.TableLocator,
                            headers: list[str], rows: list[TableRow], c1: int) -> None:
    """Repère les formules **sans** valeur en cache et les marque explicitement (§20)."""
    ws = wb_formulas[locator.sheet_name]
    keys = [sch.normalize_header(h) for h in headers]
    by_number = {r.row_number: r for r in rows}
    for r in range(locator.data_row_start, locator.data_row_end + 1):
        row = by_number.get(r)
        if row is None:
            continue
        for i, key in enumerate(keys):
            if not key:
                continue
            formula = ws.cell(row=r, column=c1 + i).value
            if isinstance(formula, str) and formula.startswith("=") and tt.is_empty(row.values.get(key)):
                row.values[key] = FORMULA_NO_CACHE


def build_table(headers: list[str], data: list[list[Any]], *, sheet_name: str = "Feuille1",
                first_data_row: int = 2, number_formats: dict[str, str] | None = None
                ) -> LoadedTable:
    """Fabrique une ``LoadedTable`` en mémoire — fixtures de test et double calcul."""
    keys = [sch.normalize_header(h) for h in headers]
    rows = [TableRow(row_number=first_data_row + i,
                     values={k: v for k, v in zip(keys, line) if k})
            for i, line in enumerate(data)]
    return LoadedTable(
        sheet_name=sheet_name, headers=tuple(headers), rows=rows,
        column_index={k: i + 1 for i, k in enumerate(keys) if k},
        number_formats=number_formats or {},
    )

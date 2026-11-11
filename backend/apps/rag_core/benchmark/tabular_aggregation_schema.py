"""Schéma versionné des cas de benchmark Excel `tabular_aggregation`.

Un cas `retrieval_lookup` demande : *« la bonne source a-t-elle été retrouvée ? »*.
Un cas `tabular_aggregation` demande : *« le bon résultat a-t-il été calculé ? »*.

La différence n'est pas cosmétique : la réponse **n'existe dans aucune cellule**. Elle
est produite par une opération sur une **population de lignes** explicitement définie.
La vérité terrain doit donc décrire, de façon reproductible et indépendante du
découpage en chunks :

1. **quel document** (SHA-256 du fichier, jamais un UUID d'ingestion) ;
2. **quelle table** (`table_locator` : feuille, plage, en-têtes, bornes de données) ;
3. **quelles lignes** (politique de population + filtres typés) ;
4. **quelle opération** (taxonomie fermée) ;
5. **quel résultat attendu**, avec son type, sa normalisation et sa preuve.

Ce module ne calcule rien : il **décrit et valide**. Le calcul vit dans
``tabular_reference_executor``. Aucun LLM, aucun retrieval, aucun accès base.

Réutilisation assumée du lot v2 (``stable_ground_truth``) pour l'identité documentaire
et les coordonnées Excel : un seul endroit où vit la logique de colonnes/plages.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from apps.rag_core.benchmark import tabular_types as tt
from apps.rag_core.benchmark.stable_ground_truth import (
    StableDocumentRef,
    col_to_num,
    is_valid_sha256,
    normalize_cell_range,
    normalize_excel_column,
    normalize_sheet_name,
    parse_cell_range,
    sha256_hex,
)

TABULAR_AGGREGATION_SCHEMA_VERSION = 1
TASK_TABULAR_AGGREGATION = "tabular_aggregation"

# --- Taxonomie des opérations (§6) -----------------------------------------
OP_COUNT_ROWS = "count_rows"
OP_FILTERED_COUNT = "filtered_count"
OP_SUM = "sum"
OP_FILTERED_SUM = "filtered_sum"
OP_AVERAGE = "average"
OP_MINIMUM = "minimum"
OP_MAXIMUM = "maximum"
OP_ARGMIN = "argmin"
OP_ARGMAX = "argmax"
OP_GROUPED_AGGREGATION = "grouped_aggregation"
OP_COMPARISON = "comparison"

#: Périmètre du MVP (§7). Le reste est **reconnu mais refusé** : un cas hors périmètre
#: doit être explicitement redirigé, jamais silencieusement traité comme autre chose.
SUPPORTED_OPERATIONS = frozenset({
    OP_COUNT_ROWS, OP_FILTERED_COUNT, OP_SUM, OP_FILTERED_SUM, OP_AVERAGE,
    OP_MINIMUM, OP_MAXIMUM, OP_ARGMIN, OP_ARGMAX,
})
DEFERRED_OPERATIONS = frozenset({OP_GROUPED_AGGREGATION, OP_COMPARISON})
ALL_OPERATIONS = SUPPORTED_OPERATIONS | DEFERRED_OPERATIONS

#: Opérations exigeant une colonne cible typée.
_NEEDS_TARGET_COLUMN = frozenset({
    OP_SUM, OP_FILTERED_SUM, OP_AVERAGE, OP_MINIMUM, OP_MAXIMUM, OP_ARGMIN, OP_ARGMAX,
})
#: Opérations exigeant au moins un filtre (sinon elles sont leur variante non filtrée).
_NEEDS_FILTERS = frozenset({OP_FILTERED_COUNT, OP_FILTERED_SUM})
#: Opérations exigeant une colonne d'identité (le « arg » de argmin/argmax).
_NEEDS_IDENTITY = frozenset({OP_ARGMIN, OP_ARGMAX})
#: Opérations dont le résultat est un entier.
_INTEGER_RESULT = frozenset({OP_COUNT_ROWS, OP_FILTERED_COUNT})

# --- Filtres (§13) ---------------------------------------------------------
F_EQ, F_NEQ = "eq", "neq"
F_LT, F_LTE, F_GT, F_GTE = "lt", "lte", "gt", "gte"
F_CONTAINS, F_NOT_CONTAINS = "contains", "not_contains"
F_IN, F_NOT_IN = "in", "not_in"
F_IS_EMPTY, F_IS_NOT_EMPTY = "is_empty", "is_not_empty"
F_BEFORE, F_AFTER, F_BETWEEN = "before", "after", "between"

FILTER_OPERATORS = frozenset({
    F_EQ, F_NEQ, F_LT, F_LTE, F_GT, F_GTE, F_CONTAINS, F_NOT_CONTAINS,
    F_IN, F_NOT_IN, F_IS_EMPTY, F_IS_NOT_EMPTY, F_BEFORE, F_AFTER, F_BETWEEN,
})
_UNARY_OPERATORS = frozenset({F_IS_EMPTY, F_IS_NOT_EMPTY})
_LIST_OPERATORS = frozenset({F_IN, F_NOT_IN})
_RANGE_OPERATORS = frozenset({F_BETWEEN})

#: Compatibilité opérateur -> types de colonne. Un `gt` sur du texte ou un `before`
#: sur un nombre est une **erreur de conception du cas**, pas une comparaison à tenter.
_OPERATOR_TYPES: dict[str, frozenset[str]] = {
    F_EQ: frozenset(tt.CELL_TYPES - {tt.TYPE_EMPTY}),
    F_NEQ: frozenset(tt.CELL_TYPES - {tt.TYPE_EMPTY}),
    F_CONTAINS: frozenset({tt.TYPE_STRING}),
    F_NOT_CONTAINS: frozenset({tt.TYPE_STRING}),
    F_IN: frozenset(tt.CELL_TYPES - {tt.TYPE_EMPTY}),
    F_NOT_IN: frozenset(tt.CELL_TYPES - {tt.TYPE_EMPTY}),
    F_LT: tt.NUMERIC_TYPES, F_LTE: tt.NUMERIC_TYPES,
    F_GT: tt.NUMERIC_TYPES, F_GTE: tt.NUMERIC_TYPES,
    F_BEFORE: frozenset({tt.TYPE_DATE, tt.TYPE_DATETIME}),
    F_AFTER: frozenset({tt.TYPE_DATE, tt.TYPE_DATETIME}),
    F_BETWEEN: tt.ORDERED_TYPES,
    F_IS_EMPTY: frozenset(tt.CELL_TYPES),
    F_IS_NOT_EMPTY: frozenset(tt.CELL_TYPES),
}

# --- Politique de population (§12) -----------------------------------------
INCLUDE_DATA_ROWS = "data_rows"
_INCLUDE_MODES = frozenset({INCLUDE_DATA_ROWS})
#: Libellés de synthèse : une ligne TOTAL n'est pas une donnée, et l'agréger la
#: compterait deux fois. Repliés casse/accents au moment de la comparaison.
DEFAULT_EXCLUDE_LABELS = ("total", "sous-total", "sous total", "cumul", "moyenne", "somme")

# --- Politique des valeurs manquantes (§29) --------------------------------
MISSING_IGNORE = "ignore"
MISSING_REJECT = "reject"
MISSING_COUNT_AS_ROW = "count_as_row"
_MISSING_POLICIES = frozenset({MISSING_IGNORE, MISSING_REJECT, MISSING_COUNT_AS_ROW})

# --- Comparaison du résultat (§30) -----------------------------------------
CMP_EXACT = "exact"
CMP_EXACT_DECIMAL = "exact_decimal"
CMP_ABSOLUTE_TOLERANCE = "absolute_tolerance"
CMP_RELATIVE_TOLERANCE = "relative_tolerance"
CMP_SET = "set_match"
CMP_ORDERED = "ordered_match"
_COMPARISONS = frozenset({
    CMP_EXACT, CMP_EXACT_DECIMAL, CMP_ABSOLUTE_TOLERANCE, CMP_RELATIVE_TOLERANCE,
    CMP_SET, CMP_ORDERED,
})

RESULT_INTEGER = "integer"
RESULT_DECIMAL = "decimal"
RESULT_DATE = "date"
RESULT_BOOLEAN = "boolean"
RESULT_STRING = "string"
RESULT_ROW_IDENTIFIER = "row_identifier"
_RESULT_TYPES = frozenset({
    RESULT_INTEGER, RESULT_DECIMAL, RESULT_DATE, RESULT_BOOLEAN, RESULT_STRING,
    RESULT_ROW_IDENTIFIER,
})

# --- Codes d'erreur STABLES (§51) ------------------------------------------
ERR_INVALID_SCHEMA_VERSION = "invalid_schema_version"
ERR_UNSUPPORTED_OPERATION = "unsupported_operation"
ERR_INVALID_TABLE_LOCATOR = "invalid_table_locator"
ERR_GENERIC_OR_INFERRED_HEADER = "generic_or_inferred_header"
ERR_AMBIGUOUS_TARGET_COLUMN = "ambiguous_target_column"
ERR_AMBIGUOUS_FILTER_COLUMN = "ambiguous_filter_column"
ERR_AMBIGUOUS_ROW_POPULATION = "ambiguous_row_population"
ERR_FORMULA_RESULT_UNAVAILABLE = "formula_result_unavailable"
ERR_UNSUPPORTED_CELL_TYPE = "unsupported_cell_type"
ERR_MIXED_UNITS = "mixed_units"
ERR_EMPTY_SELECTION = "empty_selection"
ERR_TRIVIAL_SINGLE_ROW = "trivial_single_row_aggregation"
ERR_MATERIALIZED_AGGREGATE = "materialized_aggregate_lookup"
ERR_CONSTANT_TARGET_COLUMN = "constant_target_column"
ERR_GT_COMPUTATION_MISMATCH = "ground_truth_computation_mismatch"
ERR_QUESTION_NOT_SELF_CONTAINED = "question_not_self_contained"
ERR_QUESTION_CONTAINS_ANSWER = "question_contains_answer"
ERR_DUPLICATE_CASE = "duplicate_aggregation_case"
ERR_INVALID_FILTER = "invalid_filter"
ERR_UNSUPPORTED_FILTER_OPERATOR = "unsupported_filter_operator"
ERR_INCOMPATIBLE_FILTER_TYPE = "incompatible_filter_type"
ERR_INVALID_EXPECTED_RESULT = "invalid_expected_result"
ERR_INVALID_DOCUMENT_SHA256 = "invalid_document_sha256"
ERR_UUID_FORBIDDEN = "uuid_forbidden"
ERR_MISSING_TARGET_COLUMN = "missing_target_column"
ERR_MISSING_IDENTITY_COLUMN = "missing_identity_column"
ERR_INVALID_MISSING_VALUE_POLICY = "invalid_missing_value_policy"

#: Toute apparition d'un UUID dans le cas canonique est un retour en arrière vers
#: l'identité par ingestion — exactement ce que le lot v2 a supprimé.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
#: En-têtes non exploitables (§11).
_GENERIC_HEADER_RE = re.compile(
    r"^(col(onne)?[\s_]*\d+|column[\s_]*\d*|unnamed[:\s_]*\d*|champ[\s_]*\d+|\d+|)$", re.I)


class TabularAggregationError(ValueError):
    """Erreur de schéma/validation d'un cas d'agrégation. Porte un ``code`` stable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Colonnes
# --------------------------------------------------------------------------- #
def is_generic_header(header: str) -> bool:
    """``col1`` / ``Unnamed: 0`` / vide : jamais une référence de colonne fiable."""
    return bool(_GENERIC_HEADER_RE.match((header or "").strip()))


def normalize_header(header: str) -> str:
    """Forme canonique d'un en-tête, servant de clé de rapprochement."""
    return tt.fold_for_match(header)


@dataclass(frozen=True)
class ColumnRef:
    """Référence de colonne par **nom canonique**, l'index n'étant que diagnostic (§11)."""

    source_header: str
    normalized_header: str = ""
    column_index: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "source_header", tt.normalize_text(self.source_header))
        object.__setattr__(self, "normalized_header",
                           self.normalized_header or normalize_header(self.source_header))

    def validate(self, *, code: str = ERR_AMBIGUOUS_TARGET_COLUMN) -> None:
        if not self.source_header:
            raise TabularAggregationError(code, "en-tête vide")
        if is_generic_header(self.source_header):
            raise TabularAggregationError(
                ERR_GENERIC_OR_INFERRED_HEADER, f"en-tête générique : {self.source_header!r}")
        if self.column_index is not None and self.column_index < 1:
            raise TabularAggregationError(code, "column_index doit être >= 1")

    def to_dict(self) -> dict[str, Any]:
        d = {"source_header": self.source_header, "normalized_header": self.normalized_header}
        if self.column_index is not None:
            d["column_index"] = self.column_index
        return d


# --------------------------------------------------------------------------- #
# Locator de table (§10)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableLocator:
    """Localise une **table**, pas un chunk : la population agrégée en dépend.

    ``table_range`` couvre en-têtes ET données ; ``data_row_start``/``data_row_end``
    délimitent la population. Cette séparation est ce qui rend la vérité terrain
    indépendante du découpage : deux chunkings différents décrivent la même table.
    """

    sheet_name: str
    table_range: str
    header_rows: tuple[int, ...]
    data_row_start: int
    data_row_end: int
    table_name: str = ""
    identity_columns: tuple[str, ...] = ()
    excluded_rows: tuple[int, ...] = ()
    footer_rows: tuple[int, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "sheet_name", normalize_sheet_name(self.sheet_name))
        object.__setattr__(self, "table_range", normalize_cell_range(self.table_range))
        object.__setattr__(self, "header_rows", tuple(sorted(set(self.header_rows))))
        object.__setattr__(self, "excluded_rows", tuple(sorted(set(self.excluded_rows))))
        object.__setattr__(self, "footer_rows", tuple(sorted(set(self.footer_rows))))
        object.__setattr__(self, "identity_columns",
                           tuple(tt.normalize_text(c) for c in self.identity_columns))

    def validate(self) -> None:
        if not self.sheet_name:
            raise TabularAggregationError(ERR_INVALID_TABLE_LOCATOR, "sheet_name vide")
        c1, r1, c2, r2 = parse_cell_range(self.table_range)
        if not self.header_rows:
            raise TabularAggregationError(ERR_INVALID_TABLE_LOCATOR, "header_rows vide")
        if self.data_row_start > self.data_row_end:
            raise TabularAggregationError(
                ERR_INVALID_TABLE_LOCATOR,
                f"data_row_start ({self.data_row_start}) > data_row_end ({self.data_row_end})")
        for row in (*self.header_rows, self.data_row_start, self.data_row_end):
            if not r1 <= row <= r2:
                raise TabularAggregationError(
                    ERR_INVALID_TABLE_LOCATOR,
                    f"ligne {row} hors de la plage {self.table_range}")
        if any(h >= self.data_row_start for h in self.header_rows):
            raise TabularAggregationError(
                ERR_INVALID_TABLE_LOCATOR, "un en-tête est dans la population de données")
        overlap = set(self.excluded_rows) | set(self.footer_rows)
        if any(not self.data_row_start <= row <= self.data_row_end for row in overlap):
            raise TabularAggregationError(
                ERR_INVALID_TABLE_LOCATOR, "exclusion hors de la population de données")

    @property
    def column_bounds(self) -> tuple[int, int]:
        c1, _, c2, _ = parse_cell_range(self.table_range)
        return c1, c2

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "sheet_name": self.sheet_name, "table_range": self.table_range,
            "header_rows": list(self.header_rows),
            "data_row_start": self.data_row_start, "data_row_end": self.data_row_end,
        }
        if self.table_name:
            d["table_name"] = self.table_name
        if self.identity_columns:
            d["identity_columns"] = list(self.identity_columns)
        if self.excluded_rows:
            d["excluded_rows"] = list(self.excluded_rows)
        if self.footer_rows:
            d["footer_rows"] = list(self.footer_rows)
        return d


@dataclass(frozen=True)
class RowPopulationPolicy:
    """Quelles lignes composent la population agrégée (§12)."""

    include_rows: str = INCLUDE_DATA_ROWS
    exclude_labels: tuple[str, ...] = DEFAULT_EXCLUDE_LABELS
    ignore_empty_rows: bool = True

    def validate(self) -> None:
        if self.include_rows not in _INCLUDE_MODES:
            raise TabularAggregationError(
                ERR_AMBIGUOUS_ROW_POPULATION, f"include_rows inconnu : {self.include_rows!r}")

    def is_excluded_label(self, value: Any) -> bool:
        folded = tt.fold_for_match(value or "")
        return any(folded == tt.fold_for_match(lbl) for lbl in self.exclude_labels)

    def to_dict(self) -> dict[str, Any]:
        return {"include_rows": self.include_rows,
                "exclude_labels": list(self.exclude_labels),
                "ignore_empty_rows": self.ignore_empty_rows}


@dataclass(frozen=True)
class MissingValuePolicy:
    """Traitement des cellules vides, par opération (§29).

    ``count_empty_as_row`` : une ligne dont la colonne cible est vide compte-t-elle
    dans un comptage ? ``on_missing`` : que faire d'une valeur absente dans une somme
    ou une moyenne — l'ignorer (dénominateur réduit) ou rejeter le cas.
    """

    on_missing: str = MISSING_IGNORE
    count_empty_as_row: bool = True

    def validate(self) -> None:
        if self.on_missing not in _MISSING_POLICIES:
            raise TabularAggregationError(
                ERR_INVALID_MISSING_VALUE_POLICY, f"on_missing inconnu : {self.on_missing!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"on_missing": self.on_missing, "count_empty_as_row": self.count_empty_as_row}


# --------------------------------------------------------------------------- #
# Filtres (§13)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Filter:
    """Prédicat typé sur une colonne. ``values`` sert aux opérateurs n-aires."""

    column: ColumnRef
    operator: str
    value: Any = None
    values: tuple[Any, ...] = ()

    def validate(self, *, column_type: str | None = None) -> None:
        self.column.validate(code=ERR_AMBIGUOUS_FILTER_COLUMN)
        if self.operator not in FILTER_OPERATORS:
            raise TabularAggregationError(
                ERR_UNSUPPORTED_FILTER_OPERATOR, f"opérateur inconnu : {self.operator!r}")
        if self.operator in _UNARY_OPERATORS:
            if self.value is not None or self.values:
                raise TabularAggregationError(
                    ERR_INVALID_FILTER, f"{self.operator} ne prend aucune valeur")
        elif self.operator in _LIST_OPERATORS:
            if len(self.values) < 1 or self.value is not None:
                raise TabularAggregationError(
                    ERR_INVALID_FILTER, f"{self.operator} exige une liste non vide")
        elif self.operator in _RANGE_OPERATORS:
            if len(self.values) != 2:
                raise TabularAggregationError(
                    ERR_INVALID_FILTER, "between exige exactement deux bornes")
        elif self.value is None:
            raise TabularAggregationError(
                ERR_INVALID_FILTER, f"{self.operator} exige une valeur")
        if column_type is not None:
            allowed = _OPERATOR_TYPES[self.operator]
            if column_type not in allowed:
                raise TabularAggregationError(
                    ERR_INCOMPATIBLE_FILTER_TYPE,
                    f"{self.operator} incompatible avec une colonne {column_type}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"column": self.column.to_dict(), "operator": self.operator}
        if self.value is not None:
            d["value"] = _jsonable(self.value)
        if self.values:
            d["values"] = [_jsonable(v) for v in self.values]
        return d


# --------------------------------------------------------------------------- #
# Agrégation et résultat
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Aggregation:
    function: str
    target_column: ColumnRef | None = None
    identity_columns: tuple[ColumnRef, ...] = ()

    def validate(self) -> None:
        if self.function not in ALL_OPERATIONS:
            raise TabularAggregationError(
                ERR_UNSUPPORTED_OPERATION, f"opération inconnue : {self.function!r}")
        if self.function in DEFERRED_OPERATIONS:
            raise TabularAggregationError(
                ERR_UNSUPPORTED_OPERATION,
                f"{self.function} est hors périmètre du MVP (§7)")
        if self.function in _NEEDS_TARGET_COLUMN:
            if self.target_column is None:
                raise TabularAggregationError(
                    ERR_MISSING_TARGET_COLUMN, f"{self.function} exige une colonne cible")
            self.target_column.validate()
        elif self.target_column is not None:
            raise TabularAggregationError(
                ERR_INVALID_FILTER, f"{self.function} ne prend pas de colonne cible")
        if self.function in _NEEDS_IDENTITY:
            if not self.identity_columns:
                raise TabularAggregationError(
                    ERR_MISSING_IDENTITY_COLUMN,
                    f"{self.function} exige au moins une colonne d'identité")
            for col in self.identity_columns:
                col.validate()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"function": self.function}
        d["target_column"] = self.target_column.to_dict() if self.target_column else None
        if self.identity_columns:
            d["identity_columns"] = [c.to_dict() for c in self.identity_columns]
        return d


@dataclass(frozen=True)
class ExpectedResult:
    """Résultat attendu, avec son type, sa normalisation et son mode de comparaison (§21)."""

    type: str
    value: Any
    scale: int | None = None
    comparison: str = CMP_EXACT
    tolerance: str = ""

    def validate(self, *, operation: str | None = None) -> None:
        if self.type not in _RESULT_TYPES:
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT, f"type de résultat inconnu : {self.type!r}")
        if self.comparison not in _COMPARISONS:
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT, f"comparaison inconnue : {self.comparison!r}")
        if self.comparison in (CMP_ABSOLUTE_TOLERANCE, CMP_RELATIVE_TOLERANCE) and not self.tolerance:
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT, "une tolérance chiffrée est requise")
        if self.type == RESULT_ROW_IDENTIFIER and not isinstance(self.value, dict):
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT, "row_identifier attend un dict colonne -> valeur")
        if operation in _INTEGER_RESULT and self.type != RESULT_INTEGER:
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT,
                f"{operation} doit produire un entier, pas {self.type}")
        if operation in _NEEDS_IDENTITY and self.type != RESULT_ROW_IDENTIFIER:
            raise TabularAggregationError(
                ERR_INVALID_EXPECTED_RESULT,
                f"{operation} doit produire un row_identifier, pas {self.type}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "value": _jsonable(self.value),
                             "comparison": self.comparison}
        if self.scale is not None:
            d["scale"] = self.scale
        if self.tolerance:
            d["tolerance"] = self.tolerance
        return d


@dataclass(frozen=True)
class AggregationEvidence:
    """Preuve reproductible (§22) : de quoi rejouer le calcul sans stocker les valeurs.

    Les valeurs brutes ne sont **pas** conservées dans le dataset canonique — elles
    sont sensibles. Les hachages suffisent à détecter toute dérive du corpus.
    """

    selected_row_count: int
    selected_row_keys_sha256: str
    normalized_input_values_sha256: str
    result_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"selected_row_count": self.selected_row_count,
                "selected_row_keys_sha256": self.selected_row_keys_sha256,
                "normalized_input_values_sha256": self.normalized_input_values_sha256,
                "result_sha256": self.result_sha256}


@dataclass(frozen=True)
class TabularAggregationGroundTruth:
    document_ref: StableDocumentRef
    table_locator: TableLocator
    aggregation: Aggregation
    expected_result: ExpectedResult
    filters: tuple[Filter, ...] = ()
    row_population_policy: RowPopulationPolicy = field(default_factory=RowPopulationPolicy)
    missing_value_policy: MissingValuePolicy = field(default_factory=MissingValuePolicy)
    evidence: AggregationEvidence | None = None
    locale: str | None = None

    def validate(self, *, column_types: dict[str, str] | None = None) -> None:
        if not is_valid_sha256(self.document_ref.content_sha256 or ""):
            raise TabularAggregationError(
                ERR_INVALID_DOCUMENT_SHA256, "content_sha256 absent ou mal formé")
        self.table_locator.validate()
        self.row_population_policy.validate()
        self.missing_value_policy.validate()
        self.aggregation.validate()
        for f in self.filters:
            ct = (column_types or {}).get(f.column.normalized_header)
            f.validate(column_type=ct)
        if self.aggregation.function in _NEEDS_FILTERS and not self.filters:
            raise TabularAggregationError(
                ERR_INVALID_FILTER,
                f"{self.aggregation.function} exige au moins un filtre")
        self.expected_result.validate(operation=self.aggregation.function)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "document_ref": {k: v for k, v in {
                "content_sha256": self.document_ref.content_sha256,
                "filename": self.document_ref.filename,
                "relative_path": getattr(self.document_ref, "relative_path", ""),
            }.items() if v},
            "table_locator": self.table_locator.to_dict(),
            "selection": {"filters": [f.to_dict() for f in self.filters]},
            "aggregation": self.aggregation.to_dict(),
            "row_population_policy": self.row_population_policy.to_dict(),
            "missing_value_policy": self.missing_value_policy.to_dict(),
            "expected_result": self.expected_result.to_dict(),
        }
        if self.evidence is not None:
            d["aggregation_evidence"] = self.evidence.to_dict()
        if self.locale:
            d["locale"] = self.locale
        return d


# --------------------------------------------------------------------------- #
# Identité stable du cas (§23)
# --------------------------------------------------------------------------- #
def canonical_filters_repr(filters: tuple[Filter, ...] | list[Filter]) -> str:
    """Représentation canonique des filtres, **indépendante de leur ordre de saisie**."""
    return json.dumps(sorted((f.to_dict() for f in filters),
                             key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)),
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def build_aggregation_case_id(*, document_sha256: str, sheet_name: str, table_range: str,
                              operation: str, filters: tuple[Filter, ...] | list[Filter] = (),
                              target_column: str = "", grouping: str = "") -> str:
    """`case_id` reproductible : mêmes clés logiques -> même identifiant.

    N'inclut **jamais** d'UUID, d'horodatage, de position dans le fichier, de résultat
    du moteur évalué ni de seed : deux réingestions du même classeur produisent le même
    identifiant.
    """
    payload = "|".join((
        str(TABULAR_AGGREGATION_SCHEMA_VERSION),
        (document_sha256 or "").lower(),
        normalize_sheet_name(sheet_name),
        normalize_cell_range(table_range),
        operation,
        canonical_filters_repr(tuple(filters)),
        normalize_header(target_column),
        normalize_header(grouping),
    ))
    return f"xlsx-agg-{sha256_hex(payload)[:16]}"


# --------------------------------------------------------------------------- #
# Sérialisation et analyse d'un cas complet
# --------------------------------------------------------------------------- #
def to_case_dict(*, case_id: str, question: str, rag_space: str,
                 ground_truth: TabularAggregationGroundTruth,
                 operation: str = "", difficulty: str = "",
                 human_validation_status: str = "pending") -> dict[str, Any]:
    return {
        "schema_version": TABULAR_AGGREGATION_SCHEMA_VERSION,
        "case_id": case_id,
        "rag_space": rag_space,
        "question": question,
        "task_type": TASK_TABULAR_AGGREGATION,
        "operation": operation or ground_truth.aggregation.function,
        "difficulty": difficulty,
        "human_validation_status": human_validation_status,
        "ground_truth": ground_truth.to_dict(),
    }


def dump_case(case: dict[str, Any]) -> str:
    """Sérialisation canonique : indépendante de l'ordre des clés, terminateur explicite."""
    return json.dumps(case, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def assert_no_uuid(case: dict[str, Any]) -> None:
    """Garde-fou : aucun UUID d'ingestion ne doit entrer dans le cas canonique (§9)."""
    if _UUID_RE.search(dump_case(case)):
        raise TabularAggregationError(
            ERR_UUID_FORBIDDEN,
            "un UUID figure dans le cas — l'identité doit être logique (sha256 + locator)")


def validate_case(case: dict[str, Any]) -> None:
    """Valide un cas sérialisé : version, task_type, absence d'UUID, cohérence interne."""
    if case.get("schema_version") != TABULAR_AGGREGATION_SCHEMA_VERSION:
        raise TabularAggregationError(
            ERR_INVALID_SCHEMA_VERSION,
            f"version {case.get('schema_version')!r} inconnue "
            f"(attendu {TABULAR_AGGREGATION_SCHEMA_VERSION})")
    if case.get("task_type") != TASK_TABULAR_AGGREGATION:
        raise TabularAggregationError(
            ERR_UNSUPPORTED_OPERATION, f"task_type {case.get('task_type')!r} inattendu")
    op = case.get("operation")
    if op not in ALL_OPERATIONS:
        raise TabularAggregationError(ERR_UNSUPPORTED_OPERATION, f"opération {op!r}")
    if op in DEFERRED_OPERATIONS:
        raise TabularAggregationError(
            ERR_UNSUPPORTED_OPERATION, f"{op} est hors périmètre du MVP (§7)")
    if not isinstance(case.get("question"), str) or not case["question"].strip():
        raise TabularAggregationError(ERR_QUESTION_NOT_SELF_CONTAINED, "question vide")
    assert_no_uuid(case)


def _jsonable(value: Any) -> Any:
    """Rend une valeur sérialisable sans perte : `Decimal` -> chaîne, jamais `float`."""
    from decimal import Decimal as _D
    if isinstance(value, _D):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def excel_column_letter(index: int) -> str:
    """Lettre de colonne Excel (1 -> A). Réutilise la primitive du lot v2."""
    from apps.rag_core.benchmark.stable_ground_truth import num_to_col
    return num_to_col(index)


def excel_column_index(letter: str) -> int:
    return col_to_num(normalize_excel_column(letter))

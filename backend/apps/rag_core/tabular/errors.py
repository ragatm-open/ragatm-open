"""Codes d'erreur STABLES du moteur tabulaire de production.

Stables = exposables en API, testables, journalisables, traduisibles côté front. Un
code ne change jamais de sens ; on en ajoute, on n'en recycle pas.

La règle qui gouverne tout ce module : **RagATM préfère un refus explicite à une
réponse tabulaire plausible mais fausse.** Chaque code ci-dessous est un endroit où
le moteur a choisi de ne pas deviner.
"""
from __future__ import annotations


class TabularError(Exception):
    """Erreur du moteur tabulaire, porteuse d'un code stable et exposable."""

    #: Statut de réponse associé (cf. `result_formatter`).
    status = "error"

    def __init__(self, code: str, message: str = "", **details):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.details = details

    def to_dict(self) -> dict:
        out = {"status": self.status, "code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class TabularUnsupported(TabularError):
    """Demande reconnue mais hors périmètre du lot. Jamais approximée."""

    status = "unsupported"


class TabularClarificationRequired(TabularError):
    """Ambiguïté réelle : seule une réponse de l'utilisateur peut trancher."""

    status = "clarification_required"

    def __init__(self, code: str, message: str = "", *, question: str = "",
                 candidates: list | None = None, **details):
        super().__init__(code, message, **details)
        self.question = question
        self.candidates = candidates or []

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["question"] = self.question
        out["candidates"] = self.candidates
        return out


class TabularPermissionDenied(TabularError):
    """Périmètre d'accès violé. Ne divulgue jamais l'existence de la ressource."""

    status = "forbidden"


class TabularLimitExceeded(TabularError):
    """Une limite de ressources a été atteinte. Toujours explicite, jamais tronqué."""

    status = "limit_exceeded"


# --------------------------------------------------------------------------- #
# Plan — structure
# --------------------------------------------------------------------------- #
ERR_INVALID_PLAN_SCHEMA = "invalid_plan_schema"
ERR_INVALID_SCHEMA_VERSION = "invalid_plan_schema_version"
ERR_UNSUPPORTED_OPERATION = "unsupported_operation"
ERR_UNSUPPORTED_FILTER_OPERATOR = "unsupported_filter_operator"
ERR_INVALID_FILTER = "invalid_filter"
ERR_INVALID_LIMIT = "invalid_limit"
ERR_TOO_MANY_FILTERS = "too_many_filters"
ERR_MULTIPLE_SOURCE_TABLES = "multiple_source_tables"
ERR_EXPRESSION_FORBIDDEN = "expression_forbidden"

# --------------------------------------------------------------------------- #
# Plan — sémantique
# --------------------------------------------------------------------------- #
ERR_DOCUMENT_NOT_FOUND = "document_not_found"
ERR_SHEET_NOT_FOUND = "sheet_not_found"
ERR_TABLE_NOT_FOUND = "table_not_found"
ERR_COLUMN_NOT_FOUND = "column_not_found"
ERR_COLUMN_NOT_IN_TABLE = "column_not_in_table"
ERR_TABLE_NOT_IN_SHEET = "table_not_in_sheet"
ERR_SHEET_NOT_IN_DOCUMENT = "sheet_not_in_document"
ERR_INCOMPATIBLE_COLUMN_TYPE = "incompatible_column_type"
ERR_INCOMPATIBLE_FILTER_TYPE = "incompatible_filter_type"
ERR_INCOMPATIBLE_UNITS = "incompatible_units"
ERR_CATALOG_STALE = "catalog_stale"
ERR_CATALOG_MISSING = "catalog_missing"

# --------------------------------------------------------------------------- #
# Ambiguïtés — clarification
# --------------------------------------------------------------------------- #
ERR_AMBIGUOUS_COLUMN = "ambiguous_column"
#: Un seuil numérique compris, mais non rattachable à UNE colonne (R1). Distinct de
#: `ambiguous_column` : ce n'est pas la mesure qui est ambiguë, c'est la cible du filtre.
ERR_AMBIGUOUS_FILTER_COLUMN = "ambiguous_filter_column"
ERR_AMBIGUOUS_TABLE = "ambiguous_table"
ERR_AMBIGUOUS_SHEET = "ambiguous_sheet"
ERR_AMBIGUOUS_DOCUMENT = "ambiguous_document"
ERR_DUPLICATE_COLUMN_NAME = "duplicate_column_name"

# --------------------------------------------------------------------------- #
# Données
# --------------------------------------------------------------------------- #
ERR_FORMULA_RESULT_UNAVAILABLE = "formula_result_unavailable"
ERR_EXCEL_ERROR_VALUE = "excel_error_value"
ERR_MIXED_COLUMN_TYPE = "mixed_column_type"
ERR_EMPTY_SELECTION = "empty_selection"
ERR_TIE_UNRESOLVED = "tie_unresolved"
ERR_NON_NUMERIC_TARGET = "non_numeric_target_column"

# --------------------------------------------------------------------------- #
# Limites de ressources
# --------------------------------------------------------------------------- #
ERR_TOO_MANY_ROWS = "too_many_rows_scanned"
#: Refus PRÉCOCE, décidé avant toute lecture complète (cf. `preflight`). Distinct de
#: `too_many_rows_scanned`, qui constate le dépassement une fois les lignes chargées :
#: ici, rien n'a été normalisé ni transféré.
ERR_INPUT_ROW_LIMIT = "input_row_limit_exceeded"
ERR_INPUT_COLUMN_LIMIT = "input_column_limit_exceeded"
ERR_INPUT_CELL_LIMIT = "input_cell_limit_exceeded"
ERR_INPUT_SIZE_LIMIT = "input_size_limit_exceeded"
ERR_UNCOMPRESSED_SIZE_LIMIT = "uncompressed_size_limit_exceeded"
ERR_TOO_MANY_COLUMNS = "too_many_columns"
ERR_TOO_MANY_RESULT_ROWS = "too_many_result_rows"
ERR_TOO_MANY_GROUPS = "too_many_groups"
ERR_QUERY_TIMEOUT = "query_timeout"
ERR_MEMORY_EXCEEDED = "memory_exceeded"
ERR_TOO_MANY_CANDIDATE_TABLES = "too_many_candidate_tables"
ERR_TOO_MANY_CANDIDATE_COLUMNS = "too_many_candidate_columns"

# --------------------------------------------------------------------------- #
# Périmètre / sécurité
# --------------------------------------------------------------------------- #
ERR_FEATURE_DISABLED = "tabular_query_disabled"
#: Capacité d'exécution saturée. DISTINCT de `query_timeout` : ici il restait du
#: budget, mais aucun slot. Confondre les deux rendrait la supervision illisible.
ERR_TABULAR_CAPACITY_EXCEEDED = "tabular_capacity_exceeded"
ERR_PERMISSION_DENIED = "permission_denied"
ERR_NO_TABULAR_INTENT = "no_tabular_intent"
ERR_PLANNER_FAILED = "planner_could_not_build_plan"
ERR_UNSUPPORTED_FILE_FORMAT = "unsupported_file_format"

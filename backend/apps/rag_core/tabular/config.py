"""Limites de ressources du moteur tabulaire, lues depuis les settings.

Regroupées dans un objet unique plutôt que lues à la volée : le compilateur et
l'exécuteur reçoivent les MÊMES bornes que celles qui ont validé le plan. Lire
`settings` à trois endroits différents laisserait la porte ouverte à une validation
faite sous une limite et une exécution faite sous une autre.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class TabularLimits:
    max_rows_scanned: int
    max_columns: int
    max_filters: int
    max_result_rows: int
    max_groups: int
    max_query_seconds: float
    max_memory_mb: int
    max_candidate_tables: int
    max_candidate_columns: int
    column_match_min_score: float
    column_match_min_margin: float
    max_input_file_mb: float = 0.0
    max_uncompressed_mb: float = 0.0


def current_limits() -> TabularLimits:
    return TabularLimits(
        max_rows_scanned=int(settings.TABULAR_MAX_ROWS_SCANNED),
        max_columns=int(settings.TABULAR_MAX_COLUMNS),
        max_filters=int(settings.TABULAR_MAX_FILTERS),
        max_result_rows=int(settings.TABULAR_MAX_RESULT_ROWS),
        max_groups=int(settings.TABULAR_MAX_GROUPS),
        max_query_seconds=float(settings.TABULAR_MAX_QUERY_SECONDS),
        max_memory_mb=int(settings.TABULAR_MAX_MEMORY_MB),
        max_candidate_tables=int(settings.TABULAR_MAX_CANDIDATE_TABLES),
        max_candidate_columns=int(settings.TABULAR_MAX_CANDIDATE_COLUMNS),
        column_match_min_score=float(settings.TABULAR_COLUMN_MATCH_MIN_SCORE),
        column_match_min_margin=float(settings.TABULAR_COLUMN_MATCH_MIN_MARGIN),
        max_input_file_mb=float(getattr(settings, "TABULAR_MAX_INPUT_FILE_MB", 0) or 0),
        max_uncompressed_mb=float(getattr(settings, "TABULAR_MAX_UNCOMPRESSED_MB", 0) or 0),
    )


def query_enabled() -> bool:
    return bool(settings.TABULAR_QUERY_ENABLED)


def llm_planner_enabled() -> bool:
    return bool(settings.TABULAR_LLM_PLANNER_ENABLED)


def narration_enabled() -> bool:
    return bool(settings.TABULAR_NARRATION_ENABLED)

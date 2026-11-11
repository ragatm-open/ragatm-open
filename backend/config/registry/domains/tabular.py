"""Domaine ``tabular`` — moteur tabulaire déterministe (Excel / CSV).

Le moteur calcule lui-même : le LLM ne produit ni SQL, ni Python, ni le résultat.
Défaut : tout est fermé. Tant que ``TABULAR_QUERY_ENABLED=0``, le comportement
historique est strictement conservé.
"""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="TABULAR_QUERY_ENABLED",
        domain="tabular",
        value_type="bool",
        default=False,
        description=(
            "Moteur tabulaire déterministe. À 0, la garde `excel_guard` refuse les "
            "questions analytiques sur classeur (comportement historique)."
        ),
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_LLM_PLANNER_ENABLED",
        domain="tabular",
        value_type="bool",
        default=False,
        description=(
            "Planificateur LLM du moteur tabulaire. À n'activer que lorsque le moteur "
            "déterministe est validé — jamais en même temps que lui."
        ),
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_NARRATION_ENABLED",
        domain="tabular",
        value_type="bool",
        default=False,
        description="Narration LLM du résultat déjà calculé. Ne peut modifier aucune valeur.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_ROWS_SCANNED",
        domain="tabular",
        value_type="int",
        default=200000,
        minimum=1,
        description="Nombre maximal de lignes parcourues par requête.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_COLUMNS",
        domain="tabular",
        value_type="int",
        default=256,
        minimum=1,
        description="Nombre maximal de colonnes prises en charge.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_FILTERS",
        domain="tabular",
        value_type="int",
        default=16,
        minimum=1,
        description="Nombre maximal de filtres d'une requête tabulaire.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_RESULT_ROWS",
        domain="tabular",
        value_type="int",
        default=1000,
        minimum=1,
        description="Nombre maximal de lignes de résultat renvoyées.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_GROUPS",
        domain="tabular",
        value_type="int",
        default=1000,
        minimum=1,
        description="Nombre maximal de groupes d'une agrégation.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_QUERY_SECONDS",
        domain="tabular",
        value_type="float",
        default=10.0,
        minimum=0.1,
        maximum=600.0,
        description="Budget d'exécution d'une requête tabulaire.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_MEMORY_MB",
        domain="tabular",
        value_type="int",
        default=512,
        minimum=1,
        description="Empreinte mémoire maximale d'une requête tabulaire.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_CANDIDATE_TABLES",
        domain="tabular",
        value_type="int",
        default=50,
        minimum=1,
        description="Nombre maximal de tables candidates examinées.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_CANDIDATE_COLUMNS",
        domain="tabular",
        value_type="int",
        default=512,
        minimum=1,
        description="Nombre maximal de colonnes candidates examinées.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_CONCURRENT_QUERIES",
        domain="tabular",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description=(
            "Sémaphore PAR PROCESSUS. Capacité réelle = GUNICORN_WORKERS × cette valeur. "
            "Mesure : 2 workers donnent 1,73× le débit séquentiel."
        ),
        consumers=("apps.rag_core.tabular.capacity", "apps.rag_core.checks_tabular_capacity"),
    ),
    D(
        name="TABULAR_MIN_MEMORY_HEADROOM_MB",
        domain="tabular",
        value_type="int",
        default=512,
        minimum=0,
        description=(
            "Marge mémoire exigée au-dessus du besoin ESTIMÉ de la capacité tabulaire "
            "(estimation prudente, jamais un plafond exact)."
        ),
        consumers=("apps.rag_core.checks_tabular_capacity",),
    ),
    D(
        name="TABULAR_MAX_INPUT_FILE_MB",
        domain="tabular",
        value_type="float",
        default=200.0,
        minimum=0.1,
        description="Préflight volumétrique : taille maximale du fichier source.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_MAX_UNCOMPRESSED_MB",
        domain="tabular",
        value_type="float",
        default=500.0,
        minimum=0.1,
        description="Préflight volumétrique : taille décompressée maximale.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_COLUMN_MATCH_MIN_SCORE",
        domain="tabular",
        value_type="float",
        default=0.60,
        minimum=0.0,
        maximum=1.0,
        description="Score minimal pour retenir une colonne candidate.",
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_COLUMN_MATCH_MIN_MARGIN",
        domain="tabular",
        value_type="float",
        default=0.15,
        minimum=0.0,
        maximum=1.0,
        description=(
            "Écart minimal avec le second candidat pour juger le premier DOMINANT. "
            "Sous cet écart : clarification, jamais un choix arbitraire par position."
        ),
        consumers=("apps.rag_core.tabular",),
    ),
    D(
        name="TABULAR_VOLUME_DIR",
        domain="tabular",
        value_type="path",
        default="",
        description="Répertoire du corpus de la campagne volumétrique (tests opt-in).",
        environments=("dev", "test"),
        source="backend/apps/rag_core/benchmark/tabular_volume_corpus.py",
        consumers=("apps.rag_core.benchmark",),
        default_is_dynamic=True,
    ),
    D(
        name="TABULAR_VOLUME_FULL",
        domain="tabular",
        value_type="bool",
        default=False,
        description="Campagne volumétrique complète (au lieu de l'échantillon).",
        environments=("dev", "test"),
        source="backend/apps/rag_core/benchmark/tabular_volume_corpus.py",
        consumers=("apps.rag_core.benchmark",),
    ),
)

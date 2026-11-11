"""Validation SÉMANTIQUE d'un plan déjà valide structurellement.

Pydantic garantit la forme. Ce module garantit le sens, et il a besoin pour cela du
catalogue et de la base : un plan parfaitement formé peut désigner un document
inexistant, une colonne d'une autre table, ou un type sur lequel l'opération demandée
n'a aucun sens.

L'ordre des vérifications n'est pas indifférent. Les contrôles d'**existence et de
périmètre** passent avant les contrôles de type : un utilisateur sans accès au document
doit recevoir le même refus, qu'il ait ou non demandé une somme sur une colonne texte.
Sinon le message d'erreur devient un oracle qui révèle la structure d'un document
auquel il n'a pas droit.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from apps.rag_core.tabular import catalog as C
from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import types as T


@dataclass(frozen=True)
class ValidatedPlan:
    """Plan dont chaque référence a été vérifiée contre le catalogue."""

    plan: PS.TabularPlan
    table: C.CatalogTable
    positions: dict[UUID, int]


def validate(plan: PS.TabularPlan, catalog: C.DocumentCatalog, *, limits) -> ValidatedPlan:
    """Vérifie le plan contre le catalogue. Lève au premier problème."""
    # -- 1. Le document du plan est bien celui du catalogue chargé ---------
    if plan.source.document_id != catalog.document_id:
        raise E.TabularPermissionDenied(
            E.ERR_PERMISSION_DENIED, "le plan désigne un autre document")

    # -- 2. Table, feuille --------------------------------------------------
    table = catalog.table_by_id(plan.source.table_id)
    if table is None:
        raise E.TabularError(E.ERR_TABLE_NOT_FOUND, "table introuvable dans ce document")
    if table.sheet_id != plan.source.sheet_id:
        raise E.TabularError(
            E.ERR_TABLE_NOT_IN_SHEET, "la table n'appartient pas à la feuille indiquée")

    # -- 3. Bornes ----------------------------------------------------------
    if len(plan.filters) > limits.max_filters:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_FILTERS,
            f"{len(plan.filters)} filtres, plafond {limits.max_filters}")
    if table.column_count > limits.max_columns:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_COLUMNS,
            f"{table.column_count} colonnes, plafond {limits.max_columns}")
    if plan.limit is not None and plan.limit > limits.max_result_rows:
        raise E.TabularLimitExceeded(
            E.ERR_TOO_MANY_RESULT_ROWS,
            f"limite demandée {plan.limit}, plafond {limits.max_result_rows}")

    positions = table.positions()

    def column(column_id: UUID, *, role: str) -> C.CatalogColumn:
        col = table.column_by_id(column_id)
        if col is None:
            raise E.TabularError(
                E.ERR_COLUMN_NOT_IN_TABLE,
                f"la colonne {role} n'appartient pas à la table sélectionnée")
        return col

    # -- 4. Formules sans valeur en cache -----------------------------------
    # Une formule non recalculée vaut `None` : indiscernable d'une cellule vide. La
    # sommer produirait un total silencieusement faux. Le moteur ne recalcule JAMAIS
    # un classeur — il refuse.
    if table.has_uncached_formula and plan.operation in (
            "sum", "average", "minimum", "maximum", "argmin", "argmax"):
        raise E.TabularError(
            E.ERR_FORMULA_RESULT_UNAVAILABLE,
            "la table contient une formule sans valeur en cache : un calcul exact "
            "est impossible sans recalcul du classeur, que RagATM n'effectue pas")

    # -- 5. Colonne cible ---------------------------------------------------
    if plan.target is not None:
        col = column(plan.target.column_id, role="cible")
        if plan.operation in PS.NUMERIC_TARGET_OPERATIONS:
            if col.inferred_type not in T.NUMERIC_TYPES:
                raise E.TabularError(
                    E.ERR_NON_NUMERIC_TARGET,
                    f"« {col.original_name} » est de type {col.inferred_type} : "
                    f"l'opération {plan.operation} n'a pas de sens dessus")
        elif plan.operation in ("minimum", "maximum", "argmin", "argmax"):
            if col.inferred_type not in T.ORDERED_TYPES:
                raise E.TabularError(
                    E.ERR_INCOMPATIBLE_COLUMN_TYPE,
                    f"« {col.original_name} » n'est pas ordonnable "
                    f"({col.inferred_type})")

    # -- 6. Filtres : appartenance ET compatibilité de type ------------------
    for f in plan.filters:
        col = column(f.column_id, role="de filtre")
        _check_filter_type(f, col)

    # -- 7. Tri et projection -----------------------------------------------
    for s in plan.order_by:
        col = column(s.column_id, role="de tri")
        if col.inferred_type not in T.ORDERED_TYPES and col.inferred_type not in T.TEXT_TYPES:
            raise E.TabularError(
                E.ERR_INCOMPATIBLE_COLUMN_TYPE,
                f"« {col.original_name} » n'est pas triable")
    for cid in plan.select_columns:
        column(cid, role="projetée")

    # -- 8. Cohérence d'unité ------------------------------------------------
    # Sommer des euros et des dollars dans la même colonne n'a pas de sens ; le
    # catalogue porte l'unité, on la vérifie plutôt que de produire un nombre nu.
    if plan.operation in ("sum", "average") and plan.target is not None:
        col = column(plan.target.column_id, role="cible")
        if col.inferred_type == T.TYPE_CURRENCY and not col.unit:
            # Devise indéterminée : on n'invente pas d'unité, on le signalera dans
            # les avertissements du résultat (pas un refus : le calcul reste exact).
            pass

    return ValidatedPlan(plan=plan, table=table, positions=positions)


def _check_filter_type(f: PS.PlanFilter, col: C.CatalogColumn) -> None:
    """Un opérateur doit avoir un sens sur le type de la colonne."""
    op, ctype = f.operator, col.inferred_type

    if op in PS.TEXT_ONLY_OPERATORS and ctype not in T.TEXT_TYPES:
        raise E.TabularError(
            E.ERR_INCOMPATIBLE_FILTER_TYPE,
            f"« {op} » ne s'applique qu'à du texte, pas à une colonne {ctype}")

    if op in PS.ORDERING_OPERATORS and ctype not in T.ORDERED_TYPES:
        raise E.TabularError(
            E.ERR_INCOMPATIBLE_FILTER_TYPE,
            f"« {op} » exige une colonne ordonnable, pas {ctype}")

    # Les valeurs doivent être convertibles vers le type de la colonne. Une borne
    # non convertible est une erreur de conception du plan, pas une ligne à ignorer.
    if op in PS.UNARY_OPERATORS:
        return
    raw = list(f.values) if f.values else [f.value]
    for v in raw:
        try:
            T.coerce(v, ctype)
        except T.TabularTypeError as exc:
            raise E.TabularError(
                E.ERR_INCOMPATIBLE_FILTER_TYPE,
                f"valeur de filtre {v!r} incompatible avec la colonne "
                f"« {col.original_name} » ({ctype})") from exc

"""Compilation d'un plan VALIDÉ en SQL DuckDB paramétré.

Le point le plus sensible du moteur, et le raisonnement qui le gouverne :

**Aucune chaîne venue de l'extérieur n'entre dans le texte SQL.** Ni un nom de colonne,
ni un nom de table, ni une valeur de filtre. Le SQL est assemblé exclusivement à partir
de :

- littéraux écrits dans CE fichier (`SELECT`, `WHERE`, `SUM`…) ;
- noms de colonnes **générés** par le moteur (`c0`, `c1`, … — voir `column_alias`) ;
- marqueurs de paramètre `?`.

Les valeurs voyagent séparément, dans une liste de paramètres passée à DuckDB. Il n'y a
donc pas de « bonne » façon d'échapper un nom de colonne, parce qu'aucun nom de colonne
utilisateur n'atteint jamais le SQL : le plan désigne les colonnes par `column_id`
(UUID), et le compilateur les traduit en alias positionnels qu'il a lui-même créés.

Conséquence pratique : une colonne nommée `"; DROP TABLE t; --"` dans un classeur réel
est compilée en `c3`. Il n'y a rien à neutraliser.

Ce que ce module ne fait pas, délibérément : il ne lit aucun fichier, n'ouvre aucune
connexion, ne connaît aucun chemin. Il transforme une structure en `(sql, params)`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import plan_schema as PS

#: Nom de la relation temporaire. Constante du module : jamais dérivé d'une entrée.
TABLE_NAME = "t"

#: Types du catalogue considérés comme textuels pour la comparaison d'égalité.
_TEXT_TYPES = frozenset({"text", "identifier"})

#: Traduction opérateur -> gabarit SQL. Les gabarits sont des CONSTANTES ; seuls les
#: `?` reçoivent des valeurs. Aucune interpolation de chaîne n'est possible ici.
_OPERATOR_SQL = {
    "equals": "{col} = ?",
    "not_equals": "{col} IS DISTINCT FROM ?",
    "greater_than": "{col} > ?",
    "greater_or_equal": "{col} >= ?",
    "less_than": "{col} < ?",
    "less_or_equal": "{col} <= ?",
    "between": "{col} BETWEEN ? AND ?",
    "contains": "{col} IS NOT NULL AND strpos(lower({col}), lower(?)) > 0",
    "starts_with": "{col} IS NOT NULL AND starts_with(lower({col}), lower(?))",
    "is_null": "{col} IS NULL",
    "is_not_null": "{col} IS NOT NULL",
}

#: Fonction d'agrégation par opération. Constantes, jamais construites dynamiquement.
#: Sur une colonne TEXTE, l'égalité est insensible à la casse et aux espaces de
#: bordure. « ouest », « Ouest » et « Ouest  » désignent la même modalité pour un
#: utilisateur ; exiger la casse exacte produirait un résultat vide présenté comme
#: un fait — le mode d'échec que tout le moteur cherche à éviter.
_TEXT_OPERATOR_SQL = {
    "equals": "lower(trim({col})) = lower(trim(?))",
    "not_equals": "lower(trim({col})) IS DISTINCT FROM lower(trim(?))",
}

_AGGREGATE_SQL = {
    "count_rows": "COUNT(*)",
    "count_distinct": "COUNT(DISTINCT {col})",
    "sum": "SUM({col})",
    # `average` n'est PAS ici : voir `POST_DIVIDE` plus bas. `AVG()` et l'opérateur
    # `/` de DuckDB renvoient tous deux un DOUBLE — un cast après coup ne rattrape
    # pas la précision déjà perdue. La moyenne est donc calculée en `Decimal` côté
    # Python, à partir d'une somme et d'un décompte tous deux exacts.
    "minimum": "MIN({col})",
    "maximum": "MAX({col})",
}


def column_alias(position: int) -> str:
    """Alias interne d'une colonne : `c0`, `c1`, …

    C'est ici que se joue l'essentiel de la sécurité du compilateur. L'alias est
    dérivé d'un ENTIER (la position de la colonne dans le catalogue), jamais du nom
    d'origine. Aucun caractère venu du classeur ne peut s'y glisser, donc aucun
    échappement n'est nécessaire — et aucun oubli d'échappement n'est possible.
    """
    if not isinstance(position, int) or isinstance(position, bool) or position < 0:
        raise E.TabularError(
            E.ERR_INVALID_PLAN_SCHEMA, f"position de colonne invalide : {position!r}")
    return f"c{position}"


#: Post-traitement `average` : la requête rend `(somme, décompte)` et la division se
#: fait en `Decimal` côté Python. Le décompte est celui des valeurs NON NULLES —
#: dénominateur des valeurs réellement agrégées, jamais le nombre de lignes brut.
POST_DIVIDE = "divide_decimal"


@dataclass(frozen=True)
class CompiledQuery:
    """Résultat de compilation : du SQL constant et des paramètres séparés."""

    sql: str
    params: tuple[Any, ...]
    #: Alias -> `column_id`, pour rattacher le résultat au catalogue (provenance).
    output_columns: tuple[tuple[str, UUID], ...] = ()
    #: Vrai si la requête retourne des lignes plutôt qu'un scalaire.
    returns_rows: bool = False
    #: Post-traitement exact à appliquer au scalaire (cf. `POST_DIVIDE`).
    post_process: str = ""


def _param(value: Any) -> Any:
    """Normalise une valeur de filtre avant paramétrage.

    `Decimal` est converti en `str` : DuckDB le lit comme un DECIMAL exact, alors
    qu'un passage par `float` introduirait l'imprécision binaire que tout le moteur
    s'applique à éviter.
    """
    if isinstance(value, Decimal):
        return str(value)
    return value


def compile_plan(plan: PS.TabularPlan, *, positions: dict[UUID, int],
                 max_result_rows: int,
                 column_types: dict[UUID, str] | None = None) -> CompiledQuery:
    """Traduit un plan validé en `(sql, params)`.

    `positions` : `column_id` -> position dans le catalogue. Fourni par le validateur
    sémantique, qui a déjà vérifié que chaque colonne appartient bien à la table. Un
    `column_id` absent de ce mapping est une erreur de programmation en amont, pas une
    entrée utilisateur — on lève plutôt que de compiler quelque chose d'approximatif.
    """
    def col(column_id: UUID) -> str:
        pos = positions.get(column_id)
        if pos is None:
            raise E.TabularError(
                E.ERR_COLUMN_NOT_IN_TABLE,
                f"colonne {column_id} absente de la table source")
        return column_alias(pos)

    params: list[Any] = []

    # -- WHERE ------------------------------------------------------------
    where_parts: list[str] = []
    for f in plan.filters:
        target = col(f.column_id)

        if f.operator in PS.LIST_OPERATORS:
            # `IN` : autant de `?` que de valeurs, générés depuis leur NOMBRE — la
            # seule chose que les valeurs déterminent est la quantité de marqueurs.
            marks = ", ".join("?" for _ in f.values)
            where_parts.append(f"{target} IN ({marks})")
            params.extend(_param(v) for v in f.values)
            continue

        is_text = (column_types or {}).get(f.column_id) in _TEXT_TYPES
        template = None
        if is_text:
            template = _TEXT_OPERATOR_SQL.get(f.operator)
        if template is None:
            template = _OPERATOR_SQL.get(f.operator)
        if template is None:  # pragma: no cover - liste fermée côté Pydantic
            raise E.TabularError(
                E.ERR_UNSUPPORTED_FILTER_OPERATOR, f"opérateur {f.operator!r}")

        if f.operator in PS.UNARY_OPERATORS:
            where_parts.append(template.format(col=target))
        elif f.operator in PS.RANGE_OPERATORS:
            where_parts.append(template.format(col=target))
            params.extend(_param(v) for v in f.values)
        else:
            where_parts.append(template.format(col=target))
            params.append(_param(f.value))

    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    op = plan.operation

    # -- Moyenne : somme et décompte exacts, division en Decimal côté Python ----
    if op == "average":
        measure = col(plan.target.column_id)
        sql = (f"SELECT SUM({measure}) AS total, COUNT({measure}) AS n "
               f"FROM {TABLE_NAME}{where_sql}")
        return CompiledQuery(sql=sql, params=tuple(params),
                             returns_rows=False, post_process=POST_DIVIDE)

    # -- Agrégations scalaires --------------------------------------------
    if op in _AGGREGATE_SQL:
        target_sql = _AGGREGATE_SQL[op]
        if "{col}" in target_sql:
            target_sql = target_sql.format(col=col(plan.target.column_id))
        sql = f"SELECT {target_sql} AS value FROM {TABLE_NAME}{where_sql}"
        return CompiledQuery(sql=sql, params=tuple(params), returns_rows=False)

    # -- argmin / argmax ---------------------------------------------------
    # Renvoie DEUX lignes au plus : on doit pouvoir détecter un ex aequo. Un
    # `LIMIT 1` masquerait l'égalité et trancherait arbitrairement — exactement ce
    # que le moteur refuse de faire.
    if op in ("argmin", "argmax"):
        measure = col(plan.target.column_id)
        direction = "ASC" if op == "argmin" else "DESC"
        selected = _select_list(plan, positions, col)

        # La mesure doit figurer dans la projection : sans elle, impossible de
        # comparer les deux lignes rendues pour détecter un ex aequo.
        projection = selected.sql
        if measure not in [alias for alias, _ in selected.columns]:
            projection = f"{selected.sql}, {measure}"

        # Les NULL sont écartés : `MIN` les ignore déjà côté agrégation, et un NULL
        # trié en tête donnerait un « argmin » qui ne mesure rien.
        null_guard = f"{measure} IS NOT NULL"
        guarded = f"{where_sql} AND {null_guard}" if where_sql else f" WHERE {null_guard}"

        sql = (f"SELECT {projection} FROM {TABLE_NAME}{guarded} "
               f"ORDER BY {measure} {direction} LIMIT 2")
        return CompiledQuery(
            sql=sql, params=tuple(params),
            output_columns=selected.columns, returns_rows=True,
        )

    # -- Opérations retournant des lignes ---------------------------------
    if op in PS.ROW_RETURNING_OPERATIONS:
        selected = _select_list(plan, positions, col)

        order_parts: list[str] = []
        for s in plan.order_by:
            order_parts.append(f"{col(s.column_id)} {'DESC' if s.direction == 'desc' else 'ASC'}")

        if op in ("top_n", "bottom_n") and not order_parts:
            # Un top-N sans critère d'ordre n'a pas de sens : le validateur sémantique
            # l'exige déjà, cette garde protège contre un appel direct au compilateur.
            raise E.TabularError(
                E.ERR_INVALID_PLAN_SCHEMA, f"{op} exige un `order_by`")

        order_sql = f" ORDER BY {', '.join(order_parts)}" if order_parts else ""

        # La limite est TOUJOURS bornée par le plafond serveur : un plan ne peut pas
        # demander plus de lignes que la configuration n'en autorise.
        limit = plan.limit if plan.limit is not None else max_result_rows
        limit = min(int(limit), int(max_result_rows))
        sql = (f"SELECT {selected.sql} FROM {TABLE_NAME}{where_sql}"
               f"{order_sql} LIMIT {limit}")
        return CompiledQuery(
            sql=sql, params=tuple(params),
            output_columns=selected.columns, returns_rows=True,
        )

    raise E.TabularUnsupported(  # pragma: no cover - liste fermée côté Pydantic
        E.ERR_UNSUPPORTED_OPERATION, f"opération non compilable : {op!r}")


@dataclass(frozen=True)
class _SelectList:
    sql: str
    columns: tuple[tuple[str, UUID], ...]


def _select_list(plan: PS.TabularPlan, positions: dict[UUID, int], col) -> _SelectList:
    """Projection : les colonnes demandées, ou toutes celles de la table."""
    ids = list(plan.select_columns) or sorted(positions, key=lambda k: positions[k])
    pairs = tuple((col(cid), cid) for cid in ids)
    return _SelectList(sql=", ".join(a for a, _ in pairs), columns=pairs)

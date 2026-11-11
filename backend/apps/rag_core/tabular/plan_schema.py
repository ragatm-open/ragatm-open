"""Plan tabulaire versionné — la seule chose qui puisse atteindre le compilateur.

Ce module ne calcule rien et n'accède à rien : il **décrit et refuse**. C'est la
frontière de confiance du moteur. Tout ce qui la franchit a été validé par Pydantic
contre un schéma FERMÉ ; tout ce qui n'y entre pas est refusé avant exécution.

Trois propriétés en font une frontière et pas une formalité :

1. **Aucun texte libre n'y survit.** Les colonnes sont désignées par `column_id`
   (UUID du catalogue), jamais par un nom que quelqu'un aurait tapé. Un identifiant
   inconnu ne « ressemble » à rien : il est absent du catalogue, donc refusé. Il n'y
   a aucun chemin par lequel une chaîne du plan devienne un identifiant SQL.

2. **Les listes sont fermées.** Opérations, opérateurs, directions de tri sont des
   `Literal`. Pydantic refuse tout le reste — y compris `"sum(x); DROP TABLE"`, qui
   n'est pas « une opération malveillante » mais simplement une opération inconnue.

3. **`extra="forbid"` partout.** Un champ non prévu fait échouer la validation au lieu
   d'être ignoré. C'est ce qui empêche un plan de transporter une charge utile que le
   compilateur ignorerait mais qu'un composant ultérieur lirait.

Le LLM, s'il est un jour autorisé à produire un plan (`TABULAR_LLM_PLANNER_ENABLED`),
produit **ce JSON et rien d'autre**. Il ne produit jamais de SQL, jamais de Python,
jamais un nom de colonne appliqué tel quel.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Version du schéma de plan. Seule cette valeur est acceptée : aucune inférence,
#: aucune rétro-compatibilité implicite. Un plan d'une autre version est refusé.
PLAN_SCHEMA_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Listes FERMÉES
# --------------------------------------------------------------------------- #
#: Opérations du lot 1. Toute autre valeur est un `unsupported_operation`.
Operation = Literal[
    "filter_rows",
    "count_rows",
    "count_distinct",
    "sum",
    "average",
    "minimum",
    "maximum",
    "top_n",
    "bottom_n",
    "argmin",
    "argmax",
]

#: Opérations RECONNUES mais explicitement différées. Les nommer permet de refuser
#: avec un message utile (« hors périmètre ») plutôt qu'un « opération inconnue »
#: — et garantit qu'on ne les traitera jamais par approximation.
DEFERRED_OPERATIONS = frozenset({
    "median", "percentile", "stddev", "variance",
    "join", "multi_file", "multi_table",
    "window", "pivot", "regression", "anomaly_detection",
    "group_by",  # un GROUP BY complet est différé : cf. docs
})

FilterOperator = Literal[
    "equals", "not_equals",
    "greater_than", "greater_or_equal",
    "less_than", "less_or_equal",
    "between", "in",
    "contains", "starts_with",
    "is_null", "is_not_null",
]

SortDirection = Literal["asc", "desc"]

#: Opérateurs sans valeur (unaires).
UNARY_OPERATORS = frozenset({"is_null", "is_not_null"})
#: Opérateurs prenant une LISTE de valeurs.
LIST_OPERATORS = frozenset({"in"})
#: Opérateurs prenant exactement deux bornes.
RANGE_OPERATORS = frozenset({"between"})
#: Opérateurs purement textuels — refusés sur une colonne numérique ou date.
TEXT_ONLY_OPERATORS = frozenset({"contains", "starts_with"})
#: Opérateurs d'ordre — exigent un type ordonnable.
ORDERING_OPERATORS = frozenset({
    "greater_than", "greater_or_equal", "less_than", "less_or_equal", "between",
})

#: Opérations exigeant une colonne cible numérique.
NUMERIC_TARGET_OPERATIONS = frozenset({"sum", "average"})
#: Opérations exigeant une colonne cible (numérique ou ordonnable).
NEEDS_TARGET_COLUMN = frozenset({
    "count_distinct", "sum", "average", "minimum", "maximum", "argmin", "argmax",
})
#: Opérations produisant des LIGNES (et non un scalaire).
ROW_RETURNING_OPERATIONS = frozenset({"filter_rows", "top_n", "bottom_n"})
#: Opérations exigeant une limite explicite.
NEEDS_LIMIT = frozenset({"top_n", "bottom_n"})

#: Valeur scalaire admissible dans un filtre. Volontairement ÉTROIT : pas de dict,
#: pas de liste imbriquée, pas d'objet — rien qui puisse porter une structure.
ScalarValue = Union[str, int, float, bool, Decimal, None]


# --------------------------------------------------------------------------- #
# Filtre
# --------------------------------------------------------------------------- #
class PlanFilter(BaseModel):
    """Prédicat sur UNE colonne, désignée par son identifiant de catalogue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_id: UUID
    operator: FilterOperator
    value: ScalarValue = None
    values: tuple[ScalarValue, ...] = ()

    @model_validator(mode="after")
    def _coherence_valeur_operateur(self) -> "PlanFilter":
        op = self.operator
        if op in UNARY_OPERATORS:
            if self.value is not None or self.values:
                raise ValueError(f"{op} ne prend aucune valeur")
        elif op in LIST_OPERATORS:
            if self.value is not None:
                raise ValueError(f"{op} attend `values`, pas `value`")
            if not self.values:
                raise ValueError(f"{op} exige une liste non vide")
        elif op in RANGE_OPERATORS:
            if self.value is not None:
                raise ValueError("between attend `values`, pas `value`")
            if len(self.values) != 2:
                raise ValueError("between exige exactement deux bornes")
            lo, hi = self.values
            if lo is None or hi is None:
                raise ValueError("between n'accepte pas de borne nulle")
        else:
            if self.value is None:
                raise ValueError(f"{op} exige une valeur")
            if self.values:
                raise ValueError(f"{op} attend `value`, pas `values`")
        return self


class PlanSort(BaseModel):
    """Critère de tri. Toujours sur une colonne du catalogue, jamais une expression."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_id: UUID
    direction: SortDirection = "asc"


class PlanSource(BaseModel):
    """Source UNIQUE. Le lot 1 interdit toute jointure : un seul triplet, point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    sheet_id: UUID
    table_id: UUID


class PlanTarget(BaseModel):
    """Colonne mesurée par l'opération."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_id: UUID


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
class TabularPlan(BaseModel):
    """Plan d'exécution tabulaire — représentation intermédiaire versionnée.

    Validé STRUCTURELLEMENT ici (formes, types, listes fermées, cohérence interne).
    La validation SÉMANTIQUE — le document existe-t-il, l'utilisateur y a-t-il accès,
    la colonne appartient-elle à la table, le type est-il compatible — vit dans
    `plan_validator`, parce qu'elle exige la base et le catalogue.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = PLAN_SCHEMA_VERSION
    operation: Operation
    source: PlanSource
    target: PlanTarget | None = None
    filters: tuple[PlanFilter, ...] = ()
    order_by: tuple[PlanSort, ...] = ()
    #: Colonnes d'identité rendues par `argmin`/`argmax` et par les opérations
    #: retournant des lignes. Vide = toutes les colonnes de la table.
    select_columns: tuple[UUID, ...] = ()
    limit: Annotated[int, Field(ge=0)] | None = None

    @field_validator("schema_version")
    @classmethod
    def _version_exacte(cls, v: str) -> str:
        if v != PLAN_SCHEMA_VERSION:
            raise ValueError(
                f"version de schéma non supportée : {v!r} "
                f"(seule {PLAN_SCHEMA_VERSION!r} est acceptée)")
        return v

    @model_validator(mode="after")
    def _coherence_operation(self) -> "TabularPlan":
        op = self.operation

        if op in NEEDS_TARGET_COLUMN and self.target is None:
            raise ValueError(f"{op} exige une colonne cible")
        if op not in NEEDS_TARGET_COLUMN and self.target is not None:
            raise ValueError(f"{op} ne prend pas de colonne cible")

        if op in NEEDS_LIMIT:
            if self.limit is None:
                raise ValueError(f"{op} exige une limite explicite")
            if self.limit == 0:
                raise ValueError(f"{op} avec une limite nulle ne veut rien dire")

        # `count_rows` ne trie rien : un tri sur un scalaire est une incohérence,
        # pas une préférence de présentation.
        if op not in ROW_RETURNING_OPERATIONS and self.order_by:
            raise ValueError(f"{op} produit un scalaire : `order_by` n'a pas de sens")

        # Une colonne ne peut pas être triée deux fois : l'ordre serait indéterminé.
        seen: set[UUID] = set()
        for s in self.order_by:
            if s.column_id in seen:
                raise ValueError(f"colonne {s.column_id} triée plusieurs fois")
            seen.add(s.column_id)

        return self

    # ------------------------------------------------------------------ #
    def canonical_dict(self) -> dict[str, Any]:
        """Représentation canonique — base du hash de plan.

        Deux plans sémantiquement identiques doivent produire le MÊME hash, quels que
        soient l'ordre d'écriture des clés et la machine. D'où le tri des clés et la
        sérialisation explicite des UUID et des Decimal.
        """
        return json.loads(self.model_dump_json())

    def plan_hash(self) -> str:
        """SHA-256 du plan canonique. Identifie l'exécution, sert la reproductibilité."""
        payload = json.dumps(self.canonical_dict(), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_plan(payload: dict[str, Any]) -> TabularPlan:
    """Valide un plan brut. Lève `TabularError` avec un code stable en cas d'échec.

    C'est le SEUL point d'entrée d'un plan dans le moteur. Aucun composant en aval ne
    doit accepter un dict : ils prennent tous un `TabularPlan` déjà validé.
    """
    from pydantic import ValidationError

    from apps.rag_core.tabular import errors as E

    if not isinstance(payload, dict):
        raise E.TabularError(E.ERR_INVALID_PLAN_SCHEMA, "le plan doit être un objet JSON")

    version = payload.get("schema_version")
    if version is not None and version != PLAN_SCHEMA_VERSION:
        raise E.TabularError(
            E.ERR_INVALID_SCHEMA_VERSION,
            f"version de plan non supportée : {version!r}")

    op = payload.get("operation")
    if isinstance(op, str) and op in DEFERRED_OPERATIONS:
        # Reconnue mais hors périmètre : le dire, plutôt que « opération inconnue ».
        raise E.TabularUnsupported(
            E.ERR_UNSUPPORTED_OPERATION,
            f"l'opération {op!r} est reconnue mais hors périmètre de ce lot")

    try:
        return TabularPlan.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = first.get("msg", "plan invalide")
        code = E.ERR_INVALID_PLAN_SCHEMA
        if loc.startswith("operation"):
            code = E.ERR_UNSUPPORTED_OPERATION
        elif "operator" in loc:
            code = E.ERR_UNSUPPORTED_FILTER_OPERATOR
        elif loc.startswith("filters"):
            code = E.ERR_INVALID_FILTER
        elif loc.startswith("limit"):
            code = E.ERR_INVALID_LIMIT
        raise E.TabularError(code, f"{loc or 'plan'} : {msg}") from exc

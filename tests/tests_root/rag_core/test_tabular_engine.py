"""Moteur tabulaire — unitaires : plan, compilation, exécution, types, sécurité.

Ces tests ne touchent ni la base ni le réseau : ils exercent la chaîne
plan → validation → compilation → DuckDB sur des lignes fournies en mémoire.

La vérité attendue est écrite à la main dans chaque cas. Elle n'est **jamais** produite
par `benchmark/tabular_reference_executor` : le moteur et sa vérité terrain doivent
rester indépendants, sinon un défaut de conversion partagé serait invisible.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import executor as EX
from apps.rag_core.tabular import plan_compiler as PC
from apps.rag_core.tabular import plan_schema as PS
from apps.rag_core.tabular import types as T


@dataclass(frozen=True)
class _Limits:
    max_rows_scanned: int = 100_000
    max_columns: int = 256
    max_filters: int = 16
    max_result_rows: int = 1_000
    max_groups: int = 1_000
    max_query_seconds: float = 10.0
    max_memory_mb: int = 256
    max_candidate_tables: int = 50
    max_candidate_columns: int = 512
    column_match_min_score: float = 0.60
    column_match_min_margin: float = 0.15


LIMITS = _Limits()

DOC, SHEET, TABLE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
C_REF, C_REGION, C_STATUT, C_CA, C_QTE = (uuid.uuid4() for _ in range(5))

POSITIONS = {C_REF: 0, C_REGION: 1, C_STATUT: 2, C_CA: 3, C_QTE: 4}
COLUMNS = [
    ("c0", T.TYPE_IDENTIFIER), ("c1", T.TYPE_TEXT), ("c2", T.TYPE_TEXT),
    ("c3", T.TYPE_CURRENCY), ("c4", T.TYPE_INTEGER),
]

#: Vérité terrain écrite à la main (voir docstring du module).
ROWS = [
    ("000001", "Ouest", "Gagné",   Decimal("1000.50"), 10),
    ("000002", "Est",   "Gagné",   Decimal("2000.25"), 20),
    ("000003", "Ouest", "Perdu",   Decimal("3000.00"), 30),
    ("000004", "Ouest", "Gagné",   Decimal("500.25"),  5),
    ("000005", "Nord",  "En cours", None,              0),
]
# Ouest = 1000.50 + 3000.00 + 500.25 = 4500.75 ; total = 6501.00 ; n non nul = 4


def _source() -> dict:
    return {"document_id": str(DOC), "sheet_id": str(SHEET), "table_id": str(TABLE)}


def run(payload: dict):
    plan = PS.parse_plan(payload)
    query = PC.compile_plan(plan, positions=POSITIONS,
                            max_result_rows=LIMITS.max_result_rows)
    outcome = EX.execute(query, columns=COLUMNS, rows=ROWS, limits=LIMITS)
    return plan, query, outcome


def scalar(payload: dict):
    _plan, query, outcome = run(payload)
    row = outcome.rows[0]
    if query.post_process == PC.POST_DIVIDE:
        return EX.to_decimal(row[0]) / Decimal(int(row[1]))
    value = row[0]
    return EX.to_decimal(value) if isinstance(value, (Decimal, float)) else value


# --------------------------------------------------------------------------- #
# Agrégations
# --------------------------------------------------------------------------- #
def test_sum_global():
    assert scalar({"schema_version": "1.0", "operation": "sum", "source": _source(),
                   "target": {"column_id": str(C_CA)}}) == Decimal("6501.00")


def test_sum_filtre():
    assert scalar({
        "schema_version": "1.0", "operation": "sum", "source": _source(),
        "target": {"column_id": str(C_CA)},
        "filters": [{"column_id": str(C_REGION), "operator": "equals", "value": "Ouest"}],
    }) == Decimal("4500.75")


def test_count_rows():
    assert scalar({"schema_version": "1.0", "operation": "count_rows",
                   "source": _source()}) == 5


def test_count_distinct():
    assert scalar({"schema_version": "1.0", "operation": "count_distinct",
                   "source": _source(),
                   "target": {"column_id": str(C_REGION)}}) == 3


def test_average_est_un_decimal_exact_et_ignore_les_nuls():
    """La moyenne divise par les valeurs NON NULLES, en `Decimal`.

    Ni `AVG()` ni `/` de DuckDB ne conviennent : tous deux rendent un DOUBLE, ce qui
    réintroduit l'imprécision binaire sur des montants. D'où somme et décompte exacts,
    division côté Python.
    """
    value = scalar({"schema_version": "1.0", "operation": "average",
                    "source": _source(), "target": {"column_id": str(C_CA)}})
    assert isinstance(value, Decimal)
    assert value == Decimal("6501.00") / Decimal(4)   # 4 valeurs, pas 5 lignes


def test_minimum_et_maximum():
    base = {"schema_version": "1.0", "source": _source(),
            "target": {"column_id": str(C_CA)}}
    assert scalar({**base, "operation": "minimum"}) == Decimal("500.25")
    assert scalar({**base, "operation": "maximum"}) == Decimal("3000.00")


def test_somme_entiers():
    assert scalar({"schema_version": "1.0", "operation": "sum", "source": _source(),
                   "target": {"column_id": str(C_QTE)}}) == 65


# --------------------------------------------------------------------------- #
# Filtres — un test par opérateur
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flt,attendu", [
    ({"column_id": None, "operator": "equals", "value": "Ouest"}, 3),
    ({"column_id": None, "operator": "not_equals", "value": "Ouest"}, 2),
    ({"column_id": None, "operator": "in", "values": ["Ouest", "Est"]}, 4),
    ({"column_id": None, "operator": "contains", "value": "ues"}, 3),
    ({"column_id": None, "operator": "starts_with", "value": "Ou"}, 3),
])
def test_filtres_texte(flt, attendu):
    flt = {**flt, "column_id": str(C_REGION)}
    assert scalar({"schema_version": "1.0", "operation": "count_rows",
                   "source": _source(), "filters": [flt]}) == attendu


@pytest.mark.parametrize("operator,value,attendu", [
    ("greater_than", "1000.50", 2),
    ("greater_or_equal", "1000.50", 3),
    ("less_than", "1000.50", 1),
    ("less_or_equal", "1000.50", 2),
])
def test_filtres_numeriques(operator, value, attendu):
    assert scalar({
        "schema_version": "1.0", "operation": "count_rows", "source": _source(),
        "filters": [{"column_id": str(C_CA), "operator": operator, "value": value}],
    }) == attendu


def test_filtre_between():
    assert scalar({
        "schema_version": "1.0", "operation": "count_rows", "source": _source(),
        "filters": [{"column_id": str(C_CA), "operator": "between",
                     "values": ["1000", "3000"]}],
    }) == 3


def test_filtres_is_null_et_is_not_null():
    for operator, attendu in (("is_null", 1), ("is_not_null", 4)):
        assert scalar({
            "schema_version": "1.0", "operation": "count_rows", "source": _source(),
            "filters": [{"column_id": str(C_CA), "operator": operator}],
        }) == attendu


def test_filtres_combines_en_conjonction():
    assert scalar({
        "schema_version": "1.0", "operation": "count_rows", "source": _source(),
        "filters": [
            {"column_id": str(C_REGION), "operator": "equals", "value": "Ouest"},
            {"column_id": str(C_STATUT), "operator": "equals", "value": "Gagné"},
        ],
    }) == 2


# --------------------------------------------------------------------------- #
# Classements
# --------------------------------------------------------------------------- #
def test_top_n_respecte_la_mesure_demandee():
    _plan, _q, outcome = run({
        "schema_version": "1.0", "operation": "top_n", "source": _source(),
        "order_by": [{"column_id": str(C_CA), "direction": "desc"}],
        "limit": 2, "select_columns": [str(C_REF)],
    })
    assert [r[0] for r in outcome.rows] == ["000003", "000002"]


def test_bottom_n():
    _plan, _q, outcome = run({
        "schema_version": "1.0", "operation": "bottom_n", "source": _source(),
        "order_by": [{"column_id": str(C_CA), "direction": "asc"}],
        "limit": 2, "select_columns": [str(C_REF)],
    })
    # Le NULL est trié en dernier par DuckDB en ASC : il ne doit pas usurper le minimum.
    assert outcome.rows[0][0] == "000004"


def test_argmax_rend_deux_lignes_pour_detecter_un_ex_aequo():
    """`LIMIT 2` est délibéré : sans la seconde ligne, un ex æquo serait invisible."""
    _plan, query, outcome = run({
        "schema_version": "1.0", "operation": "argmax", "source": _source(),
        "target": {"column_id": str(C_CA)}, "select_columns": [str(C_REF)],
    })
    assert "LIMIT 2" in query.sql
    assert outcome.rows[0][0] == "000003"
    assert len(outcome.rows) == 2


def test_limite_bornee_par_le_plafond_serveur():
    _plan, query, _o = run({
        "schema_version": "1.0", "operation": "top_n", "source": _source(),
        "order_by": [{"column_id": str(C_CA), "direction": "desc"}],
        "limit": 999_999, "select_columns": [str(C_REF)],
    })
    assert f"LIMIT {LIMITS.max_result_rows}" in query.sql


# --------------------------------------------------------------------------- #
# Identifiants — le zéro initial ne se perd jamais
# --------------------------------------------------------------------------- #
def test_zeros_initiaux_preserves():
    _plan, _q, outcome = run({
        "schema_version": "1.0", "operation": "filter_rows", "source": _source(),
        "filters": [{"column_id": str(C_REF), "operator": "equals", "value": "000001"}],
        "select_columns": [str(C_REF)],
    })
    assert outcome.rows == (("000001",),)


def test_un_identifiant_nest_jamais_infere_comme_nombre():
    assert T.infer_column_type(["000001", "000002"])[0] == T.TYPE_IDENTIFIER
    assert T.coerce("001245", T.TYPE_IDENTIFIER) == "001245"


# --------------------------------------------------------------------------- #
# Refus structurels — la frontière de confiance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload,code", [
    ({"schema_version": "1.0", "operation": "os.system", "source": None},
     E.ERR_UNSUPPORTED_OPERATION),
    ({"schema_version": "1.0", "operation": "median", "source": None,
      "target": {"column_id": None}}, E.ERR_UNSUPPORTED_OPERATION),
    ({"schema_version": "1.0", "operation": "join", "source": None},
     E.ERR_UNSUPPORTED_OPERATION),
    ({"schema_version": "1.0", "operation": "group_by", "source": None},
     E.ERR_UNSUPPORTED_OPERATION),
    ({"schema_version": "9.9", "operation": "count_rows", "source": None},
     E.ERR_INVALID_SCHEMA_VERSION),
])
def test_operations_hors_perimetre_sont_refusees(payload, code):
    payload = {**payload, "source": _source()}
    if payload.get("target"):
        payload["target"] = {"column_id": str(C_CA)}
    with pytest.raises(E.TabularError) as exc:
        PS.parse_plan(payload)
    assert exc.value.code == code


@pytest.mark.parametrize("extra", [
    {"sql": "SELECT 1"},
    {"expression": "__import__('os').system('id')"},
    {"path": "/etc/passwd"},
    {"function": "read_csv"},
    {"connection": "attach"},
])
def test_aucun_champ_libre_ne_survit_au_schema(extra):
    """`extra=forbid` : un champ non prévu fait ÉCHOUER, il n'est pas ignoré.

    Ignorer un champ inconnu le laisserait disponible pour un composant en aval.
    """
    with pytest.raises(E.TabularError) as exc:
        PS.parse_plan({"schema_version": "1.0", "operation": "count_rows",
                       "source": _source(), **extra})
    assert exc.value.code == E.ERR_INVALID_PLAN_SCHEMA


def test_operateur_de_filtre_inconnu_refuse():
    with pytest.raises(E.TabularError) as exc:
        PS.parse_plan({
            "schema_version": "1.0", "operation": "count_rows", "source": _source(),
            "filters": [{"column_id": str(C_REGION),
                         "operator": "; DROP TABLE t; --", "value": "x"}],
        })
    assert exc.value.code == E.ERR_UNSUPPORTED_FILTER_OPERATOR


def test_limite_negative_refusee():
    with pytest.raises(E.TabularError):
        PS.parse_plan({"schema_version": "1.0", "operation": "top_n",
                       "source": _source(), "limit": -5,
                       "order_by": [{"column_id": str(C_CA), "direction": "desc"}]})


def test_colonne_hors_table_refusee_a_la_compilation():
    plan = PS.parse_plan({"schema_version": "1.0", "operation": "sum",
                          "source": _source(),
                          "target": {"column_id": str(uuid.uuid4())}})
    with pytest.raises(E.TabularError) as exc:
        PC.compile_plan(plan, positions=POSITIONS, max_result_rows=1000)
    assert exc.value.code == E.ERR_COLUMN_NOT_IN_TABLE


# --------------------------------------------------------------------------- #
# Injection — les valeurs ne deviennent jamais du SQL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("charge", [
    "Ouest'; DROP TABLE t; --",
    "' OR 1=1 --",
    "'; INSTALL httpfs; --",
    "'; ATTACH '/tmp/x.db'; --",
    "'; COPY t TO '/tmp/out.csv'; --",
    "../../etc/passwd",
])
def test_une_valeur_adversariale_reste_une_valeur(charge):
    plan, query, outcome = run({
        "schema_version": "1.0", "operation": "count_rows", "source": _source(),
        "filters": [{"column_id": str(C_REGION), "operator": "equals", "value": charge}],
    })
    # La charge est passée en PARAMÈTRE, jamais concaténée dans le texte SQL.
    assert charge in query.params
    assert charge not in query.sql
    assert query.sql.count(";") == 0          # une seule instruction, jamais deux
    assert outcome.rows[0][0] == 0            # aucune ligne, et la table est intacte
    assert scalar({"schema_version": "1.0", "operation": "count_rows",
                   "source": _source()}) == 5


def test_un_nom_de_colonne_hostile_natteint_jamais_le_sql():
    """Une colonne nommée `"; DROP TABLE t; --"` se compile en `c3`.

    C'est le cœur du modèle : les alias viennent d'ENTIERS (la position), donc aucun
    caractère du classeur ne peut atteindre le texte SQL — il n'y a rien à échapper.
    """
    assert PC.column_alias(3) == "c3"
    _plan, query, _o = run({"schema_version": "1.0", "operation": "sum",
                            "source": _source(), "target": {"column_id": str(C_CA)}})
    assert query.sql == "SELECT SUM(c3) AS value FROM t"


# --------------------------------------------------------------------------- #
# Déterminisme
# --------------------------------------------------------------------------- #
def test_resultat_plan_et_hash_stables_sur_cinq_executions():
    payload = {
        "schema_version": "1.0", "operation": "sum", "source": _source(),
        "target": {"column_id": str(C_CA)},
        "filters": [{"column_id": str(C_REGION), "operator": "equals", "value": "Ouest"}],
    }
    vus = set()
    for _ in range(5):
        plan, query, outcome = run(payload)
        vus.add((plan.plan_hash(), query.sql, query.params, outcome.rows))
    assert len(vus) == 1


def test_le_hash_de_plan_change_avec_le_plan():
    base = {"schema_version": "1.0", "operation": "sum", "source": _source(),
            "target": {"column_id": str(C_CA)}}
    autre = {**base, "filters": [{"column_id": str(C_REGION),
                                  "operator": "equals", "value": "Ouest"}]}
    assert PS.parse_plan(base).plan_hash() != PS.parse_plan(autre).plan_hash()


# --------------------------------------------------------------------------- #
# Limites de ressources
# --------------------------------------------------------------------------- #
def test_depassement_du_nombre_de_lignes_est_explicite():
    plan = PS.parse_plan({"schema_version": "1.0", "operation": "count_rows",
                          "source": _source()})
    query = PC.compile_plan(plan, positions=POSITIONS, max_result_rows=1000)
    with pytest.raises(E.TabularLimitExceeded) as exc:
        EX.execute(query, columns=COLUMNS, rows=ROWS,
                   limits=_Limits(max_rows_scanned=2))
    assert exc.value.code == E.ERR_TOO_MANY_ROWS


def test_depassement_du_nombre_de_colonnes_est_explicite():
    plan = PS.parse_plan({"schema_version": "1.0", "operation": "count_rows",
                          "source": _source()})
    query = PC.compile_plan(plan, positions=POSITIONS, max_result_rows=1000)
    with pytest.raises(E.TabularLimitExceeded) as exc:
        EX.execute(query, columns=COLUMNS, rows=ROWS, limits=_Limits(max_columns=2))
    assert exc.value.code == E.ERR_TOO_MANY_COLUMNS


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("brut,attendu", [
    ("1 234,56", Decimal("1234.56")),
    ("1,234.56", Decimal("1234.56")),
    ("12 %", Decimal("0.12")),
    ("€ 1 250", Decimal("1250")),
    ("(1 234,56)", Decimal("-1234.56")),
    ("1234,56-", Decimal("-1234.56")),
])
def test_normalisation_numerique(brut, attendu):
    assert T.parse_decimal(brut) == attendu


def test_un_nombre_ambigu_est_refuse_pas_devine():
    """`1,234` vaut 1.234 ou 1234 selon la locale. Deviner = faux résultat silencieux."""
    with pytest.raises(T.TabularTypeError):
        T.parse_decimal("1,234")


def test_une_date_ambigue_est_refusee():
    with pytest.raises(T.TabularTypeError):
        T.parse_date("03/04/2025")
    assert str(T.parse_date("2026-02-01")) == "2026-02-01"


def test_une_erreur_excel_nest_jamais_traitee_comme_du_texte():
    with pytest.raises(T.TabularTypeError) as exc:
        T.coerce("#DIV/0!", T.TYPE_DECIMAL)
    assert exc.value.code == E.ERR_EXCEL_ERROR_VALUE


def test_une_valeur_invalide_ne_devient_jamais_zero():
    with pytest.raises(T.TabularTypeError):
        T.coerce("pas un nombre", T.TYPE_DECIMAL)


def test_zero_et_false_ne_sont_pas_vides():
    assert T.is_empty(0) is False
    assert T.is_empty(False) is False
    assert T.is_empty("") is True
    assert T.is_empty(None) is True


def test_la_devise_est_conservee():
    ctype, unit = T.infer_column_type(["€ 1 250", "€ 3 000"])
    assert ctype == T.TYPE_CURRENCY
    assert unit == "EUR"


def test_colonne_de_type_mixte_devient_texte_pas_un_nombre_partiel():
    """Un mélange non numérique rend la colonne inexploitable pour une somme.

    Convertir « ce qui passe » laisserait tomber des lignes en silence.
    """
    ctype, _ = T.infer_column_type(["100", "abc", "300"])
    assert ctype == T.TYPE_TEXT

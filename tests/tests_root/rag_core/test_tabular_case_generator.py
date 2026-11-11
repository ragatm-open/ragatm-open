"""Générateur de cas `tabular_aggregation` — profilage, gabarits, rejets, diversité.

Tests PURS : tables en mémoire, aucun classeur réel, aucune DB, aucun retrieval.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.rag_core.benchmark import tabular_aggregation_schema as s
from apps.rag_core.benchmark import tabular_case_generator as gen
from apps.rag_core.benchmark import tabular_reference_executor as ex

SHA = "a" * 64

HEADERS = ["Dossier", "Statut", "Montant", "Échéance"]
DATA = [
    ["D-1", "En cours", 100, dt.date(2025, 1, 15)],
    ["D-2", "Clos",     250, dt.date(2025, 3, 20)],
    ["D-3", "En cours",  50, dt.date(2024, 11, 5)],
    ["D-4", "En cours", 300, dt.date(2025, 6, 30)],
    ["D-5", "Clos",     175, dt.date(2024, 2, 1)],
    ["D-6", "En cours", 425, dt.date(2025, 9, 9)],
]


def _table(headers=None, data=None):
    return ex.build_table(headers or HEADERS, data if data is not None else DATA)


def _source(rows=6, **kw):
    base = dict(sheet_name="Demandes", table_range=f"A1:D{rows + 1}", header_rows=(1,),
                data_row_start=2, data_row_end=rows + 1)
    base.update(kw)
    return gen.TableSource(content_sha256=SHA, filename="d.xlsx", path="/tmp/d.xlsx",
                           locator=s.TableLocator(**base))


#: Plafonds relevés pour observer TOUT ce qu'une table peut produire. Les plafonds
#: réels (DEFAULT_CAPS) sont testés séparément — ils sont là pour brider la diversité,
#: pas pour limiter ce que le générateur sait faire.
UNCAPPED = {k: 500 for k in gen.DEFAULT_CAPS}


def _generate(table=None, source=None, caps=None):
    stats = gen.GenerationStats()
    cases, rejects = gen.generate_for_table(
        source or _source(), table or _table(), rag_space="dev_bench_excel",
        stats=stats, caps=caps or UNCAPPED)
    return cases, rejects, stats


# --------------------------------------------------------------------------- #
# Profilage des colonnes
# --------------------------------------------------------------------------- #
def test_profile_classifies_columns_by_what_they_allow():
    table = _table()
    pop = ex.build_population(table, _source().locator, s.RowPopulationPolicy())
    p = gen.profile_table(table, pop)
    assert "montant" in p.numeric
    assert "statut" in p.categorical
    assert "échéance" in p.dates
    assert "dossier" in p.identity      # valeurs toutes distinctes


def test_constant_numeric_column_is_not_aggregatable():
    """Somme = n x valeur, max = valeur : la colonne ne mesure rien."""
    table = _table(["K", "V"], [["a", 7], ["b", 7], ["c", 7], ["d", 7], ["e", 7]])
    src = _source(rows=5, table_range="A1:B6")
    pop = ex.build_population(table, src.locator, s.RowPopulationPolicy())
    assert "v" not in gen.profile_table(table, pop).numeric


def test_generic_headers_are_never_profiled():
    table = _table(["col1", "Unnamed: 0", "Montant"],
                   [[i, i, i * 10] for i in range(1, 7)])
    src = _source(rows=6, table_range="A1:C7")
    pop = ex.build_population(table, src.locator, s.RowPopulationPolicy())
    p = gen.profile_table(table, pop)
    assert p.numeric == ("montant",)


def test_high_cardinality_text_is_not_categorical():
    """Une colonne à valeurs toutes distinctes n'offre pas de filtre discriminant."""
    table = _table(["K", "V"], [[f"unique-{i}", i] for i in range(1, 8)])
    src = _source(rows=7, table_range="A1:B8")
    pop = ex.build_population(table, src.locator, s.RowPopulationPolicy())
    assert "k" not in gen.profile_table(table, pop).categorical


# --------------------------------------------------------------------------- #
# Génération
# --------------------------------------------------------------------------- #
def test_generation_produces_valid_diverse_cases():
    cases, _, stats = _generate()
    assert cases, "aucun cas généré"
    for case in cases:
        s.validate_case(case)                       # schéma, task_type, absence d'UUID
        assert case["human_validation_status"] == "pending"
        assert case["ground_truth"]["aggregation_evidence"]["selected_row_count"] >= 1
    assert len(stats.operations) >= 4, stats.operations


def test_generated_questions_are_self_contained():
    """Chaque question nomme la feuille, la mesure et le filtre éventuel (§26)."""
    cases, _, _ = _generate()
    for case in cases:
        q = case["question"]
        assert "Demandes" in q
        assert q.strip().endswith("?")
        assert len(q) > 30


def test_ground_truth_is_computed_not_declared():
    """La somme attendue vaut la vraie somme du classeur, pas une valeur posée."""
    cases, _, _ = _generate()
    sums = [c for c in cases if c["operation"] == s.OP_SUM]
    assert sums
    assert Decimal(sums[0]["ground_truth"]["expected_result"]["value"]) == Decimal(1300)


def test_count_case_matches_the_population():
    cases, _, _ = _generate()
    counts = [c for c in cases if c["operation"] == s.OP_COUNT_ROWS]
    assert counts and counts[0]["ground_truth"]["expected_result"]["value"] == 6


def test_argmax_case_carries_a_row_identifier():
    cases, _, _ = _generate()
    args = [c for c in cases if c["operation"] == s.OP_ARGMAX]
    assert args
    assert args[0]["ground_truth"]["expected_result"]["value"] == {"Dossier": "D-6"}


# --------------------------------------------------------------------------- #
# Déterminisme (§46)
# --------------------------------------------------------------------------- #
def test_generation_is_reproducible():
    a, _, _ = _generate()
    b, _, _ = _generate()
    assert [s.dump_case(c) for c in a] == [s.dump_case(c) for c in b]


def test_case_ids_are_unique_within_a_table():
    cases, _, _ = _generate()
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# Rejets (§27)
# --------------------------------------------------------------------------- #
def test_too_small_a_population_is_rejected():
    table = _table(["K", "V"], [["a", 1], ["b", 2]])
    _, rejects, stats = _generate(table, _source(rows=2, table_range="A1:B3"))
    assert stats.accepted == 0
    assert any(r["code"] == s.ERR_TRIVIAL_SINGLE_ROW for r in rejects)


def test_table_without_usable_columns_is_rejected():
    table = _table(["col1", "col2"], [[i, i] for i in range(1, 7)])
    _, rejects, stats = _generate(table, _source(rows=6, table_range="A1:B7"))
    assert stats.accepted == 0
    assert any(r["code"] == s.ERR_CONSTANT_TARGET_COLUMN for r in rejects)


def test_materialized_total_makes_the_sum_a_lookup_not_an_aggregation():
    """Si une ligne TOTAL porte déjà la somme, le cas relève du retrieval_lookup."""
    data = [["D-1", "En cours", 100, None], ["D-2", "Clos", 200, None],
            ["D-3", "En cours", 300, None], ["D-4", "Clos", 400, None],
            ["D-5", "En cours", 500, None], ["TOTAL", "", 1500, None]]
    _, rejects, _ = _generate(_table(data=data), _source(rows=6))
    assert any(r["code"] == s.ERR_MATERIALIZED_AGGREGATE for r in rejects)


def test_an_equality_filter_selecting_every_row_is_not_a_filter():
    """Colonne catégorielle constante : aucun filtre d'ÉGALITÉ ne discrimine.

    Les filtres à seuil sur la colonne numérique restent légitimes — ils découpent
    réellement la population. On vérifie donc l'absence de filtre `eq`, pas l'absence
    de tout cas filtré.
    """
    data = [[f"D-{i}", "Unique", i * 10, None] for i in range(1, 7)]
    cases, _, _ = _generate(_table(data=data), _source(rows=6))
    equality = [c for c in cases
                if any(f["operator"] == "eq"
                       for f in c["ground_truth"]["selection"]["filters"])]
    assert not equality


def test_question_containing_its_own_answer_is_detected():
    assert gen.question_contains_answer("Combien de lignes ont le statut « 12 » ?", "12")
    assert not gen.question_contains_answer("Quelle est la somme de « Montant » ?", "1300")
    assert gen.question_contains_answer("Quel Dossier a le maximum ?", {"Dossier": "Dossier"})


# --------------------------------------------------------------------------- #
# Diversité (§35)
# --------------------------------------------------------------------------- #
def test_per_table_cap_limits_the_yield():
    cases, rejects, _ = _generate(caps={**gen.DEFAULT_CAPS, "per_table": 2})
    assert len(cases) == 2
    assert any(r["code"] == s.ERR_DUPLICATE_CASE for r in rejects)


def test_expected_value_cap_prevents_repeating_the_same_answer():
    caps = {**gen.DEFAULT_CAPS, "per_table": 50, "per_expected_value": 1}
    cases, _, _ = _generate(caps=caps)
    values = [str(c["ground_truth"]["expected_result"]["value"]) for c in cases]
    assert len(values) == len(set(values))


# --------------------------------------------------------------------------- #
# Difficulté (§36)
# --------------------------------------------------------------------------- #
def test_difficulty_reflects_case_structure_not_file_size():
    from apps.rag_core.benchmark import tabular_types as tt
    assert gen.classify_difficulty(s.OP_SUM, filters=0, column_type=tt.TYPE_INTEGER) == gen.DIFFICULTY_EASY
    assert gen.classify_difficulty(s.OP_FILTERED_SUM, filters=1, column_type=tt.TYPE_INTEGER) == gen.DIFFICULTY_MEDIUM
    assert gen.classify_difficulty(s.OP_ARGMAX, filters=0, column_type=tt.TYPE_INTEGER) == gen.DIFFICULTY_HARD
    assert gen.classify_difficulty(s.OP_SUM, filters=2, column_type=tt.TYPE_INTEGER) == gen.DIFFICULTY_HARD


# --------------------------------------------------------------------------- #
# Aucun LLM (§25)
# --------------------------------------------------------------------------- #
def test_generator_never_imports_a_model_or_a_random_source():
    """Garde structurelle sur les IMPORTS, pas sur le texte.

    Chercher la sous-chaîne « llm » échouerait sur la docstring qui promet justement
    de ne pas en utiliser. Seuls les imports réels engagent le comportement.
    """
    import ast

    with open(gen.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = ("openai", "ollama", "llm_router", "random", "secrets", "time", "datetime")
    for mod in imported:
        assert not any(f in mod for f in forbidden), f"import interdit : {mod}"


@pytest.mark.parametrize("operation", sorted(gen.TEMPLATES))
def test_every_supported_operation_has_a_template(operation):
    assert operation in s.SUPPORTED_OPERATIONS
    assert "{sheet}" in gen.TEMPLATES[operation]


# --------------------------------------------------------------------------- #
# Sélection de la colonne d'identité — règle explicite et déterministe
# --------------------------------------------------------------------------- #
def test_identity_selection_follows_lexicographic_order():
    """La règle est nommée et stable, pas un `[0]` sur un ordre de parcours."""
    assert gen.select_identity_column(["Référence", "Code", "Libellé"]) == "Code"
    assert gen.select_identity_column(["b", "a"]) == "a"


def test_identity_selection_is_independent_of_input_order():
    """Deux ingestions ordonnant les colonnes différemment donnent le même cas."""
    cols = ["Référence", "Code", "Libellé"]
    assert gen.select_identity_column(cols) == gen.select_identity_column(list(reversed(cols)))


def test_identity_selection_without_candidate_returns_none():
    assert gen.select_identity_column(()) is None
    assert gen.select_identity_column([]) is None


def test_generated_argmax_uses_the_selected_identity_column():
    """Deux colonnes d'identité : le cas nomme explicitement celle qu'il retient."""
    headers = ["Zeta", "Alpha", "Montant"]
    data = [[f"z{i}", f"a{i}", i * 10] for i in range(1, 8)]
    src = _source(rows=7, table_range="A1:C8")
    cases, _, _ = _generate(ex.build_table(headers, data), src)
    args = [c for c in cases if c["operation"] in (s.OP_ARGMIN, s.OP_ARGMAX)]
    assert args, "aucun cas argmin/argmax généré"
    for case in args:
        identity = case["ground_truth"]["aggregation"]["identity_columns"]
        assert [c["source_header"] for c in identity] == ["Alpha"]

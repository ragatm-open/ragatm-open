"""Tests de la dérivation mécanique de cas XLSX (P1.9-b).

⚠️ Fixtures 100 % SYNTHÉTIQUES : aucun contenu, nom de fichier ou valeur issus du
corpus client réel ne doit apparaître ici (ni dans le dépôt).
"""
from pathlib import Path

from apps.rag_core.benchmark import xlsx_cases as xc

REPO = Path(__file__).resolve().parents[3]

# Tableau markdown synthétique (colonnes volontairement proches pour l'ambiguïté).
TABLE = """Feuille de suivi
| Lot | Montant brut | Montant net | Date début | Statut |
| --- | --- | --- | --- | --- |
| L-001 | 1 200,00 € | 1 000,00 € | 2026-01-15 | ouvert |
| L-002 | 2 400,00 € | 2 000,00 € | 2026-02-20 | clos |
"""
HEADERS = ["Lot", "Montant brut", "Montant net", "Date début", "Statut"]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_parse_markdown_table():
    headers, rows = xc.parse_markdown_table(TABLE)
    assert headers == HEADERS
    assert len(rows) == 2 and rows[0][0] == "L-001"


def test_parse_ignores_text_without_table():
    assert xc.parse_markdown_table("aucun tableau ici") == ([], [])


# --------------------------------------------------------------------------
# Typage
# --------------------------------------------------------------------------
def test_classify_values():
    assert xc.classify_value("2026-01-15") == "date"
    assert xc.classify_value("12,5 %") == "percent"
    assert xc.classify_value("1 200,00 €") == "amount"
    assert xc.classify_value("42") == "quantity"
    assert xc.classify_value("REF-2026-04") == "identifier"
    assert xc.classify_value("ouvert") == "status"


def test_column_value_type_majority():
    _, rows = xc.parse_markdown_table(TABLE)
    assert xc.column_value_type(rows, HEADERS.index("Date début")) == "date"
    assert xc.column_value_type(rows, HEADERS.index("Statut")) == "status"


# --------------------------------------------------------------------------
# Colonnes ambiguës (§9)
# --------------------------------------------------------------------------
def test_detect_ambiguous_groups():
    groups = xc.detect_ambiguous_groups(HEADERS)
    assert "montant" in groups
    assert set(groups["montant"]) == {"Montant brut", "Montant net"}
    assert xc.is_ambiguous_column("Montant net", HEADERS) is True
    assert xc.is_ambiguous_column("Statut", HEADERS) is False


def test_detect_ambiguous_groups_variants():
    hs = ["Quantité commandée", "Quantité livrée", "Valeur prévue", "Valeur réalisée", "Lot"]
    g = xc.detect_ambiguous_groups(hs)
    assert set(g["quantite"]) == {"Quantité commandée", "Quantité livrée"}
    assert set(g["valeur"]) == {"Valeur prévue", "Valeur réalisée"}


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------
def test_question_names_the_requested_column_explicitly():
    q = xc.build_question(sub_category=xc.SUB_AMBIGUOUS, sheet="Suivi",
                          measure="Montant net", identity_col="Lot", row_id="L-001")
    assert "Montant net" in q and "Suivi" in q and "L-001" in q


def test_question_does_not_contain_the_answer():
    q = xc.build_question(sub_category=xc.SUB_DIRECT_VALUE, sheet="Suivi",
                          measure="Montant net", identity_col="Lot", row_id="L-001")
    assert "1 000,00" not in q


# --------------------------------------------------------------------------
# Difficulté (§10)
# --------------------------------------------------------------------------
def test_assign_difficulty_deterministic():
    assert xc.assign_difficulty(sub_category=xc.SUB_DIRECT_VALUE, ambiguous=False,
                                wide_table=False, n_identity=1) == "easy"
    assert xc.assign_difficulty(sub_category=xc.SUB_DIRECT_VALUE, ambiguous=True,
                                wide_table=False, n_identity=1) == "medium"
    assert xc.assign_difficulty(sub_category=xc.SUB_DIRECT_VALUE, ambiguous=False,
                                wide_table=True, n_identity=1) == "medium"
    assert xc.assign_difficulty(sub_category=xc.SUB_MULTI_COLUMN, ambiguous=False,
                                wide_table=False, n_identity=1) == "hard"


# --------------------------------------------------------------------------
# Validation mécanique (§8)
# --------------------------------------------------------------------------
def _case(**over):
    base = {
        "case_id": "mission-xlsx-001",
        "question": "Dans la feuille « Suivi », quelle est la valeur de « Montant net » pour le Lot « L-001 » ?",
        "expected_source_chunk_ids": ["chunk-1"],
        "expected_document_ids": ["doc-1"],
        "fact_group_id": "g1",
        "human_validation_status": "pending",
        "ground_truth": {
            "sheet_name": "Suivi", "identity_columns": ["Lot"],
            "measure_column": "Montant net", "measure_columns": ["Montant net"],
            "row_identifier": "L-001", "expected_value": "1 000,00 €",
            "expected_values": ["1 000,00 €"],
        },
    }
    base.update(over)
    return base


def test_validate_ok():
    assert xc.validate_case(_case(), TABLE, HEADERS) == []


def test_validate_rejects_unknown_measure_column():
    c = _case()
    c["ground_truth"]["measure_columns"] = ["Colonne fantôme"]
    assert xc.REJ_NO_MEASURE in xc.validate_case(c, TABLE, HEADERS)


def test_validate_rejects_value_absent_from_chunk():
    c = _case()
    c["ground_truth"]["expected_values"] = ["9 999,00 €"]
    assert xc.REJ_VALUE_NOT_IN_CHUNK in xc.validate_case(c, TABLE, HEADERS)


def test_validate_rejects_empty_value():
    c = _case()
    c["ground_truth"]["expected_values"] = [""]
    assert xc.REJ_EMPTY_VALUE in xc.validate_case(c, TABLE, HEADERS)


def test_validate_rejects_question_containing_the_answer():
    c = _case(question="Le montant net vaut-il 1 000,00 € pour le Lot « L-001 » ?")
    assert xc.REJ_QUESTION_CONTAINS_ANSWER in xc.validate_case(c, TABLE, HEADERS)


def test_validate_rejects_missing_sheet_or_identity():
    c = _case()
    c["ground_truth"]["sheet_name"] = ""
    c["ground_truth"]["identity_columns"] = []
    errs = xc.validate_case(c, TABLE, HEADERS)
    assert xc.REJ_NO_SHEET in errs and xc.REJ_NO_IDENTITY in errs


def test_validate_requires_pending_status():
    c = _case(human_validation_status="validated")
    assert "not_marked_pending" in xc.validate_case(c, TABLE, HEADERS)


# --------------------------------------------------------------------------
# Confidentialité : valeurs non anonymisables
# --------------------------------------------------------------------------
def test_is_sensitive_detects_pii():
    assert xc.is_sensitive("jean.dupont@example.com") is True
    assert xc.is_sensitive("+33 6 12 34 56 78") is True
    assert xc.is_sensitive("FR7630006000011234567890189") is True
    assert xc.is_sensitive("L-001") is False
    assert xc.is_sensitive("1 200,00 €") is False


# --------------------------------------------------------------------------
# Split par fact_group_id (§11) — anti-fuite
# --------------------------------------------------------------------------
def _cases_with_groups():
    out = []
    for g in range(10):
        for k in range(2):  # 2 paraphrases par fait
            out.append({"case_id": f"c{g}-{k}", "fact_group_id": f"g{g}"})
    return out


def test_split_by_fact_group_has_no_leak():
    cal, hold = xc.split_by_fact_group(_cases_with_groups(), calibration_ratio=0.7, seed=1)
    assert xc.split_leaks(cal, hold) == set()
    # les 2 paraphrases d'un même fait restent du même côté
    for g in range(10):
        sides = {"cal" if any(c["fact_group_id"] == f"g{g}" for c in cal) else None,
                 "hold" if any(c["fact_group_id"] == f"g{g}" for c in hold) else None}
        assert sides & {"cal", "hold"} != {"cal", "hold"}


def test_split_is_deterministic_and_ratio_respected():
    cases = _cases_with_groups()
    a = xc.split_by_fact_group(cases, calibration_ratio=0.7, seed=42)
    b = xc.split_by_fact_group(cases, calibration_ratio=0.7, seed=42)
    assert [c["case_id"] for c in a[0]] == [c["case_id"] for c in b[0]]
    assert len({c["fact_group_id"] for c in a[0]}) == 7  # 70 % de 10 groupes


# --------------------------------------------------------------------------
# Confidentialité Git
# --------------------------------------------------------------------------
def test_mission_dataset_paths_are_gitignored():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "backend/benchmarks/datasets/mission/" in ignored
    assert "backend/benchmarks/baseline/mission-*" in ignored
    assert "backend/benchmarks/calibration/mission-*" in ignored

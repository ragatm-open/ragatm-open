"""Tests purs de apps.rag_core.domain.retrieval.build_prompt."""
from apps.rag_core.domain.retrieval import build_prompt


def test_question_appears_in_prompt():
    out = build_prompt("Quelle est la capitale ?", [])
    assert "Quelle est la capitale ?" in out


def test_chunk_label_and_text_rendered():
    chunks = [{"text": "Paris", "source": {"filename": "a.txt"}}]
    out = build_prompt("Q?", chunks)
    assert "[1] a.txt" in out
    assert "Paris" in out


def test_label_fallback_when_no_source():
    out = build_prompt("Q?", [{"text": "hello"}])
    assert "[1] Source 1" in out


def test_document_date_in_header():
    chunks = [{"text": "x", "source": {"filename": "a.txt"}, "document_date": "2025-01-02"}]
    out = build_prompt("Q?", chunks)
    assert "(2025-01-02)" in out


def test_page_number_rendered():
    chunks = [{"text": "x", "source": {"filename": "a.txt"}, "page_number": 3}]
    out = build_prompt("Q?", chunks)
    assert "p.3" in out


def test_non_integer_page_best_effort():
    chunks = [{"text": "x", "source": {"filename": "a.txt"}, "page": "iv"}]
    out = build_prompt("Q?", chunks)
    assert "p.iv" in out


def test_custom_template_with_bad_placeholder_falls_back():
    # Un placeholder inconnu ne doit pas crasher : fallback sur le template par défaut.
    out = build_prompt("Q?", [{"text": "x"}], template_content="{badkey}")
    assert isinstance(out, str) and len(out) > 0
    assert "Q?" in out


def test_total_context_budget_truncates_following_chunks():
    # chaque chunk est tronqué à 2000 chars ; budget total 12000 chars.
    # 8 chunks pleins dépassent le budget -> le chunk "MARKER_LAST" final est exclu.
    chunks = [
        {"text": "a" * 2000, "source": {"filename": f"{i}.txt"}} for i in range(8)
    ]
    chunks.append({"text": "MARKER_LAST", "source": {"filename": "last.txt"}})
    out = build_prompt("Q?", chunks)
    assert "MARKER_LAST" not in out


# --------------------------------------------------- P3 : sources tableaux
def _table_chunk():
    return {
        "text": "Tableau détecté : Efforts en têtes de pieux\n| node | case | fz |\n|---|---|---|\n| 153 | ELU+ | -160 |",
        "source": {
            "filename": "ndc.pdf",
            "page_number": 18,
            "meta": {"kind": "table", "table_type": "pile_head_forces",
                     "requires_visual_check": False},
        },
    }


def test_table_source_marked_and_rules_injected():
    out = build_prompt("FZ au noeud 153 en ELU ?", [_table_chunk()])
    assert "TABLEAU pile_head_forces" in out
    assert "RÈGLES TABLEAUX" in out
    assert "Ne calcule JAMAIS" in out
    assert "153" in out and "-160" in out


def test_table_rules_absent_without_table():
    out = build_prompt("Q?", [{"text": "Paris", "source": {"filename": "a.txt"}}])
    assert "RÈGLES TABLEAUX" not in out
    assert "TABLEAU" not in out


def test_table_rules_with_custom_template():
    out = build_prompt("Q?", [_table_chunk()], template_content="Mon template {context}")
    assert "RÈGLES TABLEAUX" in out
    assert "Mon template" in out


def _excel_chunk():
    return {
        "text": ("Feuille : Summary Pro Forma — Premoney Capitalization (B3:D13)\n"
                 "| Security | Premoney Capitalization / Shares Outstanding |\n"
                 "| Series A-1 | 3450656 |"),
        "source": {
            "filename": "Cap.xlsx",
            "meta": {"kind": "excel_table", "sheet_name": "Summary Pro Forma",
                     "column_group": "Premoney Capitalization", "cell_range": "B3:D13"},
        },
    }


def test_excel_disambiguation_rule_is_injected():
    """P1.6 — désambiguïsation ENTITÉ + MESURE. On teste les CONCEPTS, pas une chaîne
    exacte fragile : mesure demandée, en-tête de colonne, ligne partagée, autre colonne
    ignorée, l'ordre n'est pas une preuve, clarification si ambigu."""
    out = build_prompt("Shares Outstanding de Series A-1 ?", [_excel_chunk()]).lower()
    assert "mesure" in out                       # identifier la mesure demandée
    assert "colonne" in out and "en-tête" in out  # comparer les en-têtes
    assert "ordre des extraits" in out           # le rang n'est pas une preuve métier
    assert "premoney" in out and "postmoney" in out  # scénario à désambiguïser
    assert "demande laquelle" in out             # clarification si plusieurs mesures


def test_excel_disambiguation_absent_without_workbook_source():
    out = build_prompt("Q?", [{"text": "un paragraphe", "source": {"filename": "a.pdf"}}])
    assert "RÈGLES CLASSEURS" not in out
    assert "mesure demandée" not in out.lower()

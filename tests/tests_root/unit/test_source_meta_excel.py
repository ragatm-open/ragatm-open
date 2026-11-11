"""Lot E3 — métadonnées de citation : projection, API, audit, déduplication, prompt.

Le fil conducteur : **ce que l'utilisateur voit doit être exactement ce qui est
audité**. API et audit partagent donc la même projection (`project_source_meta`) —
sinon on auditerait autre chose que la réponse affichée.
"""
import pytest

from apps.chat.services.chat_sources import (
    MAX_SNIPPET_CHARS_UI,
    dedup_sources,
    normalize_source_for_api,
    sources_for_api,
)
from apps.rag_core.domain.retrieval import build_prompt
from apps.rag_core.services.query.audit import (
    SOURCES_SCHEMA_VERSION,
    normalize_sources_for_audit,
)
from apps.rag_core.services.query.source_meta import is_excel, project_source_meta

EXCEL_META = {
    "kind": "excel_table",
    "sheet_name": "Synthèse",
    "sheet_index": 2,
    "cell_range": "B8:F17",
    "row_start": 8,
    "row_end": 17,
    "column_start": "B",
    "column_end": "F",
    "header_row": 7,
    "headers": ["Projet", "Budget", "Réalisé"],
    "title": "Synthèse budgétaire",
    "chunk_of": 1,
    "chunk_total": 3,
    "has_formulas": True,
    "requires_visual_check": False,
    # Bruit d'implémentation qui NE DOIT PAS sortir :
    "chunk_index": 4,
    "char_start": 120,
    "char_end": 980,
    "source": "Budget_2026.xlsx",
    "sheet_hidden": False,
}


def _chunk(meta=None, *, filename="Budget_2026.xlsx", page=None, chunk_id="c1",
           text="| Atlas | 250000 | 220000 |"):
    return {
        "chunk_id": chunk_id,
        "text": text,
        "score": 0.9,
        "source": {
            "document_id": "d1",
            "filename": filename,
            "page_number": page,
            "meta": meta or {},
        },
    }


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #
class TestProjection:
    def test_keeps_citation_fields(self):
        m = project_source_meta(EXCEL_META)
        assert m["kind"] == "excel_table"
        assert m["sheet_name"] == "Synthèse"
        assert m["cell_range"] == "B8:F17"
        assert m["row_start"] == 8 and m["row_end"] == 17
        assert m["column_start"] == "B" and m["column_end"] == "F"
        assert m["headers"] == ["Projet", "Budget", "Réalisé"]
        assert m["title"] == "Synthèse budgétaire"
        assert m["has_formulas"] is True

    def test_drops_implementation_noise(self):
        """`Chunk.meta` est un JSONField libre : il porte aussi des détails internes."""
        m = project_source_meta(EXCEL_META)
        # `sheet_hidden` N'EST PLUS du bruit : une feuille masquée est indexée, et
        # l'utilisateur doit le savoir. La taire serait exposer sans le dire.
        for noisy in ("chunk_index", "char_start", "char_end", "source"):
            assert noisy not in m

    def test_drops_pdf_entities_blob(self):
        """Les entités regex (un dict de listes sur CHAQUE chunk PDF) ne sortent pas."""
        m = project_source_meta({
            "page_number": 3,
            "page_type": "plan",
            "entities": {"detected_massifs": ["M01"] * 50, "detected_piles": ["P1"] * 50},
            "source_is_ocr": True,
        })
        assert "entities" not in m
        assert m["page_type"] == "plan"          # ça, c'est utile
        assert m["source_is_ocr"] is True

    @pytest.mark.parametrize("bad", [None, "texte", 42, [], {"sheet_name": 123}])
    def test_never_raises_on_garbage(self, bad):
        assert isinstance(project_source_meta(bad), dict)

    def test_empty_strings_are_dropped(self):
        assert project_source_meta({"sheet_name": "  ", "cell_range": ""}) == {}

    def test_is_excel(self):
        assert is_excel({"kind": "excel_table"}) is True
        assert is_excel({"kind": "excel_narrative"}) is True
        assert is_excel({"kind": "table"}) is False       # tableau PDF (P3)
        assert is_excel({}) is False
        assert is_excel(None) is False


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class TestApiNormalization:
    def test_excel_source_carries_sheet_and_range(self):
        s = normalize_source_for_api(_chunk(EXCEL_META), 1)
        assert s["schema_version"] == SOURCES_SCHEMA_VERSION == 3
        assert s["filename"] == "Budget_2026.xlsx"
        assert s["page_number"] is None            # une feuille n'est PAS une page
        assert s["meta"]["sheet_name"] == "Synthèse"
        assert s["meta"]["cell_range"] == "B8:F17"

    def test_pdf_source_is_unchanged(self):
        s = normalize_source_for_api(
            _chunk({"page_number": 12, "chunk_index": 3}, filename="Rapport.pdf", page=12), 1
        )
        assert s["page_number"] == 12
        assert s["filename"] == "Rapport.pdf"
        assert s["meta"] == {}                     # rien à citer de plus

    def test_source_without_meta_degrades_cleanly(self):
        s = normalize_source_for_api(_chunk(None, filename="note.txt"), 1)
        assert s["meta"] == {}
        assert s["filename"] == "note.txt"


# --------------------------------------------------------------------------- #
# Audit — ce qui est vu doit être ce qui est audité
# --------------------------------------------------------------------------- #
class TestAudit:
    def test_audit_persists_sheet_and_range(self):
        logs = normalize_sources_for_audit([_chunk(EXCEL_META)])
        e = logs[0]
        assert e["schema_version"] == 3
        assert e["meta"]["sheet_name"] == "Synthèse"
        assert e["meta"]["cell_range"] == "B8:F17"
        assert e["meta"]["row_start"] == 8

    def test_audit_and_api_agree(self):
        """L'invariant du lot : auditer autre chose que l'affiché serait pire que rien."""
        ch = _chunk(EXCEL_META)
        api = normalize_source_for_api(ch, 1)
        aud = normalize_sources_for_audit([ch])[0]
        assert api["meta"] == aud["meta"]

    def test_non_excel_audit_entry_has_no_meta_key(self):
        """Additif : pour un PDF simple, le log reste identique à la v2."""
        aud = normalize_sources_for_audit(
            [_chunk({"chunk_index": 1, "char_start": 0}, filename="a.pdf", page=3)]
        )[0]
        assert "meta" not in aud
        assert aud["page_number"] == 3

    def test_old_v2_logs_remain_readable(self):
        """Aucun log n'est migré : une entrée v2 sans `meta` doit rester exploitable."""
        old = {"schema_version": 2, "rank": 1, "filename": "a.pdf", "page_number": 3,
               "snippet": "…", "document_id": "d", "chunk_id": "c", "score": 0.5}
        assert old.get("meta", {}) == {}           # lecture défensive, pas de KeyError


# --------------------------------------------------------------------------- #
# Déduplication — deux plages sont deux sources
# --------------------------------------------------------------------------- #
class TestDedup:
    def test_same_chunk_is_one_source(self):
        a = normalize_source_for_api(_chunk(EXCEL_META, chunk_id="c1"), 1)
        b = normalize_source_for_api(_chunk(EXCEL_META, chunk_id="c1"), 2)
        assert len(dedup_sources([a, b])) == 1

    def test_two_ranges_of_the_same_file_are_two_sources(self):
        m1 = {**EXCEL_META, "sheet_name": "Synthèse", "cell_range": "B8:F17"}
        m2 = {**EXCEL_META, "sheet_name": "Risques", "cell_range": "A2:G12"}
        out = sources_for_api([
            _chunk(m1, chunk_id="c1", text="ligne A"),
            _chunk(m2, chunk_id="c2", text="ligne B"),
        ])
        assert len(out) == 2
        assert {s["meta"]["sheet_name"] for s in out} == {"Synthèse", "Risques"}

    def test_fallback_key_without_chunk_id_still_separates_ranges(self):
        """Sans `chunk_id`, la clé de repli doit intégrer feuille + plage.

        Sinon deux extraits distincts d'un même classeur — tous deux sans page —
        s'effondreraient l'un sur l'autre.
        """
        base = dict(filename="Budget.xlsx", page_number=None, document_date=None,
                    snippet="x", chunk_id=None)
        a = {**base, "meta": {"sheet_name": "Synthèse", "cell_range": "B8:F17"}}
        b = {**base, "meta": {"sheet_name": "Risques", "cell_range": "A2:G12"}}
        assert len(dedup_sources([a, b])) == 2


# --------------------------------------------------------------------------- #
# Snippet
# --------------------------------------------------------------------------- #
class TestSnippet:
    def test_snippet_respects_the_ui_cap(self):
        long_table = "| Atlas | 250000 |\n" * 500
        s = normalize_source_for_api(_chunk(EXCEL_META, text=long_table), 1)
        assert len(s["snippet"]) <= MAX_SNIPPET_CHARS_UI
        assert s["snippet"].endswith("…")

    def test_snippet_never_contains_metadata_json(self):
        s = normalize_source_for_api(_chunk(EXCEL_META), 1)
        assert "sheet_name" not in s["snippet"]
        assert "cell_range" not in s["snippet"]


# --------------------------------------------------------------------------- #
# Anti-surpromesse — LE risque du lot Excel, et il n'est pas technique
# --------------------------------------------------------------------------- #
class TestAntiOverpromise:
    def test_excel_rules_appear_when_a_workbook_is_cited(self):
        p = build_prompt("Quel est le budget total par direction ?", [_chunk(EXCEL_META)])
        assert "RÈGLES CLASSEURS" in p
        assert "JAMAIS calculer" in p
        assert "calcul exhaustif" in p
        assert "extraits retrouvés" in p.lower()

    def test_header_carries_sheet_and_range_so_the_model_can_cite_them(self):
        p = build_prompt("q", [_chunk(EXCEL_META)])
        assert "CLASSEUR" in p
        assert "feuille « Synthèse »" in p
        assert "cellules B8:F17" in p

    def test_no_excel_rules_without_a_workbook(self):
        """Aucune inflation du prompt pour les documents ordinaires."""
        p = build_prompt("q", [_chunk({"page_number": 3}, filename="a.pdf", page=3)])
        assert "RÈGLES CLASSEURS" not in p
        assert "p.3" in p                          # comportement PDF inchangé

    def test_simple_cell_questions_are_not_forbidden(self):
        """On interdit l'AGRÉGATION EXHAUSTIVE, pas la lecture d'une ligne."""
        p = build_prompt("Quel est le budget du projet Atlas ?", [_chunk(EXCEL_META)])
        assert "cellule ou une ligne PRÉCISE" in p

    def test_rules_survive_a_custom_template(self):
        p = build_prompt("q", [_chunk(EXCEL_META)],
                         template_content="Mon template.\n{context}\n{question}")
        assert "RÈGLES CLASSEURS" in p
        assert "Mon template." in p

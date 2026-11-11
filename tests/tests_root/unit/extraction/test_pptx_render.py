# tests/tests_root/unit/extraction/test_pptx_render.py
"""
Lot P1 — rendu textuel d'une diapositive (`pptx_render`), module PUR.

Aucun `.pptx` n'est fabriqué ici : on teste le rendu sur des structures directement
construites, ce qui permet d'exprimer des cas (ordre visuel, cellules fusionnées) que
python-pptx rendrait pénibles à produire. L'extracteur est testé séparément.
"""

from apps.rag_core.services.ingest.extraction.pptx_render import (
    SAME_ROW_TOLERANCE_EMU,
    ShapeText,
    TextRun,
    render_bullets,
    render_notes,
    render_slide,
    render_table,
    sort_key,
)


def _body(text, top=0, left=0, order=0, level=0, bullet=False):
    return ShapeText(
        kind="body",
        runs=[TextRun(text=text, level=level, is_bullet=bullet)],
        top=top,
        left=left,
        order=order,
    )


class TestReadingOrder:
    def test_title_comes_first_even_when_placed_at_the_bottom(self):
        """Le titre est SÉMANTIQUEMENT premier, quelle que soit sa position à l'écran."""
        title = ShapeText(kind="title", runs=[TextRun("Titre")], top=9_000_000, left=0, order=5)
        body = _body("Corps", top=100, left=0, order=1)
        assert [s.kind for s in sorted([body, title], key=sort_key)] == ["title", "body"]

    def test_subtitle_between_title_and_body(self):
        title = ShapeText(kind="title", runs=[TextRun("T")], top=0, left=0, order=3)
        sub = ShapeText(kind="subtitle", runs=[TextRun("S")], top=0, left=0, order=2)
        body = _body("B", top=0, left=0, order=1)
        assert [s.kind for s in sorted([body, sub, title], key=sort_key)] == [
            "title", "subtitle", "body"
        ]

    def test_top_then_left(self):
        """Ordre visuel : de haut en bas, puis de gauche à droite."""
        haut_droite = _body("haut-droite", top=0, left=5_000_000, order=1)
        bas_gauche = _body("bas-gauche", top=5_000_000, left=0, order=2)
        haut_gauche = _body("haut-gauche", top=0, left=0, order=3)
        got = [s.runs[0].text for s in sorted([bas_gauche, haut_droite, haut_gauche], key=sort_key)]
        assert got == ["haut-gauche", "haut-droite", "bas-gauche"]

    def test_shapes_almost_aligned_are_treated_as_one_row(self):
        """Deux colonnes désalignées d'un cheveu ne doivent pas s'entrelacer.

        Sans la bande de tolérance, `gauche` (top=1000) passerait avant `droite` (top=0)
        seulement si son top était plus petit — ici un écart insignifiant déciderait de
        l'ordre de lecture. Avec la bande, c'est `left` qui tranche.
        """
        droite = _body("droite", top=0, left=5_000_000, order=1)
        gauche = _body("gauche", top=SAME_ROW_TOLERANCE_EMU - 1, left=0, order=2)
        got = [s.runs[0].text for s in sorted([droite, gauche], key=sort_key)]
        assert got == ["gauche", "droite"]

    def test_xml_order_is_the_total_tiebreaker(self):
        """Deux formes exactement superposées gardent un ordre STABLE."""
        a = _body("a", top=0, left=0, order=1)
        b = _body("b", top=0, left=0, order=2)
        assert [s.runs[0].text for s in sorted([b, a], key=sort_key)] == ["a", "b"]


class TestBullets:
    def test_levels_are_indented(self):
        runs = [
            TextRun("Niveau 1", level=0, is_bullet=True),
            TextRun("Niveau 2", level=1, is_bullet=True),
            TextRun("Niveau 2 bis", level=1, is_bullet=True),
            TextRun("Niveau 1 bis", level=0, is_bullet=True),
        ]
        assert render_bullets(runs) == [
            "- Niveau 1",
            "  - Niveau 2",
            "  - Niveau 2 bis",
            "- Niveau 1 bis",
        ]

    def test_non_bullet_is_rendered_as_is(self):
        assert render_bullets([TextRun("De la prose.", is_bullet=False)]) == ["De la prose."]

    def test_empty_paragraphs_are_dropped(self):
        """Une zone de texte PowerPoint est pleine de retours de mise en page."""
        runs = [TextRun(""), TextRun("   "), TextRun("utile")]
        assert render_bullets(runs) == ["utile"]


class TestTable:
    def test_markdown_grid(self):
        rows = [["Service", "Port"], ["Ollama", "11434"], ["AI Gateway", "8100"]]
        assert render_table(rows) == (
            "| Service | Port |\n"
            "|---|---|\n"
            "| Ollama | 11434 |\n"
            "| AI Gateway | 8100 |"
        )

    def test_ragged_rows_are_padded_not_dropped(self):
        """Une cellule fusionnée laisse des trous : la grille doit rester régulière."""
        out = render_table([["A", "B", "C"], ["1"]])
        assert out.splitlines()[-1] == "| 1 |  |  |"

    def test_empty_cells_stay_empty(self):
        """Jamais de valeur devinée pour combler un trou."""
        out = render_table([["A", "B"], ["", "2"]])
        assert "| | 2 |" not in out
        assert out.splitlines()[-1] == "|  | 2 |"

    def test_pipe_in_cell_is_escaped(self):
        out = render_table([["Col"], ["a|b"]])
        assert r"a\|b" in out

    def test_newline_in_cell_becomes_space(self):
        out = render_table([["Col"], ["a\nb"]])
        assert "| a b |" in out

    def test_blank_header_is_numbered_and_no_row_is_lost(self):
        out = render_table([["", ""], ["x", "y"]])
        lines = out.splitlines()
        assert lines[0] == "| Colonne 1 | Colonne 2 |"
        assert "| x | y |" in out

    def test_empty_table_renders_nothing(self):
        assert render_table([]) == ""


class TestRenderSlide:
    def test_provenance_is_inside_the_text(self):
        """La provenance doit survivre à l'embedding : elle est DANS le texte."""
        out = render_slide(slide_number=4, title="Architecture cible", shapes=[])
        assert out.startswith("[Diapositive 4]")
        assert "Titre : Architecture cible" in out

    def test_title_is_not_duplicated_in_the_body(self):
        out = render_slide(
            slide_number=1, title="Mon titre", shapes=[_body("Du corps", order=1)]
        )
        assert out.count("Mon titre") == 1

    def test_alt_text_is_labelled_never_passed_off_as_slide_text(self):
        shape = ShapeText(kind="alt_text", alt_text="Schéma du réseau", order=1)
        out = render_slide(slide_number=2, title=None, shapes=[shape])
        assert "[Image : Schéma du réseau]" in out

    def test_table_is_labelled(self):
        shape = ShapeText(kind="table", table=[["A"], ["1"]], order=1)
        out = render_slide(slide_number=1, title=None, shapes=[shape])
        assert "Tableau :\n| A |" in out

    def test_slide_without_content_yields_only_the_marker(self):
        """Aucune invention : pas de texte -> pas de texte."""
        assert render_slide(slide_number=7, title=None, shapes=[]) == "[Diapositive 7]"

    def test_deterministic_across_runs(self):
        shapes = [
            _body("b", top=0, left=1_000_000, order=2),
            _body("a", top=0, left=0, order=1),
            ShapeText(kind="table", table=[["x"]], top=5_000_000, order=3),
        ]
        first = render_slide(slide_number=1, title="T", shapes=list(shapes))
        second = render_slide(slide_number=1, title="T", shapes=list(reversed(shapes)))
        assert first == second


class TestRenderNotes:
    def test_notes_are_explicitly_labelled(self):
        out = render_notes(slide_number=3, runs=[TextRun("Dire bonjour")])
        assert out.startswith("[Diapositive 3 — notes du présentateur]")
        assert "Dire bonjour" in out

    def test_empty_notes_render_nothing(self):
        assert render_notes(slide_number=3, runs=[]) == ""

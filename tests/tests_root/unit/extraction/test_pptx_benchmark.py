# tests/tests_root/unit/extraction/test_pptx_benchmark.py
"""
Lot P1 — mini-benchmark d'extraction PPTX : une présentation de référence, un texte attendu.

Ce fichier ne mesure pas une qualité de retrieval ; il **fige la sortie exacte** de
l'extracteur sur un cas qui rassemble tout ce qui est censé fonctionner : titre, listes
imbriquées, tableau, formes dans un ordre visuel non trivial, diapositive image-seule,
notes optionnelles.

On assertionne le **texte complet attendu**, pas un hash : un hash dirait « ça a changé »,
le texte dit *ce qui* a changé — c'est la différence entre un test qu'on corrige et un
test qu'on supprime.

Aucun LLM n'intervient dans l'ordre de lecture ni dans la reconstruction du contenu.
"""

from apps.rag_core.services.ingest.extraction.base import ExtractionContext
from apps.rag_core.services.ingest.extraction.pptx_extractor import PptxExtractor

from ._builders import build_pptx

# EMU : 1 cm = 360 000. Les zones libres de la diapositive 3 sont AJOUTÉES dans un ordre
# (bas, droite, haut-gauche) qui ne correspond ni à l'ordre de lecture ni à l'ordre
# visuel — c'est exactement le piège que l'ordre déterministe doit neutraliser.
REFERENCE_DECK = [
    {
        "title": "Architecture cible",
        "bullets": [
            ("Frontend", 0),
            ("Next.js", 1),
            ("Loopback uniquement", 1),
            ("Backend", 0),
            ("Base PostgreSQL", 0),
        ],
    },
    {
        "title": "Services et ports",
        "table": [
            ["Service", "Port", "Exposition"],
            ["AI Gateway", "8100", "Privée"],
            ["Ollama", "11434", "Privée"],
            ["", "", ""],
        ],
    },
    {
        "title": None,
        "textboxes": [
            ("Troisième bloc, en bas", 6_000_000, 0),
            ("Deuxième bloc, à droite", 2_000_000, 5_000_000),
            ("Premier bloc, en haut à gauche", 2_000_000, 0),
        ],
    },
    # Image seule, SANS titre : aucun texte exploitable. Le `descr` que python-pptx
    # pose tout seul (« image.png ») n'est pas une description -> rien à indexer.
    {"title": None, "picture": True},
]

REFERENCE_NOTES = {1: "Insister sur le loopback"}


def _extract(*, notes=None):
    data = build_pptx(REFERENCE_DECK, notes=notes)
    return PptxExtractor().extract(data, ExtractionContext(filename="reference.pptx"))


EXPECTED_SLIDE_1 = """[Diapositive 1]

Titre : Architecture cible

- Frontend
  - Next.js
  - Loopback uniquement
- Backend
- Base PostgreSQL"""

EXPECTED_SLIDE_2 = """[Diapositive 2]

Titre : Services et ports

Tableau :
| Service | Port | Exposition |
|---|---|---|
| AI Gateway | 8100 | Privée |
| Ollama | 11434 | Privée |
|  |  |  |"""

EXPECTED_SLIDE_3 = """[Diapositive 3]

Premier bloc, en haut à gauche

Deuxième bloc, à droite

Troisième bloc, en bas"""


class TestReferenceExtraction:
    def test_slide_count(self):
        res = _extract()
        assert res.metadata["pptx"]["slide_count"] == 4

    def test_indexed_units(self):
        """La 4e diapositive (image seule, alt-text automatique) n'est PAS indexée."""
        res = _extract()
        assert len(res.slides) == 3
        assert [s["meta"]["slide_number"] for s in res.slides] == [1, 2, 3]

    def test_slide_1_exact_text(self):
        assert _extract().slides[0]["text"] == EXPECTED_SLIDE_1

    def test_slide_2_exact_text(self):
        assert _extract().slides[1]["text"] == EXPECTED_SLIDE_2

    def test_slide_3_exact_text_reading_order_is_visual_not_xml(self):
        """Les zones ont été AJOUTÉES bas → droite → haut-gauche."""
        assert _extract().slides[2]["text"] == EXPECTED_SLIDE_3

    def test_titles(self):
        res = _extract()
        assert [s["meta"]["slide_title"] for s in res.slides] == [
            "Architecture cible", "Services et ports", None,
        ]

    def test_warnings(self):
        res = _extract()
        assert "pptx_image_only_slides" in res.warnings
        assert res.metadata["pptx"]["image_only_slides"] == [4]
        # Aucune troncature sur ce deck de référence.
        assert not [w for w in res.warnings if "reached" in w]
        assert res.partial is False

    def test_counters(self):
        res = _extract()
        assert res.metadata["pptx"]["tables"] == 1
        assert res.metadata["pptx"]["indexed_slides"] == 3
        assert res.metadata["pptx"]["notes_units"] == 0

    def test_empty_table_cells_are_not_invented(self):
        """La 4e ligne du tableau est vide : elle reste vide."""
        assert "|  |  |  |" in _extract().slides[1]["text"]


class TestNotesVariant:
    def test_notes_excluded_by_default(self):
        res = _extract(notes=REFERENCE_NOTES)
        assert "Insister sur le loopback" not in res.text
        assert res.metadata["pptx"]["notes_included"] is False

    def test_notes_included_behind_the_flag(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        res = _extract(notes=REFERENCE_NOTES)
        assert res.metadata["pptx"]["notes_units"] == 1
        notes = next(s for s in res.slides if s["meta"]["kind"] == "pptx_notes")
        assert notes["text"] == (
            "[Diapositive 1 — notes du présentateur]\n\nInsister sur le loopback"
        )

    def test_slide_text_is_identical_with_or_without_notes(self, settings):
        """Activer les notes ne doit RIEN changer au texte des diapositives."""
        without = [s["text"] for s in _extract(notes=REFERENCE_NOTES).slides]
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        with_notes = [
            s["text"] for s in _extract(notes=REFERENCE_NOTES).slides
            if s["meta"]["kind"] == "pptx_slide"
        ]
        assert without == with_notes


class TestDeterminism:
    def test_same_bytes_twice_same_output(self):
        data = build_pptx(REFERENCE_DECK, notes=REFERENCE_NOTES)
        ctx = ExtractionContext(filename="reference.pptx")
        a = PptxExtractor().extract(data, ctx)
        b = PptxExtractor().extract(data, ctx)
        assert a.text == b.text
        assert [s["meta"] for s in a.slides] == [s["meta"] for s in b.slides]

    def test_rebuilt_file_same_output(self):
        """Le .pptx est régénéré : même contenu logique -> même texte extrait."""
        assert _extract().text == _extract().text

# tests/tests_root/unit/extraction/test_pptx.py
"""
Lot P1 — extracteur PowerPoint `.pptx` (python-pptx) : nominal, limites, sécurité.

Invariants vérifiés ici :
- l'extraction est structurée PAR DIAPOSITIVE et déterministe ;
- rien n'est inventé : une diapositive sans texte n'en reçoit pas ;
- aucune perte n'est silencieuse : toute troncature produit un warning ;
- une archive hostile est refusée AVANT que python-pptx ne l'ouvre.
"""

import pytest
from apps.rag_core.services.ingest.extraction.base import (
    CorruptedFile,
    ExtractionContext,
    ExtractionLimitExceeded,
    ExtractionStatus,
    RejectedFile,
)
from apps.rag_core.services.ingest.extraction.pptx_extractor import (
    PptxExtractor,
    _is_meaningful_alt_text,
)

from ._builders import (
    build_pptx,
    make_generic_zip,
    make_pptx_corrupted,
    make_pptx_encrypted,
    make_pptx_missing_presentation,
    make_pptx_with_unsafe_xml,
    make_pptx_zip,
    make_pptx_zip_bomb,
)


def _ctx(name="deck.pptx"):
    return ExtractionContext(filename=name)


def _extract(data, name="deck.pptx"):
    return PptxExtractor().extract(data, _ctx(name))


def _slide_texts(res):
    return [s["text"] for s in res.slides if s["meta"]["kind"] == "pptx_slide"]


class TestNominal:
    def test_title_and_bullets(self):
        res = _extract(build_pptx([
            {"title": "Architecture cible", "bullets": [
                ("Frontend", 0), ("Next.js", 1), ("Backend", 0),
            ]},
        ]))
        assert res.status == ExtractionStatus.SUCCESS
        assert len(res.slides) == 1
        text = res.slides[0]["text"]
        assert "Titre : Architecture cible" in text
        assert "- Frontend" in text
        assert "  - Next.js" in text
        assert "- Backend" in text

    def test_slide_metadata_carries_provenance(self):
        res = _extract(build_pptx([{"title": "Ports", "bullets": ["8100"]}]))
        meta = res.slides[0]["meta"]
        assert meta["kind"] == "pptx_slide"
        assert meta["slide_number"] == 1
        assert meta["slide_title"] == "Ports"
        assert meta["has_text"] is True

    def test_slide_numbers_are_human_and_start_at_one(self):
        res = _extract(build_pptx([
            {"title": "Un", "bullets": ["a"]},
            {"title": "Deux", "bullets": ["b"]},
            {"title": "Trois", "bullets": ["c"]},
        ]))
        assert [s["meta"]["slide_number"] for s in res.slides] == [1, 2, 3]

    def test_table_becomes_markdown(self):
        res = _extract(build_pptx([
            {"title": "Ports", "table": [
                ["Service", "Port"], ["AI Gateway", "8100"], ["Ollama", "11434"],
            ]},
        ]))
        text = res.slides[0]["text"]
        assert "| Service | Port |" in text
        assert "| AI Gateway | 8100 |" in text
        assert res.metadata["tables"] == 1

    def test_table_with_empty_cells(self):
        res = _extract(build_pptx([
            {"title": "T", "table": [["A", "B"], ["", "2"]]},
        ]))
        assert "| Service" not in res.slides[0]["text"]
        assert "| A | B |" in res.slides[0]["text"]

    def test_grouped_shapes_are_traversed(self):
        res = _extract(build_pptx([
            {"title": "Groupes", "group": [
                ("texte groupé A", 1_000_000, 0),
                ("texte groupé B", 2_000_000, 0),
            ]},
        ]))
        text = res.slides[0]["text"]
        assert "texte groupé A" in text
        assert "texte groupé B" in text

    def test_unicode_and_accents_survive(self):
        res = _extract(build_pptx([
            {"title": "Réglementation — sécurité", "bullets": ["Écart de 5 % — « oui »", "日本語"]},
        ]))
        text = res.slides[0]["text"]
        assert "Réglementation — sécurité" in text
        assert "Écart de 5 % — « oui »" in text
        assert "日本語" in text

    def test_multiple_textboxes_read_in_visual_order(self):
        """Les zones sont AJOUTÉES dans le désordre : la sortie doit être ordonnée."""
        res = _extract(build_pptx([
            {"title": None, "textboxes": [
                ("EN BAS", 6_000_000, 0),
                ("EN HAUT", 1_000_000, 0),
                ("AU MILIEU", 3_000_000, 0),
            ]},
        ]))
        text = res.slides[0]["text"]
        assert text.index("EN HAUT") < text.index("AU MILIEU") < text.index("EN BAS")

    def test_extraction_is_deterministic(self):
        """Deux extractions du même fichier -> texte identique, octet pour octet."""
        data = build_pptx([
            {"title": "T", "bullets": [("a", 0), ("b", 1)],
             "textboxes": [("z", 5_000_000, 3_000_000), ("y", 5_000_000, 0)]},
            {"title": "U", "table": [["A", "B"], ["1", "2"]]},
        ])
        assert _slide_texts(_extract(data)) == _slide_texts(_extract(data))

    def test_result_text_matches_indexed_units(self):
        """`text` (FTS + garde « vide ») doit refléter ce qui sera réellement indexé."""
        res = _extract(build_pptx([
            {"title": "Un", "bullets": ["a"]}, {"title": "Deux", "bullets": ["b"]},
        ]))
        assert res.text == "\n\n".join(s["text"] for s in res.slides)

    def test_metadata_block(self):
        res = _extract(build_pptx([{"title": "T", "bullets": ["a"]}]))
        assert res.metadata["format"] == "pptx"
        assert res.metadata["extractor"] == "pptx"
        assert res.method == "pptx:python-pptx"
        assert res.metadata["pptx"]["slide_count"] == 1
        assert res.metadata["pptx"]["indexed_slides"] == 1


class TestNonTextualSlides:
    def test_image_only_slide_is_flagged_not_invented(self):
        """Aucun texte fabriqué, et la situation est VISIBLE (warning)."""
        res = _extract(build_pptx([
            {"title": "Avec texte", "bullets": ["présent"]},
            {"title": None, "picture": True},
        ]))
        assert "pptx_image_only_slides" in res.warnings
        assert res.metadata["pptx"]["image_only_slides"] == [2]
        # La diapositive sans texte n'est pas indexée comme si elle en avait.
        assert [s["meta"]["slide_number"] for s in res.slides] == [1]

    def test_alt_text_is_extracted_and_marked(self):
        res = _extract(build_pptx([
            {"title": None, "picture": True, "alt_text": "Schéma du réseau de production"},
        ]))
        assert "[Image : Schéma du réseau de production]" in res.slides[0]["text"]
        assert res.slides[0]["meta"]["has_images"] is True

    def test_alt_text_slide_requires_visual_check(self):
        """Un alt-text n'est pas le contenu : le signal « à vérifier » doit être posé."""
        res = _extract(build_pptx([
            {"title": None, "picture": True, "alt_text": "Un schéma"},
        ]))
        assert res.slides[0]["meta"]["requires_visual_check"] is True

    def test_alt_text_can_be_disabled(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_ALT_TEXT = False
        res = _extract(build_pptx([
            {"title": None, "picture": True, "alt_text": "Un schéma"},
        ]))
        assert res.slides == []

    def test_empty_slide_is_flagged_separately_from_image_only(self):
        """« Vide » et « image seule » ne se corrigent pas pareil : deux warnings."""
        res = _extract(build_pptx([
            {"title": "Avec texte", "bullets": ["présent"]},
            {"title": None},
        ]))
        assert "pptx_empty_slides" in res.warnings
        assert "pptx_image_only_slides" not in res.warnings

    def test_presentation_without_any_text_is_empty_not_success(self):
        """Aucun faux succès : rien à indexer -> statut EMPTY."""
        res = _extract(build_pptx([{"title": None}, {"title": None}]))
        assert res.status == ExtractionStatus.EMPTY
        assert res.slides == []

    def test_document_stays_ingestible_when_only_some_slides_are_textless(self):
        res = _extract(build_pptx([
            {"title": "OK", "bullets": ["du texte"]},
            {"title": None, "picture": True},
            {"title": "OK 2", "bullets": ["encore"]},
        ]))
        assert res.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)
        assert [s["meta"]["slide_number"] for s in res.slides] == [1, 3]


class TestAltTextIsFiltered:
    """PowerPoint et python-pptx écrivent d'eux-mêmes dans `descr` : ce n'est pas
    parce que l'attribut est renseigné que l'auteur a décrit quoi que ce soit."""

    @pytest.mark.parametrize("descr", [
        "image.png",
        "Capture d'écran 2024-01-05 à 10.32.11.png",
        "schema.SVG",
        "diagramme.emf",
        "Picture 3",
        "Image 12",
        "Rectangle 4",
        "Content Placeholder 2",
        "Espace réservé 5",
        "",
        "   ",
    ])
    def test_noise_is_not_alt_text(self, descr):
        assert _is_meaningful_alt_text(descr, "Picture 3") is False

    @pytest.mark.parametrize("descr", [
        "Schéma du réseau de production",
        "Courbe de température 2024",
        "Organigramme de la direction des risques",
        "photo de l'équipe devant le siège",
    ])
    def test_real_description_is_kept(self, descr):
        assert _is_meaningful_alt_text(descr, "Picture 3") is True

    def test_descr_equal_to_shape_name_is_dropped(self):
        assert _is_meaningful_alt_text("Mon schéma", "Mon schéma") is False

    def test_default_filename_alt_text_leaves_the_slide_image_only(self):
        """Le cas réel : python-pptx pose le nom de fichier -> la diapo reste image-seule."""
        res = _extract(build_pptx([
            {"title": "Avec texte", "bullets": ["présent"]},
            {"title": None, "picture": True},  # descr = "image.png"
        ]))
        assert "pptx_image_only_slides" in res.warnings
        assert [s["meta"]["slide_number"] for s in res.slides] == [1]


class TestNotes:
    def test_notes_excluded_by_default(self):
        res = _extract(build_pptx(
            [{"title": "T", "bullets": ["corps"]}], notes={1: "à ne pas dire tout haut"},
        ))
        assert all(s["meta"]["kind"] == "pptx_slide" for s in res.slides)
        assert "à ne pas dire tout haut" not in res.text

    def test_notes_included_behind_the_flag_as_separate_units(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        res = _extract(build_pptx(
            [{"title": "T", "bullets": ["corps"]}], notes={1: "penser à respirer"},
        ))
        kinds = [s["meta"]["kind"] for s in res.slides]
        assert kinds == ["pptx_slide", "pptx_notes"]
        notes_unit = res.slides[1]
        assert "penser à respirer" in notes_unit["text"]
        assert notes_unit["meta"]["slide_number"] == 1
        # La provenance distingue la diapositive de ses notes, dans le texte lui-même.
        assert "notes du présentateur" in notes_unit["text"]

    def test_notes_never_merged_into_the_slide_unit(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        res = _extract(build_pptx(
            [{"title": "T", "bullets": ["corps"]}], notes={1: "SECRET"},
        ))
        slide_unit = next(s for s in res.slides if s["meta"]["kind"] == "pptx_slide")
        assert "SECRET" not in slide_unit["text"]

    def test_missing_notes_are_not_an_error(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        res = _extract(build_pptx([{"title": "T", "bullets": ["corps"]}]))
        assert [s["meta"]["kind"] for s in res.slides] == ["pptx_slide"]


class TestLimits:
    def test_too_many_slides_is_refused(self, settings):
        settings.DOCUMENT_PPTX_MAX_SLIDES = 2
        data = build_pptx([{"title": f"S{i}", "bullets": ["x"]} for i in range(4)])
        with pytest.raises(ExtractionLimitExceeded) as e:
            _extract(data)
        assert e.value.code == "pptx_limit_exceeded"

    def test_shape_budget_truncates_with_a_warning(self, settings):
        settings.DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE = 2
        data = build_pptx([
            {"title": "T", "textboxes": [(f"zone {i}", i * 500_000, 0) for i in range(10)]},
        ])
        res = _extract(data)
        assert "pptx_max_shapes_reached" in res.warnings
        assert res.partial is True

    def test_char_budget_truncates_with_a_warning(self, settings):
        settings.DOCUMENT_PPTX_MAX_CHARS = 20
        data = build_pptx([{"title": "T", "bullets": ["x" * 100, "y" * 100]}])
        res = _extract(data)
        assert "pptx_max_chars_reached" in res.warnings
        assert res.partial is True

    def test_table_cell_budget_truncates_with_a_warning(self, settings):
        settings.DOCUMENT_PPTX_MAX_TABLE_CELLS = 3
        data = build_pptx([
            {"title": "T", "table": [["a", "b"], ["c", "d"], ["e", "f"]]},
        ])
        res = _extract(data)
        assert "pptx_max_table_cells_reached" in res.warnings

    def test_no_loss_is_silent(self):
        """Un document intégralement extrait ne porte AUCUN warning de troncature."""
        res = _extract(build_pptx([{"title": "T", "bullets": ["a", "b"]}]))
        assert res.partial is False
        assert not [w for w in res.warnings if "reached" in w]


class TestSecurity:
    def test_corrupted_archive_is_rejected_cleanly(self):
        with pytest.raises(CorruptedFile) as e:
            _extract(make_pptx_corrupted())
        assert e.value.code == "invalid_pptx_archive"

    def test_missing_presentation_part_is_rejected(self):
        with pytest.raises(CorruptedFile) as e:
            _extract(make_pptx_missing_presentation())
        assert e.value.code == "invalid_pptx_archive"

    def test_zip_renamed_pptx_is_rejected(self):
        """Un ZIP quelconque renommé .pptx n'est pas une présentation."""
        with pytest.raises(CorruptedFile):
            _extract(make_generic_zip(), name="faux.pptx")

    def test_text_file_renamed_pptx_is_rejected(self):
        with pytest.raises(CorruptedFile):
            _extract("je ne suis pas une présentation".encode("utf-8"), name="faux.pptx")

    def test_zip_bomb_is_refused_before_parsing(self):
        with pytest.raises(ExtractionLimitExceeded) as e:
            _extract(make_pptx_zip_bomb(mb=300))
        assert e.value.code in (
            "pptx_uncompressed_too_large", "pptx_compression_ratio",
        )

    def test_unsafe_xml_is_refused_before_parsing(self):
        """Une bombe à entités est minuscule : la garde zip-bomb ne la voit pas passer."""
        with pytest.raises(RejectedFile) as e:
            _extract(make_pptx_with_unsafe_xml())
        assert e.value.code == "unsafe_xml"

    def test_encrypted_package_is_named_not_a_generic_failure(self):
        with pytest.raises(RejectedFile) as e:
            _extract(make_pptx_encrypted())
        assert e.value.code == "encrypted_pptx"

    def test_minimal_detection_fixture_is_not_extractable(self):
        """`make_pptx_zip` passe la détection mais n'est pas une présentation valide."""
        with pytest.raises((CorruptedFile, RejectedFile)):
            _extract(make_pptx_zip())

    def test_gate_closed_refuses_extraction(self, settings):
        """Défense en profondeur : le flag est re-vérifié DANS l'extracteur."""
        settings.DOCUMENT_PPTX_ENABLED = False
        with pytest.raises(RejectedFile) as e:
            _extract(build_pptx([{"title": "T", "bullets": ["a"]}]))
        assert e.value.code == "pptx_disabled"

    def test_nothing_is_executed(self):
        """L'extraction ne lit que du XML : aucun sous-processus, aucun contenu actif."""
        import subprocess

        calls = []
        original = subprocess.Popen

        def spy(*a, **k):  # pragma: no cover - ne doit jamais être appelé
            calls.append(a)
            return original(*a, **k)

        subprocess.Popen = spy
        try:
            _extract(build_pptx([{"title": "T", "bullets": ["a"], "picture": True}]))
        finally:
            subprocess.Popen = original
        assert calls == []

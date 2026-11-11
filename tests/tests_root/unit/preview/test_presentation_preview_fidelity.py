"""TEST CARDINAL — l'aperçu PPTX surligne EXACTEMENT le texte indexé.

Deux pièges rendent ce test indispensable, et aucun n'est théorique.

**Piège 1 — `res.text` n'est pas le texte des offsets.** Le chunking avance son
curseur de `len(unit.text.strip()) + 1`, l'extracteur assemble `res.text` avec
`"\\n\\n"`. Les offsets vivent donc dans un texte VIRTUEL à séparateur d'un
caractère. Un test dédié prouve que l'usage de `res.text` dérive.

**Piège 2 — l'ensemble des unités dépend du réglage des notes.** Les notes
s'intercalent entre les diapositives : les activer décale toutes les bases
suivantes. L'oracle est donc joué sous LES DEUX réglages.
"""
import sys
from pathlib import Path

import pytest
from django.test import override_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))

from _builders import build_pptx  # noqa: E402

from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.ingest.extraction.pptx_extractor import (  # noqa: E402
    PptxExtractor,
)
from apps.rag_core.services.ingest.indexing import _build_slide_specs  # noqa: E402
from apps.rag_core.services.preview import errors as E  # noqa: E402
from apps.rag_core.services.preview.presentation_blocks import (  # noqa: E402
    BLOCK_SEPARATOR,
    find_unit,
    index_units,
    project_highlight,
    split_blocks,
    validate_offsets,
)

LOREM = (
    "Le backend communique avec la passerelle applicative selon un protocole "
    "documente et borne, sans jamais exposer de service au dela du loopback. "
)

#: Corpus couvrant les familles de contenu délicates.
DECK = [
    {
        "title": "Architecture cible",
        "bullets": [("Frontend", 0), ("Next.js", 1), ("Loopback uniquement", 1),
                    ("Backend", 0), (LOREM * 3, 0)],
    },
    {
        "title": "Services et ports",
        "table": [["Service", "Port", "Exposition"], ["Gateway", "8100", "Privée"],
                  ["", "", ""]],
    },
    {
        "title": None,
        "textboxes": [("Bloc du bas", 6_000_000, 0),
                      ("Bloc de droite", 2_000_000, 5_000_000),
                      ("Bloc du haut avec accents éàü et 中文 ✅", 2_000_000, 0)],
    },
    {
        "title": "Contenu répété",
        "bullets": [("Texte répété identique.", 0), ("Intercalaire.", 0),
                    ("Texte répété identique.", 0)],
    },
    {
        "title": "Avec visuel",
        "bullets": [(LOREM * 4, 0)],
        "picture": True,
        "alt_text": "Schema du flux de donnees",
    },
]
NOTES = {
    1: "Insister sur le loopback.\nDeuxieme ligne.",
    4: LOREM * 2,
    5: "Note de la derniere diapositive",
}


def _extract(*, notes: bool):
    data = build_pptx(DECK, notes=NOTES)
    if notes:
        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            return PptxExtractor().extract(data, ExtractionContext(filename="t.pptx"))
    return PptxExtractor().extract(data, ExtractionContext(filename="t.pptx"))


def _specs(res, *, size, overlap):
    return _build_slide_specs(
        result=res, filename="t.pptx", chunk_size=size, overlap=overlap
    )


@pytest.fixture(scope="module")
def deck_off():
    return _extract(notes=False)


@pytest.fixture(scope="module")
def deck_on():
    return _extract(notes=True)


# --------------------------------------------------------------------------- #
class TestOracleCardinal:
    @pytest.mark.parametrize("notes", [False, True], ids=["notes-off", "notes-on"])
    @pytest.mark.parametrize("size,overlap", [(200, 40), (400, 80), (2500, 250)])
    def test_offsets_reconstruisent_le_chunk(self, notes, size, overlap):
        """`unit_text[cs - base : ce - base].strip() == chunk.text`, sans exception."""
        res = _extract(notes=notes)
        units = index_units(res.slides)
        specs = _specs(res, size=size, overlap=overlap)
        assert specs, "le corpus doit produire des chunks"

        by_key = {(u.kind, u.slide_number): u for u in units}
        for spec in specs:
            key = (spec.meta["kind"], spec.meta["slide_number"])
            unit = by_key[key]
            got = unit.text[
                spec.char_start - unit.start : spec.char_end - unit.start
            ].strip()
            assert got == spec.text, (
                f"DIVERGENCE chunk {spec.chunk_index} {key} "
                f"[{spec.char_start},{spec.char_end}) base={unit.start}"
            )

    @pytest.mark.parametrize("notes", [False, True], ids=["notes-off", "notes-on"])
    @pytest.mark.parametrize("size,overlap", [(200, 40), (400, 80)])
    def test_fragments_surlignes_reconstruisent_le_chunk(self, notes, size, overlap):
        """La concaténation des fragments surlignés == le texte du chunk.

        C'est l'invariant qui porte sur le RENDU : ce que l'utilisateur voit
        marqué est exactement la source citée, ni plus, ni moins.
        """
        res = _extract(notes=notes)
        units = index_units(res.slides)
        specs = _specs(res, size=size, overlap=overlap)
        compared = 0

        for spec in specs:
            unit = find_unit(
                units, kind=spec.meta["kind"], slide_number=spec.meta["slide_number"]
            )
            blocks = split_blocks(unit)
            marks = project_highlight(
                blocks, unit=unit,
                char_start=spec.char_start, char_end=spec.char_end,
            )
            assert marks, f"chunk {spec.chunk_index} ne recouvre aucun bloc"

            fragments = [
                b.text[marks[b.index].start : marks[b.index].end]
                for b in blocks
                if b.index in marks
            ]
            rebuilt = BLOCK_SEPARATOR.join(fragments).strip()
            assert rebuilt == spec.text, (
                f"chunk {spec.chunk_index} : surlignage reconstruit\n"
                f"  {rebuilt[:100]!r}\nau lieu de\n  {spec.text[:100]!r}"
            )
            compared += 1

        assert compared == len(specs)


class TestResTextInterdit:
    """`res.text` NE DOIT JAMAIS servir de référence aux offsets."""

    def test_res_text_derive_et_ne_doit_pas_etre_utilise(self, deck_off):
        res = deck_off
        specs = _specs(res, size=400, overlap=80)
        raw = res.text

        wrong = sum(
            1 for s in specs if raw[s.char_start : s.char_end].strip() != s.text
        )
        # Le premier chunk de la PREMIÈRE unité coïncide (base 0) ; tous les
        # suivants dérivent d'un caractère par unité déjà traitée.
        assert wrong > 0, (
            "res.text semble coïncider avec les offsets — la règle de séparation "
            "a changé, ce module doit être revu"
        )

    def test_la_derive_vaut_un_caractere_par_unite(self, deck_off):
        res = deck_off
        units = index_units(res.slides)
        # Base virtuelle (+1) contre position réelle dans res.text ("\n\n").
        for rank, unit in enumerate(units):
            expected_in_res_text = sum(
                len(u.text) + 2 for u in units[:rank]
            )
            assert unit.start == expected_in_res_text - rank, (
                "la base virtuelle doit valoir la position dans res.text "
                "moins un caractère par unité précédente"
            )

    def test_separateur_virtuel_vaut_exactement_un(self, deck_off):
        units = index_units(deck_off.slides)
        for a, b in zip(units, units[1:]):
            assert b.start - a.end == 1


class TestGardeNotes:
    def test_le_reglage_des_notes_decale_les_bases(self, deck_off, deck_on):
        """C'est la raison d'être de `notes_configuration_mismatch`."""
        off = {(u.kind, u.slide_number): u.start for u in index_units(deck_off.slides)}
        on = {(u.kind, u.slide_number): u.start for u in index_units(deck_on.slides)}

        shifted = [
            k for k in off
            if k in on and on[k] != off[k]
        ]
        assert shifted, "le corpus doit contenir des notes intercalées"
        # Le décalage est CROISSANT : chaque note traversée ajoute sa longueur.
        décalages = sorted(on[k] - off[k] for k in shifted)
        assert décalages[0] > 0 and décalages[-1] > décalages[0]

    def test_notes_produisent_des_unites_separees(self, deck_on):
        units = index_units(deck_on.slides)
        kinds = {u.kind for u in units}
        assert kinds == {"pptx_slide", "pptx_notes"}
        # Une note ne se mélange jamais au corps visible.
        for u in units:
            if u.kind == "pptx_notes":
                assert "notes du présentateur" in u.text

    def test_aucune_note_quand_le_flag_est_ferme(self, deck_off):
        assert all(u.kind == "pptx_slide" for u in index_units(deck_off.slides))


class TestBlocs:
    def test_decoupage_reversible(self, deck_off):
        for unit in index_units(deck_off.slides):
            blocks = split_blocks(unit)
            assert BLOCK_SEPARATOR.join(b.text for b in blocks) == unit.text

    def test_chaque_borne_rend_le_texte_du_bloc(self, deck_off):
        for unit in index_units(deck_off.slides):
            for b in split_blocks(unit):
                assert unit.text[b.start : b.end] == b.text

    def test_types_reconnus_par_prefixe_du_rendu(self, deck_off):
        units = index_units(deck_off.slides)
        types = {}
        for unit in units:
            for b in split_blocks(unit):
                types.setdefault(b.type, []).append(b.text[:30])

        assert "title" in types
        assert "list" in types
        assert "table" in types
        assert "paragraph" in types
        # Le marqueur de provenance est du texte INDEXÉ : il occupe ses caractères.
        markers = [
            b for u in units for b in split_blocks(u)
            if "provenance_marker" in b.warnings
        ]
        assert markers and all(m.text.startswith("[Diapositive ") for m in markers)

    def test_tableau_porte_ses_avertissements(self, deck_off):
        tables = [
            b for u in index_units(deck_off.slides) for b in split_blocks(u)
            if b.type == "table"
        ]
        assert tables
        for t in tables:
            assert "table_simplified" in t.warnings
            assert "merged_cells_may_repeat" in t.warnings
            assert t.text.startswith("Tableau :")

    def test_texte_alternatif_est_du_texte_indexe_pas_un_placeholder(self, deck_off):
        alts = [
            b for u in index_units(deck_off.slides) for b in split_blocks(u)
            if "alt_text" in b.warnings
        ]
        assert alts
        assert alts[0].text.startswith("[Image : ")

    def test_notes_homogenes(self, deck_on):
        notes_units = [u for u in index_units(deck_on.slides) if u.kind == "pptx_notes"]
        assert notes_units
        for u in notes_units:
            assert all(b.type == "speaker_notes" for b in split_blocks(u))

    def test_surlignage_partiel_des_blocs_de_bordure(self, deck_off):
        units = index_units(deck_off.slides)
        unit = next(u for u in units if len(split_blocks(u)) >= 3)
        blocks = split_blocks(unit)
        b0, b2 = blocks[0], blocks[2]

        start = unit.start + b0.start + 3
        end = unit.start + b2.start + 5
        marks = project_highlight(blocks, unit=unit, char_start=start, char_end=end)

        assert marks[b0.index].start == 3
        assert marks[b0.index].end == len(b0.text)
        assert marks[b2.index].start == 0
        assert marks[b2.index].end == 5
        # Les blocs intermédiaires sont couverts intégralement.
        for b in blocks[1:2]:
            assert (marks[b.index].start, marks[b.index].end) == (0, len(b.text))


class TestDeterminisme:
    def test_deux_extractions_identiques(self):
        a, b = _extract(notes=False), _extract(notes=False)
        assert a.text == b.text
        assert [u["text"] for u in a.slides] == [u["text"] for u in b.slides]

    def test_texte_repete_distingue_par_position(self, deck_off):
        """Une recherche du snippet choisirait la mauvaise occurrence."""
        unit = next(
            u for u in index_units(deck_off.slides)
            if u.text.count("Texte répété identique.") == 2
        )
        first = unit.text.index("Texte répété identique.")
        second = unit.text.index("Texte répété identique.", first + 1)
        assert first != second


class TestValidationOffsets:
    def test_offsets_hors_unite_refuses(self, deck_off):
        unit = index_units(deck_off.slides)[0]
        with pytest.raises(E.PreviewStale) as exc:
            validate_offsets(unit.start, unit.end + 50, unit=unit)
        assert exc.value.code == E.ERR_DOCUMENT_VERSION_MISMATCH

    @pytest.mark.parametrize("cs,ce", [(None, 10), (10, None), (-1, 10), (10, 10), (20, 5)])
    def test_offsets_invalides(self, deck_off, cs, ce):
        unit = index_units(deck_off.slides)[0]
        with pytest.raises(E.PreviewInvalidRequest):
            validate_offsets(cs, ce, unit=unit)

    def test_slide_inconnue(self, deck_off):
        units = index_units(deck_off.slides)
        with pytest.raises(E.PreviewNotFound) as exc:
            find_unit(units, kind="pptx_slide", slide_number=999)
        assert exc.value.code == E.ERR_SLIDE_NOT_FOUND

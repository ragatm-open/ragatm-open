"""TEST CARDINAL — l'aperçu DOCX surligne EXACTEMENT le texte indexé.

Le pipeline DOCX ne persiste aucune structure : `ExtractionResult.blocks` est
calculé puis aplati par `join_blocks_text`, et le chunker découpe le texte
**aveuglément au caractère**. Ce qui survit en base, ce sont des offsets.

L'oracle est donc direct — il ne reconstruit rien, il compare la source à
l'indexé :

    result.text[chunk.char_start:chunk.char_end].strip() == chunk.text

et son corollaire, qui porte sur le rendu :

    la concaténation des fragments surlignés reconstruit exactement le chunk
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))

from _builders import make_docx_raw  # noqa: E402

from apps.rag_core.services.chunking import chunk_text_deterministic  # noqa: E402
from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.ingest.extraction.docx_extractor import (  # noqa: E402
    DocxExtractor,
)
from apps.rag_core.services.preview.document_blocks import (  # noqa: E402
    BLOCK_SEPARATOR,
    find_covering_blocks,
    index_blocks,
)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text, style=None, num=False):
    ppr = ""
    if style or num:
        ppr = "<w:pPr>"
        if style:
            ppr += f'<w:pStyle w:val="{style}"/>'
        if num:
            ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        ppr += "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _tbl(rows):
    out = "<w:tbl>"
    for r in rows:
        out += "<w:tr>" + "".join(
            f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r
        ) + "</w:tr>"
    return out + "</w:tbl>"


#: Corpus de fidélité — une famille de contenu délicate par ligne.
BODY = (
    _p("Rapport annuel", "Heading1")
    + _p("")                                        # paragraphe vide (supprimé)
    + _p("Introduction avec accents : é à ù çœ.")
    + _p("Espaces insécables et fines.")   # NBSP + fine
    + _p("Tabulation\tinterne et suite.")
    + _p("4. Risques", "Heading1")
    + _p("4.2 Risques opérationnels", "Heading2")
    + _p("Le risque principal concerne la disponibilité du service.")
    + _p("Premier point", num=True)
    + _p("Deuxième point", num=True)
    + _p("Troisième point", num=True)
    + _p("Texte répété identique.")
    + _p("Intercalaire entre deux répétitions.")
    + _p("Texte répété identique.")                 # même contenu, autre position
    + _tbl([["Poste", "Montant"], ["Alpha", "1000"], ["", "vide"]])
    + _p("Unicode 中文 emoji ✅ et symboles ±≤∞.")
    + _p("Conclusion du document de test.")
)


@pytest.fixture(scope="module")
def docx_bytes() -> bytes:
    return make_docx_raw(
        f'<?xml version="1.0"?><w:document {W}><w:body>{BODY}</w:body></w:document>'
    )


@pytest.fixture(scope="module")
def extracted(docx_bytes):
    return DocxExtractor().extract(docx_bytes, ExtractionContext(filename="t.docx"))


def _chunks(text, *, size, overlap):
    return chunk_text_deterministic(text, source="t.docx", chunk_size=size, overlap=overlap)


class TestOracleCardinal:
    @pytest.mark.parametrize(
        "size,overlap",
        [(120, 30), (250, 50), (2500, 250), (80, 0), (60, 20)],
    )
    def test_offsets_reconstruisent_le_chunk(self, extracted, size, overlap):
        """`text[char_start:char_end].strip() == chunk.text`, sans exception."""
        raw = extracted.text.replace("\r\n", "\n").replace("\r", "\n")
        specs = _chunks(extracted.text, size=size, overlap=overlap)
        assert specs, "le corpus doit produire des chunks"

        for spec in specs:
            assert raw[spec.char_start : spec.char_end].strip() == spec.text, (
                f"DIVERGENCE chunk {spec.chunk_index} "
                f"[{spec.char_start},{spec.char_end})"
            )

    @pytest.mark.parametrize("size,overlap", [(120, 30), (250, 50), (2500, 250)])
    def test_fragments_surlignes_reconstruisent_le_chunk(
        self, extracted, size, overlap
    ):
        """La concaténation des fragments surlignés == le texte du chunk.

        C'est l'invariant qui porte sur le RENDU : ce que l'utilisateur voit
        marqué doit être exactement la source citée, ni plus, ni moins.
        """
        indexed = index_blocks(extracted.blocks, expected_text=extracted.text)
        specs = _chunks(extracted.text, size=size, overlap=overlap)
        compared = 0

        for spec in specs:
            pairs = find_covering_blocks(
                indexed, char_start=spec.char_start, char_end=spec.char_end
            )
            assert pairs, f"chunk {spec.chunk_index} ne recouvre aucun bloc"

            fragments = [
                block.text[hl.start : hl.end] for block, hl in pairs
            ]
            # Les blocs sont séparés par "\n\n" dans le texte : la reconstruction
            # doit reposer le même séparateur entre deux fragments consécutifs.
            rebuilt = BLOCK_SEPARATOR.join(fragments).strip()

            assert rebuilt == spec.text, (
                f"chunk {spec.chunk_index} : le surlignage reconstruit\n"
                f"  {rebuilt[:120]!r}\nau lieu de\n  {spec.text[:120]!r}"
            )
            compared += 1

        assert compared == len(specs)

    def test_surlignage_partiel_des_blocs_de_bordure(self, extracted):
        """Le premier et le dernier bloc ne sont PAS surlignés en entier."""
        indexed = index_blocks(extracted.blocks, expected_text=extracted.text)
        # Une plage qui démarre et finit délibérément au milieu d'un bloc.
        b0, b1 = indexed[2], indexed[4]
        start = b0.start + 5
        end = b1.start + 7

        pairs = find_covering_blocks(indexed, char_start=start, char_end=end)
        assert len(pairs) >= 3

        first_block, first_hl = pairs[0]
        last_block, last_hl = pairs[-1]
        assert first_hl.start == 5, "le premier bloc doit être surligné partiellement"
        assert first_hl.end == len(first_block.text)
        assert last_hl.start == 0
        assert last_hl.end == 7, "le dernier bloc doit être surligné partiellement"

        # Les blocs intermédiaires, eux, sont couverts intégralement.
        for block, hl in pairs[1:-1]:
            assert (hl.start, hl.end) == (0, len(block.text))


class TestBornesDeBlocs:
    def test_reconstruction_identique_au_texte_extrait(self, extracted):
        indexed = index_blocks(extracted.blocks, expected_text=extracted.text)
        rebuilt = BLOCK_SEPARATOR.join(b.text for b in indexed).strip()
        assert rebuilt == extracted.text.strip()

    def test_chaque_borne_rend_le_texte_du_bloc(self, extracted):
        indexed = index_blocks(extracted.blocks, expected_text=extracted.text)
        for block in indexed:
            assert extracted.text[block.start : block.end] == block.text

    def test_blocs_vides_ecartes_et_index_contigus(self, extracted):
        indexed = index_blocks(extracted.blocks, expected_text=extracted.text)
        assert all(b.text.strip() for b in indexed)
        assert [b.index for b in indexed] == list(range(len(indexed)))

    def test_divergence_de_jointure_refusee(self, extracted):
        """Si la reconstruction diffère du texte, on refuse — on ne surligne pas."""
        from apps.rag_core.services.preview import errors as E

        with pytest.raises(E.PreviewUnsupported) as exc:
            index_blocks(extracted.blocks, expected_text="un texte qui ne correspond pas")
        assert exc.value.code == E.ERR_PREVIEW_UNAVAILABLE


class TestDeterminisme:
    def test_deux_extractions_donnent_le_meme_texte_et_les_memes_blocs(self, docx_bytes):
        a = DocxExtractor().extract(docx_bytes, ExtractionContext(filename="t.docx"))
        b = DocxExtractor().extract(docx_bytes, ExtractionContext(filename="t.docx"))
        assert a.text == b.text
        assert [(x.type, x.text, x.level) for x in a.blocks] == [
            (y.type, y.text, y.level) for y in b.blocks
        ]

    def test_texte_repete_ne_perturbe_pas_les_offsets(self, extracted):
        """Un texte identique à deux endroits reste distingué — par position.

        C'est précisément ce qu'une recherche du snippet ne saurait pas faire.
        """
        occurrences = [
            b for b in index_blocks(extracted.blocks, expected_text=extracted.text)
            if b.text == "Texte répété identique."
        ]
        assert len(occurrences) == 2
        assert occurrences[0].start != occurrences[1].start


class TestFamillesDeContenu:
    @pytest.fixture(scope="class")
    def indexed(self, extracted):
        return index_blocks(extracted.blocks, expected_text=extracted.text)

    def test_accents_et_unicode_intacts(self, indexed):
        texts = [b.text for b in indexed]
        assert any("é à ù ç" in t for t in texts)
        assert any("中文" in t and "✅" in t for t in texts)

    def test_espaces_insecables_preserves(self, indexed):
        """Aucune normalisation d'espaces n'est appliquée — ni à l'ingestion, ni ici."""
        assert any(" " in b.text and " " in b.text for b in indexed)

    def test_tabulation_preservee(self, indexed):
        assert any("\t" in b.text for b in indexed)

    def test_liste_aplatie_en_un_bloc(self, indexed):
        lists = [b for b in indexed if b.kind == "list"]
        assert len(lists) == 1
        assert lists[0].text.count("- ") == 3

    def test_tableau_lineraise_avec_cellule_vide(self, indexed):
        tables = [b for b in indexed if b.kind == "table"]
        assert len(tables) == 1
        assert "Poste | Montant" in tables[0].text

        # ⚠️ Quirk d'extraction VERROUILLÉ ici : une cellule vide EN TÊTE de ligne
        # perd son marqueur de position, parce que `_table_text` applique `.strip()`
        # à la ligne jointe — `" | vide"` devient `"| vide"`. La colonne d'origine
        # n'est donc plus déductible. C'est une raison de plus de rendre les
        # tableaux en préformaté : reconstruire une grille à partir de ce texte
        # placerait « vide » dans la première colonne au lieu de la seconde.
        assert "\n| vide" in tables[0].text

    def test_titres_detectes_avec_leur_niveau(self, indexed):
        titles = [b for b in indexed if b.kind == "title"]
        assert [t.level for t in titles] == [1, 1, 2]

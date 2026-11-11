"""Lot P1 — métadonnées de citation d'une présentation : projection, API, audit, dédup.

Même fil conducteur que le lot E3 pour Excel : **ce que l'utilisateur voit doit être
exactement ce qui est audité**. La différence avec un classeur tient en un point : une
diapositive porte un NUMÉRO, mais ce numéro n'est pas une page — il vit dans `meta`,
pas dans `page_number`.
"""
from apps.chat.services.chat_sources import (
    dedup_sources,
    normalize_source_for_api,
    sources_for_api,
)
from apps.rag_core.services.query.audit import normalize_sources_for_audit
from apps.rag_core.services.query.source_meta import project_source_meta

PPTX_META = {
    "kind": "pptx_slide",
    "slide_number": 4,
    "slide_title": "Architecture cible",
    "has_images": False,
    "has_text": True,
    "requires_visual_check": False,
    # Bruit d'implémentation qui NE DOIT PAS sortir :
    "chunk_index": 7,
    "char_start": 120,
    "char_end": 980,
    "source": "Architecture.pptx",
    "page_number": None,
}


def _chunk(meta=None, **over):
    base = {
        "chunk_id": "c-pptx-1",
        "document_id": "d-pptx",
        "text": "[Diapositive 4]\n\nTitre : Architecture cible\n\n- Frontend",
        "score": 0.91,
        "source": {
            "document_id": "d-pptx",
            "filename": "Architecture.pptx",
            "page_number": None,
            "meta": dict(meta if meta is not None else PPTX_META),
        },
    }
    base.update(over)
    return base


class TestProjection:
    def test_slide_number_and_title_survive(self):
        out = project_source_meta(PPTX_META)
        assert out["slide_number"] == 4
        assert out["slide_title"] == "Architecture cible"
        assert out["kind"] == "pptx_slide"

    def test_implementation_noise_is_dropped(self):
        out = project_source_meta(PPTX_META)
        for noisy in ("chunk_index", "char_start", "char_end", "source"):
            assert noisy not in out

    def test_notes_kind_survives(self):
        out = project_source_meta({"kind": "pptx_notes", "slide_number": 2})
        assert out["kind"] == "pptx_notes"
        assert out["slide_number"] == 2

    def test_never_raises_on_garbage(self):
        assert project_source_meta(None) == {}
        assert project_source_meta("pas un dict") == {}


class TestApiPayload:
    def test_slide_provenance_reaches_the_client(self):
        out = normalize_source_for_api(_chunk(), rank=1)
        assert out["filename"] == "Architecture.pptx"
        assert out["meta"]["slide_number"] == 4
        assert out["meta"]["slide_title"] == "Architecture cible"

    def test_page_number_stays_null(self):
        """Une diapositive n'est pas une page : aucun « p. N » ne doit pouvoir s'afficher."""
        out = normalize_source_for_api(_chunk(), rank=1)
        assert out["page_number"] is None

    def test_no_page_is_fabricated_from_the_slide_number(self):
        """Le piège : recopier `slide_number` dans `page_number` « parce que c'est un
        entier ». Le viewer ouvrirait alors la page 4 d'un fichier qui n'a pas de pages."""
        out = normalize_source_for_api(_chunk(), rank=1)
        assert out["page_number"] != 4


class TestAuditParity:
    def test_audit_sees_exactly_what_the_user_sees(self):
        api = sources_for_api([_chunk()])
        audit = normalize_sources_for_audit([_chunk()])
        assert api[0]["meta"]["slide_number"] == audit[0]["meta"]["slide_number"]
        assert api[0]["meta"]["slide_title"] == audit[0]["meta"]["slide_title"]
        assert api[0]["page_number"] == audit[0]["page_number"] is None


class TestDedup:
    def test_two_slides_of_the_same_deck_are_two_sources(self):
        """Deux diapositives distinctes ne doivent JAMAIS fusionner."""
        a = _chunk(meta={**PPTX_META, "slide_number": 4}, chunk_id="a")
        b = _chunk(meta={**PPTX_META, "slide_number": 9, "slide_title": "Sauvegarde"},
                   chunk_id="b")
        out = sources_for_api([a, b])
        assert len(out) == 2
        assert sorted(s["meta"]["slide_number"] for s in out) == [4, 9]

    def test_ranks_are_renumbered(self):
        out = sources_for_api([_chunk(chunk_id="a"), _chunk(chunk_id="b")])
        assert [s["rank"] for s in out] == [1, 2]

    def test_identical_chunk_is_deduped(self):
        out = dedup_sources([
            normalize_source_for_api(_chunk(), rank=1),
            normalize_source_for_api(_chunk(), rank=2),
        ])
        assert len(out) == 1


class TestNonRegression:
    def test_excel_meta_is_untouched(self):
        out = project_source_meta({
            "kind": "excel_table", "sheet_name": "Synthèse", "cell_range": "B8:F17",
        })
        assert out["sheet_name"] == "Synthèse"
        assert out["cell_range"] == "B8:F17"
        assert "slide_number" not in out

    def test_pdf_keeps_its_page(self):
        pdf = {
            "chunk_id": "c-pdf",
            "document_id": "d-pdf",
            "text": "texte",
            "score": 0.5,
            "source": {"document_id": "d-pdf", "filename": "R.pdf", "page_number": 12,
                       "meta": {}},
        }
        out = normalize_source_for_api(pdf, rank=1)
        assert out["page_number"] == 12


# --------------------------------------------------------------------------- #
# évidence VISUELLE d'une diapositive
# --------------------------------------------------------------------------- #
def _visual_chunk_meta():
    """Métadonnées EXACTES d'un fragment visuel PPTX, construites par le rail générique."""
    import hashlib

    from apps.rag_core.services.ingest.visual import contracts as C
    from tests_root.unit.visual import _vision_mocks as M

    asset = C.VisualAsset(
        content=M.IMAGE_BYTES, mime_type="image/png", width=640, height=480,
        source_kind="pptx", content_hash=hashlib.sha256(M.IMAGE_BYTES).hexdigest(), ordinal=0,
        source_locator={"type": "pptx", "slide_number": 3, "shape_order": 2, "rel_id": "rId2",
                        "part": "ppt/media/image1.png", "grouped": False},
        source_page=None, source_bbox=None, origin="embedded", task="describe")
    meta = C.artifact_meta(C.build_artifact(asset, M.success("photo")), "photo")
    # Ajouts du chunker, comme pour tout fragment.
    meta.update({"chunk_index": 5, "char_start": 0, "char_end": 120, "source": "Deck.pptx",
                 "page_number": None})
    return meta


class TestVisualSlideEvidence:
    def test_projection_cites_the_slide_not_a_page(self):
        out = project_source_meta(_visual_chunk_meta())
        assert out["slide_number"] == 3
        assert out["locator"] == {"type": "pptx"}, "seules les clés génériques du repère sortent"
        assert "page_number" not in out
        assert "kind" not in out, "jamais présenté comme une diapositive native"

    def test_projection_never_leaks_visual_internals(self):
        out = project_source_meta(_visual_chunk_meta())
        assert "visual" not in out
        brut = repr(out)
        for fuite in ("asset_hash", "rId2", "ppt/media", "pipeline"):
            assert fuite not in brut

    def test_api_source_has_slide_and_no_page(self):
        chunk = {
            "chunk_id": "c-visuel",
            "document_id": "d-pptx",
            "text": "Visual type: photograph",
            "score": 0.7,
            "source": {"document_id": "d-pptx", "filename": "Deck.pptx", "page_number": None,
                       "meta": _visual_chunk_meta()},
        }
        out = normalize_source_for_api(chunk, rank=1)
        assert out["page_number"] is None
        assert out["meta"]["slide_number"] == 3

    def test_native_slide_projection_unchanged(self):
        """Non-régression : la projection d'un fragment natif est inchangée."""
        assert project_source_meta(PPTX_META) == {
            "kind": "pptx_slide",
            "slide_number": 4,
            "slide_title": "Architecture cible",
            "has_images": False,
            "has_text": True,
            "requires_visual_check": False,
        }

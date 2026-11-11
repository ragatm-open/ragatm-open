"""`GET /api/v1/documents/<id>/presentation-preview/` — contrat et sécurité.

Fil directeur : **aucun refus ne doit ressembler à un succès**. Ni diapositive
vide, ni repli sur la première, ni surlignage approché.
"""
import hashlib
import sys
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.test import override_settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "extraction"))

from _builders import build_pptx  # noqa: E402

from apps.rag_core.models.chunk import Chunk  # noqa: E402
from apps.rag_core.models.document import Document  # noqa: E402
from apps.rag_core.services.ingest.extraction import mime as M  # noqa: E402
from apps.rag_core.services.ingest.extraction.base import ExtractionContext  # noqa: E402
from apps.rag_core.services.ingest.extraction.pptx_extractor import (  # noqa: E402
    PptxExtractor,
)
from apps.rag_core.services.ingest.indexing import _build_slide_specs  # noqa: E402

pytestmark = pytest.mark.django_db

LOREM = "Le backend communique avec la passerelle selon un protocole borne. "

DECK = [
    {"title": "Architecture cible",
     "bullets": [("Frontend", 0), ("Next.js", 1), (LOREM * 4, 0)]},
    {"title": "Services et ports",
     "table": [["Service", "Port"], ["Gateway", "8100"], ["", ""]]},
    {"title": None,
     "textboxes": [("Bloc bas", 6_000_000, 0), ("Bloc haut", 2_000_000, 0)]},
    {"title": "Avec visuel", "bullets": [(LOREM * 3, 0)],
     "picture": True, "alt_text": "Schema du flux"},
    {"title": "Fin", "bullets": [("Conclusion", 0)]},
]
NOTES = {1: "Insister sur le loopback", 4: "Commentaire de l'orateur"}


def _ingest(rag_space, *, filename="deck.pptx", notes=False, deck=None,
            data=None, chunk_size=300, overlap=60):
    """Ingère RÉELLEMENT : extraction + chunks avec offsets, comme le pipeline."""
    data = data if data is not None else build_pptx(deck or DECK, notes=NOTES)

    if notes:
        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            result = PptxExtractor().extract(data, ExtractionContext(filename=filename))
            specs = _build_slide_specs(
                result=result, filename=filename,
                chunk_size=chunk_size, overlap=overlap,
            )
    else:
        result = PptxExtractor().extract(data, ExtractionContext(filename=filename))
        specs = _build_slide_specs(
            result=result, filename=filename, chunk_size=chunk_size, overlap=overlap
        )

    doc = Document.objects.create(
        rag_space=rag_space, filename=filename, content_type=M.PPTX,
        sha256=hashlib.sha256(data + filename.encode()).hexdigest(),
        status="READY", meta={"extraction": dict(result.metadata)},
    )
    doc.storage_file.save(filename, ContentFile(data), save=True)

    chunks = [
        Chunk.objects.create(
            rag_space=rag_space, document=doc, chunk_index=s.chunk_index, text=s.text,
            meta={
                "chunk_index": s.chunk_index, "page_number": None, "source": s.source,
                "char_start": s.char_start, "char_end": s.char_end, **s.meta,
            },
        )
        for s in specs
    ]
    return doc, chunks, result


def _url(doc, **params):
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return f"/api/v1/documents/{doc.id}/presentation-preview/" + (f"?{qs}" if qs else "")


# --------------------------------------------------------------------------- #
class TestPermissions:
    def test_utilisateur_autorise(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200, res.data
        assert res.data["document"]["preview_kind"] == "presentation"

    def test_non_authentifie(self, rag_space):
        from rest_framework.test import APIClient

        doc, _, _ = _ingest(rag_space)
        assert APIClient().get(_url(doc)).status_code in (401, 403)

    def test_autre_workspace_404(self, rag_space, foreign_setup):
        doc, chunks, _ = _ingest(rag_space)
        res = foreign_setup["client"].get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 404

    def test_document_inexistant(self, api_client):
        import uuid

        res = api_client.get(f"/api/v1/documents/{uuid.uuid4()}/presentation-preview/")
        assert res.status_code == 404


class TestIDOR:
    def test_chunk_d_un_autre_document(self, api_client, rag_space):
        a, _, _ = _ingest(rag_space, filename="a.pptx")
        b, chunks_b, _ = _ingest(rag_space, filename="b.pptx")
        res = api_client.get(_url(a, chunk_id=str(chunks_b[0].id)))
        assert res.status_code == 404
        assert res.data["reason_code"] == "chunk_document_mismatch"

    def test_chunk_d_un_autre_ragspace(self, api_client, rag_space, foreign_setup):
        doc, _, _ = _ingest(rag_space)
        _, f_chunks, _ = _ingest(foreign_setup["rag_space"], filename="f.pptx")
        res = api_client.get(_url(doc, chunk_id=str(f_chunks[0].id)))
        assert res.status_code == 404

    def test_chunk_supprime(self, api_client, rag_space):
        import uuid

        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(uuid.uuid4())))
        assert res.status_code == 409
        assert res.data["reason_code"] == "chunk_stale"


class TestLocalisation:
    def test_ouverture_sur_la_diapositive_citee(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        chunk = chunks[len(chunks) // 2]
        res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
        assert res.status_code == 200, res.data

        assert res.data["source"]["slide_number"] == chunk.meta["slide_number"]
        assert res.data["viewport"]["active_slide"] == chunk.meta["slide_number"]

        # Les fragments surlignés reconstruisent EXACTEMENT le texte du chunk.
        slide = next(
            s for s in res.data["slides"] if s["number"] == chunk.meta["slide_number"]
        )
        marked = [
            b["text"][b["highlight"]["start"] : b["highlight"]["end"]]
            for b in slide["blocks"] if b["highlight"]
        ]
        assert "\n\n".join(marked).strip() == chunk.text

    def test_tous_les_chunks_se_localisent(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        assert len(chunks) >= 5
        for chunk in chunks:
            res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
            assert res.status_code == 200, (chunk.chunk_index, res.data)
            slide = next(
                s for s in res.data["slides"]
                if s["number"] == chunk.meta["slide_number"]
            )
            key = "speaker_notes" if chunk.meta["kind"] == "pptx_notes" else "blocks"
            marked = [
                b["text"][b["highlight"]["start"] : b["highlight"]["end"]]
                for b in slide[key] if b["highlight"]
            ]
            assert "\n\n".join(marked).strip() == chunk.text

    def test_blocs_de_bordure_partiellement_surlignes(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        for chunk in chunks:
            res = api_client.get(_url(doc, chunk_id=str(chunk.id)))
            slide = next(
                s for s in res.data["slides"]
                if s["number"] == chunk.meta["slide_number"]
            )
            marked = [b for b in slide["blocks"] if b["highlight"]]
            if len(marked) >= 2:
                first, last = marked[0], marked[-1]
                if first["highlight"]["start"] > 0 or last["highlight"]["end"] < len(
                    last["text"]
                ):
                    return
        pytest.skip("aucun chunk de ce corpus ne coupe un bloc")

    def test_slide_number_contradictoire(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        chunk = chunks[0]
        other = chunk.meta["slide_number"] + 1
        res = api_client.get(_url(doc, chunk_id=str(chunk.id), slide_number=other))
        assert res.status_code == 400
        assert res.data["reason_code"] == "invalid_slide_range"

    def test_navigation_libre_par_slide(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, slide_number=3))
        assert res.status_code == 200
        assert res.data["source"] is None
        assert res.data["viewport"]["active_slide"] == 3
        assert all(
            b["highlight"] is None for s in res.data["slides"] for b in s["blocks"]
        )

    def test_slide_inexistante(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, slide_number=999))
        assert res.status_code == 404
        assert res.data["reason_code"] == "slide_not_found"

    def test_slide_number_0_refuse(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, slide_number=0))
        assert res.status_code == 400

    def test_sans_chunk_ni_slide(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        res = api_client.get(_url(doc))
        assert res.status_code == 400
        assert res.data["reason_code"] == "invalid_slide_range"

    def test_offsets_absents(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        c = chunks[0]
        c.meta = {k: v for k, v in c.meta.items() if k not in ("char_start", "char_end")}
        c.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "invalid_chunk_offsets"

    def test_offsets_hors_unite(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        c = chunks[0]
        c.meta = {**c.meta, "char_start": 5, "char_end": 10_000_000}
        c.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"

    def test_chunk_non_pptx(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        c = Chunk.objects.create(
            rag_space=rag_space, document=doc, chunk_index=999, text="…",
            meta={"kind": "excel_table", "sheet_name": "S"},
        )
        res = api_client.get(_url(doc, chunk_id=str(c.id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "slide_not_previewable"


class TestGardeNotes:
    def test_notes_indexees_puis_flag_ferme_refuse(self, api_client, rag_space):
        """LE défaut critique du rail PPTX : les bases se décalent."""
        doc, chunks, _ = _ingest(rag_space, notes=True)
        # Ingéré avec notes ; le réglage courant (défaut) les exclut.
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "notes_configuration_mismatch"
        assert "slides" not in res.data

    def test_notes_non_indexees_puis_flag_ouvert_refuse(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space, notes=False)
        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "notes_configuration_mismatch"

    def test_reglages_concordants_acceptes(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space, notes=True)
        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200, res.data
        assert res.data["source"]["notes_included"] is True

    def test_chunk_de_notes_rendu_a_part(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space, notes=True)
        note_chunk = next(c for c in chunks if c.meta["kind"] == "pptx_notes")
        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            res = api_client.get(_url(doc, chunk_id=str(note_chunk.id)))
        assert res.status_code == 200, res.data
        assert res.data["source"]["kind"] == "pptx_notes"

        slide = next(
            s for s in res.data["slides"]
            if s["number"] == note_chunk.meta["slide_number"]
        )
        assert slide["speaker_notes"]
        assert all(b["type"] == "speaker_notes" for b in slide["speaker_notes"])
        # Le corps visible n'est JAMAIS surligné par une citation de notes.
        assert all(b["highlight"] is None for b in slide["blocks"])
        marked = [
            b["text"][b["highlight"]["start"] : b["highlight"]["end"]]
            for b in slide["speaker_notes"] if b["highlight"]
        ]
        assert "\n\n".join(marked).strip() == note_chunk.text

    def test_aucune_note_quand_flag_ferme(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space, notes=False)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 200
        assert all(s["speaker_notes"] == [] for s in res.data["slides"])
        assert "speaker_notes_included" not in res.data["fidelity"]["warnings"]


class TestVersions:
    def test_extractor_version_differente(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {
            "extraction": {**doc.meta["extraction"], "extractor_version": "0"}
        }
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "document_version_mismatch"

    def test_chunking_version_differente(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {
            "extraction": {**doc.meta["extraction"], "chunking_version": "99"}
        }
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409

    def test_sha256_attendu_different(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id), sha256="f" * 64))
        assert res.status_code == 409

    def test_sans_metadonnees_extraction(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        doc.meta = {}
        doc.save(update_fields=["meta"])
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 422
        assert res.data["reason_code"] == "preview_unavailable"


class TestFormat:
    def test_pdf_refuse(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="r.pdf", content_type=M.PDF, status="READY"
        )
        doc.storage_file.save("r.pdf", ContentFile(b"%PDF-1.4"), save=True)
        res = api_client.get(_url(doc, slide_number=1))
        assert res.status_code == 422
        assert res.data["reason_code"] == "unsupported_format"

    def test_presentation_corrompue_jamais_500(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="c.pptx", content_type=M.PPTX,
            sha256="c" * 64, status="READY",
            meta={"extraction": {"pptx": {"notes_included": False}}},
        )
        doc.storage_file.save("c.pptx", ContentFile(b"PK\x03\x04 pas un pptx"), save=True)
        res = api_client.get(_url(doc, slide_number=1))
        assert res.status_code == 422
        assert res.data["reason_code"] == "presentation_unreadable"

    def test_fichier_absent(self, api_client, rag_space):
        doc = Document.objects.create(
            rag_space=rag_space, filename="v.pptx", content_type=M.PPTX,
            status="READY", meta={"extraction": {"pptx": {"notes_included": False}}},
        )
        res = api_client.get(_url(doc, slide_number=1))
        assert res.status_code == 404
        assert res.data["reason_code"] == "no_file_available"


class TestContrat:
    def test_forme_de_la_reponse(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, chunk_id=str(chunks[0].id))).data

        assert d["schema_version"] == 1
        assert d["document"]["sha256"] == doc.sha256
        assert d["versions"] == {"extractor_version": "1", "chunking_version": "1"}
        assert d["presentation"]["slide_count"] >= 5
        assert d["fidelity"]["simplified"] is True
        for w in ("layout_not_rendered", "images_not_rendered", "charts_not_extracted",
                  "smartart_not_extracted", "hidden_slides_not_detected",
                  "list_numbering_lost", "tables_simplified",
                  "animations_not_rendered", "transitions_not_rendered"):
            assert w in d["fidelity"]["warnings"]
        for o in ("images", "charts", "smartart", "animations", "positions", "themes"):
            assert o in d["fidelity"]["omitted"]
        assert d["truncation"]["truncated"] is False

    def test_inventaire_1_based_avec_titres(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, chunk_id=str(chunks[0].id))).data
        inv = d["presentation"]["slides"]
        assert [s["number"] for s in inv] == sorted(s["number"] for s in inv)
        assert min(s["number"] for s in inv) == 1
        assert inv[0]["title"] == "Architecture cible"
        # Une diapositive sans titre porte None, jamais un titre inventé.
        assert any(s["title"] is None for s in inv)

    def test_diapositive_citee_signalee(self, api_client, rag_space):
        doc, chunks, _ = _ingest(rag_space)
        chunk = chunks[0]
        d = api_client.get(_url(doc, chunk_id=str(chunk.id))).data
        cited = [s for s in d["presentation"]["slides"] if s["has_citation"]]
        assert [s["number"] for s in cited] == [chunk.meta["slide_number"]]

    def test_types_de_blocs_limites_au_mvp(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        kinds = set()
        for n in (1, 2, 3, 4, 5):
            d = api_client.get(_url(doc, slide_number=n)).data
            for s in d["slides"]:
                kinds |= {b["type"] for b in s["blocks"]}
        assert kinds <= {"title", "paragraph", "list", "table", "speaker_notes",
                         "unsupported"}
        assert not (kinds & {"subtitle", "image", "chart", "smartart", "video",
                             "ole", "shape"})

    def test_tableau_prefomate_et_signale(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=2)).data
        tables = [b for s in d["slides"] for b in s["blocks"] if b["type"] == "table"]
        assert tables
        for t in tables:
            assert t["render_mode"] == "preformatted"
            assert "table_simplified" in t["warnings"]
            assert "merged_cells_may_repeat" in t["warnings"]
            assert "rows" not in t and "cells" not in t

    def test_liste_declaree_aplatie(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=1)).data
        lists = [b for s in d["slides"] for b in s["blocks"] if b["type"] == "list"]
        assert lists and all(b["list_fidelity"] == "flattened" for b in lists)

    def test_presence_visuelle_signalee_sans_placeholder(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=4)).data
        slide = next(s for s in d["slides"] if s["number"] == 4)
        # Le texte alternatif est du texte RÉELLEMENT indexé, pas un placeholder.
        alts = [b for b in slide["blocks"] if "alt_text" in b["warnings"]]
        assert alts and alts[0]["text"].startswith("[Image : ")
        assert "visual_content_present" in d["fidelity"]["warnings"]

    def test_aucun_etat_masque_expose(self, api_client, rag_space):
        """L'état masqué n'est pas lu : ne jamais l'exposer, même à `false`."""
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=1)).data
        assert all("hidden" not in s for s in d["presentation"]["slides"])
        assert all("hidden" not in s for s in d["slides"])
        assert "hidden_slides_not_detected" in d["fidelity"]["warnings"]


class TestVolumetrie:
    def test_fenetre_bornee(self, api_client, rag_space):
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=3, context_before=1,
                                context_after=1)).data
        assert [s["number"] for s in d["slides"]] == [2, 3, 4]

    def test_troncature_declaree(self, api_client, rag_space, settings):
        settings.PPTX_PREVIEW_MAX_SLIDES = 2
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=3, context_before=5,
                                context_after=5)).data
        assert d["truncation"]["truncated"] is True
        assert d["truncation"]["reason"] == "max_slides"
        assert len(d["slides"]) <= 2

    def test_bloc_long_tronque_avec_longueur_reelle(self, api_client, rag_space, settings):
        settings.PPTX_PREVIEW_MAX_BLOCK_CHARS = 40
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=1)).data
        long_blocks = [b for s in d["slides"] for b in s["blocks"] if b["truncated"]]
        assert long_blocks
        for b in long_blocks:
            assert len(b["text"]) == 40
            assert b["full_length"] > 40

    @pytest.mark.parametrize(
        "params", [{"context_before": 99}, {"context_after": 99}, {"context_before": -1}]
    )
    def test_contexte_excessif(self, api_client, rag_space, params):
        doc, chunks, _ = _ingest(rag_space)
        res = api_client.get(_url(doc, chunk_id=str(chunks[0].id), **params))
        assert res.status_code == 400
        assert res.data["reason_code"] == "context_limit_exceeded"

    def test_inventaire_borne(self, api_client, rag_space, settings):
        settings.PPTX_PREVIEW_MAX_INVENTORY = 2
        doc, _, _ = _ingest(rag_space)
        d = api_client.get(_url(doc, slide_number=1)).data
        assert len(d["presentation"]["slides"]) <= 2
        assert d["presentation"]["inventory_truncated"] is True


class TestSecuriteContenu:
    def test_html_transporte_comme_texte(self, api_client, rag_space):
        deck = [
            {"title": "<script>alert(1)</script>",
             "bullets": [("<img src=x onerror=alert(1)>", 0),
                         ("javascript:alert(1)", 0)]},
        ]
        doc, chunks, _ = _ingest(rag_space, deck=deck, filename="xss.pptx")
        d = api_client.get(_url(doc, chunk_id=str(chunks[0].id))).data
        texts = " ".join(b["text"] for s in d["slides"] for b in s["blocks"])
        assert "<script>alert(1)</script>" in texts
        assert "javascript:alert(1)" in texts

    def test_aucun_texte_de_diapositive_dans_les_logs(self, api_client, rag_space, caplog):
        import logging

        caplog.set_level(logging.INFO)
        deck = [{"title": "Titre", "bullets": [("CANARI-PPTX-SECRET-42", 0)]}]
        doc, chunks, _ = _ingest(rag_space, deck=deck, filename="canari.pptx")
        d = api_client.get(_url(doc, chunk_id=str(chunks[0].id))).data

        assert any(
            "CANARI-PPTX-SECRET-42" in b["text"]
            for s in d["slides"] for b in s["blocks"]
        )
        assert "CANARI-PPTX-SECRET-42" not in caplog.text


class TestCache:
    def test_chunk_supprime_apres_mise_en_cache(self, api_client, rag_space):
        from django.core.cache import cache

        cache.clear()
        doc, chunks, _ = _ingest(rag_space)
        chunk_id = chunks[0].id
        assert api_client.get(_url(doc, chunk_id=str(chunk_id))).status_code == 200

        Chunk.objects.filter(document=doc).delete()

        res = api_client.get(_url(doc, chunk_id=str(chunk_id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "chunk_stale"
        assert "slides" not in res.data

    def test_changement_de_reglage_notes_apres_cache(self, api_client, rag_space):
        """Le cache ne doit pas survivre à un changement de configuration."""
        from django.core.cache import cache

        cache.clear()
        doc, chunks, _ = _ingest(rag_space, notes=False)
        assert api_client.get(_url(doc, chunk_id=str(chunks[0].id))).status_code == 200

        with override_settings(DOCUMENT_PPTX_INCLUDE_NOTES=True):
            res = api_client.get(_url(doc, chunk_id=str(chunks[0].id)))
        assert res.status_code == 409
        assert res.data["reason_code"] == "notes_configuration_mismatch"

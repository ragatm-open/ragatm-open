"""découverte des visuels INTÉGRÉS d'un PDF.

Ce que ces tests verrouillent, et pourquoi :

* **une seule ouverture, une seule itération, aucun rendu** — l'invariant architectural
  du lot est ici EXÉCUTABLE, pas seulement documenté : un compteur d'ouvertures et une
  doublure qui fait exploser tout appel de rendu ;
* **admission générique** — taille minimale, répétition décorative, plafonds : aucune
  règle ne connaît de vocabulaire de corpus, de nom de fichier ni de marque ;
* **provenance** — page 1-based, bbox dans le repère de la page, repère stable, empreinte
  des octets RÉELLEMENT envoyés ;
* **tâche** — `describe` et rien d'autre, jamais `auto` ni une tâche structurée.

Aucun réseau, aucun appel Gateway : la découverte n'analyse rien.
"""
from __future__ import annotations

import io

import pytest

from apps.rag_core.services.ingest.extraction import pdf_visual
from apps.rag_core.services.ingest.extraction.pdf_visual import (
    PdfVisualLimits,
    discover_pdf_visual_assets,
)

pytest.importorskip("fitz")
pytest.importorskip("reportlab")


# --------------------------------------------------------------------------- #
# Fabriques déterministes
# --------------------------------------------------------------------------- #
def _png(w: int, h: int, couleur: tuple, forme: str = "ellipse") -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(img)
    box = [w // 8, h // 8, w - w // 8, h - h // 8]
    (d.ellipse if forme == "ellipse" else d.rectangle)(box, fill=couleur)
    # Texture déterministe : un contenu utile n'est jamais un aplat.
    for i in range(0, w, 16):
        d.line([i, 0, w - i, h], fill=tuple((c + 3 * i) % 256 for c in couleur), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PHOTO = _png(400, 300, (230, 120, 30))
AUTRE = _png(360, 280, (40, 150, 70), forme="rect")
ICONE = _png(24, 24, (20, 20, 20), forme="rect")
LOGO = _png(200, 160, (10, 90, 180), forme="rect")


def _pdf(pages: list[dict]) -> bytes:
    """PDF déterministe : une entrée par page, ``{"texte": str, "images": [(bytes, x, y, w, h)]}``."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    c = canvas.Canvas(out)
    for page in pages:
        if page.get("texte"):
            c.drawString(72, 780, page["texte"])
        for raw, x, y, w, h in page.get("images", ()):
            c.drawImage(ImageReader(io.BytesIO(raw)), x, y, width=w, height=h)
        c.showPage()
    c.save()
    return out.getvalue()


PDF_TEXTE_ET_PHOTO = _pdf([
    {"texte": "Page une, texte natif parfaitement lisible.",
     "images": [(PHOTO, 100, 420, 300, 225)]},
])

PDF_MULTIPAGE = _pdf([
    {"texte": "Page une.", "images": [(PHOTO, 100, 500, 300, 225)]},
    {"texte": "Page deux, sans image."},
    {"texte": "Page trois.", "images": [(AUTRE, 120, 480, 270, 210)]},
])

PDF_TEXTE_SEUL = _pdf([{"texte": "Document strictement textuel, aucune image."}])


# --------------------------------------------------------------------------- #
# §2 / contrainte d'architecture : une passe, sans rendu
# --------------------------------------------------------------------------- #
class TestTraverseeUnique:
    def test_une_seule_ouverture_et_une_seule_iteration(self, monkeypatch):
        import fitz

        ouvertures = {"n": 0}
        vraie_open = fitz.open

        def _compte(*a, **k):
            ouvertures["n"] += 1
            return vraie_open(*a, **k)

        monkeypatch.setattr(fitz, "open", _compte)
        d = discover_pdf_visual_assets(PDF_MULTIPAGE)

        assert ouvertures["n"] == 1, "le document ne doit être ouvert QU'UNE fois"
        assert d.stats["pdf_opens"] == 1
        assert d.stats["pages_scanned"] == 3, "une itération de pages, pas davantage"

    def test_aucun_rendu_de_page(self, monkeypatch):
        """Aucun `get_pixmap` : la découverte LIT des rasters, elle n'en fabrique pas."""
        import fitz

        def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
            raise AssertionError("rendu de page interdit pendant la découverte")

        monkeypatch.setattr(fitz.Page, "get_pixmap", _interdit)
        d = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO)
        assert d.stats["admitted"] == 1

    def test_aucun_appel_gateway(self, monkeypatch):
        """La découverte n'analyse rien : le client vision ne doit pas être touché."""
        from apps.llm_router.gateway import vision as vision_mod

        def _interdit(*a, **k):  # pragma: no cover
            raise AssertionError("appel réseau interdit pendant la découverte")

        monkeypatch.setattr(vision_mod.requests, "post", _interdit)
        assert discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO).stats["admitted"] == 1


# --------------------------------------------------------------------------- #
# §11-§16 : provenance
# --------------------------------------------------------------------------- #
class TestProvenance:
    def test_page_bbox_locator_et_empreinte(self):
        d = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO)
        assert len(d.assets) == 1
        a = d.assets[0]

        assert a.source_kind == "pdf"
        assert a.source_page == 1, "pagination 1-based, comme les citations RAGatm"
        assert a.source_bbox is not None and len(a.source_bbox) == 4
        x0, y0, x1, y1 = a.source_bbox
        assert x0 < x1 and y0 < y1
        assert a.source_locator["type"] == "pdf"
        assert a.source_locator["page"] == 1
        assert isinstance(a.source_locator["xref"], int)
        assert a.mime_type == "image/png"
        assert a.origin == "embedded"
        assert a.ordinal == 0

    def test_empreinte_des_octets_reellement_envoyes(self):
        from apps.rag_core.services.ingest.visual.contracts import sha256_hex

        a = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO).assets[0]
        assert a.content_hash == sha256_hex(a.content)

    def test_decouverte_deterministe(self):
        """Mêmes octets d'entrée -> mêmes empreintes, donc même identité de requête."""
        a = [x.content_hash for x in discover_pdf_visual_assets(PDF_MULTIPAGE).assets]
        b = [x.content_hash for x in discover_pdf_visual_assets(PDF_MULTIPAGE).assets]
        assert a == b and len(a) == 2

    def test_pages_correctes_sur_plusieurs_pages(self):
        d = discover_pdf_visual_assets(PDF_MULTIPAGE)
        assert [x.source_page for x in d.assets] == [1, 3], "aucune dérive de page"
        assert [x.ordinal for x in d.assets] == [0, 1]
        assert len({x.content_hash for x in d.assets}) == 2


# --------------------------------------------------------------------------- #
# §7 / §46 : tâche opérationnelle
# --------------------------------------------------------------------------- #
class TestTache:
    def test_seule_describe_est_demandee(self):
        for a in discover_pdf_visual_assets(PDF_MULTIPAGE).assets:
            assert a.task == "describe"

    def test_aucune_tache_non_qualifiee(self):
        from apps.rag_core.services.ingest.visual.capability import UNQUALIFIED_TASKS

        taches = {a.task for a in discover_pdf_visual_assets(PDF_MULTIPAGE).assets}
        assert taches and not (taches & UNQUALIFIED_TASKS)


# --------------------------------------------------------------------------- #
# §19-§21 / §49-§50 : admission générique et bornes
# --------------------------------------------------------------------------- #
class TestAdmission:
    def test_icone_trop_petite_ecartee(self):
        data = _pdf([{"texte": "Page.", "images": [(ICONE, 60, 700, 12, 12),
                                                   (PHOTO, 100, 400, 300, 225)]}])
        d = discover_pdf_visual_assets(data)
        assert d.stats["omitted_too_small"] == 1
        assert d.stats["admitted"] == 1

    def test_visuel_repete_est_decoratif(self):
        """Mêmes octets sur beaucoup de pages = logo/bandeau. Signal GÉNÉRIQUE."""
        pages = [{"texte": f"Page {i}.", "images": [(LOGO, 450, 760, 80, 60)]} for i in range(5)]
        d = discover_pdf_visual_assets(_pdf(pages))
        assert d.stats["omitted_decorative"] == 1
        assert d.assets == []

    def test_repetition_sous_le_seuil_reste_admise(self):
        pages = [{"texte": f"Page {i}.", "images": [(PHOTO, 100, 500, 300, 225)]} for i in range(2)]
        d = discover_pdf_visual_assets(_pdf(pages), limits=PdfVisualLimits(max_repeats=3))
        assert d.stats["admitted"] == 1
        assert d.assets[0].source_page == 1
        assert [o.source_page for o in d.assets[0].occurrences] == [2]

    def test_plafond_global(self):
        d = discover_pdf_visual_assets(PDF_MULTIPAGE, limits=PdfVisualLimits(max_assets=1))
        assert d.stats["admitted"] == 1
        assert d.stats["omitted_limit"] == 1

    def test_plafond_par_page(self):
        data = _pdf([{"texte": "Planche.",
                      "images": [(PHOTO, 60, 500, 200, 150), (AUTRE, 300, 500, 200, 150)]}])
        d = discover_pdf_visual_assets(data, limits=PdfVisualLimits(max_per_page=1))
        assert d.stats["admitted"] == 1
        assert d.stats["omitted_limit"] == 1

    def test_plafond_de_pixels_reduit_au_lieu_decarter(self):
        """Le contrat V1 borne à 2 Mpx : on RÉDUIT, on ne perd pas l'évidence."""
        grande = _png(1200, 900, (200, 60, 60))
        data = _pdf([{"texte": "Page.", "images": [(grande, 60, 300, 400, 300)]}])
        d = discover_pdf_visual_assets(data, limits=PdfVisualLimits(max_pixels=100_000))
        assert d.stats["admitted"] == 1
        a = d.assets[0]
        assert a.width * a.height <= 100_000
        assert d.stats["omitted_too_large"] == 0

    def test_pdf_sans_visuel(self):
        d = discover_pdf_visual_assets(PDF_TEXTE_SEUL)
        assert d.assets == []
        assert d.stats["images_seen"] == 0
        assert d.stats["admitted"] == 0

    def test_octets_vides_ou_illisibles(self):
        assert discover_pdf_visual_assets(b"").assets == []
        assert discover_pdf_visual_assets(b"ceci n'est pas un PDF").assets == []


# --------------------------------------------------------------------------- #
# §8 / §9 : le natif fait foi
# --------------------------------------------------------------------------- #
class TestNonDuplicationTableauNatif:
    def test_visuel_couvrant_une_region_tabulaire_est_ecarte(self):
        d = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO)
        bbox = d.assets[0].source_bbox

        ecarte = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO,
                                            suppressed_regions={1: [bbox]})
        assert ecarte.assets == []
        assert ecarte.stats["omitted_table_overlap"] == 1

    def test_region_tabulaire_ailleurs_ne_change_rien(self):
        d = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO,
                                       suppressed_regions={1: [(0.0, 0.0, 10.0, 10.0)]})
        assert d.stats["admitted"] == 1
        assert d.stats["omitted_table_overlap"] == 0

    def test_region_sur_une_autre_page_ne_change_rien(self):
        d = discover_pdf_visual_assets(PDF_MULTIPAGE, suppressed_regions={2: [(0, 0, 600, 800)]})
        assert d.stats["admitted"] == 2

    def test_ratio_de_recouvrement_partiel(self):
        a = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO).assets[0]
        x0, y0, x1, y1 = a.source_bbox
        moitie = (x0, y0, x0 + (x1 - x0) / 2.0, y1)   # ~50 % de recouvrement

        strict = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO, suppressed_regions={1: [moitie]},
                                            limits=PdfVisualLimits(table_overlap_ratio=0.9))
        souple = discover_pdf_visual_assets(PDF_TEXTE_ET_PHOTO, suppressed_regions={1: [moitie]},
                                            limits=PdfVisualLimits(table_overlap_ratio=0.3))
        assert strict.stats["admitted"] == 1, "recouvrement partiel : le visuel survit"
        assert souple.stats["admitted"] == 0, "seuil abaissé : le natif l'emporte"


# --------------------------------------------------------------------------- #
# §53-§54 : confidentialité
# --------------------------------------------------------------------------- #
class TestConfidentialite:
    def test_les_statistiques_ne_portent_aucun_octet(self):
        import json

        d = discover_pdf_visual_assets(PDF_MULTIPAGE)
        brut = json.dumps(d.stats)
        selection = d.stats["selection"]
        assert all(isinstance(v, int) for k, v in d.stats.items() if k != "selection")
        assert all(isinstance(v, (int, str)) for v in selection.values())
        assert "iVBOR" not in brut and "data:image" not in brut

    def test_le_geometrique_ne_lit_aucun_contenu(self):
        """`_overlap_ratio` est de la géométrie pure : aucune inspection de cellule."""
        assert pdf_visual._overlap_ratio((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
        assert pdf_visual._overlap_ratio((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
        assert pdf_visual._overlap_ratio((0, 0, 10, 10), (0, 0, 5, 10)) == 0.5

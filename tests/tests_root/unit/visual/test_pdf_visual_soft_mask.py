"""masques doux (``/SMask``) et aplats sans information.

Ce que ces tests verrouillent, et pourquoi :

* **barrière d'identité** — une image SANS masque garde le chemin historique
  (``extract_image`` + PIL) : mêmes octets, même empreinte. Le décodeur JPEG de MuPDF ne
  rend pas les mêmes pixels ; l'utiliser changerait l'identité de presque toutes les photos ;
* **recomposition** — une image à ``/SMask`` est analysée telle qu'elle est AFFICHÉE
  (base + masque, puis composition sur blanc), y compris CMYK, indexée, niveaux de gris,
  JPX, et masque d'une autre définition que la base ;
* **aplats** — ombre portée, élément invisible, noir opaque : ``omitted_uniform``, jamais
  envoyés ; un plan clairsemé, un dégradé ou un logo ne sont PAS des aplats ;
* **repli** — une recomposition refusée par MuPDF ne fait jamais échouer le document ;
* **sélection inchangée** — premier admissible, premier servi ; les plafonds ne bougent pas.

Les fixtures reproduisent de façon synthétique les cas observés sur un PDF réel
(ombres xref 13/72, invisible xref 15, photos détourées xref 284/287, image indexée 2×2
à masque 74×120 xref 52/59) sans embarquer ce document.
"""
from __future__ import annotations

import hashlib
import io
import logging

import pytest

from apps.rag_core.services.ingest.extraction import pdf_visual
from apps.rag_core.services.ingest.extraction.image_extractor import normalize_rgb_png
from apps.rag_core.services.ingest.extraction.pdf_visual import (
    PdfVisualLimits,
    discover_pdf_visual_assets,
)

fitz = pytest.importorskip("fitz")


# --------------------------------------------------------------------------- #
# Fabriques déterministes
# --------------------------------------------------------------------------- #
def _encode(img, fmt="PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _photo(w=320, h=240, seed=1):
    """Contenu utile : bandes colorées et ellipses, jamais un aplat."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (30, 30, 30))
    d = ImageDraw.Draw(img)
    for i in range(0, w, 10):
        d.rectangle([i, 0, i + 6, h], fill=((i * 3 + seed * 40) % 256, (i * 7) % 256, (200 - i) % 256))
    for k in range(6):
        d.ellipse([k * 40, k * 25, k * 40 + 60, k * 25 + 50], fill=((k * 50) % 256, 220, (k * 30) % 256))
    return img


def _disc_mask(w, h):
    """Masque de détourage : objet central opaque, coins transparents."""
    from PIL import Image, ImageDraw

    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).ellipse([w // 10, h // 10, w - w // 10, h - h // 10], fill=255)
    return m


def _shadow(w=600, h=400, blur=6):
    """Ombre portée : aplat noir semi-transparent à bords doux (cas xref 13/72)."""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([12, 12, w - 12, h - 12], fill=(0, 0, 0, 100))
    return img.filter(ImageFilter.GaussianBlur(blur))


def _plan(w=1200, h=800, lignes=10, fond=(255, 255, 255)):
    """Dessin technique : fond blanc majoritaire, traits noirs fins, cotes."""
    import random

    from PIL import Image, ImageDraw

    r = random.Random(lignes)
    img = Image.new("RGB", (w, h), fond)
    d = ImageDraw.Draw(img)
    for _ in range(lignes):
        x, y = r.randrange(w), r.randrange(h)
        d.line([x, y, x + r.randrange(-w // 3, w // 3), y], fill="black", width=1)
        d.line([x, y, x, y + r.randrange(-h // 3, h // 3)], fill="black", width=1)
    d.text((20, 20), "COUPE A-A 1:50  NGF +12.50", fill="black")
    return img


def _pdf(images) -> bytes:
    """PDF PyMuPDF, une page par entrée : ``[(stream, mask_or_None, rect), ...]`` par page."""
    doc = fitz.open()
    for page_images in images:
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), "Page de texte natif.", fontsize=11)
        for stream, mask, rect in page_images:
            page.insert_image(fitz.Rect(*rect), stream=stream, mask=mask)
    return doc.tobytes()


def _raw_masked_pdf(*, base_w, base_h, base_cs, base_samples, mask_w, mask_h, mask_samples,
                    rect=(60, 100, 400, 400)) -> bytes:
    """PDF forgé objet par objet : base et ``/SMask`` de définitions LIBRES (cas xref 52/59)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    mx = doc.get_new_xref()
    doc.update_object(mx, f"<</Type/XObject/Subtype/Image/Width {mask_w}/Height {mask_h}"
                          "/ColorSpace/DeviceGray/BitsPerComponent 8>>")
    doc.update_stream(mx, mask_samples)
    bx = doc.get_new_xref()
    doc.update_object(bx, f"<</Type/XObject/Subtype/Image/Width {base_w}/Height {base_h}"
                          f"/ColorSpace {base_cs}/BitsPerComponent 8/SMask {mx} 0 R>>")
    doc.update_stream(bx, base_samples)
    page.insert_image(fitz.Rect(*rect), xref=bx)
    return doc.tobytes()


def _legacy_hash(data: bytes, xref: int) -> str:
    """Empreinte du chemin HISTORIQUE, recalculée indépendamment du nouveau code."""
    from PIL import Image

    with fitz.open(stream=data, filetype="pdf") as doc:
        raw = doc.extract_image(xref)["image"]
    img = Image.open(io.BytesIO(raw))
    img.load()
    return hashlib.sha256(normalize_rgb_png(pdf_visual._fit_pixels(img, 2_000_000))[0]).hexdigest()


def _images(data: bytes):
    with fitz.open(stream=data, filetype="pdf") as doc:
        return [info[:4] for page in doc for info in page.get_images(full=True)]


def _png_image(asset):
    from PIL import Image

    return Image.open(io.BytesIO(asset.content)).convert("RGB")


PHOTO_PNG = _encode(_photo())
PHOTO_JPEG = _encode(_photo(seed=2), "JPEG", quality=90)
DISC = _encode(_disc_mask(320, 240))


# --------------------------------------------------------------------------- #
# §4 / §8 : barrière d'identité — aucune image sans masque ne change
# --------------------------------------------------------------------------- #
class TestBarriereIdentiteSansMasque:
    DATA = _pdf([[(PHOTO_PNG, None, (60, 100, 380, 340))],
                 [(PHOTO_JPEG, None, (60, 100, 380, 340))]])

    def test_png_et_jpeg_gardent_leur_empreinte_historique(self):
        refs = _images(self.DATA)
        assert all(smask == 0 for _, smask, _, _ in refs), "fixture : aucun masque"
        d = discover_pdf_visual_assets(self.DATA)
        assert [a.content_hash for a in d.assets] == [_legacy_hash(self.DATA, x) for x, *_ in refs]
        assert d.stats["omitted_uniform"] == 0

    def test_aucun_decodage_mupdf_pour_une_image_sans_masque(self, monkeypatch):
        """Le décodeur MuPDF n'est JAMAIS sollicité hors masque : l'identité ne peut dériver."""
        def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
            raise AssertionError("Pixmap interdit pour une image sans masque")

        monkeypatch.setattr(fitz, "Pixmap", _interdit)
        assert discover_pdf_visual_assets(self.DATA).stats["admitted"] == 2

    def test_la_barriere_est_significative_pour_le_jpeg(self):
        """Le décodeur MuPDF rend d'autres pixels JPEG : la barrière protège réellement."""
        from PIL import Image

        xref = _images(self.DATA)[1][0]
        with fitz.open(stream=self.DATA, filetype="pdf") as doc:
            pix = fitz.Pixmap(doc, xref)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        mupdf = hashlib.sha256(normalize_rgb_png(img)[0]).hexdigest()
        if mupdf == _legacy_hash(self.DATA, xref):  # pragma: no cover - dépend des décodeurs
            pytest.skip("décodeurs JPEG identiques dans cet environnement")
        assert discover_pdf_visual_assets(self.DATA).assets[1].content_hash != mupdf


# --------------------------------------------------------------------------- #
# §5-§6 / §15-§17 : détection et recomposition
# --------------------------------------------------------------------------- #
class TestRecomposition:
    def test_le_smask_est_detecte_par_get_images(self):
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])
        (xref, smask, w, h), = _images(data)
        assert smask > 0 and (w, h) == (320, 240)
        (cand,), _pages = pdf_visual._collect(fitz.open(stream=data, filetype="pdf"),
                                             stats=pdf_visual._new_stats(), max_pages=0)
        assert (cand.xref, cand.smask, cand.width, cand.height) == (xref, smask, 320, 240)

    def test_photo_detouree_recomposee_sur_blanc(self):
        """Cas xref 284/287 : hier sur fond noir, désormais détourée sur blanc."""
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1 and d.stats["omitted_uniform"] == 0
        img = _png_image(d.assets[0])
        assert img.getpixel((1, 1)) == (255, 255, 255), "coin transparent -> blanc"
        assert img.getpixel((160, 120)) != (255, 255, 255), "l'objet reste visible"
        assert d.assets[0].content_hash != _legacy_hash(data, _images(data)[0][0]), \
            "correction voulue : l'empreinte porte désormais les pixels affichés"

    def test_png_rgba_recompose(self):
        photo = _photo().convert("RGBA")
        photo.putalpha(_disc_mask(320, 240))
        data = _pdf([[(_encode(photo), None, (60, 100, 380, 340))]])
        assert _images(data)[0][1] > 0, "PyMuPDF porte l'alpha en /SMask"
        img = _png_image(discover_pdf_visual_assets(data).assets[0])
        assert img.getpixel((1, 1)) == (255, 255, 255)

    @pytest.mark.parametrize("mode", ["CMYK", "L"])
    def test_espaces_cmyk_et_gris(self, mode):
        data = _pdf([[(_encode(_photo().convert(mode), "JPEG", quality=90), DISC,
                       (60, 100, 380, 340))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1
        img = _png_image(d.assets[0])
        assert img.getpixel((1, 1)) == (255, 255, 255)

    def test_jpx_masque(self):
        from PIL import features

        if not features.check("jpg_2000"):  # pragma: no cover - dépend de Pillow
            pytest.skip("Pillow sans JPEG 2000")
        data = _pdf([[(_encode(_photo(), "JPEG2000"), DISC, (60, 100, 380, 340))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1
        assert _png_image(d.assets[0]).getpixel((1, 1)) == (255, 255, 255)

    def test_image_indexee_masquee(self):
        w, h = 200, 150
        samples = bytes((x // 20) % 2 for y in range(h) for x in range(w))
        mask = _disc_mask(w, h).tobytes()
        data = _raw_masked_pdf(base_w=w, base_h=h, base_cs="[/Indexed/DeviceRGB 1<FF0000003399>]",
                               base_samples=samples, mask_w=w, mask_h=h, mask_samples=mask)
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1, d.stats
        img = _png_image(d.assets[0])
        assert img.getpixel((0, 0)) == (255, 255, 255)
        assert img.getpixel((w // 2, h // 2)) in {(255, 0, 0), (0, 51, 153)}

    def test_masque_dune_autre_definition_reporte_sur_la_base(self):
        """Base 300×200, masque 150×100 : le PDF affiche les deux sur le même rectangle."""
        from PIL import Image

        base = _photo(300, 200)
        data = _raw_masked_pdf(base_w=300, base_h=200, base_cs="/DeviceRGB",
                               base_samples=base.tobytes(), mask_w=150, mask_h=100,
                               mask_samples=_disc_mask(150, 100).tobytes())
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1 and d.stats["omitted_unreadable"] == 0
        img = _png_image(d.assets[0])
        assert img.size == (300, 200), "la définition est celle de l'image principale"
        assert img.getpixel((1, 1)) == (255, 255, 255)
        assert img.getpixel((150, 100)) == Image.Image.getpixel(base, (150, 100))

    def test_determinisme_de_la_recomposition(self):
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])
        runs = [discover_pdf_visual_assets(data).assets[0] for _ in range(4)]
        assert len({a.content for a in runs}) == 1
        assert len({a.content_hash for a in runs}) == 1

    def test_aucun_rendu_de_page_meme_avec_masque(self, monkeypatch):
        def _interdit(*a, **k):  # pragma: no cover
            raise AssertionError("rendu de page interdit pendant la découverte")

        monkeypatch.setattr(fitz.Page, "get_pixmap", _interdit)
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])
        assert discover_pdf_visual_assets(data).stats["admitted"] == 1


# --------------------------------------------------------------------------- #
# §7 / §18 / §34-§35 : dimensions incompatibles et repli
# --------------------------------------------------------------------------- #
class TestRepli:
    def test_minuscule_image_indexee_a_masque_geant_reste_trop_petite(self, monkeypatch):
        """Cas xref 52/59 : base 2×2, masque 74×120. Conclu sur les dimensions DÉCLARÉES."""
        def _interdit(*a, **k):  # pragma: no cover
            raise AssertionError("aucun décodage pour une image trop petite")

        data = _raw_masked_pdf(base_w=2, base_h=2, base_cs="[/Indexed/DeviceRGB 1<484F59FFFFFF>]",
                               base_samples=bytes([0, 1, 1, 0]), mask_w=74, mask_h=120,
                               mask_samples=bytes(74 * 120), rect=(60, 100, 134, 220))
        monkeypatch.setattr(fitz, "Pixmap", _interdit)
        d = discover_pdf_visual_assets(data)
        assert d.stats["omitted_too_small"] == 1
        assert d.stats["omitted_unreadable"] == 0 and d.stats["omitted_uniform"] == 0
        assert d.assets == []

    def test_dimensions_du_masque_jamais_prises_pour_celles_de_limage(self):
        """Masque 400×300 sur une base 60×40 : la base reste trop petite."""
        data = _raw_masked_pdf(base_w=60, base_h=40, base_cs="/DeviceRGB",
                               base_samples=_photo(60, 40).tobytes(), mask_w=400, mask_h=300,
                               mask_samples=_disc_mask(400, 300).tobytes())
        d = discover_pdf_visual_assets(data)
        assert d.stats["omitted_too_small"] == 1 and d.assets == []

    def test_recomposition_refusee_retombe_sur_le_chemin_historique(self, monkeypatch, caplog):
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])
        xref, smask, _, _ = _images(data)[0]
        vrai = fitz.Pixmap

        def _refuse(*args, **kwargs):
            if len(args) == 2 and isinstance(args[1], int) and args[1] == smask:
                raise RuntimeError("masque illisible")
            return vrai(*args, **kwargs)

        monkeypatch.setattr(fitz, "Pixmap", _refuse)
        with caplog.at_level(logging.WARNING, logger=pdf_visual.logger.name):
            d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1, "jamais d'échec du document"
        assert d.assets[0].content_hash == _legacy_hash(data, xref), "repli = octets historiques"
        messages = [r.getMessage() for r in caplog.records]
        assert any("smask_recompose_failed" in m and f"xref={xref}" in m for m in messages)
        assert not any("iVBOR" in m or "\\x" in m for m in messages), "aucune donnée visuelle"

    def test_exception_mupdf_native_captee(self, monkeypatch):
        erreur = getattr(getattr(fitz, "mupdf", None), "FzErrorBase", None)
        if not isinstance(erreur, type):  # pragma: no cover - implémentation classique
            pytest.skip("PyMuPDF sans classe d'erreur MuPDF native")
        assert erreur in pdf_visual._mupdf_errors(fitz)
        assert RuntimeError in pdf_visual._mupdf_errors(fitz)

    def test_erreur_inattendue_non_masquee_par_le_repli(self, monkeypatch):
        """Seules les erreurs MuPDF attendues déclenchent le repli : pas de `except Exception`."""
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))]])

        def _bug(*a, **k):
            raise ZeroDivisionError("bug")

        monkeypatch.setattr(fitz, "Pixmap", _bug)
        with pytest.raises(ZeroDivisionError):
            pdf_visual._soft_masked_image(fitz, fitz.open(stream=data, filetype="pdf"),
                                          pdf_visual._Candidate(page=1, xref=1, bbox=None,
                                                                occurrences=[], smask=2,
                                                                width=320, height=240),
                                          max_pixels=2_000_000)


# --------------------------------------------------------------------------- #
# §10-§13 / §30-§31 : aplats et faux positifs
# --------------------------------------------------------------------------- #
class TestAplats:
    def test_ombre_portee_rejetee(self):
        """Cas xref 13/72 : noir sans le masque, gris uniforme avec. Rejet dans les deux cas."""
        data = _pdf([[(_encode(_shadow()), None, (40, 100, 540, 440))]])
        assert _images(data)[0][1] > 0
        d = discover_pdf_visual_assets(data)
        assert d.assets == [] and d.stats["omitted_uniform"] == 1

    def test_element_invisible_rejete(self):
        """Cas xref 15 : masque entièrement nul -> blanc."""
        from PIL import Image

        data = _pdf([[(_encode(Image.new("RGBA", (500, 340), (0, 0, 0, 0))), None,
                       (40, 100, 540, 440))]])
        d = discover_pdf_visual_assets(data)
        assert d.assets == [] and d.stats["omitted_uniform"] == 1

    def test_ombre_a_bords_francs_rejetee(self):
        """Cas page 30 : bandeau semi-transparent sans flou."""
        data = _pdf([[(_encode(_shadow(640, 160, blur=0)), None, (40, 600, 560, 730))]])
        assert discover_pdf_visual_assets(data).stats["omitted_uniform"] == 1

    def test_aplat_opaque_sans_masque_rejete(self):
        from PIL import Image

        data = _pdf([[(_encode(Image.new("RGB", (300, 200), (0, 0, 0))), None, (60, 100, 360, 300))]])
        d = discover_pdf_visual_assets(data)
        assert d.assets == [] and d.stats["omitted_uniform"] == 1

    def test_filtre_applique_apres_recomposition(self):
        """Silhouette noire dont TOUT le contenu est dans le masque : noire sans lui, gardée avec."""
        from PIL import Image, ImageDraw

        sil = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        d = ImageDraw.Draw(sil)
        for k in range(8):
            d.polygon([(20 + k * 45, 280), (45 + k * 45, 40 + k * 10), (70 + k * 45, 280)],
                      fill=(0, 0, 0, 255))
        data = _pdf([[(_encode(sil), None, (60, 100, 460, 400))]])
        disc = discover_pdf_visual_assets(data)
        assert disc.stats["admitted"] == 1 and disc.stats["omitted_uniform"] == 0

    @pytest.mark.parametrize("lignes", [3, 10, 80])
    def test_plan_technique_admis(self, lignes):
        data = _pdf([[(_encode(_plan(lignes=lignes)), None, (40, 100, 560, 450))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1 and d.stats["omitted_uniform"] == 0

    def test_plan_a_fond_transparent_admis(self):
        plan = _plan(900, 600, lignes=12).convert("RGBA")
        alpha = plan.convert("L").point(lambda v: 255 - v)
        plan.putalpha(alpha)
        data = _pdf([[(_encode(plan), None, (40, 100, 560, 450))]])
        assert _images(data)[0][1] > 0
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 1 and d.stats["omitted_uniform"] == 0

    def test_cadre_et_cartouche_admis(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1000, 700), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([10, 10, 990, 690], outline="black")
        d.rectangle([700, 580, 990, 690], outline="black")
        d.text((710, 600), "PLANCHE 12", fill="black")
        data = _pdf([[(_encode(img), None, (40, 100, 560, 450))]])
        assert discover_pdf_visual_assets(data).stats["admitted"] == 1

    def test_image_quasi_uniforme_mais_legitime_admise(self):
        from PIL import Image

        degrade = Image.linear_gradient("L").resize((400, 300)).convert("RGB")
        bruit = Image.effect_noise((400, 300), 6).convert("RGB")
        data = _pdf([[(_encode(degrade), None, (40, 100, 300, 300)),
                      (_encode(bruit), None, (320, 100, 560, 300))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 2 and d.stats["omitted_uniform"] == 0

    def test_logo_peu_colore_non_cible(self):
        """Aucun détecteur de logo : un logo à peu de couleurs reste un visuel ordinaire."""
        from PIL import Image, ImageDraw

        logo = Image.new("RGB", (300, 110), "white")
        d = ImageDraw.Draw(logo)
        d.rectangle([5, 5, 100, 100], fill=(250, 200, 0))
        d.text((110, 45), "ACME GROUP", fill=(0, 60, 140))
        data = _pdf([[(_encode(logo), None, (60, 700, 260, 773))]])
        assert discover_pdf_visual_assets(data).stats["admitted"] == 1

    def test_mesure_impossible_ne_fait_pas_echouer(self, monkeypatch):
        def _illisible(img):
            raise ValueError("mode non mesurable")

        monkeypatch.setattr(pdf_visual, "_is_uniform", _illisible)
        d = discover_pdf_visual_assets(_pdf([[(PHOTO_PNG, None, (60, 100, 380, 340))]]))
        assert d.stats["admitted"] == 1 and d.stats["omitted_uniform"] == 0

    def test_compteur_additif_et_entier(self):
        d = discover_pdf_visual_assets(_pdf([[(PHOTO_PNG, None, (60, 100, 380, 340))]]))
        attendus = {"pdf_opens", "pages_scanned", "images_seen", "admitted", "omitted_too_small",
                    "omitted_unreadable", "omitted_too_large", "omitted_table_overlap",
                    "omitted_decorative", "omitted_limit", "omitted_uniform",
                    "omitted_visual_duplicate", "candidates_eligible", "selection"}
        assert set(d.stats) == attendus
        assert d.stats["omitted_uniform"] == 0
        assert all(isinstance(v, int) for k, v in d.stats.items() if k != "selection")


# --------------------------------------------------------------------------- #
# §19 / §22-§25 : ordre, sélection, ordinal, provenance
# --------------------------------------------------------------------------- #
class TestSelectionEtProvenance:
    PAGE = [(_encode(_shadow()), None, (40, 40, 300, 200)),
            (PHOTO_JPEG, DISC, (40, 220, 300, 420)),
            (PHOTO_PNG, None, (320, 220, 560, 420))]

    def test_ordinal_rang_dadmission_sans_les_aplats(self):
        d = discover_pdf_visual_assets(_pdf([self.PAGE]))
        assert d.stats["omitted_uniform"] == 1
        assert [a.ordinal for a in d.assets] == [0, 1]
        refs = _images(_pdf([self.PAGE]))
        assert [a.source_locator["xref"] for a in d.assets] == [refs[1][0], refs[2][0]]

    def test_aplat_ne_compte_pas_dans_le_vivier(self):
        """3 éligibles pour 3 places : l'aplat écarté ne déclenche pas la sélection bornée."""
        data = _pdf([self.PAGE, [(_encode(_photo(seed=5)), None, (60, 100, 380, 340))]])
        d = discover_pdf_visual_assets(data, limits=PdfVisualLimits(max_assets=3))
        assert [a.source_page for a in d.assets] == [1, 1, 2], "ordre de découverte conservé"
        assert [a.ordinal for a in d.assets] == [0, 1, 2]
        assert d.stats["omitted_limit"] == 0 and d.stats["omitted_uniform"] == 1
        assert d.stats["candidates_eligible"] == 3
        assert d.stats["selection"]["strategy"] == "discovery_order"

    def test_provenance_de_limage_principale(self):
        data = _pdf([[(PHOTO_JPEG, DISC, (60, 100, 380, 340))],
                     [(PHOTO_JPEG, DISC, (80, 120, 400, 360))]])
        (xref, smask, _, _), _second = _images(data)
        a, = discover_pdf_visual_assets(data).assets
        assert a.source_locator == {"type": "pdf", "page": 1, "xref": xref}
        assert a.source_locator["xref"] != smask, "le masque n'est jamais une source"
        assert a.source_page == 1
        assert a.source_bbox == pytest.approx((60.0, 100.0, 380.0, 340.0))
        assert [(o.source_page, o.source_bbox) for o in a.occurrences] == [
            (2, pytest.approx((80.0, 120.0, 400.0, 360.0)))]

    def test_budget_inchange(self):
        lim = PdfVisualLimits()
        assert (lim.max_assets, lim.max_per_page) == (12, 4)
        from apps.rag_core.services.ingest.extraction import config

        assert config.pdf_visual_max_assets() == 12
        assert config.pdf_visual_max_assets_per_page() == 4

    def test_plafond_par_page_inchange(self):
        page = [(_encode(_photo(seed=s)), None, (40 + 90 * (s % 6), 100, 120 + 90 * (s % 6), 180))
                for s in range(6)]
        d = discover_pdf_visual_assets(_pdf([page]))
        assert d.stats["admitted"] == 4 and d.stats["omitted_limit"] == 2

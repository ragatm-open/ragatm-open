# tests/tests_root/rag_core/test_ocr_optional_confidence.py
"""
OCR-CONFIDENCE-COST-02 — second passage Tesseract (`image_to_data`) optionnel.

Le chemin OCR exécutait DEUX reconnaissances complètes sur la même image :
`image_to_string` produit le texte indexé, `image_to_data` ne sert qu'à la moyenne des
confiances. Mesuré sur le corpus terrain : 36,4 % du temps OCR médian, jusqu'à 48 %.

Ce que ces tests verrouillent :

- `DOCUMENT_OCR_COMPUTE_CONFIDENCE=0` n'appelle plus `image_to_data` sur le chemin texte ;
- le texte est **strictement identique** dans les deux modes ;
- aucune confiance n'est inventée — les champs sont absents, jamais 0 ni 100 ;
- `ocr_low_confidence` n'est émis **que** si une confiance a réellement été mesurée ;
- l'orientation (OSD) est décidée avant, et reste identique ;
- le second site d'appel, `image_to_words` (tableaux OCR), reste **indépendant** du réglage.

Les tests remplacent `pytesseract` lui-même, et non le provider : c'est la seule façon
d'exercer le vrai `_TesseractProvider`, donc le réglage.
"""
from __future__ import annotations

import sys
from io import BytesIO

import pytest

from apps.rag_core.services.ingest.extraction import config as xconfig
from apps.rag_core.services.ingest.extraction import ocr as ocr_mod
from apps.rag_core.services.ingest.extraction import extract_document

_TEXTE = "MASSIF M01 - note de chantier\nBeton C40/50, enrobage 5 cm."


class _FauxTesseract:
    """`pytesseract` factice qui COMPTE ses appels, pour prouver ce qui est exécuté."""

    class Output:
        DICT = "dict"

    def __init__(self, *, rotate=0, orientation_conf=0.0, conf_moyenne=87.0, texte=_TEXTE):
        self.appels = {"osd": 0, "string": 0, "data": 0}
        # Budget RÉELLEMENT transmis à chaque appel : c'est ce qui prouve que le budget
        # de page est total et non redémarré à chaque sous-appel.
        self.budgets = {"osd": None, "string": None, "data": None}
        self._rotate = rotate
        self._orientation_conf = orientation_conf
        self._conf = conf_moyenne
        self._texte = texte

    def image_to_osd(self, im, output_type=None, timeout=0):
        self.appels["osd"] += 1
        self.budgets["osd"] = timeout
        return {"rotate": self._rotate, "orientation_conf": self._orientation_conf}

    def image_to_string(self, im, lang=None, timeout=0):
        self.appels["string"] += 1
        self.budgets["string"] = timeout
        return self._texte

    def image_to_data(self, im, lang=None, output_type=None, timeout=0):
        self.appels["data"] += 1
        self.budgets["data"] = timeout
        return {
            "conf": ["-1", str(self._conf), str(self._conf)],
            "text": ["", "MASSIF", "M01"],
            "block_num": [0, 1, 1], "par_num": [0, 1, 1], "line_num": [0, 1, 1],
            "left": [0, 10, 60], "top": [0, 20, 20],
            "width": [0, 40, 30], "height": [0, 12, 12],
        }


@pytest.fixture
def faux(monkeypatch):
    def _poser(**kw):
        f = _FauxTesseract(**kw)
        monkeypatch.setitem(sys.modules, "pytesseract", f)
        return f
    return _poser


def _png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pdf_page_vide() -> bytes:
    """PDF réel d'une page sans texte : pypdf n'en extrait rien, l'OCR se déclenche."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------
# 1. Provider : le second passage est bien conditionnel
# ---------------------------------------------------------------------
def test_provider_mode_ON_appelle_image_to_data_et_rend_la_confiance(faux, settings):
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = True
    f = faux()
    texte, conf, rot = ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra+eng")

    assert f.appels["string"] == 1
    assert f.appels["data"] == 1
    assert conf == 87.0
    assert texte == _TEXTE


def test_provider_mode_OFF_n_appelle_pas_image_to_data(faux, settings):
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    f = faux()
    texte, conf, rot = ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra+eng")

    assert f.appels["string"] == 1
    assert f.appels["data"] == 0, "le second passage Tesseract ne doit plus être exécuté"
    assert conf is None, "aucune confiance ne doit être inventée"
    assert texte == _TEXTE


def test_texte_strictement_identique_entre_les_deux_modes(faux, settings):
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = True
    faux()
    t_on, _, _ = ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra+eng")
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux()
    t_off, _, _ = ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra+eng")

    assert t_on == t_off


# ---------------------------------------------------------------------
# 2. Orientation : décidée AVANT, donc invariante
# ---------------------------------------------------------------------
@pytest.mark.parametrize("calcul", [True, False])
def test_orientation_identique_dans_les_deux_modes(faux, settings, calcul):
    """Confiance OSD au-dessus de la porte : la rotation est réellement appliquée."""
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = calcul
    settings.DOCUMENT_OCR_AUTO_ROTATE = True
    f = faux(rotate=90, orientation_conf=50.0)
    _, _, rot = ocr_mod._TesseractProvider().ocr_image_bytes(_png(), languages="fra+eng")

    assert rot == 90
    assert f.appels["osd"] == 1


# ---------------------------------------------------------------------
# 3. Le second site d'appel (tableaux OCR) reste indépendant
# ---------------------------------------------------------------------
def test_image_to_words_reste_independant_du_reglage(faux, settings):
    """`image_to_words` sert la reconstruction de tableaux OCR : un autre chemin.

    Il doit continuer d'utiliser `image_to_data` même quand la confiance texte est éteinte,
    sans quoi éteindre un réglage de coût casserait silencieusement une autre fonctionnalité.
    """
    from PIL import Image

    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    f = faux()
    mots = ocr_mod._TesseractProvider().image_to_words(
        Image.new("RGB", (60, 40), "white"), languages="fra+eng"
    )

    assert f.appels["data"] == 1, "le chemin tableaux OCR ne doit pas dépendre du réglage"
    assert [m["text"] for m in mots] == ["MASSIF", "M01"]


# ---------------------------------------------------------------------
# 4. PDF : contrat de métadonnées
# ---------------------------------------------------------------------
def _extrait_pdf(settings, calcul, faux):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = calcul
    faux()
    return extract_document(_pdf_page_vide(), filename="plan.pdf",
                            content_type="application/pdf", ocr_enabled=True)


def test_pdf_mode_ON_publie_la_confiance(faux, settings):
    r = _extrait_pdf(settings, True, faux)
    assert r.metadata["ocr_used"] is True
    assert r.metadata["ocr_confidence"] == 87.0
    assert r.metadata["ocr_confidence_computed"] is True
    assert "1" in r.metadata["page_ocr_confidences"]


def test_pdf_mode_OFF_n_invente_aucune_confiance(faux, settings):
    r = _extrait_pdf(settings, False, faux)
    assert r.metadata["ocr_used"] is True
    # Absence, jamais 0 ni 100 ni une estimation.
    assert "ocr_confidence" not in r.metadata
    assert "page_ocr_confidences" not in r.metadata
    assert r.metadata["ocr_confidence_computed"] is False


def test_pdf_texte_et_sante_identiques_entre_les_modes(faux, settings):
    on = _extrait_pdf(settings, True, faux)
    off = _extrait_pdf(settings, False, faux)

    assert on.text == off.text
    for cle in ("ocr_used", "ocr_pages", "empty_pages", "text_pages",
                "text_quality_score", "text_quality_class", "pages", "page_rotations"):
        assert on.metadata.get(cle) == off.metadata.get(cle), f"{cle} a changé"
    assert on.status == off.status
    assert sorted(on.warnings) == sorted(off.warnings)


# ---------------------------------------------------------------------
# 5. Sémantique du warning `ocr_low_confidence`
# ---------------------------------------------------------------------
def test_warning_basse_confiance_emis_en_ON(faux, settings):
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = True
    faux(conf_moyenne=12.0)
    r = extract_document(_pdf_page_vide(), filename="plan.pdf",
                         content_type="application/pdf", ocr_enabled=True)
    assert "ocr_low_confidence" in r.warnings


def test_warning_basse_confiance_absent_en_OFF(faux, settings):
    """On ne dit pas « confiance basse » quand aucune confiance n'a été mesurée."""
    settings.DOCUMENT_OCR_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = False
    faux(conf_moyenne=12.0)
    r = extract_document(_pdf_page_vide(), filename="plan.pdf",
                         content_type="application/pdf", ocr_enabled=True)
    assert "ocr_low_confidence" not in r.warnings


# ---------------------------------------------------------------------
# 6. Image autonome : même contrat que le PDF
# ---------------------------------------------------------------------
def _extrait_image(settings, calcul, faux):
    settings.DOCUMENT_IMAGE_INGEST_ENABLED = True
    settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE = calcul
    faux()
    return extract_document(_png(), filename="plan.png", content_type="image/png")


def test_image_mode_ON_publie_la_confiance(faux, settings):
    r = _extrait_image(settings, True, faux)
    assert r.metadata["ocr_confidence"] == 87.0
    assert r.metadata["ocr_confidence_computed"] is True


def test_image_mode_OFF_n_invente_aucune_confiance(faux, settings):
    r = _extrait_image(settings, False, faux)
    assert "ocr_confidence" not in r.metadata
    assert r.metadata["ocr_confidence_computed"] is False


def test_image_texte_identique_entre_les_modes(faux, settings):
    on = _extrait_image(settings, True, faux)
    off = _extrait_image(settings, False, faux)
    assert on.text == off.text
    assert on.metadata.get("text_quality_class") == off.metadata.get("text_quality_class")


# ---------------------------------------------------------------------
# 7. Contrat de configuration
# ---------------------------------------------------------------------
def test_defaut_du_reglage_est_true(settings):
    """Compatibilité ascendante : le premier commit ne change rien sans action explicite."""
    if hasattr(settings, "DOCUMENT_OCR_COMPUTE_CONFIDENCE"):
        del settings.DOCUMENT_OCR_COMPUTE_CONFIDENCE
    assert xconfig.ocr_compute_confidence() is True

"""Tests de la qualité d'extraction PDF et de la chaîne de fallback — P1.9-e.

⚠️ Fixtures 100 % SYNTHÉTIQUES : aucun contenu, nom de fichier ou valeur issus d'un corpus
client réel ne doit apparaître ici.

Le pipeline mesurait l'utilité d'un texte par sa LONGUEUR. Un PDF dont les polices
n'exposent pas de table ToUnicode produit des milliers de caractères de sélecteurs de
glyphes : long, mais vide de sens. Le document passait « extrait avec succès », le fallback
et l'OCR ne se déclenchaient jamais, et l'index se remplissait de chunks morts vectorisés.
Ces tests figent la règle : **long ≠ exploitable**.
"""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.services.ingest import extractors
from apps.rag_core.services.ingest.extraction import (
    ExtractionError,
    ExtractionStatus,
    extract_document,
)
from apps.rag_core.services.ingest.extraction import text_quality as tq

# --- Fixtures synthétiques -------------------------------------------------
TEXTE_NORMAL = (
    "Le présent document décrit le paramétrage des structures de référence. "
    "Les données sont alimentées chaque trimestre par le référentiel central. "
    "Toute modification fait l'objet d'une validation par le service concerné."
)
# Sélecteurs de glyphes : ce que produit un extracteur sur une police sans ToUnicode.
GLYPHES = "/1 /44 /42 /44 /10 /49 □ /8 /9 /23 /52 /5 /27 /5 /6 /52\n/24 /46 /6 /5 /5 /22 □ /10"
# Marqueurs pdfminer pour glyphes non mappables.
CIDS = "(cid:28)(cid:6)(cid:29)(cid:5)(cid:30)(cid:29)(cid:31)(cid:5)(cid:31)(cid:30)(cid:18)" * 4
# Mots sans voyelle : ce que donne un mapping de police faux (lettres arbitraires).
FAUX_MOTS = "ABCD EDABF ABCD EBFGD DBG LBN AKD DDAH DFQ BTF BCD FDLF DBG LBN AKD DDAH"
# Tableau financier : massivement chiffré mais parfaitement lisible.
TABLEAU_CHIFFRE = (
    "Encours par échéance au 31/12/2026\n"
    "2026 : 1 240 500 ; 2027 : 980 300 ; 2028 : 1 105 750 ; 2029 : 640 200\n"
    "Total consolidé hors filiales : 3 966 750 euros pour le périmètre retenu."
)
# Faux positif observé sur le terrain : une liste de codes départementaux.
LISTE_DEPARTEMENTS = (
    "Pour la filière concernée, périmètre CS (fédérations 11/18/44/45/47/49/50/51/52/69/70, "
    "la fédération 94 étant hors scope depuis octobre) il faut établir un barème de taux "
    "et un index associés pour chaque entité listée dans le tableau précédent."
)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def test_texte_normal_est_exploitable():
    assert tq.classify_text(TEXTE_NORMAL) == tq.USABLE
    assert tq.is_unusable_pdf_text(TEXTE_NORMAL) is False
    assert tq.extraction_quality_score(TEXTE_NORMAL) > 0.8


def test_glyphes_detectes_et_distingues_de_l_ocr():
    """La cause doit être nommée : réextraire ≠ OCRiser ≠ réviser."""
    assert tq.classify_text(GLYPHES) == tq.GLYPH
    assert tq.is_glyph_garbage(GLYPHES) is True
    assert tq.is_unusable_pdf_text(GLYPHES) is True


def test_marqueurs_cid_detectes():
    assert tq.classify_text(CIDS) == tq.CID
    assert tq.is_glyph_garbage(CIDS) is True


def test_mots_sans_voyelle_sont_degrades():
    """Un mapping de police faux produit des lettres, pas de la langue."""
    assert tq.classify_text(FAUX_MOTS) == tq.DEGRADED
    assert tq.is_unusable_pdf_text(FAUX_MOTS) is True


def test_texte_vide_ou_trop_court():
    assert tq.classify_text("") == tq.EMPTY
    assert tq.classify_text("   \n  ") == tq.EMPTY
    assert tq.classify_text("Page 3") == tq.EMPTY


def test_long_mais_inexploitable_ne_compte_pour_rien():
    """Le cœur du correctif : la longueur ne vaut pas l'utilité."""
    long_glyphes = GLYPHES * 40
    assert len(long_glyphes) > 2000          # plus long que bien des vraies pages
    assert tq.usable_chars(long_glyphes) == 0
    assert tq.usable_chars(TEXTE_NORMAL) == len(TEXTE_NORMAL.strip())


# --------------------------------------------------------------------------
# Non-régression : ce qui doit RESTER accepté
# --------------------------------------------------------------------------
def test_tableau_chiffre_reste_exploitable():
    """Un faible taux de lettres décrit un contenu chiffré, pas un texte cassé."""
    m = tq.text_metrics(TABLEAU_CHIFFRE)
    assert m.alpha_ratio < 0.6               # massivement chiffré…
    assert m.vowel_word_ratio > 0.9          # …mais bien de la langue
    assert tq.is_unusable_pdf_text(TABLEAU_CHIFFRE) is False


def test_liste_de_nombres_slashes_nest_pas_du_glyphe():
    """Faux positif d'une regex unique : « 11/18/44/45 » dans une phrase réelle.

    C'est arrivé sur le corpus terrain : un document sain a été compté comme illisible.
    D'où la double condition (sélecteurs ET absence de voyelles).
    """
    assert tq.classify_text(LISTE_DEPARTEMENTS) == tq.USABLE
    assert tq.is_glyph_garbage(LISTE_DEPARTEMENTS) is False


def test_note_courte_reelle_reste_acceptee():
    """Le plancher de mots ne doit pas condamner une note brève légitime."""
    court = "Massif M01 — note courte de chantier."
    assert tq.is_unusable_pdf_text(court) is False


# --------------------------------------------------------------------------
# Métriques
# --------------------------------------------------------------------------
def test_metrics_ne_comptent_pas_cid_comme_un_mot():
    """Piège rencontré : « cid » compté comme mot faisait passer un texte mort
    pour riche de 32 000 mots."""
    m = tq.text_metrics(CIDS)
    assert m.n_words == 0
    assert m.vowel_word_ratio == 0.0


def test_score_est_borne():
    for t in ("", GLYPHES, TEXTE_NORMAL, CIDS, FAUX_MOTS):
        assert 0.0 <= tq.extraction_quality_score(t) <= 1.0


# --------------------------------------------------------------------------
# Chaîne de fallback (§4)
# --------------------------------------------------------------------------
def _pdf(pages: list[list[str]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(72, y, line)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


pytestmark = pytest.mark.django_db


def test_pdf_normal_naccepte_aucun_fallback(monkeypatch):
    data = _pdf([[TEXTE_NORMAL]])
    res = extract_document(data, filename="note.pdf", content_type="application/pdf")
    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["pdf_native_fallback_used"] is False
    assert res.metadata["ocr_used"] is False


def test_texte_glyphique_declenche_le_second_extracteur(monkeypatch):
    """pypdf rend des glyphes : le fallback natif DOIT être tenté (il ne l'était pas)."""
    data = _pdf([[TEXTE_NORMAL]])
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, GLYPHES * 30)])
    monkeypatch.setitem(extractors._FALLBACK_ENGINES, "pymupdf",
                        lambda raw, max_pages=None: [(1, TEXTE_NORMAL)])
    res = extract_document(data, filename="glyph.pdf", content_type="application/pdf")
    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["pdf_native_fallback_used"] is True
    assert res.metadata["pdf_native_engine"] == "pymupdf"
    assert TEXTE_NORMAL.split()[0] in res.text


def test_glyphes_partout_et_ocr_desactive_echec_explicite(monkeypatch):
    """Aucun moteur natif ne peut lire : motif dédié, distinct du scan."""
    data = _pdf([[TEXTE_NORMAL]])
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, GLYPHES * 30), (2, GLYPHES * 30)])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})
    with pytest.raises(ExtractionError) as exc:
        extract_document(data, filename="glyph.pdf", content_type="application/pdf",
                         ocr_enabled=False)
    assert exc.value.code == "pdf_text_unrecoverable"
    assert "glyph_garbage" in exc.value.meta["text_quality_classes"]
    # Le diagnostic conserve ce que le moteur a produit ; la décision utilise l'exploitable.
    assert exc.value.meta["native_text_chars"] > 0
    assert exc.value.meta["native_usable_chars"] == 0


def test_ocr_recupere_le_texte_des_pages_glyphiques(monkeypatch):
    """Le fallback OCR se déclenche sur une page LONGUE mais inexploitable."""
    data = _pdf([[TEXTE_NORMAL]])
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, GLYPHES * 30)])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    from apps.rag_core.services.ingest.extraction import ocr as ocr_mod

    class _Page:
        def __init__(self):
            self.page_number, self.text, self.rotation, self.confidence = 1, TEXTE_NORMAL, 0, 92.0

    class _Res:
        used, text, avg_confidence, partial, warnings = True, TEXTE_NORMAL, 92.0, False, []
        pages = [_Page()]
        # Contrat `OcrResult` (OCR-TIMEOUT-HARDENING-02) : pages expirées vs en erreur.
        timeout_pages, failed_pages = [], []

    monkeypatch.setattr(ocr_mod, "ocr_pdf_pages", lambda data, pages, **kw: _Res())
    res = extract_document(data, filename="glyph.pdf", content_type="application/pdf",
                           ocr_enabled=True)
    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["ocr_used"] is True
    assert TEXTE_NORMAL.split()[0] in res.text


def test_echec_total_ne_produit_aucun_chunk_ni_embedding(monkeypatch):
    """Mieux vaut un document explicitement en échec que 100 chunks morts (§8)."""
    data = _pdf([[TEXTE_NORMAL]])
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, GLYPHES * 30)])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    from apps.rag_core.services.ingest.extraction import ocr as ocr_mod

    class _Res:
        used, text, avg_confidence, partial, warnings = False, "", None, True, []
        pages = []
        timeout_pages, failed_pages = [], []

    monkeypatch.setattr(ocr_mod, "ocr_pdf_pages", lambda data, pages, **kw: _Res())
    with pytest.raises(ExtractionError) as exc:
        extract_document(data, filename="glyph.pdf", content_type="application/pdf",
                         ocr_enabled=True)
    assert exc.value.code in ("ocr_no_text", "pdf_text_unrecoverable")


def test_pages_illisibles_residuelles_sont_ecartees_de_l_index(monkeypatch):
    """OCR partiel : les pages restées glyphiques ne doivent pas être indexées."""
    data = _pdf([[TEXTE_NORMAL]])
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, TEXTE_NORMAL), (2, GLYPHES * 30)])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})
    res = extract_document(data, filename="mixte.pdf", content_type="application/pdf",
                           ocr_enabled=False)
    # `partial` et non `success` : une partie du document n'a pas pu être lue. Le taire
    # sous un « success » masquerait la perte.
    assert res.status == ExtractionStatus.PARTIAL
    assert [pn for pn, _ in res.pages] == [1]            # la page 2 est écartée
    assert res.metadata["unusable_pages_dropped"] == [2]
    assert "/44" not in res.text
    assert res.partial is True

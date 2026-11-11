# tests/tests_root/rag_core/test_pdf_native_gate.py
"""
INGEST-NATIVE-CHAIN-02 — porte native + garde de qualité du sélecteur PDF.

Deux comportements sont verrouillés ici.

**Porte native.** Quand PyMuPDF a bien énuméré les pages d'un PDF mais n'y trouve aucun
caractère exploitable, les moteurs suivants (pdfminer, pdfplumber) ne sont plus tentés.
Mesuré sur 1947 PDF du corpus terrain : sur les 91 documents concernés, aucun moteur
suivant ne récupérait quoi que ce soit, pour 80 % du temps d'extraction.

**Garde de qualité.** Le sélecteur choisissait sur le seul nombre de caractères. Sur une
planche A0 réelle, pdfminer rendait 40 caractères classés `partial` — « Voie privée »
éclaté caractère par caractère — contre 32 caractères `usable` de pypdf, et gagnait.

Les cas limites qui font la sûreté de la porte sont testés explicitement : moteur
indisponible (liste de pages vide), ordre de configuration inversé, et unicité de
l'extraction PyMuPDF.
"""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.services.ingest import extractors
from apps.rag_core.services.ingest.extraction import pdf_extractor

# Textes dont la classe a été vérifiée contre `extraction.text_quality` :
#   _USABLE_COURT  -> usable,  50 caractères exploitables
#   _PARTIAL_LONG  -> partial, 56 caractères exploitables (plus riche, moins bon)
#   _USABLE_LONG   -> usable, 105 caractères exploitables
_USABLE_COURT = "Voie privee\n\nEmprise RDC projetee du batiment nord"
_PARTIAL_LONG = "A\nB\nC\nD\nE\nF\nG\nH\nI\nJ\nK\nL\nEmprise RDC projetee du batiment"
_USABLE_LONG = (
    "Note de calcul ferraillage des massifs de la verriere nord.\n"
    "Beton C40/50 et enrobage de cinq centimetres."
)


class _Moteur:
    """Moteur de secours instrumenté : compte ses appels et rend des pages fixées."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.appels = 0

    def __call__(self, raw, max_pages=None):
        self.appels += 1
        return list(self.pages)


def _brancher(monkeypatch, **moteurs):
    """Remplace les moteurs de secours nommés par des instances instrumentées."""
    for nom, moteur in moteurs.items():
        monkeypatch.setitem(extractors._FALLBACK_ENGINES, nom, moteur)
    return moteurs


def _pdf_sans_texte() -> bytes:
    """PDF réel d'une page vide : pypdf n'en extrait aucun caractère."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.showPage()
    c.save()
    return buf.getvalue()


def _selection(raw=b"%PDF-1.4 factice", *, pypdf_pages, engines, min_total_chars=200):
    return extractors.select_best_native_pdf_extraction(
        raw, pypdf_pages=pypdf_pages, engines=engines,
        max_pages=10, min_total_chars=min_total_chars,
    )


# ---------------------------------------------------------------------
# 1. Porte : PyMuPDF n'a rien trouvé -> les moteurs suivants sont sautés
# ---------------------------------------------------------------------
def test_gate_saute_les_moteurs_suivants_quand_pymupdf_ne_trouve_rien(monkeypatch):
    # Un document sans texte rend UNE ENTRÉE PAR PAGE, avec un texte vide.
    pymu = _Moteur([(1, ""), (2, "")])
    miner = _Moteur([(1, "du texte que personne ne doit lire ici")])
    plumber = _Moteur([(1, "idem")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner, pdfplumber=plumber)

    sel = _selection(pypdf_pages=[(1, ""), (2, "")],
                     engines=["pymupdf", "pdfminer", "pdfplumber"])

    assert sel.gate_applied is True
    assert sel.attempted_engines == ["pymupdf"]
    assert miner.appels == 0
    assert plumber.appels == 0
    # L'extraction de référence est conservée telle quelle.
    assert sel.engine == "pypdf"
    assert sel.fallback_used is False
    assert sel.chars_by_engine == {"pypdf": 0, "pymupdf": 0}


# ---------------------------------------------------------------------
# 2. Porte : PyMuPDF a trouvé du texte -> la chaîne se poursuit
# ---------------------------------------------------------------------
def test_gate_ne_declenche_pas_quand_pymupdf_trouve_du_texte(monkeypatch):
    pymu = _Moteur([(1, _USABLE_COURT)])
    miner = _Moteur([(1, "")])
    plumber = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner, pdfplumber=plumber)

    sel = _selection(pypdf_pages=[(1, "")],
                     engines=["pymupdf", "pdfminer", "pdfplumber"])

    assert sel.gate_applied is False
    assert sel.attempted_engines == ["pymupdf", "pdfminer", "pdfplumber"]
    assert miner.appels == 1 and plumber.appels == 1
    assert sel.engine == "pymupdf"
    assert sel.fallback_used is True


# ---------------------------------------------------------------------
# 3. SÛRETÉ : PyMuPDF indisponible -> la porte NE DOIT PAS déclencher
# ---------------------------------------------------------------------
def test_gate_ne_declenche_pas_quand_pymupdf_est_indisponible(monkeypatch):
    """Les moteurs sont import-gardés et rendent ``[]`` quand la bibliothèque manque.

    Sans distinction, un PyMuPDF absent ferait sauter pdfminer et pdfplumber SANS avoir
    rien lu : c'est exactement la perte de contenu que la porte doit interdire.
    """
    pymu = _Moteur([])                      # bibliothèque absente / PDF illisible
    miner = _Moteur([(1, _USABLE_LONG)])    # aurait récupéré le texte
    plumber = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner, pdfplumber=plumber)

    sel = _selection(pypdf_pages=[(1, "")],
                     engines=["pymupdf", "pdfminer", "pdfplumber"])

    assert sel.gate_applied is False
    assert miner.appels == 1, "pdfminer doit rester tenté quand PyMuPDF n'a rien lu"
    assert sel.engine == "pdfminer"
    assert sel.fallback_used is True


# ---------------------------------------------------------------------
# 4. Une seule extraction PyMuPDF par sélection et par document
# ---------------------------------------------------------------------
def test_une_seule_extraction_pymupdf_par_selection(monkeypatch):
    pymu = _Moteur([(1, _USABLE_COURT)])
    miner = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, "")], engines=["pymupdf", "pdfminer"])

    assert pymu.appels == 1, "la sonde et la boucle doivent partager le même résultat"
    assert sel.chars_by_engine["pymupdf"] > 0


def test_une_seule_extraction_pymupdf_meme_avec_l_alias_fitz(monkeypatch):
    """``fitz`` est un alias de ``pymupdf`` : citer les deux ne doit pas doubler l'appel."""
    pymu = _Moteur([(1, _USABLE_COURT)])
    _brancher(monkeypatch, pymupdf=pymu, fitz=pymu)

    _selection(pypdf_pages=[(1, "")], engines=["fitz", "pymupdf"])

    assert pymu.appels == 1


# ---------------------------------------------------------------------
# 5. Déterminisme : la porte ne dépend pas de l'ordre configuré
# ---------------------------------------------------------------------
@pytest.mark.parametrize("ordre", [
    ["pymupdf", "pdfminer", "pdfplumber"],
    ["pdfplumber", "pdfminer", "pymupdf"],
    ["pdfminer", "pymupdf", "pdfplumber"],
])
def test_gate_deterministe_quel_que_soit_l_ordre_des_moteurs(monkeypatch, ordre):
    pymu = _Moteur([(1, ""), (2, "")])
    miner = _Moteur([(1, "jamais lu")])
    plumber = _Moteur([(1, "jamais lu")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner, pdfplumber=plumber)

    sel = _selection(pypdf_pages=[(1, ""), (2, "")], engines=ordre)

    assert sel.gate_applied is True
    assert sel.attempted_engines == ["pymupdf"]
    assert miner.appels == 0 and plumber.appels == 0


def test_gate_ne_reintroduit_pas_un_moteur_retire_de_la_configuration(monkeypatch):
    """Sans PyMuPDF dans la liste, la porte ne s'applique pas : la config fait foi."""
    pymu = _Moteur([(1, ""), (2, "")])
    miner = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, "")], engines=["pdfminer"])

    assert sel.gate_applied is False
    assert pymu.appels == 0
    assert sel.attempted_engines == ["pdfminer"]


# ---------------------------------------------------------------------
# 6. Garde de qualité
# ---------------------------------------------------------------------
def test_garde_refuse_un_candidat_plus_riche_mais_de_pire_classe(monkeypatch):
    """Forme exacte de la sentinelle 42c9b9c8 : +8 caractères, classe dégradée."""
    miner = _Moteur([(1, _PARTIAL_LONG)])          # 56 caractères, partial
    _brancher(monkeypatch, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, _USABLE_COURT)],  # 50 caractères, usable
                     engines=["pdfminer"])

    assert sel.chars_by_engine["pdfminer"] > sel.chars_by_engine["pypdf"]
    assert sel.engine == "pypdf", "un texte fragmenté ne doit pas gagner sur le compte"
    assert sel.fallback_used is False


def test_garde_admet_un_candidat_plus_riche_de_meme_classe(monkeypatch):
    miner = _Moteur([(1, _USABLE_LONG)])           # 105 caractères, usable
    _brancher(monkeypatch, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, _USABLE_COURT)], engines=["pdfminer"])

    assert sel.engine == "pdfminer"
    assert sel.fallback_used is True


def test_garde_admet_un_candidat_plus_riche_et_de_meilleure_classe(monkeypatch):
    miner = _Moteur([(1, _USABLE_LONG)])           # 105, usable
    _brancher(monkeypatch, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, _PARTIAL_LONG)],  # 56, partial
                     engines=["pdfminer"])

    assert sel.engine == "pdfminer"


def test_garde_conserve_le_titulaire_a_nombre_egal(monkeypatch):
    miner = _Moteur([(1, _USABLE_COURT)])          # strictement le même compte
    _brancher(monkeypatch, pdfminer=miner)

    sel = _selection(pypdf_pages=[(1, _USABLE_COURT)], engines=["pdfminer"])

    assert sel.engine == "pypdf"
    assert sel.fallback_used is False


# ---------------------------------------------------------------------
# 7. Métadonnées : ce qui est préservé et ce qui change
# ---------------------------------------------------------------------
def test_metadonnees_de_la_porte(monkeypatch):
    pymu = _Moteur([(1, "")])
    miner = _Moteur([(1, "")])
    plumber = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner, pdfplumber=plumber)

    pages, info = pdf_extractor._native_pages(_pdf_sans_texte())

    assert info["pdf_native_gate_applied"] is True
    # Préservés à l'identique.
    assert info["pdf_native_engine"] == "pypdf"
    assert info["pdf_native_fallback_used"] is False
    # Seules différences autorisées : les deux inventaires de moteurs.
    assert info["pdf_native_fallback_attempted_engines"] == ["pymupdf"]
    assert set(info["pdf_native_chars_by_engine"]) == {"pypdf", "pymupdf"}


def test_metadonnees_sans_porte_inchangees(monkeypatch):
    pymu = _Moteur([(1, _USABLE_LONG)])
    _brancher(monkeypatch, pymupdf=pymu)

    pages, info = pdf_extractor._native_pages(_pdf_sans_texte())

    assert info["pdf_native_gate_applied"] is False
    assert info["pdf_native_fallback_used"] is True
    assert info["pdf_native_engine"] == "pymupdf"


# ---------------------------------------------------------------------
# 8. PDF mixte : la porte est au document, la décision OCR reste à la page
# ---------------------------------------------------------------------
def test_pdf_mixte_ne_declenche_pas_la_chaine_et_reste_page_par_page():
    """Page native, page vide, page native : pypdf suffit, aucun repli n'est tenté."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lignes in ([_USABLE_LONG, _USABLE_LONG], [], [_USABLE_LONG, _USABLE_LONG]):
        y = 800
        for ligne in lignes:
            c.drawString(72, y, ligne)
            y -= 18
        c.showPage()
    c.save()

    pages, info = pdf_extractor._native_pages(buf.getvalue())

    # pypdf atteint le seuil : ni porte, ni repli. Un PDF hybride n'est jamais
    # basculé en bloc vers l'OCR par cette porte.
    assert info["pdf_native_fallback_attempted_engines"] == []
    assert info["pdf_native_gate_applied"] is False
    assert info["pdf_native_engine"] == "pypdf"
    # La page vide reste distinguée des pages porteuses de texte.
    vides = [n for n, t in pages if not (t or "").strip()]
    assert vides == [2]


# ---------------------------------------------------------------------
# 9. Rails tabulaire et visuel : indépendants de la porte
# ---------------------------------------------------------------------
def test_rails_table_et_visuel_independants_de_la_porte(monkeypatch):
    """Quand la porte déclenche, il n'y a aucune page à texte natif.

    Les deux rails tabulaires sont gardés par ``if text_pages`` : sans texte natif ils
    sont déjà sautés aujourd'hui. La découverte visuelle, elle, ne dépend pas du texte.
    """
    from apps.rag_core.services.ingest.extraction.base import ExtractionContext

    pymu = _Moteur([(1, "")])
    miner = _Moteur([(1, "")])
    _brancher(monkeypatch, pymupdf=pymu, pdfminer=miner)

    extracteur = pdf_extractor.PdfExtractor()
    ctx = ExtractionContext(filename="planche.pdf", declared_content_type="application/pdf",
                            ocr_enabled=False, visual_sources=frozenset({"pdf"}))
    try:
        res = extracteur.extract(_pdf_sans_texte(), ctx)
    except Exception as e:
        # PDF scanné sans OCR : le verdict métier ne doit pas changer.
        assert getattr(e, "code", "") in ("pdf_scanned_requires_ocr", "ocr_no_text")
        return
    assert res.tables == []
    assert res.visual_source_kind == "pdf"

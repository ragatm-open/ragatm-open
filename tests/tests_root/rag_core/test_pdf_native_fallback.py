# tests/tests_root/rag_core/test_pdf_native_fallback.py
"""
Tests P0 — fallback d'extraction PDF natif (multi-moteur, sans OCR).

But : un PDF dont `pypdf` n'extrait rien ne doit PLUS être rejeté abusivement si
un autre moteur natif local (PyMuPDF/pdfminer/pdfplumber) récupère du texte. Le
comportement "scan/OCR requis" reste conservé uniquement quand AUCUN moteur natif
ne produit de texte (et OCR désactivé).

On combine :
- des PDF réels (reportlab) pour le chemin nominal ;
- du monkeypatching pour simuler "pypdf vide mais fallback OK" et "image-only".
"""
from __future__ import annotations

from io import BytesIO

import pytest

from apps.rag_core.models import Chunk, Document
from apps.rag_core.models.document import DocumentStatus
from apps.rag_core.services.ingest import extractors, ingest_document_from_upload
from apps.rag_core.services.ingest.extraction import (
    ExtractionError,
    ExtractionStatus,
    extract_document,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------
# Helpers : génération de PDF réels (reportlab)
# ---------------------------------------------------------------------
def _make_text_pdf(pages: list[list[str]]) -> bytes:
    """Génère un PDF avec une liste de pages ; chaque page = liste de lignes.

    Une liste de lignes vide => page sans texte (cas "page 1 vide")."""
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


_LOREM = [
    "Note de calcul ferraillage massifs verriere 4 pieux.",
    "Beton C40/50, enrobage 5 cm, acier HA500 classe B.",
    "Methode bielles et tirants, angle de 45 degres retenu.",
    "Efforts en tete de pieux fournis par STEI pour ELU et ELS.",
]


# ---------------------------------------------------------------------
# 1. PDF textuel normal : pypdf reste utilisé, pas de fallback
# ---------------------------------------------------------------------
def test_normal_text_pdf_uses_pypdf_no_fallback():
    data = _make_text_pdf([_LOREM, _LOREM])
    res = extract_document(data, filename="note.pdf", content_type="application/pdf")

    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["pdf_native_engine"] == "pypdf"
    assert res.metadata["pdf_native_fallback_used"] is False
    assert res.metadata["pdf_native_fallback_attempted_engines"] == []
    assert res.method == "pdf:pypdf"
    assert res.metadata["native_text_chars"] >= 200
    # pagination préservée
    assert [pn for pn, _ in res.pages] == [1, 2]


# ---------------------------------------------------------------------
# 2. PDF page 1 vide + pages suivantes textuelles : pas de rejet
# ---------------------------------------------------------------------
def test_first_page_empty_following_pages_text():
    data = _make_text_pdf([[], _LOREM, _LOREM])  # page 1 vide
    res = extract_document(data, filename="note.pdf", content_type="application/pdf")

    assert res.status == ExtractionStatus.SUCCESS
    # page 1 reconnue vide, mais le document n'est PAS rejeté
    assert 1 in res.metadata["empty_pages"]
    assert res.metadata["text_pages"]  # au moins une page textuelle
    # numéros de page conservés
    page_numbers = [pn for pn, _ in res.pages]
    assert page_numbers == [2, 3] or page_numbers == [1, 2, 3]


# ---------------------------------------------------------------------
# 3. pypdf renvoie vide MAIS un fallback renvoie du texte
# ---------------------------------------------------------------------
def test_pypdf_empty_but_fallback_recovers(monkeypatch):
    data = _make_text_pdf([_LOREM])  # bytes PDF valides (MIME OK)

    # pypdf "voit" 2 pages mais n'extrait rien (cas export CAO / impression image)
    monkeypatch.setattr(
        extractors, "extract_text_pages_from_pdf_bytes",
        lambda *a, **k: [(1, ""), (2, "")],
    )
    # un moteur de secours récupère le texte
    recovered = " ".join(_LOREM)
    monkeypatch.setitem(
        extractors._FALLBACK_ENGINES, "pymupdf",
        lambda raw, max_pages=None: [(1, recovered), (2, recovered)],
    )

    res = extract_document(data, filename="cao.pdf", content_type="application/pdf")

    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["pdf_native_fallback_used"] is True
    assert res.metadata["pdf_native_engine"] == "pymupdf"
    assert res.metadata.get("native_fallback_engine") == "pymupdf"
    assert "pymupdf" in res.metadata["pdf_native_fallback_attempted_engines"]
    # pas d'OCR utilisé : on a récupéré en natif
    assert res.metadata["ocr_used"] is False
    assert res.method == "pdf:pymupdf"
    assert recovered.split()[0] in res.text


# ---------------------------------------------------------------------
# 4. Image-only + OCR désactivé : comportement conservé (OCR requis)
# ---------------------------------------------------------------------
def test_image_only_ocr_disabled_still_requires_ocr(monkeypatch):
    data = _make_text_pdf([_LOREM])

    # pypdf voit les pages mais sans texte ; aucun fallback ne récupère rien
    monkeypatch.setattr(
        extractors, "extract_text_pages_from_pdf_bytes",
        lambda *a, **k: [(1, ""), (2, "")],
    )
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    with pytest.raises(ExtractionError) as exc:
        extract_document(
            data, filename="scan.pdf", content_type="application/pdf",
            ocr_enabled=False,
        )
    assert exc.value.code == "pdf_scanned_requires_ocr"


# ---------------------------------------------------------------------
# 5. Fallback désactivé par setting : pas de tentative
# ---------------------------------------------------------------------
def test_fallback_disabled_setting(monkeypatch, settings):
    settings.DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED = False
    data = _make_text_pdf([_LOREM])

    monkeypatch.setattr(
        extractors, "extract_text_pages_from_pdf_bytes",
        lambda *a, **k: [(1, ""), (2, "")],
    )
    # Même si un moteur de secours existe, il ne doit PAS être tenté.
    called = {"pymupdf": False}

    def _spy(raw, max_pages=None):
        called["pymupdf"] = True
        return [(1, "texte"), (2, "texte")]

    monkeypatch.setitem(extractors._FALLBACK_ENGINES, "pymupdf", _spy)

    with pytest.raises(ExtractionError) as exc:
        extract_document(
            data, filename="scan.pdf", content_type="application/pdf",
            ocr_enabled=False,
        )
    assert exc.value.code == "pdf_scanned_requires_ocr"
    assert called["pymupdf"] is False


# ---------------------------------------------------------------------
# 6. Sélecteur : ne remplace jamais pypdf par un fallback moins bon
# ---------------------------------------------------------------------
def test_selector_keeps_pypdf_when_better():
    # Texte RÉEL : le sélecteur compte les caractères EXPLOITABLES, pas les caractères.
    # Un remplissage « x » * 500 n'est pas de la langue et compte désormais pour zéro —
    # c'est précisément ce qui laissait passer les PDF aux polices non mappables.
    pypdf_pages = [(1, " ".join(_LOREM) * 3)]  # déjà au-dessus du seuil
    sel = extractors.select_best_native_pdf_extraction(
        b"%PDF-1.4 dummy",
        pypdf_pages=pypdf_pages,
        engines=["pymupdf", "pdfminer"],
        max_pages=10,
        min_total_chars=200,
    )
    assert sel.engine == "pypdf"
    assert sel.fallback_used is False
    # pypdf suffisant => aucun moteur de secours tenté
    assert sel.attempted_engines == []


def test_selector_prefers_richer_fallback(monkeypatch):
    pypdf_pages = [(1, ""), (2, "ab")]  # < seuil

    # Textes RÉELS de longueurs différentes (le sélecteur compte l'exploitable).
    _SHORT = "Note courte de chantier sur le massif nord."          # ~43 car. exploitables
    monkeypatch.setitem(
        extractors._FALLBACK_ENGINES, "pdfminer",
        lambda raw, max_pages=None: [(1, _SHORT), (2, _SHORT)],
    )
    monkeypatch.setitem(
        extractors._FALLBACK_ENGINES, "pymupdf",
        lambda raw, max_pages=None: [(1, " ".join(_LOREM) * 2)],  # plus riche
    )
    sel = extractors.select_best_native_pdf_extraction(
        b"%PDF-1.4 dummy",
        pypdf_pages=pypdf_pages,
        engines=["pdfminer", "pymupdf"],
        max_pages=10,
        min_total_chars=200,
    )
    # pymupdf (400 chars) atteint le seuil ; pdfminer (100) ne suffisait pas.
    assert sel.engine == "pymupdf"
    assert sel.fallback_used is True


# ---------------------------------------------------------------------
# 7. Ingestion bout-en-bout : page 1 vide -> READY + pages conservées
# ---------------------------------------------------------------------
def test_ingestion_first_page_empty_becomes_ready(rag_space):
    data = _make_text_pdf([[], _LOREM, _LOREM])
    res = ingest_document_from_upload(
        rag_space_id=rag_space.id,
        filename="note.pdf",
        content_type="application/pdf",
        file_bytes=data,
        async_mode=False,
    )
    assert res.status == DocumentStatus.READY
    assert res.chunks_created >= 1

    doc = Document.objects.get(id=res.document_id)
    assert doc.status == DocumentStatus.READY
    # méta d'extraction tracée (moteur natif)
    assert doc.meta["extraction"]["pdf_native_engine"] in ("pypdf", "pymupdf", "pdfminer", "pdfplumber")
    assert doc.meta["extraction"]["is_image_only_pdf"] is False
    # numéros de page conservés sur les chunks
    pages = {c.page_number for c in Chunk.objects.filter(document=doc)}
    assert pages and None not in pages


# =====================================================================
# Gate d'ACCEPTATION min_total_chars (P0) — plans image-only
# =====================================================================
# Régression visée : un PDF de plan vectoriel laisse un RÉSIDU de bruit (ex. 7
# caractères sur 50 pages). Avant le gate, ce résidu non vide faisait passer le
# document en READY avec un chunk inexploitable. Il doit désormais être traité
# comme image-only -> OCR requis (jamais READY trompeur).


def test_residue_below_threshold_is_image_only_not_ready(monkeypatch):
    data = _make_text_pdf([_LOREM])

    # pypdf voit 50 pages mais n'extrait rien ; un moteur natif récupère un
    # résidu de bruit (7 caractères sur la page 1, < seuil par page = 24).
    pages = [(i, "") for i in range(1, 51)]
    pages[0] = (1, "ABCDEFG")  # 7 caractères de bruit
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes", lambda *a, **k: list(pages))
    # Dict COMPLET remplacé (hermétique) : seul le fake tourne, quels que soient
    # les moteurs optionnels réellement installés (pymupdf/pdfminer/pdfplumber).
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {"pymupdf": lambda raw, max_pages=None: list(pages)})

    with pytest.raises(ExtractionError) as exc:
        extract_document(
            data, filename="plan.pdf", content_type="application/pdf",
            ocr_enabled=False,
        )
    assert exc.value.code == "pdf_scanned_requires_ocr"
    # métadonnées page-level portées par l'erreur (placeholder)
    assert exc.value.meta is not None
    assert exc.value.meta["is_image_only_pdf"] is True
    assert exc.value.meta["page_count"] == 50
    assert len(exc.value.meta["image_only_pages"]) == 50
    assert exc.value.meta["native_text_chars"] == 7


def test_residue_ingestion_is_failed_with_image_only_meta(rag_space, monkeypatch):
    data = _make_text_pdf([_LOREM])
    pages = [(i, "") for i in range(1, 51)]
    pages[0] = (1, "ABCDEFG")
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes", lambda *a, **k: list(pages))
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {"pymupdf": lambda raw, max_pages=None: list(pages)})

    res = ingest_document_from_upload(
        rag_space_id=rag_space.id,
        filename="plan.pdf",
        content_type="application/pdf",
        file_bytes=data,
        async_mode=False,
    )
    assert res.status == DocumentStatus.FAILED
    assert res.chunks_created == 0

    doc = Document.objects.get(id=res.document_id)
    assert doc.status == DocumentStatus.FAILED
    assert "pdf_scanned_requires_ocr" in doc.error_message
    # placeholder page-level conservé dans Document.meta même en échec
    ext = doc.meta["extraction"]
    assert ext["error_code"] == "pdf_scanned_requires_ocr"
    assert ext["is_image_only_pdf"] is True
    assert ext["page_count"] == 50
    # aucun chunk créé (pas d'indexation "vide" trompeuse)
    assert Chunk.objects.filter(document=doc).count() == 0


def test_genuine_short_text_pdf_still_accepted(monkeypatch):
    """Un PDF court mais réellement textuel (1 page >= seuil/page) NE doit PAS
    être traité comme image-only même si le total < DOCUMENT_PDF_MIN_TOTAL_CHARS."""
    data = _make_text_pdf([_LOREM])
    short = "Massif M01 — note courte de chantier."  # >= 24 chars, < 200 total
    monkeypatch.setattr(
        extractors, "extract_text_pages_from_pdf_bytes",
        lambda *a, **k: [(1, short)],
    )
    # On isole le cas "texte court natif" : pas de fallback qui irait relire le PDF réel.
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})
    res = extract_document(data, filename="note.pdf", content_type="application/pdf")
    assert res.status == ExtractionStatus.SUCCESS
    assert res.metadata["is_image_only_pdf"] is False
    assert res.metadata["native_text_chars"] == len(short)
    assert res.metadata["native_text_chars"] < 200
    assert res.text.startswith("Massif M01")

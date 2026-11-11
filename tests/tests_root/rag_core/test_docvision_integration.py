# -*- coding: utf-8 -*-
"""Rail Document Vision traversé par le VRAI `extract_document`.

Les tests unitaires appellent le rail directement : ils ne prouvent donc rien sur le
câblage. Ici on part des octets d'un document et on passe par la fonction de production,
avec sa frontière de processus. C'est le seul endroit qui peut démontrer que l'appel
sidecar a lieu dans le parent, et que le chemin éteint est resté inerte.
"""
from __future__ import annotations

import os
from io import BytesIO

import pytest

from apps.rag_core.services.ingest import extractors
from apps.rag_core.services.ingest.docvision import rail as dv_rail
from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction import ocr as ocr_module
from apps.rag_core.services.ingest.extraction.ocr import OcrPage, OcrResult

pytestmark = pytest.mark.django_db


def _pdf(pages: list[list[str]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lignes in pages:
        y = 800
        for ligne in lignes:
            c.drawString(60, y, ligne)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


def _png() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (200, 120), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _perimetre_declare(settings, monkeypatch):
    """Déclare le périmètre d'usage, sans quoi la porte licence bloque le rail.

    Ces tests portent sur le ROUTAGE, pas sur la porte : celle-ci a ses propres
    tests. On se place donc dans la configuration d'un exploitant ayant déclaré
    son périmètre, invariants techniques satisfaits.
    """
    settings.MINERU_SERVICE_USE_SCOPE = "personal_internal"
    from apps.rag_core.services.ingest.docvision import license_gate as _lg
    monkeypatch.setattr(_lg, "evaluer_invariants",
                        lambda: {k: True for k in _lg.INVARIANTS})


NATIF = [["Le present marche porte sur la fourniture de materiels.",
          "Le titulaire assure la maintenance preventive annuelle."]]

#: Texte rendu par l'OCR simulé. Une page RÉELLEMENT vide fait échouer l'extraction
#: entière avant le rail : le supplément ne s'applique qu'à une extraction réussie.
OCR_PAGE = "Bordereau des prix unitaires - lot 3 - terrassement general"


def _scan(monkeypatch, n_pages: int):
    """PDF vu comme image-only, dont l'OCR rend un texte déterministe."""
    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(i, "") for i in range(1, n_pages + 1)])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    def _impl(raw, page_numbers, *, languages=None, page_timeout_s=None):
        res = OcrResult(used=True, partial=False)
        for pn in page_numbers:
            res.pages.append(OcrPage(page_number=pn, text=OCR_PAGE,
                                     confidence=71.0, rotation=0))
        return res

    monkeypatch.setattr(ocr_module, "ocr_pdf_pages", _impl)


class _SidecarEspion:
    """Sidecar factice qui enregistre le PID de l'appelant."""

    def __init__(self, pages=None, echec=None):
        self.pages, self.echec, self.pids = pages or {}, echec, []

    def verifier_demarrage(self):
        self.pids.append(os.getpid())
        if self.echec:
            raise self.echec

    def parse_middle_json(self, filename, data=None, page_range=None, timeout_s=None):
        # `data` : le document voyage EN LIGNE dans la requête, il n'est plus lu
        # depuis un volume partagé côté moteur.
        self.pids.append(os.getpid())
        self.octets_recus = len(data or b"")
        n = int(page_range or 1)
        return {"middle_json": self.pages.get(n, {"pdf_info": []}),
                "parser_version": "4.0.3", "artefact_id": "test"}


# ------------------------------------------------------------------ drapeau OFF
def test_pdf_natif_identique_drapeau_eteint(settings):
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    r = extract_document(_pdf(NATIF), filename="marche.pdf",
                         content_type="application/pdf")
    assert "maintenance preventive" in r.text
    assert "docvision" not in (r.metadata or {})


def test_le_paquet_docvision_n_est_pas_charge_a_vide(settings, monkeypatch):
    """Éteint, aucun appel au rail — même pas un import du module lourd."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    vu = []
    monkeypatch.setattr(dv_rail, "appliquer_rail", lambda *a, **k: vu.append(1))
    extract_document(_pdf(NATIF), filename="m.pdf", content_type="application/pdf")
    assert vu == []


# ------------------------------------------------- drapeau ON, frontière parent
def test_appel_sidecar_dans_le_parent_pas_dans_l_enfant(settings, monkeypatch):
    """§9 : l'extraction tourne dans un enfant SIGKILL-able qui n'écrit rien de
    persistant. Un sidecar appelé là-dedans serait invisible et non annulable. Le PID
    observé doit être celui du test."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    espion = _SidecarEspion(echec=ConnectionError("pas de sidecar"))
    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: espion)

    _scan(monkeypatch, 2)
    extract_document(_pdf([[""], [""]]), filename="scan.pdf",
                     content_type="application/pdf", ocr_enabled=True)
    assert espion.pids, "le rail n'a jamais atteint le sidecar"
    assert all(p == os.getpid() for p in espion.pids)


def test_sidecar_injoignable_rend_le_texte_historique(settings, monkeypatch):
    """§46 : jamais de document vide parce que le supplément a échoué."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    monkeypatch.setattr(dv_rail, "MineruSidecar",
                        lambda *a, **k: _SidecarEspion(echec=OSError("refus")))
    r = extract_document(_pdf(NATIF), filename="m.pdf",
                         content_type="application/pdf")
    assert "maintenance preventive" in r.text
    assert r.metadata["docvision"]["pages_routed"] == 0


def test_pdf_natif_preserve_sous_drapeau_allume(settings, monkeypatch):
    """§17 : allumé, une page native n'est pas routée et son texte est intact."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True

    class _Interdit:
        def verifier_demarrage(self):
            raise AssertionError("une page native ne doit pas atteindre le moteur")

    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: _Interdit())
    avant = extract_document(_pdf(NATIF), filename="m.pdf",
                             content_type="application/pdf")
    rapport = avant.metadata["docvision"]
    assert "maintenance preventive" in avant.text
    assert rapport["pages_routed"] == 0
    assert rapport["native_chars_preserved"] > 0


def test_document_long_reste_sous_le_plafond_de_pages(settings, monkeypatch):
    """§33 : un document de 12 pages vides ne déclenche pas 12 appels moteur."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    settings.DOCUMENT_MINERU_MAX_PAGES_PER_DOC = 3
    espion = _SidecarEspion(echec=ConnectionError("absent"))
    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: espion)

    _scan(monkeypatch, 12)
    r = extract_document(_pdf([[""]] * 12), filename="long.pdf",
                         content_type="application/pdf", ocr_enabled=True)
    rap = r.metadata["docvision"]
    assert rap["pages_considered"] == 12
    assert rap["pages_skipped_max"] == 9


def test_image_autonome_traverse_le_rail(settings, monkeypatch):
    """§37 : une image autonome n'a pas de `pages` ; elle est routée comme page 1."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    espion = _SidecarEspion(echec=ConnectionError("absent"))
    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: espion)
    def _ocr_img(raw, *, languages=None, page_timeout_s=None):
        res = OcrResult(used=True, partial=False)
        res.pages.append(OcrPage(page_number=1, text=OCR_PAGE,
                                 confidence=70.0, rotation=0))
        return res

    monkeypatch.setattr(ocr_module, "ocr_image", _ocr_img)
    r = extract_document(_png(), filename="capture.png", content_type="image/png")
    assert r.metadata["docvision"]["pages_considered"] == 1


def test_echec_du_rail_ne_fait_pas_echouer_l_extraction(settings, monkeypatch):
    """§55 : une panne franche du rail laisse passer le résultat historique."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True

    def _explose(*a, **k):
        raise RuntimeError("panne")

    monkeypatch.setattr(dv_rail, "appliquer_rail", _explose)
    r = extract_document(_pdf(NATIF), filename="m.pdf",
                         content_type="application/pdf")
    assert "maintenance preventive" in r.text


def test_le_rapport_ne_contient_aucun_contenu_documentaire(settings, monkeypatch):
    """§16 : le rapport journalisé est fait de compteurs, pas de texte."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    monkeypatch.setattr(dv_rail, "MineruSidecar",
                        lambda *a, **k: _SidecarEspion(echec=OSError("x")))
    r = extract_document(_pdf(NATIF), filename="m.pdf",
                         content_type="application/pdf")
    assert "maintenance preventive" not in repr(r.metadata["docvision"])


# ------------------------------------------------ mixte natif / raster, géométrie
def _middle_json(textes: list[str]) -> dict:
    return {"pages": [{"blocks": [
        {"type": "text", "index": i,
         "bbox": [0.1, 0.1 + 0.1 * i, 0.9, 0.18 + 0.1 * i],
         "content": [{"type": "text", "content": t}]}
        for i, t in enumerate(textes)]}]}


def test_document_mixte_ne_route_que_la_page_rasterisee(settings, monkeypatch):
    """§17/§45 : la page native garde son texte, seule la page scannée est routée.

    C'est le test que l'ancien routage passait à tort : le texte OCR étant propre et
    long, toutes les pages paraissaient natives et aucune n'était routée."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    natif = "Le titulaire assure la maintenance preventive annuelle du parc."

    monkeypatch.setattr(extractors, "extract_text_pages_from_pdf_bytes",
                        lambda *a, **k: [(1, natif), (2, "")])
    monkeypatch.setattr(extractors, "_FALLBACK_ENGINES", {})

    def _impl(raw, page_numbers, *, languages=None, page_timeout_s=None):
        res = OcrResult(used=True, partial=False)
        for pn in page_numbers:
            res.pages.append(OcrPage(page_number=pn, text=OCR_PAGE,
                                     confidence=71.0, rotation=0))
        return res

    monkeypatch.setattr(ocr_module, "ocr_pdf_pages", _impl)
    espion = _SidecarEspion(echec=ConnectionError("absent"))
    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: espion)

    r = extract_document(_pdf([["x"], [""]]), filename="mixte.pdf",
                         content_type="application/pdf", ocr_enabled=True)
    routes = {d["page"]: d["route"] for d in r.metadata["docvision"]["decisions"]
              if "route" in d}
    assert routes[1] != "mineru_primary"
    assert routes[2] == "mineru_primary"
    assert natif in r.text


def test_geometrie_mineru_atteint_les_blocs_du_resultat(settings, monkeypatch):
    """§21/§47 : la bbox doit survivre au sélecteur. Sans ce rattachement, elle était
    calculée puis jetée, et aucun chunk n'en voyait jamais la couleur."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    _scan(monkeypatch, 1)
    riche = ["Bordereau des prix unitaires lot 3 terrassement general remblais",
             "Fouilles en pleine masse et evacuation des deblais excedentaires"]
    monkeypatch.setattr(dv_rail, "MineruSidecar",
                        lambda *a, **k: _SidecarEspion(pages={1: _middle_json(riche)}))

    r = extract_document(_pdf([[""]]), filename="scan.pdf",
                         content_type="application/pdf", ocr_enabled=True)
    avec_bbox = [b for b in (r.blocks or []) if getattr(b, "bbox", None)]
    assert avec_bbox, "aucun bloc géométrique n'a survécu au rail"
    assert r.metadata["docvision"]["bbox_blocks"] == len(avec_bbox)
    assert all(b.source_engine == "mineru" for b in avec_bbox)
    assert "Fouilles en pleine masse" in r.text


def test_les_octets_du_document_sont_transmis_au_sidecar(settings, monkeypatch):
    """§24 : la source est EN LIGNE. Le rail doit donc passer les octets au client ;
    sans eux, le moteur devrait lire un volume monté, ce que la posture interdit."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    _scan(monkeypatch, 1)
    espion = _SidecarEspion(pages={1: _middle_json(["Bordereau des prix unitaires"])})
    monkeypatch.setattr(dv_rail, "MineruSidecar", lambda *a, **k: espion)
    pdf = _pdf([[""]])
    extract_document(pdf, filename="scan.pdf", content_type="application/pdf",
                     ocr_enabled=True)
    assert getattr(espion, "octets_recus", 0) == len(pdf)

"""sélection des visuels PDF sur tout le document.

Ce que ces tests verrouillent, et pourquoi :

* **deux passes** — la passe 1 mesure sans garder de pixels ni de PNG ; la passe 2 ne
  normalise que les candidats retenus (au plus le budget) ;
* **petits PDF inchangés** — tant que le vivier tient dans le budget, tout est gardé dans
  l'ordre de découverte, sans score ni suppression de quasi-doublon ;
* **S4-prop** — une place par segment de pages non vide, le reste proportionnel à la masse
  de score (plus forts restes), diversité par page, redistribution, plafond par page ;
* **doublons perceptuels** — même contenu à une autre résolution/compression : un seul
  représentant, AUCUNE provenance ajoutée ; deux plans différents ne fusionnent pas ;
* **budget constant** — ``..._MAX_ASSETS`` et ``..._MAX_PER_PAGE`` ne bougent pas ;
* **déterminisme** — mêmes octets, même sélection, même ordre, mêmes statistiques.

Le corpus reprend les fixtures c01–c13 lors de la qualification, en format réduit.
"""
from __future__ import annotations

import io
import random

import pytest

from apps.rag_core.services.ingest.extraction import pdf_visual
from apps.rag_core.services.ingest.extraction.pdf_visual import (
    PdfVisualLimits,
    _Candidate,
    discover_pdf_visual_assets,
)

fitz = pytest.importorskip("fitz")

BIG = (60, 150, 540, 500)          # ~42 % de la page
HALF1 = (60, 120, 300, 300)        # ~8,6 %
HALF2 = (300, 120, 540, 300)


def _thumb(i):
    return (60 + (i % 4) * 120, 520 + (i // 4) * 90, 170 + (i % 4) * 120, 600 + (i // 4) * 90)


# --------------------------------------------------------------------------- #
# Fabriques déterministes
# --------------------------------------------------------------------------- #
def _enc(img, fmt="PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _photo_img(w, h, seed):
    from PIL import Image, ImageDraw

    r = random.Random(seed)
    img = Image.new("RGB", (w, h), tuple(r.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = r.randrange(w), r.randrange(h)
        d.ellipse([x, y, x + r.randrange(8, w // 3 + 9), y + r.randrange(8, h // 3 + 9)],
                  fill=tuple(r.randrange(256) for _ in range(3)))
    return img


def _photo(w, h, seed):
    return _enc(_photo_img(w, h, seed), "JPEG", quality=85)


def _plan_img(w, h, seed, lignes=40):
    from PIL import Image, ImageDraw

    r = random.Random(seed)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for _ in range(lignes):
        x, y = r.randrange(w), r.randrange(h)
        d.line([x, y, x + r.randrange(-w // 3, w // 3), y], fill="black")
        d.line([x, y, x, y + r.randrange(-h // 3, h // 3)], fill=(0, 0, 160))
    d.text((10, 10), f"COUPE {seed}", fill="black")
    return img


def _plan(w, h, seed):
    return _enc(_plan_img(w, h, seed))


def _pdf(pages) -> bytes:
    doc = fitz.open()
    for items in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), "Texte de page", fontsize=11)
        for rect, data in items:
            page.insert_image(fitz.Rect(*rect), stream=data)
    return doc.tobytes()


def _logo():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (300, 80), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([5, 5, 75, 75], fill=(250, 200, 0))
    for i in range(0, 200, 7):
        d.line([90 + i, 20, 90 + i, 60], fill=(0, 60, 140))
    return _enc(img)


HDR = (40, 10, 160, 40)

CORPUS = {
    "c01": lambda: _pdf([[(BIG, _photo(400, 280, p))] for p in range(30)]),
    "c02": lambda: _pdf([[(_thumb(i), _photo(300, 230, 100 * p + i)) for i in range(8)] for p in range(3)]
                        + [[] for _ in range(10)]
                        + [[(BIG, _plan(700, 450, p))] for p in range(8)]
                        + [[] for _ in range(9)]),
    "c03": lambda: _pdf([[(HDR, _logo()), (BIG, _photo(400, 300, 500 + p))] for p in range(15)]),
    "c10": lambda: _pdf([[(HALF1, _photo(300, 200, 11)), (HALF2, _plan(350, 250, 12))],
                         [(BIG, _photo(400, 300, 13))]]),
    "c11": lambda: _pdf([[(HALF1, _photo(250, 200, 20 + p)), (HALF2, _plan(300, 200, 40 + p))]
                         for p in range(6)]),
    "c12": lambda: _pdf([[(_thumb(i), _photo(200, 150, 1000 + p * 10 + i)) for i in range(4)]
                         + [(BIG, _plan(500, 350, 3000 + p))] for p in range(25)]),
    "c13": lambda: _pdf([[(BIG, _photo(900, 600, 70 + p))] for p in range(12)]
                        + [[(HALF1, _plan(350, 250, 80 + p))] for p in range(12)]),
}

_CACHE: dict = {}


def corpus(name) -> bytes:
    if name not in _CACHE:
        _CACHE[name] = CORPUS[name]()
    return _CACHE[name]


def _pages(d):
    return [a.source_page for a in d.assets]


def _cand(i, page, score, **kw):
    c = _Candidate(page=page, xref=100 + i, bbox=None, occurrences=[], discovery_index=i, **kw)
    c.score = score
    return c


# --------------------------------------------------------------------------- #
# Plus forts restes
# --------------------------------------------------------------------------- #
class TestPlusFortsRestes:
    def test_repartition_proportionnelle(self):
        assert pdf_visual._largest_remainder(8, [5.569, 3.33, 13.142, 1.429]) == [2, 1, 4, 1]

    def test_quatre_segments_equilibres(self):
        assert pdf_visual._largest_remainder(8, [1.0, 1.0, 1.0, 1.0]) == [2, 2, 2, 2]
        assert pdf_visual._largest_remainder(6, [1.0, 1.0, 1.0, 1.0]) == [2, 2, 1, 1], \
            "égalité de reste : plus petit index"

    def test_forte_concentration(self):
        assert pdf_visual._largest_remainder(8, [100.0, 0.01, 0.01, 0.01]) == [8, 0, 0, 0]

    def test_masse_nulle_repartition_uniforme_sans_division_par_zero(self):
        assert pdf_visual._largest_remainder(5, [0.0, 0.0, 0.0]) == [2, 2, 1]

    def test_aucune_place_ou_aucun_segment(self):
        assert pdf_visual._largest_remainder(0, [1.0, 2.0]) == [0, 0]
        assert pdf_visual._largest_remainder(3, []) == []

    def test_somme_toujours_exacte(self):
        r = random.Random(3)
        for _ in range(200):
            w = [r.random() * r.choice([0, 1, 10]) for _ in range(r.randrange(1, 5))]
            n = r.randrange(0, 13)
            assert sum(pdf_visual._largest_remainder(n, w)) == n


# --------------------------------------------------------------------------- #
# S4-prop sur candidats synthétiques (sans PDF)
# --------------------------------------------------------------------------- #
class TestS4Prop:
    def test_une_place_par_segment_non_vide_puis_masse(self):
        pool = [_cand(i, p, s) for i, (p, s) in enumerate(
            [(1, 1.0), (2, 1.0), (3, 1.0), (4, 1.0), (5, 1.0), (6, 1.0),     # segment 0
             (9, 0.2),                                                      # segment 1
             (16, 0.9), (17, 0.9), (18, 0.9), (19, 0.9),                    # segment 2
             (26, 0.1)])]                                                   # segment 3
        sel = pdf_visual._select_s4_prop(pool, pages=30, budget=6, per_page=4)
        pages = [c.page for c in sel]
        assert 9 in pages and 26 in pages, "chaque segment non vide a sa place"
        assert len(sel) == 6

    def test_segments_calcules_sur_letendue_du_document(self):
        pool = [_cand(i, p, 1.0) for i, p in enumerate([1, 8, 9, 15, 16, 23, 24, 30])]
        pdf_visual._select_s4_prop(pool, pages=30, budget=4, per_page=4)
        assert [c.segment for c in pool] == [0, 0, 1, 1, 2, 2, 3, 3]

    def test_segments_vides_places_redistribuees(self):
        """Images au premier et au dernier quart seulement : aucune place perdue."""
        pool = [_cand(i, p, 0.5) for i, p in enumerate([1, 2, 3, 4, 5, 6, 26, 27, 28, 29, 30, 30])]
        pool += [_cand(12, 7, 0.5), _cand(13, 29, 0.5)]
        sel = pdf_visual._select_s4_prop(pool, pages=30, budget=12, per_page=4)
        assert len(sel) == 12
        assert {c.segment for c in sel} == {0, 3}

    def test_masse_nulle_garde_sa_place_minimale(self):
        pool = [_cand(i, p, 1.0) for i, p in enumerate([1, 2, 3, 4, 5, 6])]
        pool += [_cand(6, 20, 0.0), _cand(7, 21, -0.4)]
        sel = pdf_visual._select_s4_prop(pool, pages=30, budget=4, per_page=4)
        assert sum(c.segment == 2 for c in sel) == 1

    def test_penalite_de_page_favorise_la_diversite(self):
        pool = [_cand(0, 2, 1.0), _cand(1, 2, 0.95), _cand(2, 3, 0.8)]   # même segment
        sel = pdf_visual._select_s4_prop(pool, pages=40, budget=2, per_page=4)
        assert [c.page for c in sel] == [2, 3], "0.95 − 0.3 < 0.8"

    def test_penalite_ne_force_pas_une_page_faible(self):
        pool = [_cand(0, 2, 1.0), _cand(1, 2, 0.95), _cand(2, 3, 0.5)]   # même segment
        sel = pdf_visual._select_s4_prop(pool, pages=40, budget=2, per_page=4)
        assert [c.discovery_index for c in sel] == [0, 1], "0.95 − 0.3 > 0.5"

    def test_plafond_par_page_et_pas_de_diversite_inventee(self):
        pool = [_cand(i, 1, 1.0 - i / 100) for i in range(20)]
        sel = pdf_visual._select_s4_prop(pool, pages=1, budget=12, per_page=4)
        assert len(sel) == 4, "une seule page : au plus le plafond par page"

    def test_redistribution_quand_un_segment_est_bloque_par_le_plafond(self):
        # Segment 0 : 6 candidats sur une seule page (plafond 2), fortes masses.
        pool = [_cand(i, 1, 1.0) for i in range(6)]
        pool += [_cand(10 + i, 20 + i, 0.3) for i in range(6)]
        sel = pdf_visual._select_s4_prop(pool, pages=30, budget=6, per_page=2)
        assert len(sel) == 6
        assert sum(c.page == 1 for c in sel) == 2

    def test_egalites_departagees_par_ordre_de_decouverte(self):
        pool = [_cand(i, 1 + i, 0.5) for i in range(10)]
        sel = pdf_visual._select_s4_prop(pool, pages=40, budget=3, per_page=4)
        assert [c.discovery_index for c in sel] == [0, 1, 2]

    def test_sortie_dans_lordre_documentaire_jamais_par_score(self):
        pool = [_cand(0, 1, 0.1), _cand(1, 2, 0.9), _cand(2, 30, 0.5), _cand(3, 29, 0.99)]
        sel = pdf_visual._select_s4_prop(pool, pages=30, budget=3, per_page=4)
        assert [c.discovery_index for c in sel] == sorted(c.discovery_index for c in sel)

    def test_budget_nul(self):
        assert pdf_visual._select_s4_prop([_cand(0, 1, 1.0)], pages=1, budget=0, per_page=4) == []


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #
class TestScore:
    def test_formule_explicite(self):
        c = _Candidate(page=1, xref=1, bbox=None, occurrences=[], raster_width=1000, raster_height=1000,
                       surface_ratio=0.05)
        assert pdf_visual._score(c) == pytest.approx(0.6 * 0.5 + 0.4 * 1.0)
        c.in_band, c.repeat_count = True, 3
        assert pdf_visual._score(c) == pytest.approx(0.7 - 0.5 - 0.5)

    def test_resolution_bornee_et_monotone(self):
        def res(px):
            c = _Candidate(page=1, xref=1, bbox=None, occurrences=[], raster_width=px, raster_height=1)
            return pdf_visual._score(c) / 0.4
        valeurs = [res(px) for px in (10_000, 20_000, 50_000, 200_000, 1_000_000, 16_000_000)]
        assert valeurs[0] == valeurs[1] == 0.0
        assert valeurs == sorted(valeurs)
        assert valeurs[-1] == valeurs[-2] == 1.0, "une image géante n'écrase pas les autres signaux"

    def test_surface_de_la_plus_grande_occurrence_reelle_et_bandeau(self):
        pages = {1: (0.0, 0.0, 600.0, 800.0), 2: (0.0, 0.0, 600.0, 800.0)}
        surf, band = pdf_visual._principal_geometry(
            [(1, (0, 0, 600, 60)), (2, (0, 100, 300, 500))], pages)
        assert surf == pytest.approx(0.25) and band is False
        surf, band = pdf_visual._principal_geometry([(1, (0, 0, 600, 60))], pages)
        assert band is True
        surf, band = pdf_visual._principal_geometry([(1, (0, 750, 100, 800))], pages)
        assert band is True
        assert pdf_visual._principal_geometry([(1, None)], pages) == (0.0, False)


# --------------------------------------------------------------------------- #
# Doublons perceptuels
# --------------------------------------------------------------------------- #
def _measured(img, xref=1, page=1, score=0.5):
    c = _Candidate(page=page, xref=xref, bbox=None, occurrences=[], discovery_index=xref,
                   raster_width=img.width, raster_height=img.height)
    c.dhash, c.thumb = pdf_visual._measure(img)
    c.score = score
    return c


class TestDoublonsPerceptuels:
    def test_meme_image_recompressee_et_redimensionnee(self):
        from PIL import Image

        base = _photo_img(600, 400, 7)
        recomp = Image.open(io.BytesIO(_enc(base, "JPEG", quality=40))).convert("RGB")
        reduit = base.resize((300, 200))
        a = _measured(base, 1)
        assert pdf_visual._is_visual_duplicate(a, _measured(recomp, 2))
        assert pdf_visual._is_visual_duplicate(a, _measured(reduit, 3))
        assert pdf_visual._is_visual_duplicate(a, _measured(base.copy(), 4)), "empreinte identique"

    def test_images_differentes_non_fusionnees(self):
        assert not pdf_visual._is_visual_duplicate(_measured(_photo_img(600, 400, 7), 1),
                                                   _measured(_photo_img(600, 400, 8), 2))

    def test_proportions_differentes_non_fusionnees(self):
        base = _photo_img(600, 400, 7)
        a, b = _measured(base, 1), _measured(base.resize((600, 330)), 2)
        assert bin(a.dhash ^ b.dhash).count("1") <= pdf_visual.DUPLICATE_MAX_HAMMING
        assert not pdf_visual._is_visual_duplicate(a, b), "ratio 1,50 contre 1,82"

    @pytest.mark.parametrize("lignes", [3, 10, 40])
    def test_deux_plans_semblables_mais_differents_non_fusionnes(self, lignes):
        a = _measured(_plan_img(1200, 800, 1, lignes), 1)
        b = _measured(_plan_img(1200, 800, 2, lignes), 2)
        assert not pdf_visual._is_visual_duplicate(a, b)

    def test_collision_dhash_arretee_par_la_correlation(self):
        a = _measured(_plan_img(1200, 800, 1, 10), 1)
        b = _measured(_plan_img(1200, 800, 2, 10), 2)
        b.dhash = a.dhash
        assert not pdf_visual._is_visual_duplicate(a, b)

    def test_representant_meilleur_score_puis_plus_petit_index(self):
        base = _photo_img(600, 400, 7)
        a = _measured(base, 1, page=1, score=0.4)
        b = _measured(base.resize((300, 200)), 2, page=30, score=0.9)
        c = _measured(_photo_img(600, 400, 9), 3, page=5, score=0.1)
        reps, dropped = pdf_visual._group_duplicates([a, b, c])
        assert dropped == 1
        assert [r.xref for r in reps] == [2, 3], "sortie en ordre de découverte"
        assert b.repeat_count == 2 and a.reject == "visual_duplicate"
        assert b.occurrences == [], "aucune provenance ajoutée au représentant"

        egal_a = _measured(base, 1, score=0.5)
        egal_b = _measured(base.resize((300, 200)), 2, score=0.5)
        reps, _ = pdf_visual._group_duplicates([egal_b, egal_a])
        assert [r.xref for r in reps] == [1]


# --------------------------------------------------------------------------- #
# Corpus c01–c13 : couverture et goldens
# --------------------------------------------------------------------------- #
GOLDEN_PAGES = {
    "c01": [1, 2, 3, 9, 10, 11, 16, 17, 18, 24, 25, 26],
    "c02": [1, 1, 2, 2, 3, 14, 15, 16, 17, 18, 19, 20],
    "c12": [1, 2, 3, 8, 9, 10, 14, 15, 16, 20, 21, 22],
    "c13": [1, 2, 3, 7, 8, 9, 13, 14, 15, 19, 20, 21],
}


class TestCorpus:
    @pytest.mark.parametrize("name,late_from", [("c01", 16), ("c12", 13), ("c13", 13), ("c02", 14)])
    def test_couverture_tardive(self, name, late_from):
        d = discover_pdf_visual_assets(corpus(name))
        pages = _pages(d)
        assert len(pages) == 12
        assert sum(p >= late_from for p in pages) >= 6
        assert d.stats["selection"]["strategy"] == "s4_prop_v1"

    @pytest.mark.parametrize("name", sorted(GOLDEN_PAGES))
    def test_golden_pages_et_ordre(self, name):
        d = discover_pdf_visual_assets(corpus(name))
        assert _pages(d) == GOLDEN_PAGES[name]
        assert [a.ordinal for a in d.assets] == list(range(12))
        xrefs = [a.source_locator["xref"] for a in d.assets]
        assert xrefs == sorted(xrefs), "ordre documentaire"

    def test_c02_garde_plusieurs_images_du_debut(self):
        pages = _pages(discover_pdf_visual_assets(corpus("c02")))
        assert sum(p <= 3 for p in pages) >= 4

    def test_c03_bandeau_repete_reste_decoratif(self):
        d = discover_pdf_visual_assets(corpus("c03"))
        assert d.stats["omitted_decorative"] == 1
        assert d.stats["candidates_eligible"] == 15
        assert len(set(_pages(d))) == 12

    def test_c12_plus_de_cent_images_normalise_au_plus_le_budget(self, monkeypatch):
        vraie = pdf_visual._normalize
        compte = {"n": 0}

        def _compte(img):
            compte["n"] += 1
            return vraie(img)

        monkeypatch.setattr(pdf_visual, "_normalize", _compte)
        d = discover_pdf_visual_assets(corpus("c12"))
        assert d.stats["images_seen"] == 125 and d.stats["candidates_eligible"] == 125
        assert compte["n"] == 12, "aucune normalisation finale au-delà du budget"
        assert d.stats["omitted_limit"] == 113

    def test_petits_pdf_ordre_de_decouverte_integral(self, monkeypatch):
        monkeypatch.setattr(pdf_visual, "_select_s4_prop", _interdit)
        monkeypatch.setattr(pdf_visual, "_group_duplicates", _interdit)
        for name, attendu in (("c10", [1, 1, 2]), ("c11", [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6])):
            d = discover_pdf_visual_assets(corpus(name))
            assert _pages(d) == attendu
            assert [a.ordinal for a in d.assets] == list(range(len(attendu)))
            assert d.stats["selection"] == {"strategy": "discovery_order", "eligible": len(attendu),
                                            "selected": len(attendu), "budget": 12, "segments": 0}

    def test_exactement_douze_tout_est_garde_sans_score(self, monkeypatch):
        monkeypatch.setattr(pdf_visual, "_select_s4_prop", _interdit)
        d = discover_pdf_visual_assets(corpus("c11"))
        assert d.stats["admitted"] == 12 and d.stats["omitted_limit"] == 0

    def test_plus_de_douze_sur_une_seule_page(self):
        page = [((40 + 90 * (i % 6), 100 + 90 * (i // 6), 120 + 90 * (i % 6), 180 + 90 * (i // 6)),
                 _photo(200, 160, 900 + i)) for i in range(14)]
        d = discover_pdf_visual_assets(_pdf([page]))
        assert d.stats["admitted"] == 4, "aucune diversité inventée, plafond par page"
        assert d.stats["omitted_limit"] == 10
        assert d.stats["selection"]["strategy"] == "s4_prop_v1"

    def test_petit_pdf_quasi_doublons_conserves(self):
        img = _photo_img(400, 300, 5)
        data = _pdf([[(BIG, _enc(img))], [(BIG, _enc(img.resize((300, 225)), "JPEG", quality=70))]])
        d = discover_pdf_visual_assets(data)
        assert d.stats["admitted"] == 2 and d.stats["omitted_visual_duplicate"] == 0

    def test_doublons_perceptuels_ecartes_quand_le_budget_contraint(self):
        img = _photo_img(400, 300, 5)
        pages = [[(BIG, _enc(img))], [(BIG, _enc(img.resize((300, 225)), "JPEG", quality=70))]]
        pages += [[(BIG, _photo(300, 220, 40 + p))] for p in range(3)]
        d = discover_pdf_visual_assets(_pdf(pages), limits=PdfVisualLimits(max_assets=4))
        assert d.stats["omitted_visual_duplicate"] == 1
        assert d.stats["candidates_eligible"] == 4
        assert d.stats["admitted"] == 4 and d.stats["omitted_limit"] == 0
        assert d.stats["selection"]["strategy"] == "discovery_order", "le vivier dédoublonné tient"
        assert sorted(_pages(d)) in ([1, 3, 4, 5], [2, 3, 4, 5])
        for a in d.assets:
            assert all(o.source_page == a.source_page for o in a.occurrences), "aucune fausse provenance"

    def test_comptabilite_des_candidats(self):
        d = discover_pdf_visual_assets(corpus("c03"))
        s = d.stats
        distincts = 16
        assert (s["omitted_decorative"] + s["omitted_table_overlap"] + s["omitted_too_small"]
                + s["omitted_unreadable"] + s["omitted_uniform"] + s["omitted_visual_duplicate"]
                + s["candidates_eligible"]) == distincts
        assert s["candidates_eligible"] == s["admitted"] + s["omitted_limit"] + s["omitted_too_large"]

    def test_determinisme(self):
        data = corpus("c12")
        runs = [discover_pdf_visual_assets(data) for _ in range(3)]
        ref = [(a.source_locator["xref"], a.ordinal, a.content_hash) for a in runs[0].assets]
        for r in runs[1:]:
            assert [(a.source_locator["xref"], a.ordinal, a.content_hash) for a in r.assets] == ref
            assert r.stats == runs[0].stats


def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
    raise AssertionError("sélection bornée interdite quand le vivier tient dans le budget")


# --------------------------------------------------------------------------- #
# Deux passes : aucune rétention, cohérence passe 1 / passe 2
# --------------------------------------------------------------------------- #
class TestDeuxPasses:
    def test_aucun_octet_ni_pixel_retenu_par_les_candidats(self, monkeypatch):
        vus = []
        vraie = pdf_visual._analyse

        def _espion(fitz_mod, doc, cand, **kw):
            ok = vraie(fitz_mod, doc, cand, **kw)
            vus.append(cand)
            return ok

        monkeypatch.setattr(pdf_visual, "_analyse", _espion)
        discover_pdf_visual_assets(corpus("c13"))
        assert len(vus) == 24
        for cand in vus:
            for name, value in vars(cand).items():
                assert not isinstance(value, (bytes, bytearray)), name
                assert "PIL" not in type(value).__module__, name
            assert cand.thumb is None or len(cand.thumb) == 32 * 32

    def test_rendu_identique_entre_les_deux_passes_avec_smask(self):
        from PIL import Image, ImageDraw

        photo = _photo_img(320, 240, 3).convert("RGBA")
        mask = Image.new("L", (320, 240), 0)
        ImageDraw.Draw(mask).ellipse([20, 20, 300, 220], fill=255)
        photo.putalpha(mask)
        data = _pdf([[(BIG, _enc(photo))]])
        doc = fitz.open(stream=data, filetype="pdf")
        (cand,), _ = pdf_visual._collect(doc, stats=pdf_visual._new_stats(), max_pages=0)
        assert cand.smask
        lim = PdfVisualLimits()
        a = pdf_visual._render(fitz, doc, cand, lim)[0]
        b = pdf_visual._render(fitz, doc, cand, lim)[0]
        assert a.tobytes() == b.tobytes()
        asset, = discover_pdf_visual_assets(data).assets
        assert asset.content_hash == pdf_visual.sha256_of(pdf_visual._normalize(a)[0])

    def test_budget_et_plafond_inchanges(self):
        from apps.rag_core.services.ingest.extraction import config

        assert (PdfVisualLimits().max_assets, PdfVisualLimits().max_per_page) == (12, 4)
        assert (config.pdf_visual_max_assets(), config.pdf_visual_max_assets_per_page()) == (12, 4)
        assert pdf_visual.SELECTION_SEGMENTS == 4

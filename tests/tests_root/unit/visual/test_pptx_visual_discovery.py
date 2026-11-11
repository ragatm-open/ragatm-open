"""découverte des images INTÉGRÉES d'une présentation.

Ce que ces tests verrouillent, et pourquoi :

* **aucune ouverture de paquet supplémentaire** — python-pptx a déjà chargé les parties
  atteignables ; la découverte lit les octets déjà en mémoire. Invariant EXÉCUTABLE ;
* **intégrées seulement** — une image liée n'est jamais suivie, un média orphelin n'est
  jamais un visuel, un format vectoriel est omis ;
* **une diapositive n'est pas une page** — `source_page` reste `None`, la provenance est
  structurelle (diapositive 1-based, rang de forme, relation, partie) ;
* **dédoublonnage avant contexte** — la même image sur plusieurs diapositives aux titres
  différents reste UN visuel ; au-delà du seuil de répétition, elle est décorative ;
* **vision éteinte = extraction historique** — comparée à une référence capturée AVANT
  le lot, sur le code non modifié.

Aucun réseau, aucun appel Gateway : la découverte n'analyse rien.
"""
from __future__ import annotations

import io
import json
import pathlib
import zipfile

import pytest

from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction.pptx_visual import PptxVisualLimits
from apps.rag_core.services.ingest.visual.contracts import CONTEXT_MAX_CHARS
from tests_root.unit.visual import _pptx_media as P

MODULE = (pathlib.Path(__file__).resolve().parents[4]
          / "backend/apps/rag_core/services/ingest/extraction/pptx_visual.py")
BASELINE = pathlib.Path(__file__).with_name("_pptx_vision_off_baseline.json")

ON = frozenset({"pptx"})


def _extract(data: bytes, sources=ON):
    return extract_document(data, filename="deck.pptx", visual_sources=sources)


def _deck(*slides, notes=None) -> bytes:
    return P.build_pptx_with_media(list(slides), notes=notes)


def _stats(result) -> dict:
    return result.metadata.get("pptx_visual") or {}


@pytest.fixture(autouse=True)
def _pptx_on(settings):
    settings.DOCUMENT_PPTX_ENABLED = True


@pytest.fixture
def no_network(monkeypatch):
    """Toute tentative réseau fait échouer le test (image liée, Gateway…)."""
    import socket

    import requests

    def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
        raise AssertionError("accès réseau interdit pendant la découverte")

    monkeypatch.setattr(socket, "create_connection", _interdit)
    monkeypatch.setattr(socket.socket, "connect", _interdit)
    monkeypatch.setattr(requests, "get", _interdit)
    monkeypatch.setattr(requests, "post", _interdit)
    monkeypatch.setattr("urllib.request.urlopen", _interdit)


# =========================================================================== #
# Barrière « vision éteinte » — BLOQUANTE
# =========================================================================== #
class TestVisionEteinteReferenceFigee:
    """Référence capturée sur le code NON modifié (HEAD 0fa853f, arbre propre)."""

    CONFIGS = {
        "defaut": {},
        "notes_et_alt": {"DOCUMENT_PPTX_INCLUDE_NOTES": True,
                         "DOCUMENT_PPTX_INCLUDE_ALT_TEXT": True},
        "sans_alt": {"DOCUMENT_PPTX_INCLUDE_ALT_TEXT": False},
    }

    @pytest.fixture(scope="class")
    def reference(self):
        return json.loads(BASELINE.read_text(encoding="utf-8"))

    @pytest.fixture(scope="class")
    def decks(self):
        return P.corpus_decks()

    @pytest.mark.parametrize("config", sorted(CONFIGS))
    @pytest.mark.parametrize("deck", ["principal", "lie", "orphelin", "vectoriel", "corrompu"])
    @pytest.mark.parametrize("sources", [frozenset(), frozenset({"image", "pdf", "docx"})],
                             ids=["porte-fermee", "pptx-hors-allowlist"])
    def test_extraction_identique_a_la_reference(self, reference, decks, settings,
                                                 config, deck, sources):
        for name, value in self.CONFIGS[config].items():
            setattr(settings, name, value)
        result = extract_document(decks[deck], filename=f"{deck}.pptx", visual_sources=sources)
        # Normalisation JSON identique à celle de la capture.
        actuel = json.loads(json.dumps(P.extraction_snapshot(result), ensure_ascii=False,
                                       sort_keys=True, default=str))
        assert actuel == reference[f"{config}/{deck}"]
        assert result.visual_assets == [] and result.visual_units == []
        assert "pptx_visual" not in result.metadata
        # Seul ajout toléré : la nature de source, transitoire, qui arme la garde de
        # réindexation. Hors `metadata`, hors `repr`, hors comparaison.
        assert result.visual_source_kind == "pptx"

    def test_activer_la_vision_ne_modifie_pas_lextraction_native(self, decks, reference):
        """Porte OUVERTE : le natif (texte, diapositives, avertissements) reste identique."""
        result = _extract(decks["principal"])
        actuel = json.loads(json.dumps(P.extraction_snapshot(result), ensure_ascii=False,
                                       sort_keys=True, default=str))
        attendu = dict(reference["defaut/principal"])
        actuel_meta = dict(actuel.pop("metadata"))
        attendu_meta = dict(attendu.pop("metadata"))
        assert actuel_meta.pop("pptx_visual")["admitted"] >= 1
        assert actuel_meta == attendu_meta
        assert actuel.pop("visual_assets") >= 1
        attendu.pop("visual_assets")
        assert actuel == attendu

    def test_la_reference_couvre_les_cas_historiques(self, reference):
        """La référence fige bien le défaut connu des images groupées (hors lot)."""
        pptx = reference["defaut/principal"]["metadata"]["pptx"]
        assert pptx["image_only_slides"] == [2, 3]
        assert 5 in pptx["empty_slides"], "image groupée : classement historique conservé"


# =========================================================================== #
# Aucune ouverture supplémentaire, aucun appel, aucun rendu
# =========================================================================== #
class TestPasseUnique:
    def test_aucune_ouverture_de_paquet_supplementaire(self, monkeypatch):
        """Le DELTA d'ouvertures d'archive entre vision éteinte et allumée est nul.

        La validation et la pré-passe ouvrent déjà l'archive ; python-pptx aussi. Ces
        ouvertures PRÉEXISTENT au lot : seul l'écart doit être nul.
        """
        import zipfile as zipmod

        data = _deck({"title": "Photo", "pictures": [{"data": P.PHOTO}]})

        def _compter(sources):
            n = {"v": 0}
            vraie = zipmod.ZipFile

            def _compte(*a, **k):
                n["v"] += 1
                return vraie(*a, **k)

            with monkeypatch.context() as mp:
                mp.setattr(zipmod, "ZipFile", _compte)
                resultat = _extract(data, sources)
            return n["v"], resultat

        eteint, _ = _compter(frozenset())
        allume, r = _compter(ON)
        assert eteint > 0, "le compteur voit bien les ouvertures préexistantes"
        assert allume == eteint, "aucune ouverture de paquet supplémentaire"
        assert _stats(r)["package_opens_additional"] == 0
        assert len(r.visual_assets) == 1

    def test_une_seule_traversee_de_la_presentation(self, monkeypatch):
        """python-pptx n'est chargé qu'UNE fois, vision allumée comme éteinte."""
        from apps.rag_core.services.ingest.extraction import pptx_extractor

        data = _deck({"title": "Photo", "pictures": [{"data": P.PHOTO}]})
        # L'extraction s'exécute dans un processus enfant (HARDENING-03) : un compteur
        # en mémoire y resterait confiné, et l'assertion ne pourrait plus qu'échouer.
        from tests_root.process_witness import TemoinPartage

        vraie = pptx_extractor._load_presentation
        n = TemoinPartage(prefixe="pptx-traversees-")

        def _compte(raw):
            n.note()
            return vraie(raw)

        monkeypatch.setattr(pptx_extractor, "_load_presentation", _compte)
        _extract(data)
        assert len(n) == 1

    def test_aucun_appel_gateway(self, monkeypatch):
        from apps.llm_router.gateway import vision as vision_mod

        def _interdit(*a, **k):  # pragma: no cover
            raise AssertionError("appel Gateway interdit pendant la découverte")

        monkeypatch.setattr(vision_mod.requests, "post", _interdit)
        r = _extract(_deck({"title": None, "pictures": [{"data": P.PHOTO}]}))
        assert _stats(r)["admitted"] == 1

    def test_aucun_rendu_ni_processus_externe(self):
        import ast
        import tokenize

        src = MODULE.read_text(encoding="utf-8")
        sans_commentaires = tokenize.untokenize(
            tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type != tokenize.COMMENT)
        arbre = ast.parse(sans_commentaires)
        for noeud in ast.walk(arbre):
            corps = getattr(noeud, "body", None)
            if (isinstance(noeud, (ast.Module, ast.FunctionDef, ast.ClassDef)) and corps
                    and isinstance(corps[0], ast.Expr)
                    and isinstance(getattr(corps[0], "value", None), ast.Constant)
                    and isinstance(corps[0].value.value, str)):
                corps.pop(0)
        code = ast.unparse(arbre).lower()
        for interdit in ("subprocess", "libreoffice", "soffice", "requests", "urllib",
                         "socket", "fitz", "zipfile", "presentation(", "gateway"):
            assert interdit not in code, interdit


# =========================================================================== #
# Admission : combien de visuels, lesquels
# =========================================================================== #
class TestAdmission:
    def test_texte_seul_aucun_visuel(self):
        r = _extract(_deck({"title": "Texte", "bullets": ["un", "deux"]}))
        assert r.visual_assets == []
        assert r.visual_source_kind == "pptx"

    def test_image_seule(self):
        r = _extract(_deck({"title": None, "pictures": [{"data": P.PHOTO}]}))
        assert len(r.visual_assets) == 1
        asset = r.visual_assets[0]
        assert asset.source_kind == "pptx"
        assert asset.task == "describe"
        assert asset.mime_type == "image/png"
        assert asset.origin == "embedded"

    def test_texte_et_image(self):
        r = _extract(_deck({"title": "Architecture", "bullets": ["point"],
                            "pictures": [{"data": P.PHOTO}]}))
        assert len(r.visual_assets) == 1
        assert r.visual_assets[0].context.nearby_text == "Architecture"

    def test_image_groupee_est_admise(self):
        r = _extract(_deck({"title": None, "pictures": [{"data": P.PHOTO, "grouped": True}]}))
        assert len(r.visual_assets) == 1
        assert r.visual_assets[0].source_locator["grouped"] is True

    def test_tableau_natif_aucun_visuel(self):
        r = _extract(_deck({"title": "Tableau", "table": [["A", "B"], ["1", "2"]]}))
        assert r.visual_assets == []

    def test_plusieurs_images_distinctes_sur_une_diapositive(self):
        images = [P.PHOTO, P.AUTRE, P.TROISIEME, P.QUATRIEME, P.CINQUIEME]
        r = _extract(_deck({"title": "Planche", "pictures": [{"data": d} for d in images]}))
        stats = _stats(r)
        assert len(r.visual_assets) == 4, "plafond par diapositive (réglage existant)"
        assert stats["omitted_limit"] == 1
        assert [a.ordinal for a in r.visual_assets] == [0, 1, 2, 3]

    def test_plafond_par_document(self, settings):
        settings.DOCUMENT_PDF_VISUAL_MAX_ASSETS = 2
        images = [P.PHOTO, P.AUTRE, P.TROISIEME]
        r = _extract(_deck(*({"title": f"D{i}", "pictures": [{"data": d}]}
                             for i, d in enumerate(images))))
        assert len(r.visual_assets) == 2
        assert _stats(r)["omitted_limit"] == 1

    def test_plafond_par_diapositive_suit_le_reglage_existant(self, settings):
        settings.DOCUMENT_PDF_VISUAL_MAX_PER_PAGE = 1
        r = _extract(_deck({"title": "Deux", "pictures": [{"data": P.PHOTO}, {"data": P.AUTRE}]},
                           {"title": "Une", "pictures": [{"data": P.TROISIEME}]}))
        assert [a.source_locator["slide_number"] for a in r.visual_assets] == [1, 2]
        assert PptxVisualLimits.from_settings().max_per_slide == 1

    def test_image_trop_petite(self):
        r = _extract(_deck({"title": "Icône", "pictures": [{"data": P.ICONE}]}))
        assert r.visual_assets == []
        assert _stats(r)["omitted_too_small"] == 1

    def test_image_trop_lourde_jamais_decodee(self, settings, monkeypatch):
        from apps.rag_core.services.ingest.extraction import docx_visual

        appels = {"v": 0}
        vraie = docx_visual._decode

        def _compte(*a, **k):
            appels["v"] += 1
            return vraie(*a, **k)

        monkeypatch.setattr(docx_visual, "_decode", _compte)
        settings.DOCUMENT_PDF_VISUAL_MAX_BYTES = 100
        r = _extract(_deck({"title": "Lourde", "pictures": [{"data": P.PHOTO}]}))
        assert r.visual_assets == []
        assert _stats(r)["omitted_too_large"] == 1
        assert appels["v"] == 0, "aucun décodage PIL au-delà de la borne d'octets"

    def test_image_trop_grande_est_reduite(self, settings):
        settings.DOCUMENT_PDF_VISUAL_MAX_PIXELS = 20_000
        big = P.png(400, 300, (90, 10, 10))
        r = _extract(_deck({"title": "Grande", "pictures": [{"data": big}]}))
        (asset,) = r.visual_assets
        assert asset.width * asset.height <= 20_000

    def test_image_corrompue(self):
        data = P.replace_first_media(
            _deck({"title": "Cassée", "pictures": [{"data": P.PHOTO}]}), b"\x89PNG pas une image")
        r = _extract(data)
        assert r.visual_assets == []
        assert _stats(r)["omitted_unreadable"] == 1
        assert "Cassée" in r.text, "l'extraction native continue"

    @pytest.mark.parametrize("fmt", ["JPEG", "GIF", "BMP", "TIFF", "WEBP"])
    def test_formats_raster_decodables(self, fmt):
        if fmt == "WEBP":
            # python-pptx n'insère pas de WEBP : les octets remplacent un média existant.
            data = P.replace_first_media(
                _deck({"title": fmt, "pictures": [{"data": P.PHOTO}]}), P.raster(fmt))
        else:
            data = _deck({"title": fmt, "pictures": [{"data": P.raster(fmt)}]})
        r = _extract(data)
        assert len(r.visual_assets) == 1, fmt
        assert r.visual_assets[0].mime_type == "image/png", "normalisé en PNG RVB"

    @pytest.mark.parametrize("ext,ct", [("emf", "image/x-emf"), ("wmf", "image/x-wmf"),
                                        ("svg", "image/svg+xml")])
    def test_format_vectoriel_omis(self, ext, ct):
        data = P.make_first_media_vector(
            _deck({"title": "Vecteur", "pictures": [{"data": P.PHOTO}]}), ext=ext, content_type=ct)
        r = _extract(data)
        assert r.visual_assets == []
        assert _stats(r)["omitted_vector"] == 1


# =========================================================================== #
# Intégrées seulement
# =========================================================================== #
class TestIntegreesSeulement:
    def test_image_liee_jamais_suivie(self, no_network):
        data = P.link_first_picture(_deck({"title": "Liée", "pictures": [{"data": P.PHOTO}]}))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            rels = zf.read("ppt/slides/_rels/slide1.xml.rels").decode()
            assert P.LINKED_TARGET in rels and 'TargetMode="External"' in rels
            assert zf.namelist().count("ppt/media/image1.png") == 1, "l'ancienne relation reste"
        r = _extract(data)
        assert r.visual_assets == []
        assert _stats(r)["omitted_external"] == 1

    def test_media_orphelin_jamais_un_visuel(self):
        base = _deck({"title": "Orphelin", "bullets": ["texte"]})
        data = P.add_orphan_media(base, P.PHOTO)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert "ppt/media/orphan-image.png" in zf.namelist()
        r = _extract(data)
        assert r.visual_assets == []
        assert "pptx_visual" not in r.metadata, "aucune image rencontrée : aucune métadonnée"

    def test_orphelin_a_cote_dune_vraie_image(self):
        base = _deck({"title": "Mixte", "pictures": [{"data": P.PHOTO}]})
        r = _extract(P.add_orphan_media(base, P.AUTRE))
        assert len(r.visual_assets) == 1
        assert r.visual_assets[0].source_locator["part"] == "ppt/media/image1.png"


# =========================================================================== #
# Dédoublonnage et répétition
# =========================================================================== #
class TestDedoublonnage:
    def test_meme_image_sur_trois_diapositives_un_seul_visuel(self):
        r = _extract(_deck(*({"title": f"Titre {i}", "pictures": [{"data": P.PHOTO}]}
                             for i in range(3))))
        (asset,) = r.visual_assets
        assert asset.source_locator["slide_number"] == 1
        assert asset.source_locator["slide_numbers"] == [1, 2, 3]
        assert len(asset.occurrences) == 2
        assert all(o.source_page is None and o.source_bbox is None for o in asset.occurrences)
        assert asset.context.nearby_text == "Titre 0", "contexte de la PREMIÈRE occurrence"

    def test_au_dela_du_seuil_de_repetition_decoratif(self):
        slides = [{"title": f"Logo {i}", "pictures": [{"data": P.LOGO}]} for i in range(4)]
        slides.append({"title": "Utile", "pictures": [{"data": P.PHOTO}]})
        r = _extract(_deck(*slides))
        stats = _stats(r)
        assert stats["omitted_decorative"] == 1
        assert [a.source_locator["slide_number"] for a in r.visual_assets] == [5]
        assert stats["parts_decoded"] == 2, "la partie répétée n'est décodée qu'UNE fois"

    def test_image_unique_sans_slide_numbers(self):
        r = _extract(_deck({"title": "Seule", "pictures": [{"data": P.PHOTO}]}))
        assert "slide_numbers" not in r.visual_assets[0].source_locator
        assert r.visual_assets[0].occurrences == ()


# =========================================================================== #
# Provenance et contexte
# =========================================================================== #
class TestProvenance:
    def test_locator_structurel_et_aucune_page(self):
        r = _extract(_deck({"title": "Un", "bullets": ["x"]},
                           {"title": "Deux", "pictures": [{"data": P.PHOTO}]}))
        (asset,) = r.visual_assets
        assert asset.source_page is None
        assert asset.source_bbox is None
        loc = asset.source_locator
        assert set(loc) == {"type", "slide_number", "shape_order", "rel_id", "part", "grouped"}
        assert loc["type"] == "pptx"
        assert loc["slide_number"] == 2, "1-based, ordre réel de la présentation"
        assert isinstance(loc["shape_order"], int) and loc["shape_order"] >= 1
        assert loc["rel_id"].startswith("rId")
        assert loc["part"].startswith("ppt/media/")
        assert loc["grouped"] is False
        assert "page" not in loc and "page_number" not in loc

    def test_locator_et_empreinte_stables(self):
        data = _deck({"title": "A", "pictures": [{"data": P.PHOTO}, {"data": P.AUTRE}]})
        a, b = _extract(data), _extract(data)
        assert [x.source_locator for x in a.visual_assets] == [x.source_locator for x in b.visual_assets]
        assert [x.content_hash for x in a.visual_assets] == [x.content_hash for x in b.visual_assets]
        assert [x.source_locator["shape_order"] for x in a.visual_assets] == sorted(
            x.source_locator["shape_order"] for x in a.visual_assets)

    def test_texte_alternatif_utile_transmis_en_contexte(self):
        r = _extract(_deck({"title": None, "pictures": [
            {"data": P.PHOTO, "alt": "Schéma du réseau électrique"}]}))
        ctx = r.visual_assets[0].context
        assert ctx.alt_text == "Schéma du réseau électrique"
        assert ctx.caption is None
        assert ctx.nearby_text is None

    @pytest.mark.parametrize("bruit", ["Picture 7", "image.png", "Capture d'écran 2024.png",
                                       "Espace réservé 3"])
    def test_texte_alternatif_automatique_ecarte(self, bruit):
        r = _extract(_deck({"title": "T", "pictures": [{"data": P.PHOTO, "alt": bruit}]}))
        assert r.visual_assets[0].context.alt_text is None

    def test_texte_alternatif_desactive_nest_pas_transmis(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_ALT_TEXT = False
        r = _extract(_deck({"title": "T", "pictures": [
            {"data": P.PHOTO, "alt": "Schéma du réseau électrique"}]}))
        assert r.visual_assets[0].context.alt_text is None

    def test_ni_corps_ni_notes_dans_le_contexte(self, settings):
        settings.DOCUMENT_PPTX_INCLUDE_NOTES = True
        r = _extract(_deck({"title": "Titre", "bullets": ["CORPS-SENTINELLE"],
                            "pictures": [{"data": P.PHOTO}]},
                           notes={1: "NOTES-SENTINELLE"}))
        demande = json.dumps(r.visual_assets[0].context.as_request(), ensure_ascii=False)
        assert "CORPS-SENTINELLE" not in demande
        assert "NOTES-SENTINELLE" not in demande
        assert r.visual_assets[0].context.nearby_text == "Titre"

    def test_titre_borne(self):
        titre = "mot " * 300
        r = _extract(_deck({"title": titre, "pictures": [{"data": P.PHOTO}]}))
        borne = r.visual_assets[0].context.as_request()["nearby_text"]
        assert len(borne) <= CONTEXT_MAX_CHARS

    def test_statistiques_bornees_et_sans_contenu(self):
        r = _extract(_deck({"title": "SECRET-TITRE", "pictures": [
            {"data": P.PHOTO, "alt": "SECRET-ALT description"}]}))
        brut = json.dumps(r.metadata["pptx_visual"])
        assert "SECRET" not in brut
        assert all(isinstance(v, int) for v in r.metadata["pptx_visual"].values())

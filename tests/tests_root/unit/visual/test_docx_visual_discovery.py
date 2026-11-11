"""découverte des visuels INTÉGRÉS d'un DOCX.

Ce que ces tests verrouillent, et pourquoi :

* **aucune ouverture de paquet supplémentaire** — l'adaptateur se greffe sur l'archive
  DÉJÀ ouverte et l'arbre DÉJÀ parsé par l'extracteur. L'invariant est EXÉCUTABLE
  (compteur d'ouvertures), pas seulement documenté ;
* **aucun rendu** — ni DOCX→PDF, ni LibreOffice, ni capture de page. Un DOCX n'est pas
  paginé tant qu'il n'est pas rendu, et ce lot ne le rend jamais ;
* **aucune page fabriquée** — `source_page` reste `None` et la provenance est
  STRUCTURELLE. Inventer un numéro de page serait une citation fausse ;
* **le graphe de relations fait foi** — un média orphelin n'est pas un visuel du
  document, et une image LIÉE n'est jamais récupérée (aucun accès réseau ni fichier) ;
* **admission générique** — taille, répétition, plafonds : aucune règle ne connaît de
  vocabulaire de corpus, de nom de fichier ni de marque.

Aucun réseau, aucun appel Gateway : la découverte n'analyse rien.
"""
from __future__ import annotations

import io
import pathlib
import zipfile
from xml.etree import ElementTree as ET

import pytest

from apps.rag_core.services.ingest.extraction import extract_document
from apps.rag_core.services.ingest.extraction.docx_visual import (
    DocxVisualLimits,
    discover_docx_visual_assets,
)
from tests_root.unit.extraction._builders import make_docx, make_docx_with_media

MODULE = (pathlib.Path(__file__).resolve().parents[4]
          / "backend/apps/rag_core/services/ingest/extraction/docx_visual.py")


# --------------------------------------------------------------------------- #
# Fabriques déterministes
# --------------------------------------------------------------------------- #
def _png(w: int, h: int, couleur: tuple, forme: str = "ellipse") -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(img)
    box = [w // 8, h // 8, w - w // 8, h - h // 8]
    (d.ellipse if forme == "ellipse" else d.rectangle)(box, fill=couleur)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PHOTO = _png(200, 160, (230, 120, 30))
AUTRE = _png(180, 150, (40, 150, 70), forme="rect")
ICONE = _png(32, 32, (20, 20, 20), forme="rect")
LOGO = _png(160, 140, (10, 90, 180), forme="rect")


def _code_seul(path) -> str:
    """Code EXÉCUTABLE seul, sans docstrings ni commentaires.

    Le module ÉNONCE les interdictions qu'il respecte (« ni DOCX→PDF, ni LibreOffice »).
    Une interdiction écrite ne doit pas faire échouer le test qui vérifie qu'elle est
    TENUE : c'est le code appelé qui est examiné, pas la prose qui le décrit.
    """
    import ast
    import tokenize

    src = path.read_text(encoding="utf-8")
    sans_commentaires = tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT
    )
    arbre = ast.parse(sans_commentaires)
    for noeud in ast.walk(arbre):
        corps = getattr(noeud, "body", None)
        if (isinstance(noeud, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and corps and isinstance(corps[0], ast.Expr)
                and isinstance(corps[0].value, ast.Constant)
                and isinstance(corps[0].value.value, str)):
            corps.pop(0)
    return ast.unparse(arbre).lower()


def _discover(data: bytes, **kw):
    """Découverte DIRECTE, sur un paquet ouvert et un arbre parsé — comme l'extracteur."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
        return discover_docx_visual_assets(zf, root, **kw)


DOCX_PHOTO = make_docx_with_media(inline_images=(PHOTO,))
DOCX_DEUX = make_docx_with_media(inline_images=(PHOTO, AUTRE))
DOCX_TEXTE_SEUL = make_docx_with_media()


# --------------------------------------------------------------------------- #
# §3 / §64 : une seule ouverture, aucun rendu
# --------------------------------------------------------------------------- #
class TestPasseUnique:
    def test_aucune_ouverture_de_paquet_supplementaire(self, monkeypatch):
        """La découverte réutilise le handle de l'extracteur : elle n'ouvre RIEN de plus.

        La couche de validation ouvre déjà l'archive pour détecter le format et refuser
        les macros : ces ouvertures PRÉEXISTENT au lot et ne lui appartiennent pas. Ce qui
        doit être nul, c'est le DELTA entre rail allumé et rail éteint.
        """
        from apps.rag_core.services.ingest.extraction import docx_extractor

        def _compter(sources):
            n = {"v": 0}
            vraie = zipfile.ZipFile

            def _compte(*a, **k):
                n["v"] += 1
                return vraie(*a, **k)

            with monkeypatch.context() as mp:
                mp.setattr(docx_extractor.zipfile, "ZipFile", _compte)
                return n, extract_document(DOCX_PHOTO, filename="f.docx",
                                           visual_sources=sources)

        eteint, _ = _compter(frozenset())
        allume, r = _compter(frozenset({"docx"}))

        assert allume["v"] == eteint["v"], "aucune ouverture de paquet supplémentaire"
        assert r.metadata["docx_visual"]["package_opens_additional"] == 0
        assert len(r.visual_assets) == 1

    def test_aucun_appel_gateway(self, monkeypatch):
        """La découverte n'analyse rien : le client vision ne doit pas être touché."""
        from apps.llm_router.gateway import vision as vision_mod

        def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
            raise AssertionError("appel réseau interdit pendant la découverte")

        monkeypatch.setattr(vision_mod.requests, "post", _interdit)
        assert _discover(DOCX_PHOTO).stats["admitted"] == 1

    @pytest.mark.parametrize("interdit", ["libreoffice", "soffice", "get_pixmap", "docx2pdf",
                                          "unoconv", "screenshot"])
    def test_aucun_rendu_ni_conversion(self, interdit):
        """Le lot interdit tout rendu : la preuve est dans le CODE, pas dans la prose."""
        assert interdit not in _code_seul(MODULE)



# --------------------------------------------------------------------------- #
# §6 / §57 : provenance STRUCTURELLE, jamais paginée
# --------------------------------------------------------------------------- #
class TestProvenance:
    def test_image_en_ligne(self):
        d = _discover(DOCX_PHOTO)
        assert len(d.assets) == 1
        a = d.assets[0]

        assert a.source_kind == "docx"
        assert a.mime_type == "image/png"
        assert a.origin == "embedded"
        assert a.ordinal == 0
        assert a.task == "describe"

        loc = a.source_locator
        assert loc["type"] == "docx"
        assert loc["story"] == "body", "périmètre visuel = périmètre textuel (corps seul)"
        assert loc["rel_id"].startswith("rId")
        assert loc["part"] == "word/media/image1.png"
        assert loc["block"] == 2, "titre=0, paragraphe=1, image=2"
        assert loc["paragraph"] == 2
        assert loc["anchored"] is False

    def test_aucune_page_n_est_fabriquee(self):
        """Un DOCX n'a pas de page avant rendu : aucun numéro ne doit apparaître."""
        for a in _discover(DOCX_DEUX).assets:
            assert a.source_page is None
            assert a.source_bbox is None
            assert "page" not in a.source_locator
            assert all(o.source_page is None for o in a.occurrences)

    def test_image_dans_une_cellule_de_tableau(self):
        d = _discover(make_docx_with_media(table_images=(PHOTO,)))
        loc = d.assets[0].source_locator
        assert loc["table"] == 0 and loc["row"] == 0 and loc["cell"] == 0
        assert "paragraph" not in loc, "une cellule n'est pas un paragraphe du corps"

    def test_image_flottante(self):
        d = _discover(make_docx_with_media(anchored_images=(PHOTO,)))
        assert d.assets[0].source_locator["anchored"] is True

    def test_empreinte_des_octets_reellement_envoyes(self):
        from apps.rag_core.services.ingest.visual.contracts import sha256_hex

        a = _discover(DOCX_PHOTO).assets[0]
        assert a.content_hash == sha256_hex(a.content)

    def test_decouverte_deterministe(self):
        a = [x.content_hash for x in _discover(DOCX_DEUX).assets]
        b = [x.content_hash for x in _discover(DOCX_DEUX).assets]
        assert a == b and len(a) == 2

    def test_plusieurs_visuels_ordonnes(self):
        d = _discover(DOCX_DEUX)
        assert [x.ordinal for x in d.assets] == [0, 1]
        assert len({x.content_hash for x in d.assets}) == 2


# --------------------------------------------------------------------------- #
# §37-§44 : le graphe de relations fait foi
# --------------------------------------------------------------------------- #
class TestRelations:
    def test_media_orphelin_ignore(self):
        """Un média sans relation n'appartient pas au récit du corps : jamais balayé."""
        d = _discover(make_docx_with_media(orphan_media=PHOTO))
        assert d.assets == []
        assert d.stats["drawings_seen"] == 0

    def test_image_liee_jamais_recuperee(self):
        """`r:link` + TargetMode External : omise, et AUCUN accès réseau ou fichier."""
        d = _discover(make_docx_with_media(linked_image=True))
        assert d.assets == []
        assert d.stats["omitted_external"] == 1

    def test_relation_externe_par_embed(self):
        d = _discover(make_docx_with_media(external_embed=True))
        assert d.assets == []
        assert d.stats["omitted_external"] == 1

    def test_cible_sortant_du_paquet_refusee(self):
        """Une cible qui remonte au-dessus de la racine ne doit jamais être lue."""
        d = _discover(make_docx_with_media(traversal_rel=True))
        assert d.assets == []
        assert d.stats["omitted_unresolved"] == 1

    def test_relation_absente(self):
        d = _discover(make_docx_with_media(broken_rel=True))
        assert d.assets == []
        assert d.stats["omitted_unresolved"] == 1

    def test_media_illisible(self):
        d = _discover(make_docx_with_media(malformed_media=True))
        assert d.assets == []
        assert d.stats["omitted_unreadable"] == 1

    def test_aucune_partie_de_relations(self):
        """`make_docx` n'écrit aucune relation : la découverte reste vide, sans erreur."""
        d = _discover(make_docx())
        assert d.assets == []

    def test_visuel_valide_survit_a_ses_voisins_defectueux(self):
        d = _discover(make_docx_with_media(inline_images=(PHOTO,), broken_rel=True,
                                           malformed_media=True, linked_image=True))
        assert d.stats["admitted"] == 1
        assert d.assets[0].source_locator["part"] == "word/media/image1.png"


# --------------------------------------------------------------------------- #
# §19-§21 : admission générique et bornes
# --------------------------------------------------------------------------- #
class TestAdmission:
    def test_icone_trop_petite_ecartee(self):
        d = _discover(make_docx_with_media(inline_images=(ICONE, PHOTO)))
        assert d.stats["omitted_too_small"] == 1
        assert d.stats["admitted"] == 1

    def test_visuel_repete_est_decoratif(self):
        """Mêmes octets répétés = logo/bandeau. Signal GÉNÉRIQUE, jamais un nom de fichier."""
        d = _discover(make_docx_with_media(repeated_image=(LOGO, 5)))
        assert d.stats["omitted_decorative"] == 1
        assert d.assets == []

    def test_repetition_sous_le_seuil_reste_admise(self):
        d = _discover(make_docx_with_media(repeated_image=(PHOTO, 2)),
                      limits=DocxVisualLimits(max_repeats=3))
        assert d.stats["admitted"] == 1
        assert len(d.assets[0].occurrences) == 1, "l'autre emplacement est conservé"

    def test_plafond_global(self):
        d = _discover(DOCX_DEUX, limits=DocxVisualLimits(max_assets=1))
        assert d.stats["admitted"] == 1
        assert d.stats["omitted_limit"] == 1

    def test_plafond_de_pixels_reduit_au_lieu_decarter(self):
        """Le contrat V1 borne les pixels : on RÉDUIT, on ne perd pas l'évidence."""
        grande = _png(1200, 900, (200, 60, 60))
        d = _discover(make_docx_with_media(inline_images=(grande,)),
                      limits=DocxVisualLimits(max_pixels=100_000))
        assert d.stats["admitted"] == 1
        assert d.assets[0].width * d.assets[0].height <= 100_000
        assert d.stats["omitted_too_large"] == 0

    def test_docx_sans_visuel(self):
        d = _discover(DOCX_TEXTE_SEUL)
        assert d.assets == []
        assert d.stats["drawings_seen"] == 0


# --------------------------------------------------------------------------- #
# §67-§71 : le rail est ADDITIF — porte fermée, DOCX strictement historique
# --------------------------------------------------------------------------- #
class TestRailInerte:
    def test_porte_fermee_aucun_visuel_aucune_metadonnee(self):
        r = extract_document(DOCX_PHOTO, filename="f.docx")
        assert r.visual_assets == []
        assert "docx_visual" not in r.metadata

    def test_source_declaree_meme_porte_fermee(self):
        """La nature de source arme la garde de réindexation, même rail éteint."""
        r = extract_document(DOCX_PHOTO, filename="f.docx")
        assert r.visual_source_kind == "docx"

    def test_texte_identique_rail_allume_ou_eteint(self):
        """Le rail est ADDITIF : il ne déplace aucun offset d'aperçu."""
        doc = make_docx_with_media(paragraphs=("Un paragraphe de corps.",),
                                   inline_images=(PHOTO,), table_images=(AUTRE,))
        eteint = extract_document(doc, filename="f.docx")
        allume = extract_document(doc, filename="f.docx", visual_sources=frozenset({"docx"}))

        assert allume.text == eteint.text
        assert [b.text for b in allume.blocks] == [b.text for b in eteint.blocks]

    def test_version_d_extracteur_inchangee(self):
        """`docx-v1` NE DOIT PAS bouger : le texte produit est identique, les offsets aussi."""
        r = extract_document(DOCX_PHOTO, filename="f.docx",
                             visual_sources=frozenset({"docx"}))
        assert r.metadata["extractor_version"] == "docx-v1"

    def test_statistiques_exposees_quand_le_rail_est_actif(self):
        r = extract_document(DOCX_PHOTO, filename="f.docx",
                             visual_sources=frozenset({"docx"}))
        stats = r.metadata["docx_visual"]
        assert stats["admitted"] == 1 and stats["drawings_seen"] == 1

    def test_autre_source_active_ne_declenche_rien(self):
        """Porte `image` ouverte, `docx` fermée : le DOCX ne découvre rien."""
        r = extract_document(DOCX_PHOTO, filename="f.docx",
                             visual_sources=frozenset({"image", "pdf"}))
        assert r.visual_assets == []
        assert "docx_visual" not in r.metadata

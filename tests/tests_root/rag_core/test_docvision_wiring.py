# -*- coding: utf-8 -*-
"""Câblage réel du rail dans `extract_document` : drapeau, sélecteur, gardes, géométrie.

Ce fichier couvre ce que le rail peut CASSER, pas ce qu'il apporte. Les deux propriétés
les plus importantes sont négatives : éteint il ne touche à rien, et allumé il ne peut
pas faire échouer une extraction qui a réussi.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.ingest.docvision import rail as dv_rail
from apps.rag_core.services.ingest.docvision.selector import (
    mots_distincts, select_raster_extraction, texte_complet,
)
from apps.rag_core.services.ingest.extraction import docvision_hook
from apps.rag_core.services.ingest.extraction.base import (
    Block, BlockType, ExtractionResult,
)

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


TABLE = {"markdown": "| Intitulé | Valeur |\n| --- | --- |\n| Hauteur | 30,0 |"}


# ------------------------------------------------------------------ drapeau OFF
def test_flag_off_rend_le_meme_objet(settings):
    """§8 : sous flag OFF, pas de copie ni de recomposition — le MÊME objet."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    r = ExtractionResult(text="bonjour")
    sortie = docvision_hook.appliquer_si_actif(r, data=b"x", filename="a.pdf",
                                               content_type="application/pdf")
    assert sortie is r
    assert "docvision" not in (sortie.metadata or {})


def test_flag_off_ne_charge_pas_le_rail(settings, monkeypatch):
    """Éteint, le module lourd ne doit même pas être atteint."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    appele = []
    monkeypatch.setattr(dv_rail, "appliquer_rail",
                        lambda *a, **k: appele.append(1) or (None, None))
    r = ExtractionResult(text="x")
    docvision_hook.appliquer_si_actif(r, data=b"", filename="a.pdf")
    assert appele == []


def test_le_rail_ne_casse_jamais_l_extraction(settings, monkeypatch):
    """§55 : une erreur inattendue du rail laisse le résultat historique intact."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True

    def _explose(*a, **k):
        raise RuntimeError("panne interne")

    monkeypatch.setattr(dv_rail, "appliquer_rail", _explose)
    r = ExtractionResult(text="texte historique")
    sortie = docvision_hook.appliquer_si_actif(r, data=b"", filename="a.pdf")
    assert sortie.text == "texte historique"
    assert "erreur inattendue" in sortie.metadata["docvision"]["skipped_reason"]


# ------------------------------------------------------------------- sélecteur
def test_sortie_complete_inclut_les_tables():
    """§11 : comparer Tesseract complet aux seuls blocs MinerU est l'erreur à éviter."""
    c = texte_complet("des blocs", [TABLE])
    assert "des blocs" in c and "Hauteur" in c


def test_selecteur_prefere_mineru_a_vocabulaire_superieur():
    s = select_raster_extraction(
        {"texte": "alpha beta gamma delta", "tables": []},
        {"texte": "alpha beta"}, {"page_number": 1})
    assert s.moteur == "mineru"
    assert s.mesures["mineru_distinct_words"] == 4


def test_selecteur_ne_se_trompe_pas_sur_un_texte_plus_court():
    """Le cas réel : MinerU plus court en caractères mais aussi riche en vocabulaire,
    parce que sa table est structurée. Une règle sur les caractères aurait basculé."""
    mineru = {"texte": "Pour les stocks inventaire tournant", "tables": [TABLE]}
    tess = {"texte": "Pour les stocks inventaire tournant " * 3}
    s = select_raster_extraction(mineru, tess, {"page_number": 1})
    assert s.mesures["mineru_chars"] < s.mesures["tesseract_chars"]
    assert s.moteur == "mineru"


def test_selecteur_egalite_va_a_mineru():
    s = select_raster_extraction({"texte": "alpha beta", "tables": []},
                                 {"texte": "beta alpha"}, {})
    assert s.moteur == "mineru"
    assert "geometrie" in s.raison


def test_selecteur_repli_si_vocabulaire_inferieur():
    s = select_raster_extraction({"texte": "alpha", "tables": []},
                                 {"texte": "alpha beta gamma"}, {})
    assert s.moteur == "tesseract"
    assert s.raison == "vocabulaire distinct inferieur"


def test_selecteur_repli_si_sortie_vide():
    s = select_raster_extraction({"texte": "", "tables": []},
                                 {"texte": "du texte tesseract"}, {})
    assert s.moteur == "tesseract" and s.raison == "sortie moteur vide"


def test_selecteur_repli_sur_erreur_technique():
    s = select_raster_extraction({"erreur": "mineru_timeout"},
                                 {"texte": "du texte"}, {})
    assert s.moteur == "tesseract" and "mineru_timeout" in s.raison


def test_mesures_ne_contiennent_aucun_contenu(settings):
    """§16/§57 : les journaux portent des compteurs, jamais du texte documentaire."""
    s = select_raster_extraction({"texte": "secret medical", "tables": []},
                                 {"texte": "secret medical"}, {"page_number": 4})
    assert "secret" not in repr(s.mesures)
    assert set(s.mesures) >= {"mineru_chars", "tesseract_chars",
                              "mineru_distinct_words", "tesseract_distinct_words"}


def test_mots_distincts_ignore_les_jetons_d_un_caractere():
    assert mots_distincts("a b c alpha") == 1


# ------------------------------------------------------ garde de budget Celery
def test_garde_saute_le_rail_sur_le_chemin_etendu(settings):
    """§31 : 480 s d'extraction étendue + 3 pages à 60 s = 660 s, au-delà du hard
    limit de 600 s. Sur ce chemin, le rail ne s'exécute pas du tout."""
    settings.DOCUMENT_EXTRACTION_EXTENDED_TIMEOUT_SECONDS = 480
    ok, raison, _ = dv_rail._budget_celery_disponible(480, 0.0)
    assert ok is False and "chemin_etendu" in raison


def test_garde_saute_si_le_budget_restant_est_insuffisant(settings):
    settings.CELERY_TASK_SOFT_TIME_LIMIT = 540
    settings.DOCUMENT_MINERU_PAGE_TIMEOUT_S = 60
    ok, raison, pages = dv_rail._budget_celery_disponible(180, 520.0)
    assert ok is False and "insuffisant" in raison and pages == 0


def test_garde_autorise_sur_le_chemin_standard(settings):
    settings.CELERY_TASK_SOFT_TIME_LIMIT = 540
    settings.DOCUMENT_MINERU_PAGE_TIMEOUT_S = 60
    ok, _, pages = dv_rail._budget_celery_disponible(180, 10.0)
    assert ok is True and pages >= 3


def test_garde_reduit_le_nombre_de_pages_selon_le_temps_restant(settings):
    settings.CELERY_TASK_SOFT_TIME_LIMIT = 540
    settings.DOCUMENT_MINERU_PAGE_TIMEOUT_S = 60
    _, _, pages = dv_rail._budget_celery_disponible(180, 420.0)
    assert pages == 2


# ------------------------------------------------------------------ max pages
def test_plafond_de_pages_est_respecte(settings, monkeypatch):
    """§33 : au-delà du plafond, les pages restent sur le chemin historique."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    settings.DOCUMENT_MINERU_MAX_PAGES_PER_DOC = 2
    r = ExtractionResult(text="", pages=[(1, ""), (2, ""), (3, ""), (4, "")])

    class _SidecarMuet:
        def verifier_demarrage(self): raise RuntimeError("absent")

    _, rap = dv_rail.appliquer_rail(r, data=b"", filename="a.pdf",
                                    budget_extraction_s=180, ecoule_extraction_s=1.0,
                                    sidecar=_SidecarMuet())
    assert rap.pages_considered == 4
    assert rap.pages_skipped_max == 2


def test_sidecar_absent_conserve_le_resultat(settings):
    """§35/§46 : sidecar injoignable → chemin historique, jamais de document vide."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    r = ExtractionResult(text="historique", pages=[(1, "")])

    class _SidecarMuet:
        def verifier_demarrage(self): raise ConnectionError("refus")

    sortie, rap = dv_rail.appliquer_rail(r, data=b"", filename="a.pdf",
                                         budget_extraction_s=180,
                                         ecoule_extraction_s=1.0,
                                         sidecar=_SidecarMuet())
    assert sortie.text == "historique"
    assert rap.technical_fallback == 1
    assert "sidecar indisponible" in rap.skipped_reason


def test_page_native_n_est_jamais_routee(settings):
    """§17 : invariant de préservation — la page native ne part pas au moteur."""
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    natif = "Un paragraphe natif parfaitement exploitable et suffisamment long."
    r = ExtractionResult(text=natif, pages=[(1, natif)])

    class _SidecarInterdit:
        def verifier_demarrage(self): raise AssertionError("ne doit pas etre appele")

    sortie, rap = dv_rail.appliquer_rail(r, data=b"", filename="a.pdf",
                                         budget_extraction_s=180,
                                         ecoule_extraction_s=1.0,
                                         sidecar=_SidecarInterdit())
    assert sortie.text == natif
    assert rap.pages_routed == 0
    assert rap.native_chars_preserved == len(natif)


# ------------------------------------------------------------------ géométrie
def test_geometrie_arrive_dans_le_meta_du_chunk():
    """§21/§47 : Block.bbox → ChunkSpec.meta, sans migration."""
    from apps.rag_core.services.ingest.indexing import _attacher_geometrie
    from apps.rag_core.services.chunking import chunk_text_deterministic

    texte = "Villeneuve d'Ascq est mentionnee dans ce paragraphe de test."
    specs = chunk_text_deterministic(texte, source="f.pdf", chunk_size=2500,
                                     overlap=0, page_number=1)
    res = ExtractionResult(text=texte, pages=[(1, texte)], blocks=[
        Block(type=BlockType.PARAGRAPH, text="Villeneuve d'Ascq", page_number=1,
              bbox=(0.1, 0.2, 0.3, 0.4), reading_order=0, source_engine="mineru")])
    _attacher_geometrie(specs, res, 1)
    src = specs[0].meta["source_blocks"]
    assert src[0]["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert src[0]["source_engine"] == "mineru"
    assert specs[0].meta["bbox_space"] == "mediabox"


def test_aucune_bbox_fabriquee_si_le_moteur_n_en_donne_pas():
    """§23 : pas de géométrie inventée."""
    from apps.rag_core.services.ingest.indexing import _attacher_geometrie
    from apps.rag_core.services.chunking import chunk_text_deterministic

    texte = "Un texte sans aucune geometrie disponible."
    specs = chunk_text_deterministic(texte, source="f.pdf", chunk_size=2500,
                                     overlap=0, page_number=1)
    res = ExtractionResult(text=texte, pages=[(1, texte)], blocks=[
        Block(type=BlockType.PARAGRAPH, text="Un texte", page_number=1)])
    _attacher_geometrie(specs, res, 1)
    assert "source_blocks" not in specs[0].meta


def test_chunk_multi_blocs_liste_les_sources_sans_boite_englobante():
    """§24 : plusieurs régions ne se réduisent pas à un rectangle inventé."""
    from apps.rag_core.services.ingest.indexing import _attacher_geometrie
    from apps.rag_core.services.chunking import chunk_text_deterministic

    texte = "Premier fragment. Second fragment."
    specs = chunk_text_deterministic(texte, source="f.pdf", chunk_size=2500,
                                     overlap=0, page_number=1)
    res = ExtractionResult(text=texte, pages=[(1, texte)], blocks=[
        Block(type=BlockType.PARAGRAPH, text="Premier fragment", page_number=1,
              bbox=(0.0, 0.0, 0.5, 0.2), source_engine="mineru"),
        Block(type=BlockType.PARAGRAPH, text="Second fragment", page_number=1,
              bbox=(0.0, 0.6, 0.5, 0.8), source_engine="mineru")])
    _attacher_geometrie(specs, res, 1)
    src = specs[0].meta["source_blocks"]
    assert len(src) == 2
    assert "bbox" not in specs[0].meta      # aucune boîte unique n'est dérivée


# ------------------------------------------------------- allowlist d'hôtes (§28)
def _client(url, settings, hotes=""):
    from apps.rag_core.services.ingest.docvision.client import MineruSidecar
    settings.DOCUMENT_MINERU_SIDECAR_ALLOWED_HOSTS = hotes
    return MineruSidecar(url)


def test_boucle_locale_acceptee(settings):
    assert _client("http://127.0.0.1:16580/v1", settings).url.endswith("/v1")


def test_hote_non_declare_refuse_sans_connexion(settings):
    from apps.rag_core.services.ingest.docvision.client import SidecarRemoteRefused

    with pytest.raises(SidecarRemoteRefused):
        _client("http://mineru.net/v1", settings)


def test_hote_interne_declare_et_prive_accepte(settings, monkeypatch):
    """Le sidecar sur pont Docker `--internal` est joignable par nom de conteneur."""
    import apps.rag_core.services.ingest.docvision.client as cl

    monkeypatch.setattr(cl.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("198.51.100.7", 0))])
    assert _client("http://docvision-mineru:16580/v1", settings,
                   hotes="docvision-mineru").url.startswith("http://docvision")


def test_hote_declare_mais_public_reste_refuse(settings, monkeypatch):
    """Le refus qui compte : un nom déclaré par erreur qui sort du poste.
    Une allowlist par nom seul l'aurait laissé passer."""
    import apps.rag_core.services.ingest.docvision.client as cl
    from apps.rag_core.services.ingest.docvision.client import SidecarRemoteRefused

    monkeypatch.setattr(cl.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    with pytest.raises(SidecarRemoteRefused, match="non privé"):
        _client("http://exfiltration.example:16580/v1", settings,
                hotes="exfiltration.example")


def test_hote_declare_non_resolvable_refuse(settings, monkeypatch):
    """Échec fermé : une résolution impossible ne vaut pas autorisation."""
    import apps.rag_core.services.ingest.docvision.client as cl
    from apps.rag_core.services.ingest.docvision.client import SidecarRemoteRefused

    def _boom(*a, **k):
        raise OSError("pas de DNS")

    monkeypatch.setattr(cl.socket, "getaddrinfo", _boom)
    with pytest.raises(SidecarRemoteRefused):
        _client("http://inconnu:16580/v1", settings, hotes="inconnu")


# ------------------------------------------------------ budget DOCUMENT opposable
def test_budget_document_interrompt_le_routage(settings, monkeypatch):
    """Le budget par page ne borne pas le document : trois pages chacune sous leur
    limite peuvent le dépasser. Un budget seulement déclaré ne borne rien."""
    from apps.rag_core.services.ingest.docvision import config as dv_config
    from apps.rag_core.services.ingest.extraction.base import ExtractionResult

    settings.DOCUMENT_MINERU_HYBRID_ENABLED = True
    settings.DOCUMENT_MINERU_MAX_PAGES_PER_DOC = 5
    monkeypatch.setattr(dv_config, "document_timeout_s", lambda: 0)

    class _Sidecar:
        def __init__(self):
            self.appels = 0

        def verifier_demarrage(self):
            return None

        def parse_middle_json(self, *a, **k):
            self.appels += 1
            return {"middle_json": {"pages": []}, "parser_version": "4.0.3"}

    s = _Sidecar()
    r = ExtractionResult(text="", pages=[(1, ""), (2, ""), (3, "")])
    _, rap = dv_rail.appliquer_rail(r, data=b"", filename="a.pdf",
                                    budget_extraction_s=180, ecoule_extraction_s=1.0,
                                    sidecar=s)
    assert s.appels == 0
    assert rap.pages_skipped_budget == 3
    assert rap.skipped_reason == "budget document epuise"

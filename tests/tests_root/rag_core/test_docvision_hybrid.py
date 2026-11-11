# -*- coding: utf-8 -*-
"""Rail hybride Document Vision — routage, normalisation, repli, drapeau éteint.

Le rail est un ENRICHISSEMENT. Les deux propriétés qui comptent le plus ici sont
négatives : éteint il n'existe pas, et allumé il ne remplace jamais un texte natif
exploitable. Un test qui ne vérifierait que le chemin nominal laisserait passer
exactement la régression que ce rail pourrait causer.
"""
from __future__ import annotations

import json
import types

import pytest

from apps.rag_core.services.ingest.docvision import config as dv_config
from apps.rag_core.services.ingest.docvision import client as dv_client
from apps.rag_core.services.ingest.docvision import normalize as dv_norm
from apps.rag_core.services.ingest.docvision.router import (
    Route, router_image_autonome, router_page, texte_natif_exploitable,
)
from apps.rag_core.services.ingest.extraction.base import Block, BlockType

TEXTE_OK = "Ceci est un paragraphe parfaitement lisible et suffisamment long."
TEXTE_CID = "\x00\x01\x02\x03\x04\x05\x06\x07" * 20


# --------------------------------------------------------------------- drapeau
def test_flag_eteint_par_defaut(settings):
    """Le rail n'est jamais actif par accident."""
    assert dv_config.hybrid_enabled() is False


def test_flag_eteint_ne_route_rien(settings):
    settings.DOCUMENT_MINERU_HYBRID_ENABLED = False
    from apps.rag_core.services.ingest.docvision.router import rail_actif
    assert rail_actif() is False


def test_attribution_licence_fausse_par_defaut(settings):
    """On ne présume pas la conformité : la porte de service est fermée au départ."""
    assert dv_config.attribution_satisfied() is False


# --------------------------------------------------------------------- routage
def test_routage_texte_natif_exploitable():
    d = router_page(3, TEXTE_OK)
    assert d.route is Route.NATIF_ENRICHI
    assert d.page_number == 3
    assert d.caracteres_utiles > 0


def test_routage_texte_natif_absent():
    d = router_page(1, "")
    assert d.route is Route.MINERU_PRIMAIRE
    assert "insuffisant" in d.raison


def test_routage_texte_natif_trop_court():
    d = router_page(1, "abc")
    assert d.route is Route.MINERU_PRIMAIRE


def test_routage_encodage_casse_est_inexploitable():
    """Le cas le plus traître : la page A une couche texte, elle est illisible.

    Un routage fondé sur le seul nombre de caractères la classerait « native » et le
    rail ne serait jamais appelé là où il sert le plus.
    """
    d = router_page(1, TEXTE_CID)
    assert d.route is Route.MINERU_PRIMAIRE
    assert d.texte_inexploitable is True
    assert "encodage" in d.raison


def test_routage_image_autonome():
    d = router_image_autonome()
    assert d.route is Route.MINERU_PRIMAIRE
    assert d.caracteres_utiles == 0


def test_predicat_partage_avec_extraction():
    assert texte_natif_exploitable(TEXTE_OK) is True
    assert texte_natif_exploitable("") is False
    assert texte_natif_exploitable(TEXTE_CID) is False


# ------------------------------------------------------------------ géométrie
@pytest.mark.parametrize(
    "rotation,attendu",
    [(0, [0.1, 0.2, 0.3, 0.4]),
     (90, [0.2, 0.7, 0.4, 0.9]),
     (180, [0.7, 0.6, 0.9, 0.8]),
     (270, [0.6, 0.1, 0.8, 0.3])],
)
def test_bbox_vers_mediabox(rotation, attendu):
    assert dv_norm.vers_mediabox([0.1, 0.2, 0.3, 0.4], rotation) == attendu


def test_bbox_invalide_rendue_absente():
    """Aucune boîte synthétique : une géométrie inutilisable devient None."""
    assert dv_norm.vers_mediabox(None, 0) is None
    assert dv_norm.vers_mediabox([0.1, 0.2], 0) is None


def test_bbox_bornee_a_la_page():
    """Une image en fond perdu déborde du mediabox ; la boîte reste dans [0,1]."""
    b = dv_norm.vers_mediabox([-0.2, -0.1, 1.4, 1.2], 0)
    assert b == [0.0, 0.0, 1.0, 1.0]


# ------------------------------------------------------------------- tableaux
def test_table_html_colspan_replique():
    rows = dv_norm.table_html_en_lignes(
        '<table><tr><td>a</td><td colspan="2">b</td></tr>'
        '<tr><td>1</td><td>2</td><td>3</td></tr></table>')
    assert rows == [["a", "b", "b"], ["1", "2", "3"]]


def test_table_html_rowspan_replique():
    rows = dv_norm.table_html_en_lignes(
        '<table><tr><td rowspan="2">X</td><td>p</td></tr><tr><td>q</td></tr></table>')
    assert rows == [["X", "p"], ["X", "q"]]


def test_table_markdown_rectangulaire():
    md = dv_norm.lignes_en_markdown([["a", "b"], ["1"]])
    assert md.splitlines()[0] == "| a | b |"
    assert md.splitlines()[2] == "| 1 |  |"


# --------------------------------------------------- normalisation middle_json
def _middle(rotation_blocs=None):
    return {
        "schema_version": "4.0",
        "pages": [{"page_idx": 0, "blocks": rotation_blocs or [
            {"type": "header", "index": 0, "bbox": [0.06, 0.05, 0.23, 0.17],
             "content": [{"type": "text", "content": "Villeneuve d'Ascq"}]},
            {"type": "text", "index": 1, "bbox": [0.06, 0.18, 0.23, 0.21],
             "content": [{"type": "text", "content": "Une ville en mouvement"}]},
            {"type": "table", "index": 2, "bbox": [0.1, 0.3, 0.9, 0.6],
             "content": [{"type": "table_body", "index": 0, "bbox": [0.1, 0.3, 0.9, 0.6],
                          "content": "<table><tr><td>Intitulé</td><td>Valeur</td></tr>"
                                     "<tr><td>Hauteur</td><td>30,0</td></tr></table>"}]},
        ]}],
    }


def test_normalisation_produit_blocs_et_tables():
    blocs, tables = dv_norm.blocs_depuis_middle_json(
        _middle(), page_number=1, rotation=0, provenance={"engine": "mineru"})
    assert len(blocs) == 2 and len(tables) == 1
    assert all(b.source_engine == "mineru" for b in blocs)
    assert all(b.bbox is not None for b in blocs)
    assert [b.reading_order for b in blocs] == [0, 1]
    assert blocs[0].type is BlockType.TITLE


def test_table_sortie_du_flux_de_texte():
    """§18 : le contenu d'une table ne doit pas exister deux fois.

    La table devient une unité atomique ; elle ne laisse aucun bloc de texte derrière
    elle, sans quoi ses valeurs seraient indexées une seconde fois dans le texte de page.
    """
    blocs, tables = dv_norm.blocs_depuis_middle_json(
        _middle(), page_number=1, rotation=0, provenance={})
    corpus_blocs = " ".join(b.text for b in blocs)
    assert "30,0" in tables[0]["markdown"]
    assert "30,0" not in corpus_blocs
    assert "Hauteur" not in corpus_blocs


def test_table_porte_sa_geometrie_et_sa_forme():
    _, tables = dv_norm.blocs_depuis_middle_json(
        _middle(), page_number=7, rotation=0, provenance={"engine": "mineru"})
    t = tables[0]
    assert t["meta"]["rows"] == 2 and t["meta"]["columns"] == 2
    assert t["meta"]["bbox"] == [0.1, 0.3, 0.9, 0.6]
    assert t["meta"]["page_number"] == 7
    assert t["meta"]["source_engine"] == "mineru"


def test_provenance_conservee():
    blocs, _ = dv_norm.blocs_depuis_middle_json(
        _middle(), page_number=2, rotation=0,
        provenance={"engine": "mineru", "engine_version": "4.0.3", "tier": "basic"})
    p = blocs[0].provenance
    assert p["engine_version"] == "4.0.3" and p["tier"] == "basic"
    assert p["source_block_index"] == 0
    assert p["bbox_engine"] == [0.06, 0.05, 0.23, 0.17]


def test_rotation_appliquee_aux_blocs():
    blocs, _ = dv_norm.blocs_depuis_middle_json(
        _middle(), page_number=1, rotation=270, provenance={})
    assert blocs[0].bbox == pytest.approx((0.83, 0.06, 0.95, 0.23), abs=1e-2)


# ---------------------------------------------------- contrat rétrocompatible
def test_block_sans_geometrie_inchange():
    assert Block(type=BlockType.PARAGRAPH, text="x").as_dict() == {
        "type": "paragraph", "text": "x"}


def test_block_avec_geometrie():
    d = Block(type=BlockType.TABLE, text="y", bbox=(0.1, 0.2, 0.3, 0.4),
              reading_order=3, source_engine="mineru",
              provenance={"v": "4.0.3"}).as_dict()
    assert d["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert d["reading_order"] == 3 and d["source_engine"] == "mineru"


# ------------------------------------------------------------------- sidecar
def test_sidecar_refuse_une_cible_distante():
    """Échec FERMÉ : aucune connexion n'est même tentée vers un hôte non local."""
    with pytest.raises(dv_client.SidecarRemoteRefused):
        dv_client.MineruSidecar("https://mineru.net/api")
    with pytest.raises(dv_client.SidecarRemoteRefused):
        dv_client.MineruSidecar("http://ai-gateway:8100/v1")


def test_sidecar_accepte_la_boucle_locale():
    assert dv_client.MineruSidecar("http://127.0.0.1:16580/v1").url.endswith("/v1")


def _pret(**controles):
    """Réponse `/ready` de l'enveloppe. Tous les contrôles passent sauf indication.

    L'aptitude se lit sur `/ready` et non sur `/health` : le serveur de MinerU ne
    publie ni son état de télémétrie ni ses modèles. La version précédente de ces
    tests exigeait de `/health` une information que ce composant ne produit pas,
    et le rail n'aurait jamais démarré contre un sidecar réel.
    """
    base = {"engine_process": True, "engine_http": True, "middle_json": True,
            "no_local_source": True, "models_local": True,
            "telemetry_attested": True, "telemetry_component_absent": True}
    base.update(controles)
    return {"ready": all(base.values()), "checks": base, "engine_version": "4.0.3",
            "tier": "basic", "device": "cpu", "attestation": {"mineru_version": "4.0.3"}}


def _sidecar_factice(reponse_ready):
    s = dv_client.MineruSidecar("http://127.0.0.1:16580/v1")
    s._appel = types.MethodType(
        lambda self, chemin, **kw: reponse_ready if chemin == "/ready" else {}, s)
    return s


def test_telemetrie_non_attestee_echoue_ferme():
    """La porte reste fermée tant que la désactivation n'est pas ATTESTÉE : une
    absence de preuve n'est pas une preuve d'absence d'envoi."""
    s = _sidecar_factice(_pret(telemetry_attested=False))
    with pytest.raises(dv_client.SidecarTelemetryRefused):
        s.verifier_demarrage()


def test_composant_de_telemetrie_present_echoue_ferme():
    """doclib est le seul composant qui porte la télémétrie — et il ouvre au passage
    un second serveur de parsing avec `--allow-local-source`. Sa présence suffit à
    refuser."""
    s = _sidecar_factice(_pret(telemetry_component_absent=False))
    with pytest.raises(dv_client.SidecarTelemetryRefused):
        s.verifier_demarrage()


def test_reponse_ready_illisible_echoue_ferme():
    s = _sidecar_factice({"pas": "le contrat attendu"})
    with pytest.raises(dv_client.SidecarError):
        s.verifier_demarrage()


def test_sidecar_apte_ouvre_la_porte():
    d = _sidecar_factice(_pret()).verifier_demarrage()
    assert d["telemetry"] == "disabled"
    assert d["version"] == "4.0.3" and d["device"] == "cpu"


def test_format_indisponible_echoue():
    s = _sidecar_factice(_pret(middle_json=False))
    with pytest.raises(dv_client.SidecarError):
        s.verifier_demarrage()


def test_source_locale_active_refuse_la_porte():
    """Une source `local` signifierait que le moteur peut lire le disque du
    conteneur sur simple requête : ce n'est pas la posture retenue."""
    s = _sidecar_factice(_pret(no_local_source=False))
    with pytest.raises(dv_client.SidecarError):
        s.verifier_demarrage()


def test_sidecar_injoignable_leve_unavailable():
    """Port fermé : le rail doit se replier, jamais bloquer l'ingestion."""
    s = dv_client.MineruSidecar("http://127.0.0.1:1/v1", timeout_s=2)
    with pytest.raises(dv_client.SidecarUnavailable):
        s.sante()


# --------------------------------------------------------------------- fusion
from apps.rag_core.services.ingest.docvision.fusion import (  # noqa: E402
    controle_non_duplication, fusionner_page, texte_document,
)

_TABLE = {"markdown": "| Intitulé | Valeur |\n| --- | --- |\n| Hauteur | 30,0 |",
          "meta": {"rows": 2, "columns": 2, "page_number": 1}}


def _bloc(txt, bbox=(0.1, 0.1, 0.2, 0.2)):
    return Block(type=BlockType.PARAGRAPH, text=txt, bbox=bbox, source_engine="mineru")


def test_page_native_conserve_le_texte_natif():
    """Le cœur du rail : sur une page native, MinerU n'écrit pas une ligne du flux."""
    d = router_page(1, TEXTE_OK)
    r = fusionner_page(d, texte_natif=TEXTE_OK,
                       blocs_mineru=[_bloc("version MinerU appauvrie")],
                       tables_mineru=[_TABLE])
    assert r.texte == TEXTE_OK
    assert r.texte_source == "native"
    assert "appauvrie" not in r.texte
    assert r.stats["native_chars_preserved"] == len(TEXTE_OK)
    assert r.stats["raster_chars_added"] == 0


def test_page_native_importe_les_tables_et_la_geometrie():
    d = router_page(1, TEXTE_OK)
    r = fusionner_page(d, texte_natif=TEXTE_OK,
                       blocs_mineru=[_bloc("x")], tables_mineru=[_TABLE])
    assert len(r.tables) == 1
    assert r.stats["blocks_with_bbox"] == 1


def test_page_native_ignore_un_bloc_mineru_sans_geometrie():
    d = router_page(1, TEXTE_OK)
    r = fusionner_page(d, texte_natif=TEXTE_OK, blocs_mineru=[_bloc("x", bbox=None)])
    assert r.stats["blocks_with_bbox"] == 0
    assert r.blocs == []


def test_page_raster_mineru_primaire():
    d = router_page(1, "")
    r = fusionner_page(d, texte_natif="", blocs_mineru=[_bloc("TEXTE OCRISE")],
                       texte_ocr_repli="version tesseract")
    assert r.texte == "TEXTE OCRISE"
    assert r.texte_source == "mineru"
    assert r.stats["raster_chars_added"] > 0


def test_page_raster_repli_tesseract_si_mineru_vide():
    """MinerU muet : le chemin actuel reprend, sans que rien ne bloque."""
    d = router_page(1, "")
    r = fusionner_page(d, texte_natif="", blocs_mineru=[],
                       texte_ocr_repli="version tesseract")
    assert r.texte == "version tesseract"
    assert r.texte_source == "tesseract_fallback"


def test_page_raster_sans_rien_ne_leve_pas():
    d = router_page(1, "")
    r = fusionner_page(d, texte_natif="", blocs_mineru=[], texte_ocr_repli=None)
    assert r.texte == "" and r.texte_source == "none"


def test_aucune_duplication_de_table():
    """§28 : la valeur d'une table ne doit pas exister aussi dans le flux de page."""
    d = router_page(1, TEXTE_OK)
    r = fusionner_page(d, texte_natif=TEXTE_OK, tables_mineru=[_TABLE])
    ctrl = controle_non_duplication([r])
    assert ctrl["duplicate_table_rows"] == 0
    assert ctrl["pages_double_source"] == 0


def test_table_non_importee_si_deja_dans_le_natif():
    """Un tableau déjà présent dans le texte natif n'est pas importé.

    L'autre issue serait de le retirer du texte natif ; elle violerait la préservation
    intégrale du natif, qui est une porte absolue. On renonce donc à l'import — et le
    renoncement est compté, pour que « MinerU n'apporte rien ici » soit un chiffre et
    non une impression.
    """
    natif = TEXTE_OK + " Intitulé Valeur Hauteur 30,0"
    d = router_page(1, natif)
    r = fusionner_page(d, texte_natif=natif, tables_mineru=[_TABLE])
    assert r.tables == []
    assert r.stats["tables_ecartees_doublon"] == 1
    assert r.texte == natif  # le natif reste intact


def test_table_importee_si_absente_du_natif():
    d = router_page(1, TEXTE_OK)
    r = fusionner_page(d, texte_natif=TEXTE_OK, tables_mineru=[_TABLE])
    assert len(r.tables) == 1
    assert r.stats["tables_ecartees_doublon"] == 0


def test_controle_duplication_sait_dire_non():
    """Le détecteur doit pouvoir signaler un doublon : sinon il ne prouve rien.

    On le nourrit directement, sans passer par la fusion, puisque la fusion empêche
    désormais ce cas de se produire.
    """
    from apps.rag_core.services.ingest.docvision.fusion import ResultatPage
    r = ResultatPage(page_number=1, route="native_enriched",
                     texte="Intitulé Valeur Hauteur 30,0", texte_source="native",
                     tables=[_TABLE])
    assert controle_non_duplication([r])["duplicate_table_rows"] >= 1


def test_texte_document_exclut_les_tables():
    d1 = router_page(1, TEXTE_OK)
    r1 = fusionner_page(d1, texte_natif=TEXTE_OK, tables_mineru=[_TABLE])
    assert "30,0" not in texte_document([r1])


# ------------------------------------------------- pannes forcées (§45, §46)
import http.server  # noqa: E402
import threading  # noqa: E402


class _Serveur(http.server.BaseHTTPRequestHandler):
    """Sidecar factice : répond ce que le scénario demande, sans MinerU."""

    scenario = "lent"

    def log_message(self, *a):  # silence
        pass

    def _json(self, code, corps):
        d = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if self.path.endswith("/ready"):
            return self._json(200, {"ready": True, "engine_version": "4.0.3",
                                    "tier": "basic", "device": "cpu",
                                    "attestation": {"mineru_version": "4.0.3"},
                                    "checks": {"engine_process": True,
                                               "engine_http": True,
                                               "middle_json": True,
                                               "no_local_source": True,
                                               "models_local": True,
                                               "telemetry_attested": True,
                                               "telemetry_component_absent": True}})
        if self.path.endswith("/health"):
            return self._json(200, {"status": "ok", "version": "4.0.3"})
        if "/parse/jobs/" in self.path:
            if _Serveur.scenario == "lent":
                return self._json(200, {"status": "running"})
            return self._json(200, {"status": "completed", "files": [
                {"output_files": {"middle_json": {"file_id": "f1", "bytes": 3}}}]})
        if "/files/" in self.path:
            d = b"ceci n'est pas du json"
            self.send_response(200)
            self.send_header("Content-Length", str(len(d)))
            self.end_headers()
            self.wfile.write(d)
            return
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._json(200, {"job_id": "job_1", "status": "queued"})


@pytest.fixture()
def sidecar_factice():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Serveur)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_timeout_force_leve_sidecar_timeout(sidecar_factice):
    """Le budget par page doit être APPLIQUÉ, pas seulement déclaré : un serveur qui
    répond toujours « running » ne doit pas retenir l'ingestion indéfiniment."""
    _Serveur.scenario = "lent"
    s = dv_client.MineruSidecar(sidecar_factice, timeout_s=2)
    t0 = __import__("time").monotonic()
    with pytest.raises(dv_client.SidecarTimeout):
        s.parse_middle_json("x.pdf", data=b"%PDF-1.4 minimal")
    assert __import__("time").monotonic() - t0 < 15


def test_sortie_invalide_est_signalee(sidecar_factice):
    """Un artefact illisible est une erreur nommée, jamais un document vide silencieux."""
    _Serveur.scenario = "fini"
    s = dv_client.MineruSidecar(sidecar_factice, timeout_s=10)
    with pytest.raises(dv_client.SidecarError) as e:
        s.parse_middle_json("x.pdf", data=b"%PDF-1.4 minimal")
    assert e.value.code == "mineru_invalid_output"


def test_demarrage_ok_sur_sidecar_factice(sidecar_factice):
    s = dv_client.MineruSidecar(sidecar_factice, timeout_s=10)
    assert s.verifier_demarrage()["telemetry"] == "disabled"


def test_document_sans_contenu_refuse(sidecar_factice):
    """La source est EN LIGNE : sans octets, il n'y a rien à transmettre."""
    s = dv_client.MineruSidecar(sidecar_factice, timeout_s=10)
    with pytest.raises(dv_client.SidecarError) as e:
        s.parse_middle_json("x.pdf")
    assert e.value.code == "mineru_no_payload"


def test_document_trop_gros_refuse_avant_emission(settings, sidecar_factice):
    """Le plafond en ligne est opposé AVANT l'envoi : inutile d'encoder 40 Mo en
    base64 pour se les faire refuser à l'autre bout."""
    settings.DOCUMENT_MINERU_MAX_INLINE_BYTES = 1024
    s = dv_client.MineruSidecar(sidecar_factice, timeout_s=10)
    with pytest.raises(dv_client.SidecarError) as e:
        s.parse_middle_json("x.pdf", data=b"x" * 2048)
    assert e.value.code == "mineru_payload_too_large"

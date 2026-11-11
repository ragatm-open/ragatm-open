"""VISUAL-RETRIEVAL-QUALITY-1B — voisinage visuel de page (fonction pure).

Ce que ces tests verrouillent, et pourquoi :

* **pool fermé** — seuls les candidats déjà récupérés peuvent être promus ; aucune
  requête n'est permise (un accès ORM fait échouer le test) ;
* **même document ET même page** — ni un autre document à la même page, ni une autre
  page du même document, ni un passage sans page (DOCX, image, diapositive) ;
* **aucun score** — les dictionnaires candidats ressortent identiques, le pool n'est
  pas réordonné ;
* **déplacement sans duplication**, sans rétrogradation d'un visuel déjà devant ;
* **garde** ``requires_visual_check`` ; plafonds par passage natif et en nombre de
  passages natifs ; longueur de séquence inchangée ; déterminisme.
"""
from __future__ import annotations

import copy

import pytest

from apps.rag_core.services.query import visual_neighbors as V
from apps.rag_core.services.query.visual_neighbors import promote_same_page_visuals

DOC_A = "aaaaaaaa-0000-0000-0000-000000000001"
DOC_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _c(cid, doc=DOC_A, page=22, visual=None, rank=None, text="texte", source_type="workspace_document",
       **extra):
    meta = dict(extra.pop("meta", {}))
    if visual is not None:
        meta["visual"] = visual
    c = {
        "chunk_id": cid,
        "chunk_index": int(cid.split("-")[-1]) if cid.split("-")[-1].isdigit() else 0,
        "text": text,
        "score": 0.5,
        "fusion_score": 1.0 / (60 + (rank or 99)),
        "source_type": source_type,
        "source": {"source_type": source_type, "document_id": doc, "page_number": page,
                   "filename": "x.pdf", "meta": meta},
    }
    if rank is not None:
        c["rerank_rank"] = rank
        c["rerank_score"] = -float(rank)
    c.update(extra)
    return c


def VIS(**kw):
    return {"kind": "photo", "asset_hash": "h" * 64, "requires_visual_check": False, **kw}


def ids(seq):
    return [c["chunk_id"] for c in seq]


@pytest.fixture(autouse=True)
def _aucune_requete(monkeypatch):
    """Pool FERMÉ : toute lecture ORM de chunk ou de document ferait échouer le test."""
    from apps.rag_core.models import Chunk, Document

    def _interdit(*a, **k):  # pragma: no cover - ne doit jamais s'exécuter
        raise AssertionError("aucune requête n'est permise pendant la promotion")

    monkeypatch.setattr(Chunk.objects, "filter", _interdit)
    monkeypatch.setattr(Chunk.objects, "get", _interdit)
    monkeypatch.setattr(Document.objects, "filter", _interdit)


class TestPromotion:
    def test_meme_document_meme_page_devient_adjacent(self):
        nat, b1, b2, vis = (_c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2),
                            _c("b-3", doc=DOC_B, rank=3), _c("vis-4", visual=VIS(), rank=4))
        out, promos = promote_same_page_visuals([nat, b1, b2, vis], [nat, b1, b2, vis])
        assert ids(out) == ["nat-1", "vis-4", "b-2", "b-3"]
        (p,) = promos
        assert (p.native_chunk_id, p.visual_chunk_id, p.document_id, p.page_number) == \
            ("nat-1", "vis-4", DOC_A, 22)
        assert (p.original_position, p.promoted_position) == (4, 2)
        assert p.to_dict()["reason"] == "same_page_visual_neighbor"

    def test_visuel_venu_du_pool_longueur_inchangee(self):
        nat, b1, b2 = _c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2), _c("b-3", doc=DOC_B, rank=3)
        vis = _c("vis-9", visual=VIS(), rank=9)
        out, promos = promote_same_page_visuals([nat, b1, b2], [nat, b1, b2, vis])
        assert ids(out) == ["nat-1", "vis-9", "b-2"], "le dernier passage sort de la séquence"
        assert promos[0].original_position is None

    def test_autre_page_aucune_promotion(self):
        seq = [_c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2), _c("vis-3", page=21, visual=VIS(), rank=3)]
        out, promos = promote_same_page_visuals(seq, seq)
        assert ids(out) == ids(seq) and promos == []

    def test_autre_document_meme_page_aucune_promotion(self):
        seq = [_c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2), _c("visb-3", doc=DOC_B, visual=VIS(), rank=3)]
        seq[1]["source"]["page_number"] = 5
        out, promos = promote_same_page_visuals(seq, seq)
        assert ids(out) == ids(seq) and promos == []

    def test_meme_nom_de_fichier_autre_document_aucune_promotion(self):
        nat = _c("nat-1", rank=1)
        vis = _c("vis-2", doc=DOC_B, visual=VIS(), rank=5)
        assert nat["source"]["filename"] == vis["source"]["filename"]
        out, promos = promote_same_page_visuals([nat, _c("b-3", doc=DOC_B, page=1, rank=2)], [nat, vis])
        assert promos == []

    def test_autre_provenance_aucune_promotion(self):
        nat = _c("nat-1", rank=1)
        vis = _c("corp-2", visual=VIS(), rank=5, source_type="domain_corpus")
        out, promos = promote_same_page_visuals([nat], [nat, vis])
        assert promos == [] and ids(out) == ["nat-1"]

    def test_visuel_absent_du_pool_rien_ne_change(self):
        seq = [_c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2)]
        out, promos = promote_same_page_visuals(seq, list(seq))
        assert ids(out) == ids(seq) and promos == []

    def test_visuel_deja_retenu_deplace_sans_duplication(self):
        nat, b, vis = _c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2), _c("vis-3", visual=VIS(), rank=3)
        out, _ = promote_same_page_visuals([nat, b, vis], [nat, b, vis])
        assert ids(out) == ["nat-1", "vis-3", "b-2"]
        assert len(ids(out)) == len(set(ids(out)))

    def test_visuel_deja_devant_son_natif_non_retrograde(self):
        vis, nat, b = _c("vis-1", visual=VIS(), rank=1), _c("nat-2", rank=2), _c("b-3", doc=DOC_B, rank=3)
        out, promos = promote_same_page_visuals([vis, nat, b], [vis, nat, b])
        assert ids(out) == ["vis-1", "nat-2", "b-3"] and promos == []

    @pytest.mark.parametrize("cap,attendu", [(1, ["nat-1", "v-2"]), (2, ["nat-1", "v-2", "v-3"])])
    def test_plusieurs_visuels_meme_page_plafond_et_ordre_rerank(self, cap, attendu):
        nat = _c("nat-1", rank=1)
        pool = [nat, _c("v-4", visual=VIS(), rank=4), _c("v-2", visual=VIS(), rank=2),
                _c("v-3", visual=VIS(), rank=3)]
        tail = [_c(f"b-{i}", doc=DOC_B, page=i, rank=10 + i) for i in range(5, 8)]
        out, promos = promote_same_page_visuals([nat] + tail, pool + tail, max_per_native=cap)
        assert ids(out)[:len(attendu)] == attendu
        assert len(promos) == cap

    def test_requires_visual_check_jamais_promu(self):
        nat = _c("nat-1", rank=1)
        doute = _c("vis-2", visual=VIS(requires_visual_check=True), rank=2)
        out, promos = promote_same_page_visuals([nat, _c("b-3", doc=DOC_B, rank=3)], [nat, doute])
        assert promos == []

    def test_visuel_sans_texte_jamais_promu(self):
        nat = _c("nat-1", rank=1)
        vide = _c("vis-2", visual=VIS(), rank=2, text="   ")
        assert promote_same_page_visuals([nat], [nat, vide])[1] == []

    @pytest.mark.parametrize("np,vp", [(None, 22), (22, None), (None, None)])
    def test_page_absente_aucun_effet(self, np, vp):
        nat = _c("nat-1", page=np, rank=1)
        vis = _c("vis-2", page=vp, visual=VIS(), rank=2)
        seq = [nat, _c("b-3", doc=DOC_B, rank=3)]
        out, promos = promote_same_page_visuals(seq, seq + [vis])
        assert ids(out) == ids(seq) and promos == []

    def test_page_non_entiere_ignoree(self):
        nat = _c("nat-1", page="22", rank=1)
        vis = _c("vis-2", page="22", visual=VIS(), rank=2)
        assert promote_same_page_visuals([nat], [nat, vis])[1] == []

    def test_diapositive_pptx_non_traitee_comme_page(self):
        nat = _c("s-1", page=None, rank=1, meta={"slide_number": 3})
        vis = _c("s-2", page=None, visual=VIS(), rank=2, meta={"slide_number": 3})
        assert promote_same_page_visuals([nat], [nat, vis])[1] == []

    def test_limite_de_passages_natifs(self):
        natifs = [_c(f"nat-{p}", page=p, rank=p) for p in range(1, 6)]
        visuels = [_c(f"vis-{p}", page=p, visual=VIS(), rank=20 + p) for p in range(1, 6)]
        out, promos = promote_same_page_visuals(natifs, natifs + visuels, max_natives=2)
        assert [p.native_chunk_id for p in promos] == ["nat-1", "nat-2"]
        assert ids(out) == ["nat-1", "vis-1", "nat-2", "vis-2", "nat-3"], "nat-3 hors limite : aucun voisin"
        assert len(out) == len(natifs)

    def test_promotion_tronquee_non_consignee(self):
        """Un voisin poussé hors de la séquence (longueur constante) n'est pas déclaré promu."""
        natifs = [_c(f"nat-{p}", page=p, rank=p) for p in range(1, 4)]
        visuels = [_c(f"vis-{p}", page=p, visual=VIS(), rank=20 + p) for p in range(1, 4)]
        out, promos = promote_same_page_visuals(natifs, natifs + visuels, max_natives=3)
        assert ids(out) == ["nat-1", "vis-1", "nat-2"]
        assert [p.visual_chunk_id for p in promos] == ["vis-1"]

    def test_un_visuel_n_est_rattache_qu_une_fois(self):
        n1, n2 = _c("nat-1", rank=1), _c("nat-2", rank=2)
        vis = _c("vis-3", visual=VIS(), rank=3)
        out, promos = promote_same_page_visuals([n1, n2], [n1, n2, vis])
        assert [p.native_chunk_id for p in promos] == ["nat-1"]
        assert ids(out) == ["nat-1", "vis-3"]


class TestInvariants:
    def test_sans_visuel_ordre_strictement_identique(self):
        seq = [_c(f"t-{i}", doc=DOC_A if i % 2 else DOC_B, page=i, rank=i) for i in range(1, 9)]
        before = copy.deepcopy(seq)
        out, promos = promote_same_page_visuals(seq, seq)
        assert out == before and promos == []

    def test_aucune_mutation_ni_score_modifie(self):
        nat, b, vis = _c("nat-1", rank=1), _c("b-2", doc=DOC_B, rank=2), _c("vis-5", visual=VIS(), rank=5)
        selected, pool = [nat, b], [b, vis, nat]
        snap_sel, snap_pool = copy.deepcopy(selected), copy.deepcopy(pool)
        out, _ = promote_same_page_visuals(selected, pool)
        assert selected == snap_sel and pool == snap_pool, "listes et dictionnaires intacts"
        assert out[1] is vis and out[1] == snap_pool[1], "aucun champ (score compris) ajouté ni modifié"

    def test_determinisme(self):
        nat = _c("nat-1", rank=1)
        pool = [nat] + [_c(f"v-{i}", visual=VIS()) for i in (7, 3, 5)]   # sans rerank : ex æquo
        runs = {tuple(ids(promote_same_page_visuals([nat, _c("b-2", doc=DOC_B)], list(p))[0]))
                for p in (pool, list(reversed(pool)), pool[1:] + pool[:1])}
        assert len(runs) == 1

    def test_visuel_non_pertinent_meme_page_promu_comportement_fige(self):
        """Risque connu et assumé : un visuel mal classé mais sûr est tout de même promu."""
        nat = _c("nat-1", rank=1)
        faible = _c("vis-2", visual=VIS(), rank=40)
        out, promos = promote_same_page_visuals([nat, _c("b-3", doc=DOC_B, rank=2)], [nat, faible])
        assert ids(out) == ["nat-1", "vis-2"] and len(promos) == 1

    def test_entrees_vides_ou_plafonds_nuls(self):
        assert promote_same_page_visuals([], []) == ([], [])
        nat, vis = _c("nat-1", rank=1), _c("vis-2", visual=VIS(), rank=2)
        assert promote_same_page_visuals([nat], [nat, vis], max_natives=0)[1] == []
        assert promote_same_page_visuals([nat], [nat, vis], max_per_native=0)[1] == []

    def test_constantes_qualifiees(self):
        assert V.MAX_VISUAL_NEIGHBORS_PER_NATIVE == 1
        assert V.MAX_NATIVE_NEIGHBOR_EXPANSIONS >= 1

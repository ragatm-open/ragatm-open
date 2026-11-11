"""Identité d'embedding par magasin — dimension RÉELLE.

Le point vérifié : la dimension inscrite est celle du vecteur **obtenu**, jamais une
constante de configuration. Un provider peut rendre une dimension autre que celle
demandée ; une identité d'embedding qui recopierait la valeur réclamée masquerait
exactement l'écart qu'elle devrait rendre visible.
"""

from __future__ import annotations

import pytest

from apps.rag_core.services.query import candidate_trace as CT
from apps.rag_core.services.query.trace import CorpusLegRecorder


@pytest.fixture
def recorder():
    r = CorpusLegRecorder()
    r.note_embedding_groups([("openai", "text-embedding-3-small", ["c1", "c2"]),
                             ("ollama", "nomic-embed-text", ["c3"])])
    return r


class TestDimensionDuGroupeCorpus:
    def test_la_dimension_se_pose_sur_le_bon_groupe(self, recorder):
        recorder.note_embedding_dimension("openai", "text-embedding-3-small", 768)
        par_modele = {g["model"]: g for g in recorder.embedding_groups}
        assert par_modele["text-embedding-3-small"]["dimension"] == 768
        assert "dimension" not in par_modele["nomic-embed-text"]

    def test_chaque_groupe_garde_la_sienne(self, recorder):
        recorder.note_embedding_dimension("openai", "text-embedding-3-small", 768)
        recorder.note_embedding_dimension("ollama", "nomic-embed-text", 384)
        assert [g.get("dimension") for g in recorder.embedding_groups] == [768, 384]

    def test_un_couple_inconnu_ne_pose_rien(self, recorder):
        recorder.note_embedding_dimension("autre", "modele-x", 1536)
        assert all("dimension" not in g for g in recorder.embedding_groups)

    @pytest.mark.parametrize("valeur", [None, 0, -1, "x", float("nan")])
    def test_une_dimension_invalide_est_ecartee(self, recorder, valeur):
        """Mieux vaut absente que fausse : un 0 se lirait comme une dimension nulle."""
        recorder.note_embedding_dimension("openai", "text-embedding-3-small", valeur)
        assert all("dimension" not in g for g in recorder.embedding_groups)

    def test_sans_encodage_aucune_dimension(self, recorder):
        """Leg vectoriel non exécuté : aucune question encodée, donc rien d'observé."""
        assert all("dimension" not in g for g in recorder.embedding_groups)


class TestIdentiteCompleteDansLaTrace:
    def _candidat(self, cid, store):
        return {"chunk_id": cid, "document_id": "d1", "source_type": store,
                "text": "t", "text_rank": 1, "text_score": 0.04,
                "vector_rank": 1, "vector_score": 0.8, "fusion_score": 0.03}

    def _construire(self, identites):
        pool = [self._candidat("p1", "workspace_document"),
                self._candidat("b1", "domain_corpus")]
        apres = [dict(c, rerank_rank=i + 1, rerank_score=5.0 - i) for i, c in enumerate(pool)]
        return CT.construire(
            pool_avant=pool, pool_apres=apres,
            info_rerank={"attempted": True, "success": True, "documents": 2},
            reranker_disponible=True, identites=identites,
        )["identities"]["embedding_by_store"]

    def test_le_corpus_metier_porte_sa_dimension(self):
        e = self._construire({"embedding_by_store": {
            CT.STORE_METIER: {"provider": "openai", "model": "text-embedding-3-small",
                              "dimension": 768}}})
        assert e[CT.STORE_METIER]["dimension"] == 768

    def test_les_deux_magasins_portent_leur_dimension(self):
        e = self._construire({"embedding_by_store": {
            CT.STORE_PRIVE: {"provider": "ai_gateway", "model": "fr-en-retrieval",
                             "pipeline_family": "fr-en-retrieval@1a37926b", "dimension": 768},
            CT.STORE_METIER: {"provider": "openai", "model": "text-embedding-3-small",
                              "dimension": 1536}}})
        assert e[CT.STORE_PRIVE]["dimension"] == 768
        assert e[CT.STORE_METIER]["dimension"] == 1536
        assert e[CT.STORE_PRIVE]["dimension"] != e[CT.STORE_METIER]["dimension"]

    def test_pipeline_family_reste_absent_cote_metier(self):
        """Notion propre aux espaces privés : absente, et non nulle."""
        e = self._construire({"embedding_by_store": {
            CT.STORE_METIER: {"provider": "openai", "model": "text-embedding-3-small",
                              "dimension": 768}}})
        assert "pipeline_family" not in e[CT.STORE_METIER]

    def test_une_dimension_absente_ne_cree_pas_de_cle(self):
        e = self._construire({"embedding_by_store": {
            CT.STORE_METIER: {"provider": "openai", "model": "text-embedding-3-small"}}})
        assert "dimension" not in e[CT.STORE_METIER]


class TestLaDimensionVientDuVecteurObtenu:
    def test_le_recorder_enregistre_la_longueur_transmise(self):
        """Le service corpus transmet ``len(emb)``. Si un provider rend 1536 là où
        768 étaient demandés, c'est 1536 qui doit apparaître."""
        r = CorpusLegRecorder()
        r.note_embedding_groups([("openai", "text-embedding-3-large", ["c1"])])
        vecteur_obtenu = [0.0] * 1536          # le provider a rendu autre chose
        r.note_embedding_dimension("openai", "text-embedding-3-large", len(vecteur_obtenu))
        assert r.embedding_groups[0]["dimension"] == 1536

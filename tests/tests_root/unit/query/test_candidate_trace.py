"""Trace des candidats — schéma v1.

Ce que ces tests verrouillent, et pourquoi :

* les **rangs** sont attachés à un identifiant, jamais à une position supposée —
  c'est l'erreur qui rendrait la trace pire qu'absente ;
* les **échelles de score restent séparées** : un `ts_rank` et un logit de
  cross-encoder dans le même champ produiraient des comparaisons fausses ;
* un score **absent vaut `None`, jamais 0** — un 0 se lirait comme un jugement de
  non-pertinence ;
* **aucun texte métier** n'entre dans la trace.
"""

from __future__ import annotations

import json

import pytest

from apps.rag_core.services.query import candidate_trace as CT


def _candidat(
    cid, *, document="doc-1", store="workspace_document",
    text_rank=None, text_score=None, vector_rank=None, vector_score=None,
    fusion=None, rerank_score=None, rerank_rank=None, famille=None, texte="texte confidentiel",
):
    c = {
        "chunk_id": cid, "document_id": document, "source_type": store, "text": texte,
        "text_rank": text_rank, "vector_rank": vector_rank,
    }
    if text_score is not None:
        c["text_score"] = text_score
    if vector_score is not None:
        c["vector_score"] = vector_score
    if fusion is not None:
        c["fusion_score"] = fusion
    if rerank_score is not None:
        c["rerank_score"] = rerank_score
    if rerank_rank is not None:
        c["rerank_rank"] = rerank_rank
    if famille:
        c["pipeline_family"] = famille
    return c


def _succes(documents=3, **extra):
    info = {"attempted": True, "success": True, "fallback": False,
            "documents": documents, "model_logical": "rerank-default",
            "model_revision": "rev-abc", "pool_k": 40}
    info.update(extra)
    return info


class TestSchemaV1:
    def test_la_version_est_explicite(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True)
        assert t["version"] == CT.SCHEMA_VERSION == 1

    def test_les_champs_de_requete_sont_presents(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True)
        for cle in ("version", "ranking_mode", "reranker_used", "fallback_used",
                    "pool_size", "candidates"):
            assert cle in t, cle

    def test_chaque_candidat_porte_le_contrat_complet(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True)
        attendus = {
            "candidate_id", "chunk_id", "document_id", "source_store", "source_methods",
            "pre_rerank_rank", "post_rerank_rank", "reranker_score",
            "vector_score", "lexical_score", "fusion_score", "submitted_to_reranker",
        }
        assert attendus <= set(t["candidates"][0])

    def test_un_candidat_sans_identifiant_est_ecarte(self):
        """Une ligne sans `chunk_id` ne peut être rattachée à rien : la garder
        décalerait tous les rangs suivants."""
        t = CT.construire(pool_avant=[_candidat(""), _candidat("b")],
                          pool_apres=[_candidat("b")], info_rerank=_succes(1),
                          reranker_disponible=True)
        assert [c["chunk_id"] for c in t["candidates"]] == ["b"]

    def test_le_volume_est_borne(self):
        pool = [_candidat(f"c{i}") for i in range(CT.MAX_CANDIDATS_TRACES + 25)]
        t = CT.construire(pool_avant=pool, pool_apres=pool, info_rerank=_succes(),
                          reranker_disponible=True)
        assert len(t["candidates"]) == CT.MAX_CANDIDATS_TRACES
        assert t["truncated"] is True


class TestRangs:
    """Le scénario du lot : pré A,B,C → post C,A,B."""

    def _trace(self):
        avant = [_candidat("A"), _candidat("B"), _candidat("C")]
        apres = [
            _candidat("C", rerank_score=9.0, rerank_rank=1),
            _candidat("A", rerank_score=5.0, rerank_rank=2),
            _candidat("B", rerank_score=1.0, rerank_rank=3),
        ]
        return CT.construire(pool_avant=avant, pool_apres=apres,
                             info_rerank=_succes(3), reranker_disponible=True)

    @pytest.mark.parametrize(("cid", "pre", "post"), [("A", 1, 2), ("B", 2, 3), ("C", 3, 1)])
    def test_les_rangs_avant_et_apres_sont_exacts(self, cid, pre, post):
        par_id = {c["chunk_id"]: c for c in self._trace()["candidates"]}
        assert (par_id[cid]["pre_rerank_rank"], par_id[cid]["post_rerank_rank"]) == (pre, post)

    def test_le_score_du_reranker_suit_son_candidat(self):
        """Association par identifiant : une association par position se tromperait
        exactement ici, puisque l'ordre a changé."""
        par_id = {c["chunk_id"]: c for c in self._trace()["candidates"]}
        assert par_id["C"]["reranker_score"] == 9.0
        assert par_id["A"]["reranker_score"] == 5.0
        assert par_id["B"]["reranker_score"] == 1.0

    def test_le_rang_avant_suit_l_ordre_recu(self):
        t = self._trace()
        assert [c["pre_rerank_rank"] for c in t["candidates"]] == [1, 2, 3]


class TestMagasins:
    def test_le_magasin_est_lu_et_non_deduit(self):
        avant = [
            _candidat("B1", store="domain_corpus"),
            _candidat("P2", store="workspace_document"),
            _candidat("P1", store="workspace_document"),
            _candidat("B2", store="domain_corpus"),
        ]
        apres = [dict(c, rerank_rank=i + 1, rerank_score=10.0 - i)
                 for i, c in enumerate(avant)]
        t = CT.construire(pool_avant=avant, pool_apres=apres,
                          info_rerank=_succes(4), reranker_disponible=True)
        lu = {c["chunk_id"]: c["source_store"] for c in t["candidates"]}
        assert lu == {"B1": CT.STORE_METIER, "P2": CT.STORE_PRIVE,
                      "P1": CT.STORE_PRIVE, "B2": CT.STORE_METIER}

    def test_le_magasin_se_lit_aussi_dans_la_provenance_imbriquee(self):
        c = {"chunk_id": "x", "source": {"source_type": "domain_corpus"}}
        assert CT.magasin_du_candidat(c) == CT.STORE_METIER

    def test_un_magasin_non_marque_est_declare_inconnu(self):
        """Ne pas deviner. `unknown` est une information ; un `private` inventé
        serait une erreur silencieuse."""
        assert CT.magasin_du_candidat({"chunk_id": "x"}) == CT.STORE_INCONNU

    def test_les_familles_d_embedding_sont_ventilees_par_magasin(self):
        avant = [
            _candidat("P1", store="workspace_document", famille="fr-en-retrieval@1a37926b"),
            _candidat("B1", store="domain_corpus", famille="text-embedding-3-small"),
        ]
        t = CT.construire(pool_avant=avant, pool_apres=avant, info_rerank=_succes(2),
                          reranker_disponible=True)
        assert t["identities"]["embedding_by_store"] == {
            CT.STORE_METIER: {"pipeline_family_observee": ["text-embedding-3-small"]},
            CT.STORE_PRIVE: {"pipeline_family_observee": ["fr-en-retrieval@1a37926b"]},
        }


class TestIdentitesParMagasin:
    """l'identité fournie par le pipeline prime sur toute dérivation."""

    def _fournie(self):
        return {"embedding_by_store": {
            CT.STORE_PRIVE: {"provider": "ai_gateway", "model": "fr-en-retrieval",
                             "pipeline_family": "fr-en-retrieval@1a37926b", "dimension": 768},
            CT.STORE_METIER: {"provider": "openai", "model": "text-embedding-3-small"},
        }}

    def test_l_identite_fournie_est_conservee_telle_quelle(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True,
                          identites=self._fournie())
        e = t["identities"]["embedding_by_store"]
        assert e[CT.STORE_PRIVE]["provider"] == "ai_gateway"
        assert e[CT.STORE_PRIVE]["pipeline_family"] == "fr-en-retrieval@1a37926b"
        assert e[CT.STORE_PRIVE]["dimension"] == 768
        assert e[CT.STORE_METIER]["model"] == "text-embedding-3-small"

    def test_les_deux_magasins_restent_distincts(self):
        e = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True,
                          identites=self._fournie())["identities"]["embedding_by_store"]
        assert e[CT.STORE_PRIVE] != e[CT.STORE_METIER]
        assert e[CT.STORE_PRIVE]["model"] != e[CT.STORE_METIER]["model"]

    def test_la_derivation_n_ecrase_jamais_l_identite_fournie(self):
        """Le candidat porte une famille CONTRADICTOIRE : la valeur du pipeline gagne."""
        pool = [_candidat("a", store="workspace_document", famille="AUTRE-FAMILLE")]
        e = CT.construire(pool_avant=pool, pool_apres=pool, info_rerank=_succes(1),
                          reranker_disponible=True, identites=self._fournie(),
                          )["identities"]["embedding_by_store"]
        assert e[CT.STORE_PRIVE]["pipeline_family"] == "fr-en-retrieval@1a37926b"
        assert "pipeline_family_observee" not in e[CT.STORE_PRIVE]

    def test_la_derivation_complete_un_magasin_absent(self):
        pool = [_candidat("b", store="domain_corpus", famille="nomic-embed-text")]
        fournie = {"embedding_by_store": {
            CT.STORE_PRIVE: {"provider": "ai_gateway", "model": "fr-en-retrieval"}}}
        e = CT.construire(pool_avant=pool, pool_apres=pool, info_rerank=_succes(1),
                          reranker_disponible=True, identites=fournie,
                          )["identities"]["embedding_by_store"]
        assert e[CT.STORE_PRIVE]["provider"] == "ai_gateway"
        assert e[CT.STORE_METIER] == {"pipeline_family_observee": ["nomic-embed-text"]}

    def test_aucune_identite_aucune_cle_inventee(self):
        """Rien de connu : la clé est absente, jamais remplie d'un placeholder."""
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True)
        assert "embedding_by_store" not in (t.get("identities") or {})

    def test_l_identite_reste_au_niveau_requete(self):
        t = CT.construire(pool_avant=[_candidat("a"), _candidat("b")],
                          pool_apres=[_candidat("a"), _candidat("b")],
                          info_rerank=_succes(2), reranker_disponible=True,
                          identites=self._fournie())
        for c in t["candidates"]:
            assert "provider" not in c and "embedding_by_store" not in c


class TestMethodesDeRetrieval:
    def test_un_candidat_trouve_par_les_deux_voies_les_conserve(self):
        c = _candidat("a", text_rank=3, text_score=0.04, vector_rank=1, vector_score=0.81)
        t = CT.construire(pool_avant=[c], pool_apres=[c], info_rerank=_succes(1),
                          reranker_disponible=True)
        entree = t["candidates"][0]
        assert entree["source_methods"] == [CT.METHODE_LEXICALE, CT.METHODE_VECTORIELLE]
        assert entree["lexical_score"] == 0.04
        assert entree["vector_score"] == 0.81

    def test_un_seul_candidat_final_sans_duplication(self):
        c = _candidat("a", text_rank=1, text_score=0.04, vector_rank=2, vector_score=0.7)
        t = CT.construire(pool_avant=[c], pool_apres=[c], info_rerank=_succes(1),
                          reranker_disponible=True)
        assert len(t["candidates"]) == 1

    @pytest.mark.parametrize(
        ("kwargs", "attendu"),
        [
            ({"text_rank": 1}, [CT.METHODE_LEXICALE]),
            ({"vector_rank": 1}, [CT.METHODE_VECTORIELLE]),
            ({}, []),
        ],
    )
    def test_les_origines_reflettent_les_rangs_de_leg(self, kwargs, attendu):
        assert CT.methodes_du_candidat(_candidat("a", **kwargs)) == attendu


class TestScoresSeparesEtAbsents:
    def test_un_score_absent_vaut_none_et_jamais_zero(self):
        """`merge_rrf` initialise `text_score` à 0.0 pour TOUS les candidats. Sans
        le rang de leg comme discriminant, un candidat jamais trouvé lexicalement
        se lirait « trouvé, pertinence nulle »."""
        c = _candidat("a", text_score=0.0, vector_rank=1, vector_score=0.8)
        t = CT.construire(pool_avant=[c], pool_apres=[c], info_rerank=_succes(1),
                          reranker_disponible=True)
        entree = t["candidates"][0]
        assert entree["lexical_score"] is None
        assert entree["vector_score"] == 0.8

    def test_les_quatre_echelles_restent_dans_des_champs_distincts(self):
        c = _candidat("a", text_rank=1, text_score=0.04, vector_rank=1, vector_score=0.81,
                      fusion=0.032, rerank_score=7.5, rerank_rank=1)
        t = CT.construire(pool_avant=[c], pool_apres=[c], info_rerank=_succes(1),
                          reranker_disponible=True)
        e = t["candidates"][0]
        assert (e["lexical_score"], e["vector_score"], e["fusion_score"], e["reranker_score"]) \
            == (0.04, 0.81, 0.032, 7.5)

    @pytest.mark.parametrize("valeur", [None, float("nan"), float("inf"), "x", True])
    def test_une_valeur_non_finie_ne_devient_pas_un_score(self, valeur):
        assert CT._nombre_ou_none(valeur) is None


class TestFallbackEtModes:
    def test_le_repli_est_explicite_et_sans_score_de_reranker(self):
        avant = [_candidat("a"), _candidat("b")]
        info = {"attempted": True, "success": False, "fallback": True,
                "documents": 2, "error_kind": "timeout"}
        t = CT.construire(pool_avant=avant, pool_apres=avant, info_rerank=info,
                          reranker_disponible=True)
        assert t["ranking_mode"] == CT.MODE_FALLBACK_RRF
        assert t["reranker_used"] is False
        assert t["fallback_used"] is True
        assert t["failure_kind"] == "timeout"
        assert all(c["reranker_score"] is None for c in t["candidates"])

    def test_un_candidat_non_reranke_ne_recoit_jamais_un_faux_score(self):
        """Même si le dict porte un `rerank_score` résiduel."""
        avant = [_candidat("a", rerank_score=3.0, rerank_rank=1)]
        t = CT.construire(pool_avant=avant, pool_apres=avant,
                          info_rerank={"fallback": True}, reranker_disponible=True)
        assert t["candidates"][0]["reranker_score"] is None
        assert t["candidates"][0]["post_rerank_rank"] is None

    def test_le_reranker_eteint_est_distingue_du_repli(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank={}, reranker_disponible=False)
        assert t["ranking_mode"] == CT.MODE_RERANKER_INDISPONIBLE
        assert t["fallback_used"] is False

    def test_l_absence_de_candidat_est_un_etat_nomme(self):
        t = CT.construire(pool_avant=[], pool_apres=[], info_rerank={},
                          reranker_disponible=True)
        assert t["ranking_mode"] == CT.MODE_SANS_CANDIDAT
        assert t["pool_size"] == 0

    def test_l_ordre_non_canonique_est_declare(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True,
                          ordre_canonique=False)
        assert t["pre_rerank_order_canonical"] is False

    def test_la_queue_non_soumise_est_distinguee(self):
        avant = [_candidat(f"c{i}") for i in range(5)]
        t = CT.construire(pool_avant=avant, pool_apres=avant, info_rerank=_succes(3),
                          reranker_disponible=True)
        soumis = [c["submitted_to_reranker"] for c in t["candidates"]]
        assert soumis == [True, True, True, False, False]


class TestIdentitesTechniques:
    def test_l_identite_du_reranker_est_au_niveau_requete(self):
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1), reranker_disponible=True)
        assert t["identities"]["reranker"] == {
            "model_logical": "rerank-default", "model_revision": "rev-abc", "pool_k": 40}

    def test_elle_n_est_pas_dupliquee_sur_chaque_candidat(self):
        t = CT.construire(pool_avant=[_candidat("a"), _candidat("b")],
                          pool_apres=[_candidat("a"), _candidat("b")],
                          info_rerank=_succes(2), reranker_disponible=True)
        for c in t["candidates"]:
            assert "model_logical" not in c and "reranker" not in c


class TestConfidentialite:
    """Aucun texte métier, à aucune profondeur."""

    def _trace_riche(self):
        avant = [
            _candidat("a", text_rank=1, text_score=0.04, vector_rank=2, vector_score=0.8,
                      fusion=0.03, famille="fr-en", texte="SECRET METIER ALPHA"),
            _candidat("b", store="domain_corpus", vector_rank=1, vector_score=0.9,
                      texte="SECRET METIER BETA"),
        ]
        apres = [dict(c, rerank_rank=i + 1, rerank_score=5.0 - i) for i, c in enumerate(avant)]
        return CT.construire(pool_avant=avant, pool_apres=apres, info_rerank=_succes(2),
                             reranker_disponible=True)

    def test_aucun_texte_de_passage_ne_fuit(self):
        serialise = json.dumps(self._trace_riche(), ensure_ascii=False)
        for interdit in ("SECRET METIER", "texte confidentiel"):
            assert interdit not in serialise

    def test_aucune_cle_de_contenu_n_est_presente(self):
        interdites = {"text", "content", "question", "prompt", "answer",
                      "embedding", "vector", "token", "secret"}

        def parcourir(noeud):
            if isinstance(noeud, dict):
                for cle, valeur in noeud.items():
                    assert cle not in interdites, f"clé de contenu dans la trace : {cle}"
                    parcourir(valeur)
            elif isinstance(noeud, list):
                for element in noeud:
                    parcourir(element)

        parcourir(self._trace_riche())

    def test_la_trace_est_serialisable_sans_perte(self):
        t = self._trace_riche()
        assert json.loads(json.dumps(t)) == t


class TestPureteEtReproductibilite:
    def test_la_construction_ne_mute_pas_ses_entrees(self):
        avant = [_candidat("a", text_rank=1, text_score=0.04)]
        empreinte = json.dumps(avant, sort_keys=True)
        CT.construire(pool_avant=avant, pool_apres=avant, info_rerank=_succes(1),
                      reranker_disponible=True)
        assert json.dumps(avant, sort_keys=True) == empreinte

    def test_deux_constructions_identiques_donnent_la_meme_trace(self):
        avant = [_candidat("a", text_rank=1, text_score=0.04, vector_rank=2, vector_score=0.8),
                 _candidat("b", store="domain_corpus", vector_rank=1, vector_score=0.9)]
        apres = [dict(c, rerank_rank=i + 1, rerank_score=5.0 - i) for i, c in enumerate(avant)]
        args = dict(pool_avant=avant, pool_apres=apres, info_rerank=_succes(2),
                    reranker_disponible=True)
        assert json.dumps(CT.construire(**args), sort_keys=True) == \
            json.dumps(CT.construire(**args), sort_keys=True)

    def test_aucun_champ_volatil_dans_la_trace(self):
        """Ni horodatage ni durée : une trace reproductible n'en contient pas."""
        t = CT.construire(pool_avant=[_candidat("a")], pool_apres=[_candidat("a")],
                          info_rerank=_succes(1, duration_ms=12.3), reranker_disponible=True)
        serialise = json.dumps(t)
        for volatil in ("timestamp", "duration_ms", "created_at", "elapsed"):
            assert volatil not in serialise

"""Sémantique réelle de `RAG_MIN_TEXT_RANK` — P1.9-f (§3).

Fige le chemin de décision, qui est **contre-intuitif** et doit le rester documenté.

Le seuil agit à TROIS endroits, et un seul compte vraiment :

1. **SQL** (`_search_chunks_fts`) : `AND ts_rank(...) >= min_rank` — les candidats
   lexicaux faibles sont écartés **à la source**.
2. **Fusion** (`merge`) : un chunk vu par le seul vecteur reçoit `text_score = 0.0`
   **explicitement** ; un chunk du leg FTS porte un `ts_rank` qui a déjà passé (1).
3. **Décision** (`should_answer`) : si `best_text > 0` alors la réponse dépend de
   `best_text >= min_text_rank` — et `RAG_MIN_SCORE` est **contourné**.

Conséquence : en (3), le test est une **tautologie** — tout `text_score` survivant a déjà
franchi le seuil en (1). Relever `RAG_MIN_TEXT_RANK` ne durcit donc PAS la règle FTS : il
**vide le leg FTS**, ce qui fait retomber la décision sur la règle vectorielle et son
`RAG_MIN_SCORE`. C'est par ce détour que le refus opère.

Corollaire à ne jamais perdre de vue : avec `RAG_MIN_TEXT_RANK = 0.0` (défaut), le moindre
signal lexical non nul suffit à répondre, quelle que soit la faiblesse du vecteur.
"""
from __future__ import annotations

import pytest

from apps.rag_core.domain.policies import RagPolicy, should_answer
from apps.rag_core.services.query.hygiene import clean_candidates
from apps.rag_core.services.query.merge import merge_hybrid


def _p(**kw) -> RagPolicy:
    base = {"min_sources": 1, "min_score": 0.76, "min_text_rank": 0.0}
    base.update(kw)
    return RagPolicy(**base)


def _chunk(cid, *, text_score=0.0, vector_score=0.0):
    return {"chunk_id": cid, "text": "t", "text_score": text_score,
            "vector_score": vector_score,
            "score": max(text_score, vector_score)}


# --------------------------------------------------------------------------
# 1. La règle FTS court-circuite RAG_MIN_SCORE
# --------------------------------------------------------------------------
def test_signal_fts_court_circuite_min_score():
    """Un vecteur très faible ne bloque pas : le FTS décide seul."""
    chunks = [_chunk("a", text_score=0.03, vector_score=0.10)]
    # vector 0.10 << min_score 0.76, et pourtant :
    assert should_answer(chunks, _p(min_text_rank=0.0)) is True
    assert should_answer(chunks, _p(min_text_rank=0.02)) is True


def test_seuil_zero_repond_sur_le_moindre_signal_lexical():
    """Défaut 0.0 : `best_text >= 0.0` est toujours vrai. C'est le plafond no-answer."""
    chunks = [_chunk("a", text_score=1e-9, vector_score=0.01)]
    assert should_answer(chunks, _p(min_text_rank=0.0)) is True


def test_fts_sous_le_seuil_est_refuse_si_il_atteint_la_decision():
    """Si un text_score sous-seuil parvenait jusqu'à la décision, il refuserait."""
    chunks = [_chunk("a", text_score=0.005, vector_score=0.90)]
    # Malgré un vecteur excellent : la branche FTS est prioritaire et tranche.
    assert should_answer(chunks, _p(min_text_rank=0.02)) is False


# --------------------------------------------------------------------------
# 2. Branche vectorielle seule : gouvernée par RAG_MIN_SCORE
# --------------------------------------------------------------------------
def test_vectoriel_seul_gouverne_par_min_score():
    assert should_answer([_chunk("a", vector_score=0.80)], _p()) is True
    assert should_answer([_chunk("a", vector_score=0.50)], _p()) is False


def test_vectoriel_seul_insensible_a_min_text_rank():
    """Sans signal lexical, le seuil lexical n'a aucun effet."""
    chunks = [_chunk("a", vector_score=0.80)]
    for t in (0.0, 0.02, 0.5):
        assert should_answer(chunks, _p(min_text_rank=t)) is True


# --------------------------------------------------------------------------
# 3. Le pré-filtre rend la règle FTS tautologique — le VRAI mécanisme
# --------------------------------------------------------------------------
def test_le_prefiltre_rend_la_regle_fts_tautologique():
    """Tout `text_score` qui atteint la décision a DÉJÀ passé le seuil en SQL.

    On simule le leg FTS filtré à la source : il ne peut plus produire de refus.
    """
    seuil = 0.02
    fts_filtre = [c for c in ({"chunk_id": "a", "score": 0.005},
                              {"chunk_id": "b", "score": 0.05})
                  if c["score"] >= seuil]                       # ce que fait le SQL
    fused = merge_hybrid(text_chunks=fts_filtre, vector_chunks=[], top_k=5)
    assert [c["text_score"] for c in fused] == [0.05]
    assert should_answer(fused, _p(min_text_rank=seuil)) is True


def test_relever_le_seuil_bascule_la_decision_sur_la_regle_vectorielle():
    """LE mécanisme réel. Le leg FTS se vide -> `best_text = 0` -> min_score tranche.

    Question hors corpus : lexique très faible, vecteur médiocre. À 0.0 le système
    répond (à tort) ; en relevant le seuil, le FTS disparaît et le vecteur refuse.
    """
    faible = [{"chunk_id": "a", "score": 0.004}]
    vect = [{"chunk_id": "b", "score": 0.30}]

    # Seuil 0.0 : le FTS survit et impose « répondre ».
    fused_bas = merge_hybrid(
        text_chunks=[c for c in faible if c["score"] >= 0.0], vector_chunks=vect, top_k=5)
    assert should_answer(fused_bas, _p(min_text_rank=0.0)) is True

    # Seuil 0.02 : le SQL vide le leg FTS -> décision vectorielle -> refus.
    fused_haut = merge_hybrid(
        text_chunks=[c for c in faible if c["score"] >= 0.02], vector_chunks=vect, top_k=5)
    assert max(c["text_score"] for c in fused_haut) == 0.0
    assert should_answer(fused_haut, _p(min_text_rank=0.02)) is False


def test_un_vecteur_fort_survit_a_la_disparition_du_fts():
    """Le levier ne doit pas casser les cas answerable à vecteur solide."""
    faible = [{"chunk_id": "a", "score": 0.004}]
    fort = [{"chunk_id": "b", "score": 0.88}]
    fused = merge_hybrid(
        text_chunks=[c for c in faible if c["score"] >= 0.02], vector_chunks=fort, top_k=5)
    assert should_answer(fused, _p(min_text_rank=0.02)) is True


# --------------------------------------------------------------------------
# 4. Fusion : les échelles ne sont pas comparables
# --------------------------------------------------------------------------
def test_fusion_marque_explicitement_l_absence_de_modalite():
    """C'est cette mise à zéro EXPLICITE qui rend le mécanisme sûr."""
    fused = merge_hybrid(
        text_chunks=[{"chunk_id": "a", "score": 0.05}],
        vector_chunks=[{"chunk_id": "b", "score": 0.80}], top_k=5)
    by = {c["chunk_id"]: c for c in fused}
    assert by["a"]["vector_score"] == 0.0 and by["a"]["text_rank"] == 1
    assert by["b"]["text_score"] == 0.0 and by["b"]["vector_rank"] == 1


def test_chunk_vu_par_les_deux_legs_cumule_les_votes():
    fused = merge_hybrid(
        text_chunks=[{"chunk_id": "a", "score": 0.05}],
        vector_chunks=[{"chunk_id": "a", "score": 0.80}], top_k=5)
    assert fused[0]["text_score"] == 0.05 and fused[0]["vector_score"] == 0.80
    assert fused[0]["score"] == 0.80          # max(text, vector) — contrat d'API


# --------------------------------------------------------------------------
# 5. Hygiène : jamais le seuil vectoriel sur un candidat lexical
# --------------------------------------------------------------------------
def test_hygiene_seuils_separes_par_modalite(settings):
    settings.RAG_MIN_SCORE = 0.76
    settings.RAG_MIN_TEXT_RANK = 0.02
    kept = clean_candidates([
        _chunk("lex", text_score=0.05),          # lexical seul, au-dessus du seuil FTS
        _chunk("vec", vector_score=0.80),        # vectoriel seul, au-dessus de min_score
        _chunk("faible", text_score=0.001, vector_score=0.10),   # les deux sous seuil
    ])
    assert {c["chunk_id"] for c in kept} == {"lex", "vec"}


def test_un_candidat_lexical_pertinent_nest_pas_jete_au_seuil_vectoriel(settings):
    """ts_rank 0.05 est un bon score lexical ; le comparer à 0.76 le tuerait."""
    settings.RAG_MIN_SCORE = 0.76
    settings.RAG_MIN_TEXT_RANK = 0.02
    assert len(clean_candidates([_chunk("lex", text_score=0.05)])) == 1


# --------------------------------------------------------------------------
# 6. min_sources reste strict
# --------------------------------------------------------------------------
def test_aucune_source_refuse_quel_que_soit_le_seuil():
    for t in (0.0, 0.02, 0.5):
        assert should_answer([], _p(min_text_rank=t)) is False


@pytest.mark.parametrize("seuil", [0.0, 0.005, 0.02, 0.05, 0.2])
def test_min_score_nest_jamais_modifie_par_le_sweep(seuil):
    """Garde-fou de calibration : le sweep ne touche QUE le seuil lexical."""
    p = _p(min_text_rank=seuil)
    assert p.min_score == 0.76

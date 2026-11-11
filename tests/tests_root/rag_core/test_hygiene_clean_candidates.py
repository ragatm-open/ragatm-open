"""
Correctif filtrage RAG : clean_candidates ne doit JAMAIS jeter un candidat FTS
(score = ts_rank, faible) au seuil VECTORIEL RAG_MIN_SCORE.

Échelles : ts_rank ~ 0.0..0.1 ; cosinus vectoriel ~ 0.0..1.0.

NB : ces tests portent sur la LOGIQUE de `clean_candidates` (filtrage conscient de la
modalité, dédup, tri) — pas sur la valeur du seuil global. Ils fixent donc `min_score`
explicitement plutôt que de dépendre du défaut applicatif (désormais 0.76, calibré pour
nomic-embed-text), qui peut évoluer avec le profil d'embeddings.
"""

from apps.rag_core.services.query.hygiene import clean_candidates


def _ids(chunks):
    return {c["chunk_id"] for c in chunks}


def test_fts_low_tsrank_kept_when_filtered_with_text_threshold():
    # Liste FTS pré-fusion : seul `score` (= ts_rank) est présent.
    fts = [
        {"chunk_id": "a", "score": 0.05, "text": "match lexical exact"},
        {"chunk_id": "b", "score": 0.03, "text": "autre match"},
    ]
    # Appel tel que fait par l'orchestrateur sur la liste FTS.
    out = clean_candidates(fts, min_score=0.0)  # RAG_MIN_TEXT_RANK
    assert _ids(out) == {"a", "b"}  # rien n'est jeté


def test_fts_candidate_would_have_been_dropped_by_vector_threshold():
    # Régression : avec l'ancien comportement (seuil vectoriel 0.20 sur tous),
    # ces ts_rank faibles disparaissaient. Ici le défaut mono-modalité applique
    # RAG_MIN_SCORE=0.20, ce qui illustre POURQUOI il faut passer le seuil FTS.
    fts = [{"chunk_id": "a", "score": 0.05, "text": "x"}]
    dropped = clean_candidates(fts)  # défaut = seuil vectoriel
    assert dropped == []
    kept = clean_candidates(fts, min_score=0.0)  # seuil lexical
    assert _ids(kept) == {"a"}


def test_post_fusion_modality_aware_keeps_lexical_drops_weak_vector():
    # Candidats POST-FUSION : portent text_score ET vector_score.
    fused = [
        # fort lexical, nul vectoriel -> gardé (text_score >= RAG_MIN_TEXT_RANK=0.0)
        {"chunk_id": "lex", "text_score": 0.05, "vector_score": 0.0, "score": 0.05, "text": "t"},
        # faible vectoriel, nul lexical -> jeté (0.10 < seuil vectoriel 0.20)
        {"chunk_id": "weakvec", "text_score": 0.0, "vector_score": 0.10, "score": 0.10, "text": "t"},
        # fort vectoriel -> gardé (0.55 >= seuil vectoriel 0.20)
        {"chunk_id": "vec", "text_score": 0.0, "vector_score": 0.55, "score": 0.55, "text": "t"},
    ]
    # Seuil vectoriel fixé par le scénario (indépendant du défaut applicatif).
    out = clean_candidates(fused, min_score=0.20)
    assert _ids(out) == {"lex", "vec"}
    assert "weakvec" not in _ids(out)


def test_heterogeneous_mix_keeps_lexical_even_with_low_combined_score():
    fused = [
        {"chunk_id": "mix", "text_score": 0.04, "vector_score": 0.60, "score": 0.60, "text": "t"},
        {"chunk_id": "lexonly", "text_score": 0.02, "vector_score": 0.0, "score": 0.02, "text": "t"},
    ]
    out = clean_candidates(fused)
    assert _ids(out) == {"mix", "lexonly"}  # le lexical pur n'est pas jeté


def test_dedup_and_sort_preserved():
    chunks = [
        {"chunk_id": "a", "score": 0.30, "text": "lo"},
        {"chunk_id": "a", "score": 0.90, "text": "hi"},
        {"chunk_id": "b", "score": 0.50, "text": "mid"},
    ]
    # min_score=0.0 : ce test porte sur la dédup/le tri, pas sur le filtrage.
    out = clean_candidates(chunks, min_score=0.0)
    assert [c["chunk_id"] for c in out] == ["a", "b"]  # trié décroissant
    assert out[0]["score"] == 0.90  # meilleur score conservé pour le doublon


def test_empty_and_missing_id():
    assert clean_candidates(None) == []
    assert clean_candidates([{"score": 0.9, "text": "no id"}]) == []

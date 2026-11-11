"""`RRF_K` — constante d'amortissement de la fusion par rang réciproque.

Histoire de ce fichier, parce qu'elle explique ce qu'il teste aujourd'hui.

P2.5 devait mesurer l'effet de K sur le Recall@5 Mission. K était une constante en
dur ; le lot de mesure a introduit une lecture ``getattr(settings, "RAG_RRF_K", 60)``
pour pouvoir le faire varier. Mesure faite : K=5 apportait +0.020 de Recall@5 seul,
et **rien** une fois combiné à `candidate_k=96` + `max_per_group=1`. K est resté à 60.

Ce que la cartographie de configuration a montré ensuite : `RAG_RRF_K` n'a JAMAIS été
projeté dans les settings. Le poser dans un `.env` n'avait aucun effet — le lever ne
fonctionnait que depuis Python, via `override_settings`. Une variable qui a l'air
réglable en exploitation et ne l'est pas est pire qu'une constante : elle invite à
un réglage qui ne se produit pas.

K est donc redevenu une constante. Le prix est assumé : refaire varier K demande
d'éditer `merge.RRF_K` dans une branche — ce qui laisse une trace dans l'historique,
contrairement à une variable d'environnement posée un soir sur un serveur.

Ce qui reste testé ici : la valeur de référence, et les PROPRIÉTÉS mathématiques de
l'amortissement, qui sont ce qui compte réellement pour le classement.
"""
from __future__ import annotations

from apps.rag_core.services.query.merge import RRF_K, _rrf, _rrf_k, merge_hybrid


def test_constante_de_reference_inchangee():
    """60 est la valeur de la littérature, et celle que P2.5 a conservée."""
    assert RRF_K == 60
    assert _rrf_k() == 60


def test_contribution_identique_a_la_formule_historique():
    """Au bit près : aucun classement passé n'est déplacé par ce lot."""
    for rank in (1, 2, 10, 200):
        assert _rrf(rank) == 1.0 / (60 + rank)


def test_aucun_reglage_d_environnement_ne_deplace_k(monkeypatch):
    """Poser `RAG_RRF_K` ne doit RIEN changer — et ne doit pas non plus lever.

    C'est l'invariant qui remplace l'ancien test de réglage : une variable
    résiduelle dans un `.env` de production ne doit pas se réactiver par accident.
    """
    monkeypatch.setenv("RAG_RRF_K", "5")
    assert _rrf_k() == 60
    assert _rrf(1) == 1.0 / 61


def test_k_gouverne_l_amortissement():
    """Le sens de K, vérifié sur la formule : petit K creuse l'écart de tête.

    Propriété structurelle du RRF, indépendante de la valeur retenue — c'est elle
    qu'il faut protéger, pas le mécanisme de réglage.
    """
    ecart_petit_k = 1.0 / (5 + 1) - 1.0 / (5 + 2)
    ecart_grand_k = 1.0 / (60 + 1) - 1.0 / (60 + 2)
    assert ecart_petit_k > ecart_grand_k


def test_fusion_mono_leg_ne_depend_pas_de_k():
    """Témoin : avec une seule modalité, K décale les scores mais jamais l'ORDRE."""
    text = [{"chunk_id": "a", "score": 0.9}, {"chunk_id": "b", "score": 0.1}]
    ordre = [c["chunk_id"] for c in merge_hybrid(text_chunks=text, vector_chunks=[], top_k=2)]
    assert ordre == ["a", "b"]

"""La policy `balanced-v1` — bornes exactes et cas non évaluables.

Ce que ce fichier verrouille
----------------------------
Deux nombres et une règle. Les nombres viennent du lot de conception
(`backend/benchmarks/calibration/admission-policy-design/`) et **ne se déplacent pas** :
les changer exige une nouvelle `POLICY_VERSION`, ce qu'un test ci-dessous impose.

Les bornes sont testées à ±0,01 parce qu'une policy dont on ignore de quel côté penche
l'égalité produit des décisions irreproductibles à la valeur exacte du seuil — et le
reranker, lui, a un bruit de ~0,01 logit.

Le second sujet est plus important que le premier : ce qui n'est **pas** évaluable. Le
reranker tourne en `fail_open_rrf` ; un repli rend un ordre RRF dont les scores ne sont
pas des logits cross-encoder. Les comparer à `-1.92` n'aurait aucun sens, et les ranger
sous « balanced-v1 evaluated » ferait passer une baseline pour une mesure.
"""
from __future__ import annotations

import pytest

from apps.rag_core.domain import admission_shadow as sh


def _info(score: float, **surcharges):
    base = {"success": True, "fallback": False,
            "movements": [{"chunk_id": "c1", "score": score, "before_rank": 1, "after_rank": 1}],
            "model_revision": "rev123", "model_logical": "rerank-default"}
    base.update(surcharges)
    return base


# ===========================================================================
# T1–T7 — les bornes, au centième
# ===========================================================================
@pytest.mark.parametrize(
    "score,attendu",
    [
        (-100.0, sh.ABSTAIN),
        (-2.00, sh.ABSTAIN),      # T1
        (-1.92, sh.ABSTAIN),      # T2 — borne INCLUSIVE côté abstention
        (-1.91, sh.UNCERTAIN),    # T3 — un centième plus haut : plus d'abstention
        (0.0, sh.UNCERTAIN),      # T4
        (1.59, sh.UNCERTAIN),     # T5
        (1.60, sh.ALLOW),         # T6 — borne INCLUSIVE côté autorisation
        (3.0, sh.ALLOW),          # T7
        (100.0, sh.ALLOW),
    ],
)
def test_les_bornes_de_la_regle(score, attendu):
    assert sh.decide(score) == attendu


def test_la_zone_incertaine_est_bien_un_intervalle_ouvert_des_deux_cotes():
    """Aucun trou, aucun recouvrement : tout réel reçoit exactement une décision."""
    assert sh.ABSTAIN_LTE < sh.ALLOW_GTE, "les seuils se croiseraient"
    for x in (sh.ABSTAIN_LTE + 1e-9, 0.0, sh.ALLOW_GTE - 1e-9):
        assert sh.decide(x) == sh.UNCERTAIN


def test_les_seuils_sont_ceux_du_lot_de_conception():
    """Ces deux nombres ne se déplacent pas en silence.

    Ils sont ancrés sur la distribution des NÉGATIFS du jeu d'ajustement — médiane et
    p95 — et non sur un quantile des positifs, dont la queue basse n'est pas stable
    d'un jeu à l'autre. Changer l'un des deux change la policy : ce test échoue alors,
    et le rédacteur doit incrémenter `POLICY_VERSION`.
    """
    assert sh.ABSTAIN_LTE == -1.92
    assert sh.ALLOW_GTE == 1.60
    assert sh.POLICY_VERSION == "balanced-v1"
    assert sh.SIGNAL == "reranker_top1_score"


def test_les_seuils_ne_sont_pas_desserrables_par_l_environnement():
    """Une policy dont les seuils se règlent par variable d'environnement n'est pas
    versionnée : c'est un réglage anonyme. Même doctrine que `MAX_QUESTION_CHARS`."""
    import os
    from pathlib import Path

    source = Path(sh.__file__).read_text(encoding="utf-8")
    for lecture in ("os.getenv", "os.environ", "getattr(settings", "setting_float", "setting_bool"):
        assert lecture not in source, f"`{lecture}` rendrait un seuil desserrable"
    assert "RAG_ADMISSION_ABSTAIN" not in os.environ


# ===========================================================================
# Ce qui n'est PAS évaluable — le sujet qui compte
# ===========================================================================
def test_un_repli_rrf_n_est_jamais_evalue():
    """`fail_open_rrf` rend un ordre RRF : ses scores ne sont pas des logits.

    Le score fourni ici (`5.0`) tomberait franchement dans `ALLOW` si on le comparait.
    On ne le compare pas.
    """
    r = sh.evaluate(_info(5.0, fallback=True), enabled=True)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_RERANKER_FALLBACK
    assert r["top1_score"] is None
    assert r["fallback_used"] is True


def test_un_reranker_en_echec_n_est_jamais_evalue():
    r = sh.evaluate(_info(5.0, success=False), enabled=True)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_RERANKER_UNAVAILABLE


@pytest.mark.parametrize("info", [None, {}, "pas un dict"], ids=["none", "vide", "type"])
def test_une_trace_absente_ou_invalide_n_est_jamais_evaluee(info):
    r = sh.evaluate(info, enabled=True)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_RERANKER_UNAVAILABLE


def test_sans_candidat_aucune_decision():
    r = sh.evaluate(_info(2.0, movements=[]), enabled=True, candidate_count=0)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_NO_CANDIDATES


def test_un_mouvement_sans_score_ne_fabrique_pas_de_decision():
    r = sh.evaluate(_info(2.0, movements=[{"chunk_id": "c", "after_rank": 1, "score": None}]),
                    enabled=True)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_NO_SCORE


def test_drapeau_eteint_aucune_evaluation():
    r = sh.evaluate(_info(5.0), enabled=False)
    assert r["decision"] == sh.NOT_EVALUATED
    assert r["reason"] == sh.REASON_DISABLED
    assert r["shadow_enabled"] is False


# ===========================================================================
# Le rang 1 est celui que le reranker a DÉSIGNÉ
# ===========================================================================
def test_le_score_lu_est_celui_du_rang_1_apres_rerank_pas_le_premier_de_la_liste():
    """L'ordre de la liste `movements` n'est pas le classement. Confondre les deux
    lirait le score d'un autre passage — et rendrait une décision sur le mauvais."""
    info = {"success": True, "fallback": False, "movements": [
        {"chunk_id": "a", "score": -9.0, "before_rank": 1, "after_rank": 7},
        {"chunk_id": "b", "score": 4.2, "before_rank": 12, "after_rank": 1},
    ]}
    r = sh.evaluate(info, enabled=True)
    assert r["top1_score"] == 4.2
    assert r["decision"] == sh.ALLOW


def test_sans_rang_1_explicite_on_prend_le_meilleur_rang_disponible():
    info = {"success": True, "fallback": False, "movements": [
        {"chunk_id": "a", "score": -5.0, "after_rank": 4},
        {"chunk_id": "b", "score": -3.0, "after_rank": 2},
    ]}
    assert sh.evaluate(info, enabled=True)["top1_score"] == -3.0


# ===========================================================================
# Le descripteur : ce qui part dans la trace
# ===========================================================================
def test_le_descripteur_porte_tout_ce_qui_identifie_la_policy():
    d = sh.policy_descriptor()
    assert d == {
        "policy_version": "balanced-v1", "status": "shadow",
        "signal": "reranker_top1_score", "abstain_lte": -1.92, "allow_gte": 1.60,
        "source_lot": "admission-policy-design",
    }


def test_toute_decision_porte_la_version_et_les_seuils():
    """Une décision sans sa version ni ses seuils est inexploitable a posteriori :
    on ne saurait pas quelle règle l'a produite."""
    for r in (sh.evaluate(_info(2.0), enabled=True),
              sh.evaluate(_info(2.0, fallback=True), enabled=True),
              sh.evaluate(None, enabled=False)):
        assert r["policy_version"] == "balanced-v1"
        assert r["abstain_lte"] == -1.92 and r["allow_gte"] == 1.60
        assert r["signal"] == "reranker_top1_score"
        assert "decision" in r and "reason" in r


def test_la_revision_du_modele_est_conservee_quand_elle_existe():
    r = sh.evaluate(_info(2.0), enabled=True)
    assert r["model_revision"] == "rev123"
    assert r["model_logical"] == "rerank-default"


def test_aucune_donnee_sensible_ne_sort_de_la_policy():
    """Ni question, ni texte de passage, ni vecteur : des identifiants et des nombres."""
    info = _info(2.0)
    info["movements"][0]["text"] = "contenu confidentiel du passage"
    r = sh.evaluate(info, enabled=True)
    assert "contenu confidentiel" not in repr(r)
    assert set(r) <= {
        "policy_version", "status", "signal", "abstain_lte", "allow_gte", "source_lot",
        "shadow_enabled", "decision", "reason", "top1_score", "reranker_status",
        "fallback_used", "model_revision", "model_logical",
    }

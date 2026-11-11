"""Answerability par cross-encoder — P2.0 (§27).

⚠️ Fixtures 100 % SYNTHÉTIQUES. Le modèle réel n'est PAS chargé : ces tests figent le
CONTRAT (séparation, agrégation, panne, identifiants forts, identités de baseline), pas la
qualité du modèle — celle-ci se mesure par le sweep, pas par des tests unitaires.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.rag_core.benchmark.compare import _benchmark_identity
from apps.rag_core.services import answerability_ce as ace

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _reset():
    ace.reset_model_cache()
    yield
    ace.reset_model_cache()


def _cfg(**kw):
    base = dict(enabled=True, model="fake-model", device="cpu", threshold=0.5, top_k=5,
                strategy=ace.STRATEGY_MAX, fail_open=True)
    base.update(kw)
    return ace.AnswerabilityConfig(**base)


def _chunks(*texts):
    return [{"chunk_id": f"c{i}", "text": t} for i, t in enumerate(texts)]


class _FakeModel:
    """Modèle factice : renvoie des scores fixés, compte ses chargements."""

    loads = 0

    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def predict(self, pairs, **kw):
        self.calls += 1
        return self.scores[: len(pairs)]


def _install(monkeypatch, scores):
    model = _FakeModel(scores)
    monkeypatch.setattr(ace, "get_model", lambda *a, **k: model)
    return model


# --------------------------------------------------------------------------
# Chargement du modèle
# --------------------------------------------------------------------------
def test_le_modele_est_charge_une_seule_fois_par_processus(monkeypatch):
    calls = {"n": 0}

    class _CE:
        def __init__(self, name, device=None):
            calls["n"] += 1

        def predict(self, pairs, **kw):
            return [0.9] * len(pairs)

    import sys
    import types

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = _CE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    ace.get_model("m", "cpu")
    ace.get_model("m", "cpu")
    ace.get_model("m", "cpu")
    assert calls["n"] == 1, "le modèle doit être un singleton paresseux"

    ace.get_model("autre-modele", "cpu")      # signature différente => rechargement
    assert calls["n"] == 2


def test_modele_absent_leve_une_erreur_explicite(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(ace.AnswerabilityUnavailable):
        ace.get_model("m", "cpu")


def test_cuda_demande_sans_gpu_retombe_sur_cpu():
    """Ne jamais échouer pour ça : le CPU suffit, et un refus massif serait pire."""
    assert ace._resolve_device("cpu") == "cpu"
    assert ace._resolve_device("cuda") in ("cpu", "cuda")   # dépend de la machine


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def test_score_d_une_paire(monkeypatch):
    _install(monkeypatch, [0.87])
    assert ace.score_pairs("q", _chunks("t"), _cfg()) == [0.87]


def test_batch_limite_au_top_k(monkeypatch):
    _install(monkeypatch, [0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    r = ace.decide(question="q", chunks=_chunks(*"abcdef"), cfg=_cfg(top_k=3))
    assert r.n_pairs == 3, "on ne rescore pas 50 candidats sans raison"


@pytest.mark.parametrize("mauvais", [float("nan"), float("inf"), "abc"])
def test_score_invalide_est_une_erreur(monkeypatch, mauvais):
    _install(monkeypatch, [mauvais])
    with pytest.raises(ace.AnswerabilityUnavailable):
        ace.score_pairs("q", _chunks("t"), _cfg())


def test_le_texte_est_tronque(monkeypatch):
    vu = {}

    class _M:
        def predict(self, pairs, **kw):
            vu["len"] = len(pairs[0][1])
            return [0.5]

    monkeypatch.setattr(ace, "get_model", lambda *a, **k: _M())
    ace.score_pairs("q", _chunks("x" * 5000), _cfg(max_text_chars=100))
    assert vu["len"] == 100


# --------------------------------------------------------------------------
# Agrégation (§6)
# --------------------------------------------------------------------------
def test_agregation_max():
    assert ace.aggregate([0.1, 0.9, 0.3], ace.STRATEGY_MAX) == 0.9


def test_agregation_mean_top2():
    assert ace.aggregate([0.1, 0.9, 0.7], ace.STRATEGY_MEAN_TOP2) == pytest.approx(0.8)


def test_agregation_mean_top2_avec_un_seul_score():
    assert ace.aggregate([0.9], ace.STRATEGY_MEAN_TOP2) == 0.9


def test_agregation_liste_vide():
    for s in ace.STRATEGIES:
        assert ace.aggregate([], s) == 0.0


def test_un_seul_chunk_qui_repond_suffit(monkeypatch):
    """Le max est le bon agrégat : la réponse peut n'être que dans UN chunk."""
    _install(monkeypatch, [0.01, 0.01, 0.95])
    r = ace.decide(question="q", chunks=_chunks("a", "b", "c"), cfg=_cfg(threshold=0.5))
    assert r.decision == ace.ANSWERABLE and r.score == 0.95


# --------------------------------------------------------------------------
# Seuil
# --------------------------------------------------------------------------
def test_seuil_au_bord_est_inclusif(monkeypatch):
    _install(monkeypatch, [0.5])
    assert ace.decide(question="q", chunks=_chunks("t"), cfg=_cfg(threshold=0.5)).decision == ace.ANSWERABLE


def test_juste_sous_le_seuil_refuse(monkeypatch):
    _install(monkeypatch, [0.4999])
    assert ace.decide(question="q", chunks=_chunks("t"), cfg=_cfg(threshold=0.5)).decision == ace.NO_ANSWER


def test_aucun_candidat_refuse(monkeypatch):
    _install(monkeypatch, [])
    r = ace.decide(question="q", chunks=[], cfg=_cfg())
    assert r.decision == ace.NO_ANSWER and r.n_pairs == 0


# --------------------------------------------------------------------------
# Séparation reranking / answerability (§3) — l'invariant central
# --------------------------------------------------------------------------
def test_answerability_ne_reordonne_jamais(monkeypatch):
    """Le module DÉCIDE, il n'ORDONNE pas : l'ordre d'entrée doit survivre."""
    _install(monkeypatch, [0.1, 0.9, 0.3])
    entree = _chunks("a", "b", "c")
    avant = [c["chunk_id"] for c in entree]
    ace.decide(question="q", chunks=entree, cfg=_cfg())
    assert [c["chunk_id"] for c in entree] == avant


def test_answerability_fonctionne_reranker_eteint(monkeypatch, settings):
    settings.RAG_RERANK_ENABLED = False
    settings.RAG_ANSWERABILITY_ENABLED = True
    _install(monkeypatch, [0.9])
    assert ace.decide(question="q", chunks=_chunks("t"), cfg=_cfg()).decision == ace.ANSWERABLE


def test_les_deux_reglages_sont_independants(settings):
    settings.RAG_RERANK_ENABLED = True
    settings.RAG_ANSWERABILITY_ENABLED = False
    assert ace.AnswerabilityConfig.from_settings().enabled is False


# --------------------------------------------------------------------------
# Stratégie C : garde retrieval
# --------------------------------------------------------------------------
def test_garde_retrieval_empeche_de_rattraper_un_retrieval_perdu(monkeypatch):
    _install(monkeypatch, [0.99])
    r = ace.decide(question="q", chunks=_chunks("t"),
                   cfg=_cfg(strategy=ace.STRATEGY_MAX_GUARDED), retrieval_ok=False)
    assert r.decision == ace.NO_ANSWER


def test_garde_retrieval_neutre_si_retrieval_ok(monkeypatch):
    _install(monkeypatch, [0.99])
    r = ace.decide(question="q", chunks=_chunks("t"),
                   cfg=_cfg(strategy=ace.STRATEGY_MAX_GUARDED), retrieval_ok=True)
    assert r.decision == ace.ANSWERABLE


# --------------------------------------------------------------------------
# Identifiants forts (§13)
# --------------------------------------------------------------------------
def test_extraction_d_identifiants_forts():
    assert "REF-2026-04" in ace.extract_strong_ids("Voir REF-2026-04 pour le detail.")
    assert ace.extract_strong_ids("une phrase sans reference") == set()


def test_identifiant_exact_present_dans_un_chunk():
    q = "Que dit la reference REF-2026-04 ?"
    assert ace.has_exact_strong_id_match(q, _chunks("La reference REF-2026-04 precise…")) is True
    assert ace.has_exact_strong_id_match(q, _chunks("Un texte sans la reference")) is False


def test_composite_protege_un_identifiant_fort_malgre_un_score_faible(monkeypatch):
    """Un identifiant exact est une preuve dure : un score faible seul ne doit pas refuser."""
    _install(monkeypatch, [0.01])
    r = ace.decide(question="Que dit REF-2026-04 ?",
                   chunks=_chunks("La reference REF-2026-04 fixe le seuil."),
                   cfg=_cfg(strategy=ace.STRATEGY_COMPOSITE, threshold=0.5))
    assert r.decision == ace.ANSWERABLE and r.strong_id_override is True


def test_composite_ne_protege_pas_un_identifiant_absent(monkeypatch):
    _install(monkeypatch, [0.01])
    r = ace.decide(question="Que dit REF-2026-04 ?", chunks=_chunks("texte sans la reference"),
                   cfg=_cfg(strategy=ace.STRATEGY_COMPOSITE, threshold=0.5))
    assert r.decision == ace.NO_ANSWER and r.strong_id_override is False


def test_max_ne_protege_pas_les_identifiants(monkeypatch):
    """La protection est une propriété de la stratégie D, mesurée — pas un ajout implicite."""
    _install(monkeypatch, [0.01])
    r = ace.decide(question="Que dit REF-2026-04 ?",
                   chunks=_chunks("La reference REF-2026-04 fixe le seuil."),
                   cfg=_cfg(strategy=ace.STRATEGY_MAX, threshold=0.5))
    assert r.decision == ace.NO_ANSWER


# --------------------------------------------------------------------------
# Politique de panne (§23)
# --------------------------------------------------------------------------
def _casse(monkeypatch):
    def _boom(*a, **k):
        raise ace.AnswerabilityUnavailable("modèle absent")

    monkeypatch.setattr(ace, "get_model", _boom)


def test_fail_open_ne_refuse_pas_mais_trace(monkeypatch):
    """Une panne du modèle ne doit pas devenir un refus massif — mais jamais en silence."""
    _casse(monkeypatch)
    r = ace.decide(question="q", chunks=_chunks("t"), cfg=_cfg(fail_open=True))
    assert r.decision == ace.UNAVAILABLE
    assert r.fallback_used is True and r.error
    assert r.decision != ace.ANSWERABLE, "jamais « answerable » muet"


def test_fail_closed_refuse_explicitement(monkeypatch):
    _casse(monkeypatch)
    r = ace.decide(question="q", chunks=_chunks("t"), cfg=_cfg(fail_open=False))
    assert r.decision == ace.NO_ANSWER and r.fallback_used is True


def test_aucun_repli_vers_une_api_distante_ni_le_llm():
    src = (REPO / "backend/apps/rag_core/services/answerability_ce.py").read_text(encoding="utf-8")
    for interdit in ("requests", "urllib", "openai", "http://", "https://",
                     "generate_text", "chat_query"):
        assert interdit not in src, f"repli interdit détecté : {interdit}"


# --------------------------------------------------------------------------
# Observabilité (§22)
# --------------------------------------------------------------------------
def test_le_log_ne_contient_ni_question_ni_chunk(monkeypatch):
    _install(monkeypatch, [0.9])
    r = ace.decide(question="question confidentielle ABC12-01",
                   chunks=_chunks("contenu confidentiel du chunk"), cfg=_cfg())
    blob = json.dumps(r.as_log(), ensure_ascii=False)
    assert "confidentiel" not in blob
    for champ in ("answerability_model", "threshold", "top_k", "decision", "latency_ms",
                  "fallback_used", "max_score", "error"):
        assert champ in r.as_log()


# --------------------------------------------------------------------------
# Identité de baseline (§25)
# --------------------------------------------------------------------------
def _meta(**kw):
    m = {"dataset": "demo-fr-extended.jsonl", "profile": "nomic",
         "answerability_enabled": True, "answerability_strategy": "cross_encoder"}
    m.update(kw)
    return {"meta": m}


def test_answerability_change_l_identite():
    sans = {"meta": {"dataset": "demo-fr-extended.jsonl", "profile": "nomic"}}
    assert _benchmark_identity(_meta()) != _benchmark_identity(sans)
    assert "ans-cross_encoder" in _benchmark_identity(_meta())


def test_seuil_lexical_et_answerability_coexistent_dans_l_identite():
    ident = _benchmark_identity(_meta(min_text_rank=0.025))
    assert "ans-cross_encoder" in ident and "tr0.025" in ident


# --------------------------------------------------------------------------
# Discipline du holdout (§17)
# --------------------------------------------------------------------------
def _decision() -> dict | None:
    p = REPO / "backend/benchmarks/calibration/mission-answerability-decision.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@pytest.mark.skipif(_decision() is None, reason="artefact local absent")
def test_aucune_strategie_eligible_ne_consomme_pas_le_holdout():
    d = _decision()
    if not d["selected"]:
        assert d["holdout_not_yet_used"] is True
        assert d["holdout_run_count"] == 0


@pytest.mark.skipif(_decision() is None, reason="artefact local absent")
def test_la_decision_documente_la_semantique_du_score():
    """Confondre sigmoïde et logit invaliderait toute la grille de seuils."""
    assert "sigmoid" in _decision()["score_semantics"].lower()


@pytest.mark.skipif(_decision() is None, reason="artefact local absent")
def test_le_profil_retrieval_est_reste_fige():
    p = _decision()["profile"]
    assert p["RAG_MIN_SCORE"] == 0.76
    assert p["RAG_MIN_TEXT_RANK"] == 0.0
    assert p["RAG_RERANK_ENABLED"] is False


# --------------------------------------------------------------------------
# Réglages & confidentialité
# --------------------------------------------------------------------------
def test_answerability_desactivee_par_defaut(settings):
    """Un mécanisme non promu ne s'active jamais tout seul."""
    from django.conf import settings as s

    assert getattr(s, "RAG_ANSWERABILITY_ENABLED", False) is False


def test_fail_open_par_defaut(settings):
    from django.conf import settings as s

    assert getattr(s, "RAG_ANSWERABILITY_FAIL_OPEN", None) is True


def test_artefacts_terrain_gitignores():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "backend/benchmarks/calibration/mission-*" in ignored

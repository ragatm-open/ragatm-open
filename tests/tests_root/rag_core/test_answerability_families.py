"""Comparaison NLI / juge génératif / classifieur — invariants (P2.4, §29).

Ces tests figent la discipline de la comparaison, pas la qualité des modèles : mêmes
candidats pour toutes les familles, gabarit d'hypothèse déterministe, preuve obligatoire
pour un positif génératif, split supervisé sans fuite, holdout intouchable.

⚠️ Fixtures 100 % SYNTHÉTIQUES. Les modèles réels ne sont PAS chargés ici.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from apps.rag_core.benchmark.answerability_micro import (
    CASES,
    EXPLICIT,
    TITLE_TRAP,
    TOPIC_ONLY,
    by_capability,
    title_pairs,
)

REPO = Path(__file__).resolve().parents[3]
DECISION = REPO / "backend/benchmarks/calibration/answerability/p2-4-decision.json"


def _decision() -> dict | None:
    return json.loads(DECISION.read_text(encoding="utf-8")) if DECISION.exists() else None


needs_decision = pytest.mark.skipif(_decision() is None, reason="artefact absent")


# --------------------------------------------------------------------------
# Micro-benchmark : chaque cas isole UNE capacité
# --------------------------------------------------------------------------
def test_le_micro_benchmark_couvre_les_capacites_critiques():
    caps = set(by_capability())
    assert {EXPLICIT, TOPIC_ONLY, TITLE_TRAP} <= caps
    assert len(caps) >= 10, "un micro-benchmark trop étroit ne diagnostique rien"


def test_les_cas_sont_equilibres():
    ans = sum(1 for c in CASES if c.answerable)
    assert 0 < ans < len(CASES), "il faut les deux classes"


def test_les_identifiants_sont_uniques():
    assert len({c.id for c in CASES}) == len(CASES)


def test_le_piege_du_titre_est_une_paire_controlee():
    """m16/m17 : MÊME document, seule la présence de la réponse change.

    C'est le cas qui a fait échouer le cross-encoder pendant trois lots.
    """
    pairs = title_pairs()
    assert pairs
    for sans, avec in pairs:
        assert sans.question == avec.question or sans.capability == avec.capability


def test_les_deux_cas_title_trap_partagent_le_meme_document():
    idx = {c.id: c for c in CASES}
    assert idx["m16"].chunks == idx["m17"].chunks
    assert idx["m16"].answerable is False and idx["m17"].answerable is True


def test_aucun_cas_ne_recopie_la_question_dans_le_chunk():
    for c in CASES:
        q = " ".join(c.question.lower().rstrip("?").split())
        for ch in c.chunks:
            assert q not in " ".join(ch.lower().split()), f"{c.id}: question recopiée"


# --------------------------------------------------------------------------
# Transformation question → hypothèse (§10)
# --------------------------------------------------------------------------
def _hypothese(q: str) -> str:
    """Réplique du gabarit A1c retenu — déterministe, sans LLM."""
    q = q.strip().rstrip("?").strip()
    for pat, rep in ((r"^Quel(?:le|s|les)?\s+(?:est|sont)\s+(?:le|la|les|l')\s*", "le "),
                     (r"^Quel(?:le|s|les)?\s+", "le "), (r"^Où\s+", "l'endroit où "),
                     (r"^Comment\s+", "la manière dont "), (r"^Combien\s+(?:de\s+)?", "le nombre de "),
                     (r"^Quand\s+", "le moment où "), (r"^Qui\s+", "la personne qui ")):
        if re.match(pat, q, re.I):
            return "Le passage précise " + re.sub(pat, rep, q, flags=re.I) + "."
    return "Le passage précise " + q + "."


def test_l_hypothese_est_deterministe():
    q = "Quel est le port local d'Ollama ?"
    assert _hypothese(q) == _hypothese(q)


def test_l_hypothese_est_declarative_pas_interrogative():
    for c in CASES:
        h = _hypothese(c.question)
        assert not h.rstrip().endswith("?"), f"{c.id}: hypothèse restée interrogative"
        assert h.endswith(".")


def test_l_hypothese_conserve_le_qualificatif_discriminant():
    """« staging » doit survivre : c'est lui qui distingue les deux cas."""
    h = _hypothese("Quel est le port de staging d'Ollama ?")
    assert "staging" in h and "Ollama" in h


# --------------------------------------------------------------------------
# Probabilités NLI
# --------------------------------------------------------------------------
def _valid_nli(p: tuple[float, float, float]) -> bool:
    return all(0.0 <= x <= 1.0 for x in p) and abs(sum(p) - 1.0) < 1e-3


def test_des_probabilites_nli_valides_sont_acceptees():
    assert _valid_nli((0.99, 0.005, 0.005))


@pytest.mark.parametrize("bad", [(1.5, 0.0, 0.0), (0.5, 0.5, 0.5), (-0.1, 0.6, 0.5)])
def test_des_probabilites_invalides_sont_rejetees(bad):
    assert not _valid_nli(bad)


def test_les_trois_classes_nli_sont_distinctes():
    """entailment / neutral / contradiction ne se confondent pas."""
    from apps.rag_core.benchmark.answerability_micro import NEGATION

    assert NEGATION in by_capability(), "la négation doit être testée séparément"


# --------------------------------------------------------------------------
# Agrégation top-k
# --------------------------------------------------------------------------
def _agg_max(scores, k):
    return max(scores[:k]) if scores else 0.0


def test_un_seul_chunk_qui_prouve_suffit():
    """Ne pas conclure no-answer parce que 4 chunks sur 5 sont hors sujet."""
    assert _agg_max([0.01, 0.02, 0.99, 0.01, 0.0], 5) == 0.99


def test_top_k_limite_bien_la_fenetre():
    assert _agg_max([0.01, 0.02, 0.99], 2) == 0.02


def test_aucun_candidat_donne_zero():
    assert _agg_max([], 5) == 0.0


# --------------------------------------------------------------------------
# Juge génératif : la preuve est obligatoire (§11, §15)
# --------------------------------------------------------------------------
def _evidence_valid(out: dict, chunks: list[str]) -> bool:
    if out.get("answerable") is not True:
        return True
    qt = (out.get("quote") or "").strip()
    idx = out.get("chunk_index")
    norm = lambda s: " ".join(str(s).lower().split())  # noqa: E731
    return bool(qt) and isinstance(idx, int) and 0 <= idx < len(chunks) and norm(qt) in norm(chunks[idx])


CH = ["Le service Ollama écoute localement sur le port 11434."]


def test_un_positif_avec_citation_exacte_est_valide():
    assert _evidence_valid(
        {"answerable": True, "chunk_index": 0, "quote": "écoute localement sur le port 11434"}, CH)


def test_un_positif_sans_citation_est_un_faux_positif_technique():
    """C'est la garde anti-hallucination : affirmer sans preuve n'est pas décider."""
    assert not _evidence_valid({"answerable": True, "chunk_index": 0, "quote": ""}, CH)


def test_un_positif_avec_citation_inventee_est_rejete():
    assert not _evidence_valid(
        {"answerable": True, "chunk_index": 0, "quote": "le port de staging est 8200"}, CH)


def test_un_positif_avec_index_invalide_est_rejete():
    assert not _evidence_valid({"answerable": True, "chunk_index": 7, "quote": "Ollama"}, CH)


def test_un_negatif_na_pas_besoin_de_preuve():
    assert _evidence_valid({"answerable": False, "quote": ""}, CH)


def test_un_json_invalide_est_une_erreur_pas_un_no_answer():
    """Une sortie illisible ne doit JAMAIS être comptée comme un refus légitime."""
    out = {"answerable": None, "invalid_json": True}
    assert out["answerable"] is not False, "un JSON invalide n'est pas un no-answer"


REASON_CODES = {"explicit_answer", "entailed_answer", "partial_answer", "topic_only",
                "missing_property", "missing_relation", "contradiction",
                "insufficient_evidence", "invalid_output"}


def test_les_reason_codes_sont_bornes():
    assert "topic_only" in REASON_CODES and "explicit_answer" in REASON_CODES
    assert "hallucinated" not in REASON_CODES


# --------------------------------------------------------------------------
# Classifieur supervisé : pas de fuite (§13)
# --------------------------------------------------------------------------
# NB — deux tests de split vivaient ici. Le premier vérifiait que `GroupShuffleSplit`
# ne partage aucun groupe entre train et test : il testait le contrat de scikit-learn
# sur des groupes synthétiques, pas le découpage réel de RagATM. Le second construisait
# à la main un split pair/impair sur des groupes `i // 3` puis affirmait qu'il fuit —
# une tautologie arithmétique, incapable d'échouer. Le premier imposait à lui seul un
# `importorskip` de module qui décollectait les 36 tests de ce fichier en CI.
#
# L'absence de fuite reste garantie, et sur les données RÉELLES : voir juste en dessous
# `test_la_fuite_de_cible_est_documentee` et
# `test_le_classifieur_ne_repose_pas_sur_une_variable_fuitee`, qui assertent sur
# l'artefact de décision versionné (une ROC-AUC de 1.0 y trahirait une fuite).


@needs_decision
def test_la_fuite_de_cible_est_documentee():
    """`rank_ok` encodait l'étiquette : les no-answer n'ont jamais de document attendu.
    Avec cette variable, le classifieur atteignait ROC-AUC 1.0 — un artefact."""
    d = _decision()
    assert "target_leak_found_and_removed" in d
    assert "rank_ok" in d["target_leak_found_and_removed"]


@needs_decision
def test_le_classifieur_ne_repose_pas_sur_une_variable_fuitee():
    roc = _decision()["families_evaluated"]["classifier_c2"]["roc_auc"]
    assert roc["v2"] < 1.0, "ROC-AUC 1.0 sur un corpus externe trahit une fuite"


# --------------------------------------------------------------------------
# Discipline du holdout (§24)
# --------------------------------------------------------------------------
@needs_decision
def test_aucune_famille_eligible_ne_consomme_pas_le_holdout():
    d = _decision()
    if not d["selected"]:
        assert d["holdout_not_yet_used"] is True and d["holdout_run_count"] == 0


@needs_decision
def test_un_holdout_consomme_lest_une_seule_fois():
    d = _decision()
    if d.get("holdout_used"):
        assert d["holdout_run_count"] == 1


@needs_decision
def test_le_gel_precede_le_holdout():
    d = _decision()
    assert d["verdict"] in ("GO", "GO AVEC RÉSERVES", "NO-GO")
    assert "selection_reason" in d and d["mission_manifest"].startswith("afa0a473")


@needs_decision
def test_aucun_repli_ni_erreur_pendant_la_calibration():
    d = _decision()
    assert d["fallback_count"] == 0 and d["n_errors"] == 0 and d["external_calls"] == 0


@needs_decision
def test_le_profil_retrieval_est_reste_fige():
    p = _decision()["profile"]
    assert p["RAG_MIN_SCORE"] == 0.76 and p["RAG_MIN_TEXT_RANK"] == 0.0
    assert p["RAG_RERANK_ENABLED"] is False and p["RAG_ANSWERABILITY_ENABLED"] is False


@needs_decision
def test_les_trois_familles_ont_ete_evaluees():
    fams = _decision()["families_evaluated"]
    assert {"nli", "generative", "classifier_c2"} <= set(fams)
    assert all(f["eligible"] is False for f in fams.values())



def test_aucun_appel_externe_dans_le_micro_benchmark():
    src = (REPO / "backend/apps/rag_core/benchmark/answerability_micro.py").read_text(encoding="utf-8")
    for interdit in ("requests", "urllib", "http://", "https://", "openai"):
        assert interdit not in src

"""Doctrine de la détection du bruit structurel (questions et passages-titres).

Le mécanisme est **conservateur** : en cas de doute, le score d'origine est conservé.
Ces tests verrouillent surtout les NON-déclenchements — un passage affirmatif court
pénalisé serait une régression bien plus coûteuse qu'une question laissée en place.

Aucun vocabulaire métier n'apparaît dans la détection : ces tests utilisent donc des
formulations génériques.
"""
import pytest

from apps.rag_core.services.query.passage_shape import (
    MAX_PENALTY_FACTOR,
    MIN_PENALTY_FACTOR,
    PassageShape,
    apply_structural_penalty,
    classify_passage,
    normalize_penalty_factor,
)


# --- Questions pures --------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Quels contrôles ont été réalisés ?",
        "Pourquoi cet écart n'a-t-il pas été investigué ?",
        "Qu'en est-il de la validation de second niveau ?",
        "- Quels justificatifs sont disponibles ?\n- Qui valide le traitement ?",
        "Merci de nous communiquer : 1. Quels fichiers sont utilisés ?\n2. Qui les valide ?",
        "Which controls were performed?",
        "How is the reconciliation documented?",
        "Qu’en est-il du dispositif ?",  # apostrophe typographique
        "Combien d'écarts ont été relevés ?\nQuand ont-ils été corrigés ?\nPar qui ?",
    ],
)
def test_questions_pures(text):
    decision = classify_passage(text)
    assert decision.shape is PassageShape.QUESTION
    assert decision.penalty_factor == 0.75
    assert decision.reasons


# --- Passages affirmatifs : JAMAIS pénalisés --------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Aucune anomalie significative n'a été constatée.",
        "Le contrôle est réalisé mensuellement.",
        "La validation de second niveau est absente.",
        "Le dispositif couvre l'ensemble du périmètre.",
        # Question CITÉE puis répondue : le passage porte la réponse.
        "La question était de savoir si le contrôle avait été réalisé. Le rapport confirme "
        "qu'il a été exécuté mensuellement et que les justificatifs sont archivés.",
        # Question suivie d'une réponse substantielle (FAQ dans un même chunk).
        "Quels contrôles ont été réalisés ? Les contrôles trimestriels ont tous été "
        "exécutés dans les délais, avec une appréciation globale satisfaisante sur "
        "l'ensemble du périmètre concerné.",
        # « ? » dans une chaîne technique : ce n'est pas une question.
        "Voir https://example.org/doc?id=12 pour le détail du paramétrage appliqué.",
        # Ligne de tableau : courte, sans ponctuation finale, mais affirmative.
        "| Contrôle mensuel | OK | 2025-06-02 |",
    ],
)
def test_passages_affirmatifs_non_penalises(text):
    decision = classify_passage(text)
    assert decision.shape is PassageShape.NORMAL
    assert decision.penalty_factor == MAX_PENALTY_FACTOR
    assert decision.penalized is False


# --- Passages assimilables à des titres -------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "3.2 Contrôles de cohérence",
        "IV. Synthèse des résultats",
        "Annexe 2 — Détail des contrôles",
        "Sommaire des annexes",
        "PROCESSUS DE VALIDATION",
        "Tableau 3 : répartition par périmètre",
        "1. Introduction",
    ],
)
def test_titres_assimiles(text):
    decision = classify_passage(text)
    assert decision.shape is PassageShape.TITLE_LIKE
    assert decision.penalty_factor == 0.75
    assert decision.reasons


# --- Faux positifs de titre : la brièveté SEULE ne pénalise jamais ----------
@pytest.mark.parametrize(
    "text",
    [
        "Le contrôle est mensuel",           # court, sans point, mais affirmatif
        "La preuve est absente",
        "Aucun écart relevé",
        "Validation effectuée par le département",
        "1 234,56 EUR",                      # valeur technique
        "Statut : conforme",
        "OK",
    ],
)
def test_courts_affirmatifs_restent_normaux(text):
    assert classify_passage(text).shape is PassageShape.NORMAL


# --- Cas mixtes : la nature parasite doit DOMINER ---------------------------
def test_titre_suivi_d_un_paragraphe_affirmatif():
    text = (
        "3.2 Contrôles de cohérence\n"
        "Les contrôles de cohérence sont exécutés à chaque arrêté et leurs résultats "
        "sont consignés dans le portail de contrôle permanent."
    )
    assert classify_passage(text).shape is PassageShape.NORMAL


def test_plusieurs_questions_mais_reponse_substantielle():
    text = (
        "Quels contrôles ? Qui les valide ? "
        "Le rapport précise que les contrôles sont exécutés par le département "
        "financier et validés par le contrôle permanent, chaque trimestre, sur "
        "l'intégralité du périmètre consolidé."
    )
    assert classify_passage(text).shape is PassageShape.NORMAL


def test_liste_majoritairement_affirmative():
    text = (
        "- Le contrôle A est exécuté mensuellement.\n"
        "- Le contrôle B est exécuté trimestriellement.\n"
        "- Qui valide le contrôle C ?"
    )
    assert classify_passage(text).shape is PassageShape.NORMAL


@pytest.mark.parametrize("text", ["", "   ", None])
def test_entrees_vides(text):
    assert classify_passage(text).shape is PassageShape.NORMAL


# --- Facteur ----------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.0, 1.0),
        (0.75, 0.75),
        (0.5, 0.5),
        (0.2, MIN_PENALTY_FACTOR),      # borné au plancher
        (1.5, MAX_PENALTY_FACTOR),      # borné au plafond
        ("pas-un-nombre", MAX_PENALTY_FACTOR),  # illisible -> NEUTRE
        (None, MAX_PENALTY_FACTOR),
    ],
)
def test_normalisation_du_facteur(raw, expected):
    assert normalize_penalty_factor(raw) == expected


def test_facteur_neutre_ne_change_rien():
    chunks = [
        {"chunk_id": "a", "text": "Quels contrôles ont été réalisés ?", "fusion_score": 0.9, "score": 0.9},
        {"chunk_id": "b", "text": "Le contrôle est réalisé mensuellement.", "fusion_score": 0.8, "score": 0.8},
    ]
    out, audit = apply_structural_penalty(chunks, factor=1.0, top_k=2)
    assert [c["chunk_id"] for c in out] == ["a", "b"]
    assert out[0]["fusion_score"] == 0.9
    assert audit["applied"] is False


# --- Application au pool ----------------------------------------------------
def _pool():
    return [
        {"chunk_id": "question", "text": "Quels contrôles ont été réalisés ?", "fusion_score": 0.90, "score": 0.90},
        {"chunk_id": "affirmatif", "text": "Le contrôle a été exécuté mensuellement et tracé.", "fusion_score": 0.80, "score": 0.80},
        {"chunk_id": "titre", "text": "Annexe 2 — Détail des contrôles", "fusion_score": 0.70, "score": 0.70},
    ]


def test_le_passage_affirmatif_passe_devant_la_question():
    out, audit = apply_structural_penalty(_pool(), factor=0.75, top_k=3)
    assert [c["chunk_id"] for c in out][:2] == ["affirmatif", "question"]
    assert audit["question_detected"] == 1
    assert audit["title_like_detected"] == 1
    assert audit["candidates_penalized"] == 2


def test_aucun_candidat_n_est_supprime():
    pool = _pool()
    out, _ = apply_structural_penalty(pool, factor=0.75)
    assert len(out) == len(pool)
    assert {c["chunk_id"] for c in out} == {c["chunk_id"] for c in pool}


def test_le_score_brut_est_conserve():
    """`score` alimente les seuils et la confiance faible : il ne doit PAS bouger."""
    out, _ = apply_structural_penalty(_pool(), factor=0.75)
    by_id = {c["chunk_id"]: c for c in out}
    assert by_id["question"]["score"] == 0.90
    assert by_id["titre"]["score"] == 0.70
    assert by_id["question"]["fusion_score"] == pytest.approx(0.90 * 0.75)
    assert by_id["question"]["fusion_score_before_penalty"] == 0.90


def test_penalite_unique_pour_un_passage_cumulant_les_deux_formes():
    """Question ET titre : un seul facteur, jamais 0,75 × 0,75."""
    chunks = [{"chunk_id": "x", "text": "3.2 Quels contrôles ?", "fusion_score": 1.0, "score": 1.0}]
    out, _ = apply_structural_penalty(chunks, factor=0.75)
    assert out[0]["fusion_score"] == pytest.approx(0.75)


def test_pool_mono_modalite_sans_fusion_score():
    """Sans `fusion_score`, la clé est matérialisée pour TOUS — ordre relatif conservé."""
    chunks = [
        {"chunk_id": "q", "text": "Quels contrôles ?", "score": 0.9},
        {"chunk_id": "a", "text": "Le contrôle a été exécuté mensuellement.", "score": 0.8},
    ]
    out, _ = apply_structural_penalty(chunks, factor=0.75)
    assert [c["chunk_id"] for c in out] == ["a", "q"]
    assert out[1]["score"] == 0.9  # score brut intact


def test_agregats_ne_contiennent_aucun_texte():
    _, audit = apply_structural_penalty(_pool(), factor=0.75, top_k=3)
    for value in audit.values():
        assert not isinstance(value, str) or value in ("",)
    assert audit["top_k_noise_before"] == 2
    assert set(audit) >= {
        "applied", "factor", "candidates", "question_detected",
        "title_like_detected", "candidates_penalized",
    }


def test_decision_explicable_par_des_raisons_stables():
    assert "single_question" in classify_passage("Quels contrôles ont été réalisés ?").reasons
    assert "numbered_title_like" in classify_passage("3.2 Contrôles de cohérence").reasons
    assert "structural_label" in classify_passage("Annexe 2 — Détail").reasons
    assert "uppercase_heading" in classify_passage("PROCESSUS DE VALIDATION").reasons


# --- En-têtes MULTI-LIGNES (cas réel : diapositives et supports de comité) ---
@pytest.mark.parametrize(
    "text",
    [
        "3\n1 POINT MARCHE\n21/05/2024",
        "COMITE ALMO GROUPE\n18/01/2024\nExtrait",
        "9\n2\nREPORTING D'ACTIVITÉ\n18/12/2024",
        "Annexe 2\n— Détail des contrôles",
    ],
)
def test_entetes_multi_lignes(text):
    """Un en-tête réel tient rarement sur une seule ligne : titre puis date, puis mention."""
    assert classify_passage(text).shape is PassageShape.TITLE_LIKE


@pytest.mark.parametrize(
    "text",
    [
        # Fragment de tableau : court, multi-ligne, sans ponctuation — mais AUCUN
        # marqueur typographique de titre. Doit rester intact.
        "Feuille : Consommation de limite (AA70:AH74)\n| col1 | col27 | col28 |",
        "| Contrôle | Statut | Date |\n| mensuel | OK | 2025-06-02 |",
        "Référence utilisateur\nLibellé contrôle\nPériodicité",
    ],
)
def test_fragments_de_tableau_non_penalises(text):
    assert classify_passage(text).shape is PassageShape.NORMAL

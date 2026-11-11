"""Contrat de réponse : abstention + citations RÉELLES (rag_core.services.answer_contract).

Cas fondateur (reproduit en dev le 25/07/2026) : la réponse « Je ne sais pas. » était publiée
avec « Sources: [1] … [8] » et un badge vert « 8 sources citées ». Le moteur avait bien retrouvé
8 extraits, le modèle n'en avait cité AUCUN. Ces tests verrouillent la distinction.
"""
import pytest

from apps.rag_core.services.answer_contract import (
    build_answer_contract,
    extract_cited_source_ids,
    finalize_answer,
    format_cited_sources_line,
    is_abstention,
    strip_sources_line,
)


# --- Détection d'abstention -------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    [
        "Je ne sais pas.",
        "je ne sais pas",
        "  Je ne sais pas  ",
        "Je ne sais pas.\n\nSources: [1], [2], [3]",
        "JE NE SAIS PAS !",
        "Je ne dispose pas d'informations suffisantes pour répondre.",
        "Je ne dispose pas des informations nécessaires.",
        "Je n'ai pas assez d'informations pour répondre.",
        "Je n’ai pas suffisamment d’informations.",  # apostrophe typographique
        "Les sources ne permettent pas de répondre à cette question.",
        "Les documents fournis ne contiennent pas cette information.",
        "Pas assez d'informations dans les extraits.",
        "Informations insuffisantes.",
        "Impossible de répondre à partir des sources.",
        "Je ne peux pas répondre avec les extraits fournis.",
        "I don't know.",
        "I do not know.",
        "I cannot answer from the provided sources.",
        "Not enough information in the excerpts.",
        "The sources do not allow answering this question.",
        # Avertissement ajouté en aval (mode partial) : il ne masque pas l'abstention.
        "⚠️ Les documents disponibles ne couvrent qu’une partie du sujet.\n\nJe ne sais pas.",
        "",
        "   ",
        "Sources: [1], [2]",  # rien d'autre qu'une ligne de références
    ],
)
def test_is_abstention_true(answer):
    assert is_abstention(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Valteria applique un reporting trimestriel [2].",
        "Le dispositif repose sur trois piliers : gouvernance, mesure et reporting.",
        # Réponse SUBSTANTIELLE qui mentionne au passage une limite : ce n'est pas une
        # abstention — elle a le droit de citer ses sources.
        (
            "Tessaly et Valteria partagent le même socle documentaire [1]. Valteria ajoute un reporting qualité "
            "mensuel [3] alors que Tessaly le produit trimestriellement [2]. En revanche, les "
            "sources ne permettent pas de répondre sur le volet maintenance préventive."
        ),
        "Je ne sais pas précisément le montant, mais les sources indiquent un plafond de 5 M€ [3].",
    ],
)
def test_is_abstention_false(answer):
    assert is_abstention(answer) is False


# --- Extraction des citations ----------------------------------------------
def test_extract_cited_source_ids_dedup_sort_and_bounds():
    text = "A [3], B [1], C [3] et une hallucination [12]."
    assert extract_cited_source_ids(text, max_id=8) == [1, 3]


def test_extract_cited_source_ids_no_sources():
    assert extract_cited_source_ids("Réponse [1]", max_id=0) == []


def test_strip_sources_line():
    assert strip_sources_line("Réponse.\n\nSources: [1], [2]") == "Réponse."
    assert strip_sources_line("Réponse.\nsource : [1]") == "Réponse."


def test_format_cited_sources_line():
    assert format_cited_sources_line([2, 5]) == "Sources: [2], [5]"
    assert format_cited_sources_line([]) == ""


# --- Contrat ----------------------------------------------------------------
def test_contract_abstention_claims_nothing():
    """8 sources retrouvées, ZÉRO citée : c'est tout l'objet du correctif."""
    text, contract = finalize_answer("Je ne sais pas.", prompt_sources_count=8, retrieved_sources_count=20)
    assert text == "Je ne sais pas."
    assert "Sources:" not in text
    assert contract.answer_is_abstention is True
    assert contract.cited_source_ids == ()
    assert contract.cited_sources_count == 0
    assert contract.prompt_sources_count == 8
    assert contract.retrieved_sources_count == 20


def test_contract_abstention_drops_model_reference_line():
    text, contract = finalize_answer(
        "Je ne sais pas.\n\nSources: [1], [2], [3]", prompt_sources_count=3
    )
    assert text == "Je ne sais pas."
    assert contract.cited_source_ids == ()


def test_contract_citations_are_the_model_s_own():
    """La ligne finale n'énumère PLUS le top-k : seulement ce qui est cité."""
    text, contract = finalize_answer(
        "Valteria publie un reporting mensuel [2], contre trimestriel pour Tessaly [5].",
        prompt_sources_count=8,
    )
    assert text.endswith("Sources: [2], [5]")
    assert contract.cited_source_ids == (2, 5)
    assert contract.answer_is_abstention is False


def test_contract_model_sources_line_counts_as_citation():
    text, contract = finalize_answer("Réponse substantielle et documentée.\nSources: [4]", prompt_sources_count=8)
    assert contract.cited_source_ids == (4,)
    assert text.endswith("Sources: [4]")


def test_contract_answer_without_citation_gets_no_reference_line():
    """On n'invente pas une traçabilité que le modèle n'a pas fournie."""
    text, contract = finalize_answer("Le dispositif repose sur trois piliers.", prompt_sources_count=8)
    assert "Sources:" not in text
    assert contract.cited_source_ids == ()
    assert contract.answer_is_abstention is False


def test_contract_hallucinated_reference_is_dropped():
    text, contract = finalize_answer("Selon [12], le seuil est de 5 %.", prompt_sources_count=8)
    assert contract.cited_source_ids == ()
    assert "Sources:" not in text


def test_contract_force_abstention_for_deterministic_refusals():
    """Garde Excel / no_answer : aucune génération, donc aucune source citée."""
    contract = build_answer_contract("", prompt_sources_count=8, force_abstention=True)
    assert contract.answer_is_abstention is True
    assert contract.cited_source_ids == ()


def test_contract_retrieved_never_below_prompt_count():
    contract = build_answer_contract("Réponse [1].", prompt_sources_count=8, retrieved_sources_count=3)
    assert contract.retrieved_sources_count == 8


def test_contract_to_dict_shape():
    contract = build_answer_contract("Réponse [1].", prompt_sources_count=4, retrieved_sources_count=9)
    assert contract.to_dict() == {
        "retrieved_sources_count": 9,
        "prompt_sources_count": 4,
        "cited_source_ids": [1],
        "cited_sources_count": 1,
        "answer_is_abstention": False,
        # `None` et non `False` : on ne signale pas une troncature qu'on n'a pas
        # constatée. L'absence d'information et l'absence de troncature ne sont pas
        # la même chose, et les confondre ferait passer une réponse coupée pour
        # complète dès que le compteur de tokens manquerait.
        "answer_truncated": None,
    }

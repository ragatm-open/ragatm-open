"""Une réponse coupée doit le dire.

Le plafond de génération (`RAG_FACTUAL_NUM_PREDICT`) coupe la réponse au milieu d'une
phrase. La ligne « Sources: [1], [2] » est ajoutée APRÈS, par `finalize_answer` : une
réponse tronquée reçoit donc un pied de page bien formé et un badge de citations. Elle
est indistinguable d'une réponse complète — y compris pour son lecteur.

Mesuré le 23/08/2026 sur 200 réponses réelles : 6 % en production, 20 % en staging.
"""
import pytest

from apps.rag_core.services.answer_contract import (
    answer_was_truncated,
    build_answer_contract,
    declared_stop_reason,
    finalize_answer,
)


# --- Le détecteur ----------------------------------------------------------

@pytest.mark.parametrize(
    ("usage", "plafond", "attendu"),
    [
        ({"completion_tokens": 768}, 768, True),    # exactement le plafond
        ({"completion_tokens": 900}, 768, True),    # au-delà (le provider peut dépasser)
        ({"completion_tokens": 767}, 768, False),   # juste en dessous
        ({"completion_tokens": 42}, 768, False),
        ({"completion_tokens": 0}, 768, False),
    ],
)
def test_le_detecteur_compare_les_tokens_produits_au_plafond(usage, plafond, attendu):
    """Signal DÉTERMINISTE, pas une heuristique de ponctuation.

    Vérifié contre la Gateway de production le 23/08/2026 :
        num_predict=48    -> completion_tokens = 48    texte coupé
        num_predict=4096  -> completion_tokens = 1507  texte complet
    """
    assert answer_was_truncated(usage, plafond) is attendu


# --- La raison DÉCLARÉE prime ----------------------------------------------

@pytest.mark.parametrize(
    ("raw", "attendu"),
    [
        ({"done_reason": "length"}, True),                     # Gateway / Ollama
        ({"done_reason": "stop"}, False),
        ({"done_reason": "LENGTH"}, True),                     # casse indifférente
        ({"choices": [{"finish_reason": "length"}]}, True),    # OpenAI
        ({"choices": [{"finish_reason": "stop"}]}, False),
    ],
)
def test_la_raison_d_arret_declaree_tranche_seule(raw, attendu):
    """Vérifié le 23/08/2026 contre la Gateway de production, synchrone ET flux :

        num_predict=16    -> done_reason='length'  completion_tokens=16
        num_predict=4096  -> done_reason='stop'    completion_tokens=832

    Le `usage` passé ici CONTREDIT délibérément la raison déclarée : c'est la raison
    qui doit l'emporter, sinon le repli arithmétique n'est pas un repli mais un second
    avis qui annule le premier.
    """
    contradictoire = {"completion_tokens": 768 if not attendu else 1}
    assert answer_was_truncated(contradictoire, 768, raw=raw) is attendu


@pytest.mark.parametrize("raw", [None, {}, "pas un dict", {"done_reason": ""},
                                 {"done_reason": "load"}, {"choices": []},
                                 {"choices": [{"finish_reason": None}]}])
def test_sans_raison_exploitable_on_retombe_sur_l_arithmetique(raw):
    """Une raison absente, vide ou inconnue n'est pas « complet ». Le repli reste
    nécessaire : tous les fournisseurs ne déclarent pas."""
    assert answer_was_truncated({"completion_tokens": 768}, 768, raw=raw) is True
    assert answer_was_truncated({"completion_tokens": 10}, 768, raw=raw) is False


def test_declared_stop_reason_ne_devine_pas():
    assert declared_stop_reason({"done_reason": "length"}) == "length"
    assert declared_stop_reason({"choices": [{"finish_reason": "Stop"}]}) == "stop"
    assert declared_stop_reason({}) is None
    assert declared_stop_reason(None) is None
    assert declared_stop_reason({"done": True}) is None, (
        "`done: True` ne dit RIEN de la cause de l'arrêt — c'est la confusion qui a "
        "fait croire un temps que la Gateway ne déclarait pas de raison"
    )


@pytest.mark.parametrize("usage", [None, {}, {"completion_tokens": None},
                                   {"completion_tokens": "abc"}, "pas un dict"])
def test_sans_information_on_ne_signale_rien(usage):
    """On ne signale pas une troncature qu'on n'a pas constatée : un faux positif
    apprendrait à l'utilisateur à ignorer l'avertissement."""
    assert answer_was_truncated(usage, 768) is False


@pytest.mark.parametrize("plafond", [0, -1, None, "x"])
def test_un_plafond_absent_ou_absurde_ne_declenche_rien(plafond):
    assert answer_was_truncated({"completion_tokens": 10_000}, plafond) is False


# --- Le contrat porte l'information ----------------------------------------

def test_le_contrat_distingue_inconnu_de_non_tronque():
    """`None` et `False` ne disent pas la même chose. Les confondre ferait passer une
    réponse coupée pour complète dès que le compteur manquerait."""
    assert build_answer_contract("R [1].", prompt_sources_count=2).to_dict()["answer_truncated"] is None
    assert build_answer_contract("R [1].", prompt_sources_count=2,
                                 answer_truncated=False).to_dict()["answer_truncated"] is False
    assert build_answer_contract("R [1].", prompt_sources_count=2,
                                 answer_truncated=True).to_dict()["answer_truncated"] is True


def test_une_reponse_tronquee_conserve_ses_citations_et_son_drapeau():
    """La ligne « Sources: » reste ajoutée — les citations émises avant la coupure sont
    réelles et vérifiables. Ce qui change est qu'on dit AUSSI que c'est incomplet."""
    texte, contrat = finalize_answer(
        "Selon [1], la méthode repose sur une estimation. Elles ne",
        prompt_sources_count=3, answer_truncated=True,
    )
    assert texte.endswith("Sources: [1]")
    assert contrat.answer_truncated is True
    assert contrat.cited_source_ids == (1,)


def test_une_abstention_tronquee_reste_une_abstention():
    """Une abstention ne cite rien, tronquée ou non — et le drapeau n'y change rien."""
    _, contrat = finalize_answer("Je ne sais pas.", prompt_sources_count=3,
                                 answer_truncated=True)
    assert contrat.answer_is_abstention is True
    assert contrat.cited_source_ids == ()
    assert contrat.answer_truncated is True


# --- Le câblage : les deux chemins du chat ---------------------------------

def test_les_chemins_du_chat_transmettent_le_drapeau():
    """Le chemin de l'interface est `chat_stream` : c'est celui qui compte. Un
    câblage partiel laisserait la troncature invisible là où elle est vue."""
    import inspect

    from apps.chat.services import chat_service

    src = inspect.getsource(chat_service)
    assert src.count("answer_truncated=answer_was_truncated(") == 2, (
        "les deux chemins de génération (synchrone et flux) doivent détecter la "
        "troncature ; le résumé d'actualité en est exclu, faute de compteur de tokens"
    )
    # …et le flux lit bien l'événement final du stream, pas une variable du chemin sync.
    i = src.index("final_answer, sources_contract = finalize_answer_with_sources(")
    assert "(final_result or {}).get(\"usage\")" in src[i:i + 700]
    assert src.count('raw=(llm_response or {}).get("raw")') == 1
    assert src.count('raw=(final_result or {}).get("raw")') == 1, (
        "sans `raw`, la détection retombe sur l'arithmétique alors que la Gateway "
        "déclare la raison d'arrêt — un signal exact remplacé par une inférence"
    )

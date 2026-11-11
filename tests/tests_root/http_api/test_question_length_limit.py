"""La borne de longueur d'une question — au niveau du CONTRAT, pas de l'interface.

Ce que ce fichier verrouille
----------------------------
`question` était déclarée `CharField(allow_blank=True)`, sans `max_length`. Les seules
limites étaient très en amont : `DATA_UPLOAD_MAX_MEMORY_SIZE` (25 Mo) et
`client_max_body_size 50m` côté nginx.

Or le classifieur d'intention s'exécute sur **chaque** requête, **avant tout
retrieval**, et son coût est QUADRATIQUE sur certaines entrées adversariales — mesuré
le 04/09/2026 sur une répétition de guillemets droits : 4 000 caractères coûtent 242 ms,
8 000 en coûtent 970, 16 000 en coûtent 3 910. Une requête authentifiée de 100 Ko
occupait donc un worker web plusieurs minutes, avant d'avoir cherché quoi que ce soit.

La borne ne corrige pas le défaut de complexité — c'est un lot distinct. Elle
l'empêche d'être **atteignable**.

Deux portes, pas une
--------------------
`/api/v1/chat/query` et `/api/v1/core/retrieve` délèguent tous deux à `query_rag`,
donc au même classifieur. Borner une seule des deux ne fermerait rien : les deux
serializers sont testés ici.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from api.v1.chat.serializers import ChatRequestSerializer
from api.v1.core.serializers import CoreRetrieveSerializer
from api.v1.limits import MAX_QUESTION_CHARS

_ESPACE = "00000000-0000-0000-0000-000000000001"


def _chat(question: str) -> ChatRequestSerializer:
    return ChatRequestSerializer(data={"rag_space_id": _ESPACE, "question": question})


def _core(question: str) -> CoreRetrieveSerializer:
    return CoreRetrieveSerializer(data={"rag_space_id": _ESPACE, "question": question})


# ===========================================================================
# T1 / T2 — la borne elle-même
# ===========================================================================
@pytest.mark.parametrize("fabrique", [_chat, _core], ids=["chat", "core"])
def test_t1_une_question_a_la_limite_exacte_est_acceptee(fabrique):
    assert fabrique("a" * MAX_QUESTION_CHARS).is_valid(), "la limite doit être inclusive"


@pytest.mark.parametrize("fabrique", [_chat, _core], ids=["chat", "core"])
def test_t2_un_caractere_de_trop_est_refuse(fabrique):
    s = fabrique("a" * (MAX_QUESTION_CHARS + 1))
    assert not s.is_valid()
    assert "question" in s.errors


@pytest.mark.parametrize("fabrique", [_chat, _core], ids=["chat", "core"])
def test_le_refus_ne_tronque_jamais(fabrique):
    """Une question coupée produirait une réponse à une AUTRE question.

    C'est pourquoi le dépassement est une erreur de validation et non une
    normalisation silencieuse.
    """
    s = fabrique("a" * (MAX_QUESTION_CHARS + 1))
    assert not s.is_valid()
    assert "validated_data" not in s.__dict__ or not s.validated_data


# ===========================================================================
# T3 — la borne compte des CARACTÈRES, pas des octets
# ===========================================================================
def test_t3_la_limite_porte_sur_les_caracteres_unicode():
    """Une question en français ou en japonais ne doit pas être pénalisée.

    `é` pèse 2 octets et `語` en pèse 3 : compter en octets rendrait la limite
    deux à trois fois plus stricte pour ces langues, alors que le coût du
    classifieur suit le nombre de caractères.
    """
    for caractere in ("é", "語", "🙂"):
        texte = caractere * MAX_QUESTION_CHARS
        assert len(texte) == MAX_QUESTION_CHARS
        assert len(texte.encode("utf-8")) > MAX_QUESTION_CHARS, "cas de test mal choisi"
        assert _chat(texte).is_valid(), f"{caractere!r} refusé alors qu'il tient dans la limite"


# ===========================================================================
# T4 / T5 — aucune régression sur l'existant
# ===========================================================================
def test_t4_une_question_vide_garde_son_comportement_historique():
    """`allow_blank=True` reste vrai : c'est `validate()` qui exige un contenu,
    sauf si un `corpus_choice` structuré remplace la question."""
    sans_choix = _chat("")
    assert not sans_choix.is_valid()
    assert "question" in sans_choix.errors

    from apps.chat.services.corpus_mode_service import CHOICES

    avec_choix = ChatRequestSerializer(
        data={"rag_space_id": _ESPACE, "question": "", "corpus_choice": CHOICES[0]}
    )
    assert avec_choix.is_valid(), avec_choix.errors


@pytest.mark.parametrize(
    "question",
    [
        "Quelle est la date du rapport ?",
        "Compare les deux dispositifs de reporting réglementaire.",
        "What are the differences between framework A and framework B?",
        # p99.9 de production (156 caractères) et max observé (164) : les usages
        # réels doivent passer sans y penser.
        "q" * 156,
        "q" * 164,
        # Max observé en staging (324) et dans les jeux de qualification (413).
        "q" * 324,
        "q" * 413,
    ],
)
def test_t5_les_usages_reels_mesures_restent_acceptes(question):
    assert _chat(question).is_valid()


# ===========================================================================
# T6 — LE test du lot : le rejet précède les heuristiques
# ===========================================================================
def test_t6_une_entree_adversariale_hors_limite_n_atteint_jamais_les_heuristiques():
    """La borne doit fermer la porte AVANT le chemin quadratique.

    Un plafond qui ne serait appliqué qu'après le classifieur ne protégerait de
    rien : le coût est payé à l'entrée de `query_rag`, pas à la sortie.
    """
    adversariale = '"' * (MAX_QUESTION_CHARS + 1)

    with patch(
        "apps.rag_core.services.query.heuristics.detect_comparative_intent"
    ) as comparatif, patch(
        "apps.rag_core.services.query.heuristics.is_explanatory_question"
    ) as explicatif:
        assert not _chat(adversariale).is_valid()
        assert not _core(adversariale).is_valid()

    comparatif.assert_not_called()
    explicatif.assert_not_called()


def test_une_entree_de_100_ko_est_refusee():
    """La taille qui occupait un worker plusieurs minutes.

    100 000 caractères de guillemets : ~150 s de CPU extrapolées des mesures
    réelles, avant tout retrieval.
    """
    assert not _chat('"' * 100_000).is_valid()
    assert not _core('"' * 100_000).is_valid()


# ===========================================================================
# La constante : une seule, et pas configurable
# ===========================================================================
def test_les_deux_portes_partagent_la_meme_borne():
    """Deux nombres magiques divergents seraient pires qu'aucune borne : la porte
    la plus permissive déciderait, sans que personne ne le sache."""
    for serializer in (ChatRequestSerializer(), CoreRetrieveSerializer()):
        champ = serializer.fields["question"]
        validateurs = [v for v in champ.validators if hasattr(v, "limit_value")]
        assert validateurs, "aucune borne de longueur sur `question`"
        assert validateurs[0].limit_value == MAX_QUESTION_CHARS


def test_la_borne_n_est_pas_desserrable_par_l_environnement():
    """Une borne de sécurité qui se règle par variable d'environnement n'est pas
    une borne — même doctrine que `MAX_REQUEST_ID_CHARS`."""
    import os

    from api.v1 import limits

    assert isinstance(limits.MAX_QUESTION_CHARS, int)
    source = (
        __import__("pathlib").Path(limits.__file__).read_text(encoding="utf-8")
    )
    for lecture in ("os.getenv", "os.environ", "getattr(settings", "env_int", "env_str"):
        assert lecture not in source, f"`{lecture}` rendrait la borne desserrable"
    assert "MAX_QUESTION_CHARS" not in os.environ


def test_le_frontend_declare_la_meme_valeur():
    """Confort de saisie aligné sur le contrat. Le backend reste l'autorité — ce
    test empêche seulement les deux nombres de diverger en silence."""
    from pathlib import Path

    composer = (
        Path(__file__).resolve().parents[3]
        / "frontend/src/app/chat/_components/Composer.tsx"
    ).read_text(encoding="utf-8")
    assert f"MAX_QUESTION_CHARS = {MAX_QUESTION_CHARS}" in composer, (
        "la valeur du Composer a divergé de celle du contrat d'API"
    )
    assert "maxLength={MAX_QUESTION_CHARS}" in composer

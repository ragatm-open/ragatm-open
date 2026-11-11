"""valeur d'une énumération « respectively » rattachée à une AUTRE entité.

Constat réel (PB14 C B002 flux, PG15 B002 sync) : source « … with EU (16) and Chinese ones (20 % and 16 %, respectively,
of the total mixed deals with a US partner …) » ; réponse « 16 % des deals mixtes … impliquent un investisseur européen »
[1]. La valeur existe dans [1] : le contrôle numérique l'acceptait ; l'énumération donne 16 % à la Chine, 20 % à l'UE.

Contrat : une phrase CITÉE qui place une valeur d'une énumération de SA source citée près d'une autre entité de
l'énumération, sans nommer l'entité de cette valeur, est retirée — sauf si une autre phrase de la source citée relie la
valeur à l'entité nommée. Une affirmation étayée par sa source citée n'est jamais retirée. Entités, valeurs et
formulations SYNTHÉTIQUES, sans lien avec le benchmark.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.query import numeric_evidence as NE

SRC_A = "Japanese and German investors account for 31 % and 12 %, respectively, of cross-border rounds."
SRC_B = "Among deals with a French lead, UK and Indian partners appear in 44 % and 9 % of cases, respectively."
SRC_C = "Japanese investors (7) and German ones (31 % and 12 %, respectively, of rounds with a French lead) dominate."
SRC_D = "China, India and Japan represent 18 %, 18 % and 25 %, respectively."
SRC_E = "These shares reach 31 % and 12 %, respectively."
SRC_F = ("Japanese investors account for 31 % of rounds. German and Japanese investors account for 12 % and 31 %, "
         "respectively.")
SRC_FR = "Les fonds japonais et allemands pèsent 31 % et 12 %, respectivement."


def _src(text, doc="d1", page=4):
    return {"chunk_id": f"c-{doc}", "text": text,
            "source": {"document_id": doc, "page_number": page, "filename": f"{doc}.pdf", "meta": {"source_is_ocr": False}}}


def _flagged(answer, *texts):
    return bool(NE._misattributed_list_values(NE._split(answer), [_src(t, doc=f"d{i}") for i, t in enumerate(texts, 1)]))


@pytest.mark.parametrize("source,answer,expected", [
    pytest.param(SRC_A, "Japanese investors account for 31 % of cross-border rounds [1].", False, id="en-correct"),
    pytest.param(SRC_A, "Les investisseurs japonais représentent 31 % des tours transfrontaliers [1].", False, id="fr-correct"),
    pytest.param(SRC_A, "Les investisseurs allemands représentent 31 % des tours transfrontaliers [1].", True, id="fr-swapped"),
    pytest.param(SRC_A, "German investors account for 31 % of cross-border rounds [1].", True, id="en-swapped"),
    pytest.param(SRC_A, "Les Japonais pèsent 31 % et les Allemands 12 % [1].", False, id="fr-both-correct"),
    pytest.param(SRC_A, "Japonais et Allemands pèsent 31 % et 12 % [1].", False, id="fr-both-one-clause"),
    pytest.param(SRC_A, "Les Allemands pèsent 31 % contre 12 % pour les Japonais [1].", False, id="owner-named-in-clause"),
    pytest.param(SRC_B, "Les partenaires indiens apparaissent dans 44 % des opérations [1].", True, id="wrong-member"),
    pytest.param(SRC_B, "Les partenaires britanniques apparaissent dans 44 % des opérations [1].", False, id="right-member"),
    pytest.param(SRC_C, "Les investisseurs allemands représentent 7 % des tours [1].", False, id="footnote-not-a-value"),
    pytest.param(SRC_C, "Les investisseurs japonais représentent 31 % des tours [1].", False, id="footnote-list-correct"),
    pytest.param(SRC_C, "Les investisseurs allemands représentent 31 % des tours [1].", True, id="footnote-list-swapped"),
    pytest.param(SRC_D, "L'Inde représente 18 % [1].", False, id="duplicate-values-ambiguous"),
    pytest.param(SRC_D, "Le Japon représente 18 % [1].", False, id="duplicate-values-rejected"),
    pytest.param(SRC_E, "Les Allemands pèsent 31 % [1].", False, id="no-entities-in-source"),
    pytest.param(SRC_F, "Les investisseurs japonais représentent 31 % des tours [1].", False, id="independent-evidence"),
    pytest.param(SRC_A, "Les investisseurs français représentent 31 % des tours [1].", False, id="entity-outside-list"),
    pytest.param(SRC_FR, "German funds weigh 31 % [1].", True, id="fr-source-en-answer"),
    pytest.param(SRC_A + " Overall volume grew by 12 % in 2023.", "Le volume des Allemands a crû de 12 % [1].", False,
                 id="several-numbers"),
])
def test_list_attribution(source, answer, expected):
    assert _flagged(answer, source) is expected


def test_uncited_sentence_is_never_checked():
    assert _flagged("Les investisseurs allemands représentent 31 % des tours.", SRC_A) is False


def test_other_cited_source_is_out_of_scope():
    """Valeur coïncidente sous une autre source citée : rien n'est démontré contre elle."""
    assert _flagged("La croissance des investissements allemands a atteint 31 % en 2023 [2].",
                    SRC_A, "Venture funding in Europe rose sharply in 2023.") is False


def test_claim_supported_by_its_cited_source_is_never_removed():
    supported = SRC_A + " German investors also account for 31 % of seed rounds."
    assert _flagged("Les investisseurs allemands représentent 31 % des tours d'amorçage [1].", supported) is False


def test_residual_unsupported_coincident_value_is_removed_documented():
    """Limite DOCUMENTÉE : même source citée, autre métrique, valeur coïncidente. La source ne l'étaye pas (aucune phrase
    ne relie 31 % aux Allemands) et attribue 31 % à une autre entité : la phrase est retirée — non certifiée, sans
    prétendre démontrer que la métrique est la même."""
    assert _flagged("German venture funding grew by 31 % in 2023 [1].", SRC_A) is True


def test_values_and_entities_are_not_hard_coded():
    """Permutation des valeurs et des entités : le contrôle suit la source, pas des constantes."""
    src = "Indian and French investors account for 64 % and 3 %, respectively, of late-stage rounds."
    assert _flagged("Les investisseurs français représentent 64 % des tours [1].", src) is True
    assert _flagged("Les investisseurs indiens représentent 64 % des tours [1].", src) is False


class TestGuardIntegration:
    def test_unprotected_context_removes_the_sentence_with_notice(self):
        answer = "Les investisseurs japonais pèsent 12 % [1]. Le marché reste concentré [1]."
        d = NE.guard_answer(answer, question="Quelle part pour chaque pays ?", sources=[_src(SRC_A)], language="fr")
        assert d.verdict == NE.VERDICT_REDACT and d.list_attributions_removed == 1
        assert "12 %" not in d.answer.split("\n\n", 1)[1] and "Le marché reste concentré [1]." in d.answer
        assert d.answer.startswith("⚠️ 1 affirmation(s) attribuant une valeur à une autre entité")
        assert d.to_dict()["list_attributions_removed"] == 1

    def test_english_notice(self):
        d = NE.guard_answer("Japanese investors hold 12 % [1]. Activity is concentrated [1].", question="Shares?",
                            sources=[_src(SRC_A)], language="en")
        assert d.answer.startswith("⚠️ 1 statement(s) attributing a value to an entity other than")

    def test_correct_answer_passes_unchanged(self):
        answer = "Les investisseurs japonais pèsent 31 % et les allemands 12 % [1]."
        d = NE.guard_answer(answer, question="Parts ?", sources=[_src(SRC_A)], language="fr")
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_protected_context_applies_the_same_check(self):
        table = {"chunk_id": "t1", "text": "Row: Lot A\n\nLot:\nLot A\n\nMontant:\n120",
                 "source": {"document_id": "d9", "page_number": 3, "filename": "annexe.pdf",
                            "meta": {"kind": "table", "table": {}}}}
        sources = NE.annotate_numeric_evidence([_src(SRC_A), table], table_pages_by_doc={})
        d = NE.guard_answer("Les investisseurs japonais pèsent 12 % [1]. Le lot A figure à l'annexe [2].",
                            question="Parts ?", sources=sources, language="fr")
        assert NE.context_is_protected(sources) and d.list_attributions_removed == 1

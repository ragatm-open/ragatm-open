"""UNIT-GROUNDING — certification STRICTE des unités affichées.

Constat réel (FINAL-QUALIFICATION-13) : « **15,38** m² » publié sept fois (R05, R06) alors qu'aucune
source ne porte d'unité. Deux causes : le gras Markdown masquait l'unité de l'affirmation, et
``_units_compatible`` acceptait une source sans unité — acceptation lue comme une validation.

Contrat : le statut de la VALEUR est inchangé ; l'unité affichée reçoit son propre statut
(certifiée / absente de la source / ambiguë / contredite) d'après la preuve de CETTE valeur. Une unité
non certifiée est retirée, la valeur conservée, un avis l'indique. Valeurs SYNTHÉTIQUES.
"""
from __future__ import annotations

import pytest

from apps.rag_core.services.query import numeric_evidence as NE

DOC = "aaaaaaaa-0000-0000-0000-000000000001"


def _row(label, page=3, **cells):
    """Fragment du rail générique : « Row: … » puis « En-tête:\\nvaleur » par colonne."""
    text = "\n\n".join([f"Row: {label}", f"Désignation:\n{label}"] + [f"{h}:\n{v}" for h, v in cells.items()])
    return {"chunk_id": f"c-{page}-{len(text)}", "text": text,
            "source": {"document_id": DOC, "page_number": page, "filename": "surfaces.pdf",
                       "meta": {"kind": "table", "table_type": "generic", "table": {"table_type": "generic"}}}}


def _native(text, page=4):
    return {"chunk_id": f"n-{page}-{len(text)}", "text": text,
            "source": {"document_id": DOC, "page_number": page, "filename": "surfaces.pdf",
                       "meta": {"source_is_ocr": False}}}


def _guard(answer, *chunks, question, language=None):
    sources = NE.annotate_numeric_evidence(list(chunks), table_pages_by_doc={})
    return NE.guard_answer(answer, question=question, sources=sources, language=language)


Q = "Quelle est la valeur de « Surface » pour la ligne « Local ménage » ?"


class TestUnitStatus:
    def test_unit_written_in_the_cell_is_certified_and_the_answer_unchanged(self):
        answer = "La surface du local ménage est de 15,38 m² [1]."
        d = _guard(answer, _row("Local ménage", Surface="15,38 m²"), question=Q)
        assert (d.verdict, d.answer, d.units) == (NE.VERDICT_PASS, answer, [])

    def test_unit_absent_from_the_source_is_removed_and_the_value_kept(self):
        """Cas R05 : gras Markdown, aucune unité dans la ligne ni dans l'en-tête."""
        d = _guard("La surface du local ménage est de **15,38** m² [1].", _row("Local ménage", Surface="15,38"),
                   question=Q)
        assert d.verdict == NE.VERDICT_REDACT
        assert "est de **15,38** [1]." in d.answer and "m²" not in d.answer.split("\n\n", 1)[1]
        assert d.units == [{"token": "15,38", "value": "15.38", "unit": "m²", "status": NE.UNIT_ABSENT_FROM_SOURCE}]
        assert d.answer.startswith("ℹ️ 1 unité(s) non établie(s) par la source")
        assert d.claims[0]["status"] == NE.CLAIM_SUPPORTED and d.to_dict()["units_removed"] == 1

    def test_unit_declared_by_the_column_header_is_certified(self):
        answer = "La surface du local ménage est de 15,38 m² [1]."
        assert _guard(answer, _row("Local ménage", **{"Surface (m²)": "15,38"}), question=Q).answer == answer
        assert _guard(answer, _row("Local ménage", **{"Surface SU m²": "15,38"}), question=Q).answer == answer

    def test_unit_of_another_column_does_not_certify(self):
        d = _guard("La surface du local ménage est de 15,38 W [1].",
                   _row("Local ménage", Surface="15,38", **{"Puissance (W)": "12"}), question=Q)
        assert d.units[0]["status"] == NE.UNIT_ABSENT_FROM_SOURCE and "15,38 W" not in d.answer

    def test_unit_in_another_section_does_not_certify(self):
        d = _guard("La surface du local ménage est de 15,38 m² [1].", _row("Local ménage", Surface="15,38"),
                   _native("Toutes les surfaces du programme sont exprimées en m² (voir annexe 3).", page=9),
                   question=Q)
        assert d.units and d.units[0]["status"] == NE.UNIT_ABSENT_FROM_SOURCE

    def test_unit_cited_only_by_the_question_is_not_evidence(self):
        d = _guard("La surface du local ménage est de 15,38 m² [1].", _row("Local ménage", Surface="15,38"),
                   question="Quelle est la surface en m² de la ligne « Local ménage » ?")
        assert d.units[0]["status"] == NE.UNIT_ABSENT_FROM_SOURCE and "15,38 [1]" in d.answer

    def test_incompatible_unit_rejects_the_value_and_adds_no_unit_reserve(self):
        """« 11 000 € » sous une colonne « P (W) » : la VALEUR n'est pas prouvée — aucune réserve d'unité."""
        d = _guard("La puissance de la pompe A est de 11 000 € [1].",
                   _row("Pompe A", **{"P installée (W)": "11 000"}),
                   question="Quelle est la valeur de « P installée (W) » pour la ligne « Pompe A » ?")
        assert d.claims[0]["status"] != NE.CLAIM_SUPPORTED and d.units == []
        assert d.verdict == NE.VERDICT_BLOCK

    def test_compound_unit_must_match_entirely(self):
        q = "Quelle est la valeur de « Consommation » pour la ligne « Bâtiment A » ?"
        row = _row("Bâtiment A", **{"Consommation (kWh/m²)": "120"})
        assert _guard("La consommation du bâtiment A est de 120 kWh/m² [1].", row, question=q).verdict == NE.VERDICT_PASS
        d = _guard("La consommation du bâtiment A est de 120 kWh [1].", row, question=q)
        assert d.units[0]["status"] == NE.UNIT_CONTRADICTED and "120 [1]" in d.answer

    def test_two_different_units_for_the_same_value_are_ambiguous(self):
        q = "Quelle est la valeur de « Valeur » pour la ligne « Conso (kWh) » ?"
        row = _row("Conso (kWh)", **{"Valeur (kWh/m²)": "120"})
        d = _guard("La valeur de la ligne Conso est de 120 kWh [1].", row, question=q)
        assert d.units[0]["status"] == NE.UNIT_AMBIGUOUS and "120 kWh" not in d.answer


class TestUnitForms:
    @pytest.mark.parametrize("cell,expected_kept", [("12,5 %", True), ("12,5", False)])
    def test_percent(self, cell, expected_kept):
        d = _guard("Le taux du local ménage est de 12,5 % [1].", _row("Local ménage", Taux=cell),
                   question="Quelle est la valeur de « Taux » pour la ligne « Local ménage » ?")
        assert ("12,5 %" in d.answer) is expected_kept
        assert "Le taux du local ménage est de 12,5" in d.answer

    @pytest.mark.parametrize("header,expected_kept", [("Montant (€)", True), ("Montant", False)])
    def test_amount_and_currency(self, header, expected_kept):
        d = _guard("Le montant du lot A est de 11 000 € [1].", _row("Lot A", **{header: "11 000"}),
                   question="Quelle est la valeur de « Montant » pour la ligne « Lot A » ?")
        assert ("11 000 €" in d.answer) is expected_kept and "11 000" in d.answer

    def test_row_label_unit_certifies_every_column_of_the_row(self):
        """Cas R07 : l'unité est portée par le libellé de ligne ; notations d'équivalent CO2 confondues."""
        q = "Quelle est la valeur de « Projet » pour la ligne « Eges PCE (kg eq.CO2/m²) » ?"
        row = _row("Eges PCE (kg eq.CO2/m²)", Projet="866.2", **{"Niveau C1": "1050"})
        answer = "Pour Eges PCE, la valeur du Projet est de 866.2 kg eq.CO2/m² [1]."
        assert _guard(answer, row, question=q).answer == answer

    def test_unit_column_qualifies_only_the_quantity_column(self):
        """Cas R12/R14 : « UF: m » qualifie la colonne QTE, jamais une autre colonne de la ligne."""
        q = "Quelle est la valeur de « QTE » pour la ligne « Couvre joint » ?"
        row = _row("Couvre joint", QTE="19,6", UF="m", **{"Id fiche": "30082"})
        answer = "La quantité QTE du couvre joint est de 19,6 m [1]."
        assert _guard(answer, row, question=q).answer == answer
        d = _guard("La valeur Id fiche du couvre joint est 30082 m [1].", row,
                   question="Quelle est la valeur de « Id fiche » pour la ligne « Couvre joint » ?")
        assert d.units and d.units[0]["status"] == NE.UNIT_ABSENT_FROM_SOURCE

    def test_several_values_keep_only_their_own_proven_units(self):
        q = "Quelles sont les surfaces de la ligne « Local ménage » ?"
        row = _row("Local ménage", **{"Surface moyenne (m²)": "3,85"}, **{"Surface par catégorie": "15,38"})
        d = _guard("Pour le local ménage, la surface moyenne est 3,85 m² et la surface par catégorie 15,38 m² [1].",
                   row, question=q)
        assert "3,85 m²" in d.answer and "15,38 m²" not in d.answer and "15,38" in d.answer
        assert [u["token"] for u in d.units] == ["15,38"]

    def test_english_notice(self):
        d = _guard("The area of the cleaning room is 15.38 m² [1].", _row("Cleaning room", Area="15.38"),
                   question="What is the value of “Area” for row « Cleaning room »?", language="en")
        assert d.answer.startswith("ℹ️ 1 unit(s) not established by the source were removed")
        assert "is 15.38 [1]." in d.answer

    def test_native_text_line_carries_its_unit(self):
        q = "Quelle est la valeur de « Surface » pour la ligne « Local ménage » ?"
        d = _guard("La surface du local ménage est de 15,38 m² [2].", _row("Local AEP", Surface="3,31"),
                   _native("Local ménage : surface utile 15,38 m²"), question=q)
        assert "15,38 m²" in d.answer and d.units == []

    def test_unit_never_added_when_the_value_has_none(self):
        answer = "La surface du local ménage est de 15,38 [1]."
        d = _guard(answer, _row("Local ménage", Surface="15,38"), question=Q)
        assert (d.verdict, d.answer, d.units) == (NE.VERDICT_PASS, answer, [])

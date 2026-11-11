"""fiabilité des preuves numériques OCR.

Classes d'erreurs constatées en staging (documents réels, rejeu sous e9d80f7), reproduites avec
des valeurs SYNTHÉTIQUES (aucune donnée des documents) :
  * A1 signe perdu      : -6,2  -> « 6,2 »
  * A2 virgule perdue   : 24,8  -> « 248 »  (réel : 19,6 -> 196 ; 16,0 -> 160)
  * A3 ligne voisine    : 12,90 demandé, 41,25 (ligne au-dessus) répondu
  * A4 autre colonne    : 18 406 demandé, 812 répondu
  * A5 tableau refusé   : texte OCR de la page toujours indexé, présenté comme fiable
  * A6 tableau non routé: aucune preuve de tableau -> OCR ordinaire (réserve, pas de blocage)
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.rag_core.domain.retrieval import build_prompt
from apps.rag_core.services.query import numeric_evidence as NE
from apps.rag_core.services.query.source_meta import project_source_meta

DOC_A, DOC_B = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"


def _chunk(text, *, doc=DOC_A, page=3, filename="bilan.pdf", **meta):
    return {"chunk_id": f"c-{doc[:4]}-{page}-{len(text)}", "text": text,
            "source": {"document_id": doc, "page_number": page, "filename": filename, "label": filename,
                       "meta": dict(meta)}}


def _ocr(text, **kw):
    return _chunk(text, source_is_ocr=True, requires_visual_check=False, page_type="text", **kw)


def _native(text, **kw):
    return _chunk(text, source_is_ocr=False, requires_visual_check=False, page_type="text", **kw)


#: Moteur DÉCLARÉ qualifié, valeur NON certifiée (22B seul) : non probant depuis une version antérieure.
QUALIFIED_UNCERTIFIED = {"experimental": False, "qualified_for_production": True, "extraction_status": "success"}
#: Provenance HYPOTHÉTIQUE entièrement certifiée — le contrat 22A.1 ne l'émet JAMAIS (`certified` toujours faux).
#: Sert uniquement à tester la règle ligne + colonne d'une source probante.
QUALIFIED = {**QUALIFIED_UNCERTIFIED, "schema_version": "2",
             "evidence": {"certified": True, "numeric_claims_supported": True,
                          "consistency_status": "no_anomaly_detected"},
             "qualification": {"business_numeric_use_authorized": True},
             "item_consistency": {"status": "no_anomaly_detected", "anomalies": []}}
#: Provenance réelle des sorties StructX actuelles (staging et production) : expérimentale, non qualifiée.
UNQUALIFIED = {"experimental": True, "qualified_for_production": False, "extraction_status": "success"}


def _structured(text, *, provenance=None, task="table", **kw):
    return _chunk(text, visual={"unit": f"structured_{task}", "task": task, "kind": task,
                                "structured": dict(QUALIFIED if provenance is None else provenance)}, **kw)


def _describe(text, **kw):
    return _chunk(text, visual={"unit": "photo", "task": "describe"}, **kw)


#: Registre : page 3 du document A porte un tableau REFUSÉ, page 5 un tableau publié.
TABLE_PAGES = {DOC_A: {3: "table_refused", 5: "table_published"}}


def _annotated(*chunks, pages=None):
    return NE.annotate_numeric_evidence(list(chunks), table_pages_by_doc=TABLE_PAGES if pages is None else pages)


def _guard(answer, *chunks, question="Quelle est la valeur ?", pages=None):
    return NE.guard_answer(answer, question=question, sources=_annotated(*chunks, pages=pages))


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
class TestClassification:
    def test_registry_pages_from_auto_assets_and_records(self):
        meta = {"structured_visual": {
            "auto": {"assets": {"h1": {"class": "TABLE", "status": "refused", "source_page": 3},
                                "h2": {"class": "OTHER", "status": "skipped", "source_page": 4},
                                "h3": {"class": "CHART", "status": "published", "source_page": 6}}},
            "h9:table": {"status": "published", "source_page": 5}}}
        assert NE.table_pages_from_meta(meta) == {3: "table_refused", 5: "table_published"}
        assert NE.table_pages_from_meta({}) == {} and NE.table_pages_from_meta(None) == {}

    @pytest.mark.parametrize("chunk,expected", [
        (_ocr("x", page=3), (NE.OCR_TABLE_UNVERIFIED, "ocr_page_table_refused")),
        (_ocr("x", page=5), (NE.OCR_TABLE_UNVERIFIED, "ocr_page_table_published")),
        (_ocr("x", page=4), (NE.OCR_OTHER, "ocr_page_without_table_evidence")),
        (_describe("x", page=3), (NE.OCR_TABLE_UNVERIFIED, "visual_description_table_refused")),
        (_describe("x", page=4), (NE.OCR_OTHER, "visual_description")),
        (_structured("x", page=5), (NE.STRUCTURED_ACCEPTED, "structured_visual_certified")),
        (_structured("x", page=5, provenance=QUALIFIED_UNCERTIFIED), (NE.STRUCTURED_UNQUALIFIED, "structured_visual_uncertified")),
        (_structured("x", page=5, provenance=UNQUALIFIED), (NE.STRUCTURED_UNQUALIFIED, "structured_visual_unqualified")),
        (_native("x", page=3), (NE.NATIVE_TEXT, "text_layer")),
        (_chunk("x"), (NE.UNKNOWN, "provenance_not_declared")),
    ])
    def test_status_follows_declared_provenance_only(self, chunk, expected):
        [annotated] = _annotated(chunk)
        meta = annotated["source"]["meta"]
        assert (meta[NE.META_KEY], meta[NE.REASON_KEY]) == expected

    def test_table_ocr_requires_visual_check_and_input_is_not_mutated(self):
        original = _ocr("x", page=3)
        [annotated] = _annotated(original)
        assert annotated["source"]["meta"]["requires_visual_check"] is True
        assert original["source"]["meta"]["requires_visual_check"] is False and NE.META_KEY not in original["source"]["meta"]

    def test_other_document_page_never_inherits_the_table(self):
        [annotated] = _annotated(_ocr("x", doc=DOC_B, page=3))
        assert annotated["source"]["meta"][NE.META_KEY] == NE.OCR_OTHER

    def test_status_is_projected_into_api_sources(self):
        [annotated] = _annotated(_ocr("x", page=3))
        projected = project_source_meta(annotated["source"]["meta"])
        assert projected["numeric_evidence"] == NE.OCR_TABLE_UNVERIFIED
        assert projected["requires_visual_check"] is True


# --------------------------------------------------------------------------- #
# Nombres affirmés
# --------------------------------------------------------------------------- #
class TestNumericClaims:
    @pytest.mark.parametrize("text,value", [
        ("La valeur est 19,6 m.", "19.6"), ("La valeur est 19.6 m.", "19.6"), ("Total : 1 960 kWh", "1960"),
        ("Écart : -6,2 %", "-6.2"), ("Montant : 29 327 €", "29327"), ("Part : 12 %", "12"),
        ("Puissance : 16,0 kW", "16.0"), ("Valeur 11 000.00 W", "11000.00"),
    ])
    def test_formats(self, text, value):
        assert [c.value for c in NE.numeric_claims(text)] == [Decimal(value)]

    @pytest.mark.parametrize("text", [
        "Voir la source [2] et [1]-[4].", "Document du 12/03/2025.", "Révision 2024-11-03.", "Publié en 2025.",
        "Voir p. 12 et page 14.", "Section 5. Détails.", "Le lot 06 et l'article 12.", "1) Contexte\n2) Points clés",
        "Il y a 3 lots.", "Le n° 42 du registre.",
    ])
    def test_references_dates_and_markers_are_not_claims(self, text):
        assert NE.numeric_claims(text) == []

    def test_numbers_repeated_from_the_question_are_not_claims(self):
        claims = NE.numeric_claims("Pour le poste C2.3 / 01-04, la surface est 105 m².",
                                   question="Quelle est la surface du poste C2.3 / 01-04 ?")
        assert [c.value for c in claims] == [Decimal("105")]

    def test_identifiers_glued_to_letters_are_not_claims(self):
        assert NE.numeric_claims("Repère P12, câble H07 et CO2.") == []


# --------------------------------------------------------------------------- #
# Décision — classes d'erreurs réelles (A1-A6)
# --------------------------------------------------------------------------- #
def _row(label, **cells):
    """Fragment structuré de LIGNE (format réel : « En-tête:\nvaleur » par bloc)."""
    return "\n\n".join([f"Désignation:\n{label}"] + [f"{h.replace('_', ' ')}:\n{v}" for h, v in cells.items()])


Q_POWER = "Quelle est la puissance installée de la ligne « Pompe de relevage » ?"


class TestClaimLevelBypasses:
    """NUMERIC-CLAIM-GUARD-2 — contournements réels (R05, R11) et cas adversariaux, valeurs synthétiques."""

    def test_a1_coincidental_identifier_never_confirms_another_value(self):
        """Réel R05 : « Base aérienne 123 » retrouvé dans un fragment structuré abaissait la protection
        et laissait afficher la valeur fausse avec une simple réserve."""
        d = _guard("La base aérienne 123 dispose d'une puissance de 67,74 kW [1][2].",
                   _structured(_row("Base aérienne 123", Adresse="Rue de la base"), page=5),
                   _ocr("Local hygiène 1 67,74 67,74", page=3), question="Quelle est la puissance du local ménage ?")
        assert d.verdict == NE.VERDICT_BLOCK and "67,74" not in d.answer
        statuses = {c["value"]: c["status"] for c in d.claims}
        assert statuses["67.74"] == NE.CLAIM_UNVERIFIED

    def test_a1_stream_variant_value_is_blocked_too(self):
        d = _guard("Le local est listé avec une surface utile de 1,59 m² [2], dans la base aérienne 123 [1].",
                   _structured(_row("Base aérienne 123", Adresse="Rue"), page=5), _ocr("Local courants 4 1,59 6,00", page=3),
                   question="Quelle est la surface du local ménage ?")
        assert d.verdict in (NE.VERDICT_BLOCK, NE.VERDICT_REDACT) and "1,59" not in d.answer

    def test_a2_ocr_value_on_a_page_never_detected_as_table_is_not_shown(self):
        """Réel R11 : « 35000 » lu par OCR sur une page non reconnue comme tabulaire — réserve seulement."""
        d = _guard("La valeur du lot est 35000 [1].", _ocr("Mur double pierre m² 35000 individuel", page=4),
                   question="Quelle est la valeur du lot pour le mur double en pierre ?")
        assert d.verdict == NE.VERDICT_BLOCK and "35000" not in d.answer

    def test_a3_composite_answer_keeps_supported_and_non_numeric_statements(self):
        answer = ("Le document concerne le bâtiment de la pompe de relevage [1]. La puissance installée est de 18,5 kW [1]. "
                  "La puissance maximale est de 67,74 kW [2]. Le bâtiment est situé dans la zone A.")
        # PRODUCTION-BLOCKERS-14 : l'unité affichée doit être portée par la source (en-tête de la colonne).
        d = _guard(answer, _structured(_row("Pompe de relevage", **{"Puissance installée (kW)": "18,5"}), page=5),
                   _ocr("Puissance maximale 67,74", page=3), question=Q_POWER)
        assert d.verdict == NE.VERDICT_REDACT
        assert "18,5 kW" in d.answer and "67,74" not in d.answer and "zone A" in d.answer
        assert "La puissance maximale est de  kW" not in d.answer  # proposition retirée ENTIÈRE, jamais le seul nombre
        assert d.answer.startswith("⚠️ 1 affirmation(s)")

    @pytest.mark.parametrize("answer", ["196", "196 [1]", "19,6 kW"])
    def test_a4_lone_number_answers_are_controlled(self, answer):
        d = _guard(answer, _ocr("Couvre-joint m 196", page=3), question="Quelle est la quantité du couvre-joint ?")
        assert d.verdict == NE.VERDICT_BLOCK

    def test_a4_single_digit_lone_answer_is_controlled(self):
        assert _guard("6", _ocr("Lot 6", page=4), question="Quel est le lot ?").verdict == NE.VERDICT_BLOCK

    def test_a5_two_values_in_one_statement_are_checked_independently(self):
        d = _guard("La puissance installée passe de 16,0 à 160 kW [1].",
                   _structured(_row("Pompe de relevage", Puissance_installée="16,0"), page=5), _ocr("Pompe 160", page=3),
                   question=Q_POWER)
        statuses = {c["value"]: c["status"] for c in d.claims}
        assert statuses["16.0"] == NE.CLAIM_SUPPORTED and statuses["160"] != NE.CLAIM_SUPPORTED
        assert d.verdict == NE.VERDICT_BLOCK and "160" not in d.answer

    def test_decimal_loss_is_a_conflict_not_a_confirmation(self):
        d = _guard("La puissance installée est de 196 kW [1].",
                   _structured(_row("Pompe de relevage", Puissance_installée="19,6"), page=5), _ocr("Pompe 196", page=3),
                   question=Q_POWER)
        assert d.claims[0]["status"] == NE.CLAIM_CONFLICTING and d.verdict == NE.VERDICT_BLOCK

    def test_sign_loss_is_a_conflict(self):
        d = _guard("L'évolution de la pompe de relevage est de 6,2 % [1].",
                   _structured(_row("Pompe de relevage", Évolution="-6,2 %"), page=5), _ocr("Pompe 6,2", page=3),
                   question="Quelle est l'évolution de la pompe de relevage ?")
        assert d.claims[0]["status"] == NE.CLAIM_CONFLICTING

    def test_same_number_on_another_row_is_not_proof(self):
        d = _guard("La puissance installée est de 41,25 kW [1].",
                   _structured(_row("Ventilateur de désenfumage", Puissance_installée="41,25"), page=5),
                   _ocr("x", page=3), question=Q_POWER)
        assert d.claims[0]["status"] == NE.CLAIM_UNVERIFIED and d.verdict == NE.VERDICT_BLOCK

    def test_same_number_under_another_column_is_not_proof(self):
        d = _guard("La puissance installée est de 400 [1].",
                   _structured(_row("Pompe de relevage", Tension="400", Puissance_installée="18,5"), page=5),
                   _ocr("x", page=3), question=Q_POWER)
        assert d.claims[0]["status"] == NE.CLAIM_UNVERIFIED

    def test_same_number_in_another_named_document_is_not_proof(self):
        d = _guard("La surface du hall est de 105 m² [2].",
                   _native("Surface du hall : 105 m²", doc=DOC_B, filename="autre_projet.pdf"),
                   _ocr("Hall 1 05", page=3, filename="programme_surfaces.pdf"),
                   question="Quelle est la surface du hall dans le document « programme_surfaces.pdf » ?")
        assert d.claims[0]["status"] == NE.CLAIM_UNSUPPORTED and d.verdict == NE.VERDICT_BLOCK

    def test_incompatible_unit_is_not_proof(self):
        d = _guard("La puissance installée est de 18,5 kWh [1].",
                   _native("Pompe de relevage : puissance installée 18,5 kW", page=3), _ocr("x", page=4), question=Q_POWER)
        assert d.claims[0]["status"] == NE.CLAIM_UNVERIFIED

    def test_number_found_nowhere_is_unsupported(self):
        d = _guard("La somme vaut 1 234 kW [1].", _ocr("Valeurs 600 et 634", page=3), question="Quelle est la somme ?")
        assert d.claims[0]["status"] == NE.CLAIM_UNSUPPORTED and d.verdict == NE.VERDICT_BLOCK

    def test_abstention_cites_documents_as_consulted_never_a_page_as_proof(self):
        d = _guard("La quantité est 248 m [1].", _chunk("Joint 248", page=3, filename="bilan.pdf p.3",
                                                        source_is_ocr=True), question="Quelle quantité ?")
        assert d.verdict == NE.VERDICT_BLOCK
        assert "« bilan.pdf »" in d.answer and "p.3" not in d.answer and "p. 3" not in d.answer and "248" not in d.answer

    def test_english_wording(self):
        d = NE.guard_answer("The value is 248 m [1].", question="What is the value?",
                            sources=_annotated(_ocr("Joint 248", page=3)), language="en")
        assert d.verdict == NE.VERDICT_BLOCK and d.answer.startswith("I cannot establish")


class TestClaimLevelPreservesSupportedAnswers:
    def test_structured_value_on_the_right_row_and_column_passes_unchanged(self):
        answer = "La puissance installée de la pompe de relevage est de 18,5 kW [1]."
        d = _guard(answer, _structured(_row("Pompe de relevage", **{"Puissance installée (kW)": "18,5"}, Tension="400"),
                                       page=5), _ocr("Pompe de relevage 18,5 400", page=5), question=Q_POWER)
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)
        assert d.claims[0]["proof"][0]["status"] == NE.STRUCTURED_ACCEPTED

    def test_overview_fragment_row_and_column(self):
        overview = "Tableau (extraction structurée expérimentale)\nContenu :\nIndicateur | Projet | Niveau C1\nEges PCE | 866.2 | 1000"
        answer = "La valeur projet pour Eges PCE est 866.2 [1]."
        d = _guard(answer, _structured(overview, page=5), _ocr("x", page=3),
                   question="Quelle est la valeur « Projet » de la ligne « Eges PCE » ?")
        assert d.verdict == NE.VERDICT_PASS

    def test_native_text_value_passes(self):
        answer = "Le budget prévisionnel du lot façade est de 12 500 € [1]."
        d = _guard(answer, _native("Le budget prévisionnel du lot façade est de 12 500 €.", page=3), _ocr("x", page=4),
                   question="Quel est le budget prévisionnel du lot façade ?")
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    @pytest.mark.parametrize("answer", [
        "Le document décrit le programme de rénovation du bâtiment [1].",
        "Je ne sais pas.",
        "Le document date du 12/03/2025 et la section 5 porte sur les composants (p. 3) [1].",
        "Il y a 3 lots dans ce marché [1].",
    ])
    def test_non_numeric_answers_pass(self, answer):
        d = _guard(answer, _ocr("Tableau 12 500 3 4", page=3))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_contexts_without_ocr_or_visual_sources_are_outside_the_perimeter(self):
        """Texte natif, classeurs, corpus : comportement inchangé (y compris une somme calculée)."""
        for chunk in (_native("Poste X 4 512"), _chunk("Poste X 4 512")):
            d = _guard("La valeur est 9 999 [1].", chunk)
            assert (d.verdict, d.claims) == (NE.VERDICT_PASS, [])

    def test_document_label_drops_the_page_suffix(self):
        assert NE.document_label({"source": {"filename": "PTR_x_raster.pdf p.3"}}) == "PTR_x_raster.pdf"
        assert NE.document_label({"source": {"label": "plan.pdf — page 12"}}) == "plan.pdf"
        assert NE.document_label({"source": {"filename": "rapport.pdf"}}) == "rapport.pdf"


# --------------------------------------------------------------------------- #
# Contexte de génération
# --------------------------------------------------------------------------- #
class TestPromptContext:
    MARKER = "TRANSCRIPTION OCR D'UN TABLEAU, NON VÉRIFIÉE"

    def test_table_ocr_source_is_flagged_in_its_header(self):
        prompt = build_prompt("Quelle valeur ?", _annotated(_ocr("Poste 248", page=3), _native("Texte 12", page=3)))
        header_lines = [line for line in prompt.split("\n") if line.startswith("[")]
        assert self.MARKER in header_lines[0] and self.MARKER not in header_lines[1]

    @pytest.mark.parametrize("chunk", [_ocr("Poste 248", page=4), _native("Texte 12"), _chunk("Sans méta 3")])
    def test_other_sources_render_byte_identical(self, chunk):
        assert build_prompt("Q ?", _annotated(chunk)) == build_prompt("Q ?", [chunk])

    def test_english_header(self):
        prompt = build_prompt("Which value?", _annotated(_ocr("Item 248", page=3)), answer_language="en")
        assert "OCR TABLE TRANSCRIPTION, UNVERIFIED" in prompt


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-GUARD-1 — tableaux de PDF natifs (rail générique)
# --------------------------------------------------------------------------- #
def _native_row(label, page=4, doc=DOC_A, **cells):
    """Fragment du rail générique (format réel : « Row: … » puis « En-tête:\\nvaleur » par colonne)."""
    text = "\n\n".join([f"Row: {label}", f"Équipement:\n{label}"] + [f"{h}:\n{v}" for h, v in cells.items()])
    return _chunk(text, doc=doc, page=page, kind="table", table_type="generic",
                  table={"table_type": "generic"}, row_label=label)


def _flat(text, page=4, **kw):
    return _chunk(text, page=page, source_is_ocr=False, native_table_unverified=True, **kw)


ROW_A = {"Puissance (W)": "866,2", "Coût (€)": "750"}
Q_A = "Quelle est la puissance (W) de l'équipement « Pompe A » ?"


class TestNativeTables:
    def test_classification(self):
        [structured, flat, narrative] = _annotated(_native_row("Pompe A", **ROW_A), _flat("Pompe A 866,2 750"),
                                                   _native("Le budget est de 12 500 €.", page=4), pages={})
        assert [c["source"]["meta"][NE.META_KEY] for c in (structured, flat, narrative)] == [
            NE.NATIVE_TABLE_STRUCTURED, NE.NATIVE_TABLE_UNVERIFIED, NE.NATIVE_TEXT]
        assert NE.context_is_protected([structured]) and NE.context_is_protected([flat])
        assert not NE.context_is_protected([narrative])

    def test_ocr_table_rail_is_never_native_structured(self):
        [c] = _annotated(_chunk("| a | b |", kind="table", table={"source": "ocr"}), pages={})
        assert c["source"]["meta"][NE.META_KEY] == NE.OCR_TABLE_UNVERIFIED

    def test_right_row_and_column_is_kept(self):
        answer = "La puissance de la pompe A est de 866,2 W [1]."
        d = _guard(answer, _native_row("Pompe A", **ROW_A), question=Q_A, pages={})
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_r07_value_of_the_neighbour_column_is_blocked(self):
        d = _guard("La puissance de la pompe A est de 750 [1].", _native_row("Pompe A", **ROW_A), question=Q_A, pages={})
        assert d.verdict == NE.VERDICT_BLOCK and "750" not in d.answer

    def test_r08_values_of_another_row_are_blocked(self):
        d = _guard("La pompe A a une puissance de 1 000,7 W [1].", _native_row("Pompe A", **ROW_A),
                   _native_row("Pompe B", **{"Puissance (W)": "1 000,7", "Coût (€)": "900"}), question=Q_A, pages={})
        assert d.verdict == NE.VERDICT_BLOCK

    def test_r16_value_from_elsewhere_in_the_table_is_blocked(self):
        q = "Quelle est la valeur « Total » de la ligne « Ensemble du programme » ?"
        d = _guard("Le total du programme est de 627 m² [1].",
                   _native_row("Ensemble du programme", **{"Total (m²)": "1 351"}),
                   _native_row("Sous-total zone B", **{"Total (m²)": "627"}), question=q, pages={})
        assert d.verdict == NE.VERDICT_BLOCK and "627" not in d.answer

    def test_r01_watts_read_as_euros_is_blocked(self):
        q = "Quelle est la puissance installée (W) de la ligne « Armoire technique » ?"
        row = _native_row("Armoire technique", **{"Puissance installée (W)": "11 000.00"})
        assert _guard("L'armoire technique coûte 11 000,00 € [1].", row, question=q, pages={}).verdict == NE.VERDICT_BLOCK
        assert _guard("L'armoire technique a une puissance installée de 11 000,00 W [1].", row,
                      question=q, pages={}).verdict == NE.VERDICT_PASS

    def test_flattened_table_text_is_never_proof(self):
        d = _guard("La puissance de la pompe A est de 866,2 W [1].", _flat("Pompe A 866,2 750"), question=Q_A, pages={})
        assert d.verdict == NE.VERDICT_BLOCK

    def test_flattened_text_does_not_confirm_the_structured_value_of_another_row(self):
        d = _guard("La puissance de la pompe A est de 750 W [1].", _native_row("Pompe A", **ROW_A),
                   _flat("Pompe A 750 W"), question=Q_A, pages={})
        assert d.verdict == NE.VERDICT_BLOCK

    def test_same_value_in_two_tables_needs_the_right_row(self):
        d = _guard("La puissance de la pompe A est de 900 W [1].", _native_row("Pompe A", **{"Puissance (W)": "900"}),
                   _native_row("Pompe C", page=7, **{"Puissance (W)": "900"}), question=Q_A, pages={})
        assert d.verdict == NE.VERDICT_PASS and d.claims[0]["proof"][0]["page"] == 4

    def test_neutral_columns_with_several_numbers_prove_nothing(self):
        """NATIVE-TABLE-RECOVERY-2 : colonnes anonymes, plusieurs nombres sur la ligne — la colonne
        de la valeur est indéterminable, la ligne seule ne la prouve pas (avant : acceptée)."""
        row = _chunk("Row: 6\n\nColumn 1:\n6\n\nColumn 2:\nJoint de façade\n\nColumn 4:\n19,6\n\nColumn 5:\nm",
                     page=5, kind="table", table_type="generic", table={})
        d = _guard("La quantité du joint de façade est de 19,6 m [1].", row,
                   question="Quelle est la quantité de la ligne « Joint de façade » ?", pages={})
        assert d.claims[0]["status"] != NE.CLAIM_SUPPORTED and "19,6" not in d.answer

    def test_short_token_header_is_a_named_column(self):
        """« SU m² » n'est pas un en-tête neutre : il se vérifie par ses mots, jetons courts compris."""
        row = _chunk("Row: B4.1 Salle\n\nSalle:\nB4.1 Salle\n\nNbre:\n12\n\nSU m²:\n105\n\nTotal SU m²:\n1260",
                     page=1, kind="table", table_type="generic", table={})
        q = "Quelle est la valeur de « SU m² » pour la ligne « B4.1 Salle » ?"
        assert _guard("La valeur de SU m² pour B4.1 Salle est 105 m² [1].", row, question=q, pages={}).verdict == NE.VERDICT_PASS
        d = _guard("La valeur de SU m² pour B4.1 Salle est 12 [1].", row, question=q, pages={})
        assert d.claims[0]["status"] != NE.CLAIM_SUPPORTED

    def test_neutral_column_accepts_the_only_number_of_the_right_row(self):
        row = _chunk("Row: Joint de façade\n\nColumn 1:\nJoint de façade\n\nColumn 2:\n19,6\n\nColumn 3:\nm",
                     page=5, kind="table", table_type="generic", table={})
        d = _guard("La quantité du joint de façade est de 19,6 m [1].", row,
                   question="Quelle est la quantité de la ligne « Joint de façade » ?", pages={})
        # La valeur reste prouvée ; l'unité « m » d'une colonne NEUTRE n'est pas rattachée à la valeur
        # (PRODUCTION-BLOCKERS-14) : retirée, la valeur conservée.
        assert d.claims[0]["status"] == NE.CLAIM_SUPPORTED and d.verdict == NE.VERDICT_REDACT
        assert "est de 19,6 [1]." in d.answer and "19,6 m" not in d.answer
        assert d.units == [{"token": "19,6", "value": "19.6", "unit": "m", "status": NE.UNIT_ABSENT_FROM_SOURCE}]

    def test_decimal_loss_against_a_native_table_is_a_conflict(self):
        d = _guard("La puissance de la pompe A est de 8662 W [1].", _native_row("Pompe A", **ROW_A), question=Q_A, pages={})
        assert d.claims[0]["status"] == NE.CLAIM_CONFLICTING

    def test_narrative_native_text_stays_outside_the_perimeter(self):
        answer = "Le budget prévisionnel est de 12 500 € [1]."
        d = _guard(answer, _native("Le budget prévisionnel est de 12 500 €.", page=4), pages={})
        assert (d.verdict, d.claims) == (NE.VERDICT_PASS, [])

    def test_prompt_flags_flattened_table_text(self):
        prompt = build_prompt("Q ?", _annotated(_flat("Pompe A 866,2 750"), pages={}))
        assert "TEXTE DE TABLEAU APLATI" in prompt

    def test_long_question_keeps_the_row_label_at_its_end(self):
        """Le contexte n'est jamais borné : le libellé cité en fin de longue question reste visible."""
        q = ("Dans le document « Tableau détaillé des surfaces utiles et surfaces de plancher », quelle est la "
             "valeur de « Surface (m²) » pour la ligne « 3e étage » ?")
        row = _chunk("Row: x\n\nNiveau:\n3e étage\n\nSurface (m²):\n831,34", page=2, kind="table", table={})
        d = _guard("La surface du 3e étage est de 831,34 m² [1].", row, question=q, pages={})
        assert d.verdict == NE.VERDICT_PASS

    def test_value_under_a_column_that_is_not_the_one_named_is_blocked(self):
        """Constat réel (R07 en flux) : « le niveau 1 … 750 » alors que 750 est sous « Niveau 2 »."""
        row = _native_row("Pompe A", **{"Niveau 1": "1050", "Niveau 2": "750"})
        q = "Quelle est la valeur de la ligne « Pompe A » ?"
        assert _guard("Pour la pompe A, le niveau 1 est de 750 [1].", row, question=q, pages={}).verdict == NE.VERDICT_BLOCK
        assert _guard("Pour la pompe A, le niveau 1 est de 1050 [1].", row, question=q, pages={}).verdict == NE.VERDICT_PASS


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-GROUNDING-3 — réduction d'une proposition à ses affirmations prouvées
# --------------------------------------------------------------------------- #
ROW_B = {"Puissance (W)": "912,4", "Coût (€)": "980"}
Q_EGES = "Quelle est la valeur de « Projet » pour la ligne « Eges PCE (kg eq.CO2/m²) » ?"


def _eges_row():
    text = ("Row: Eges PCE (kg eq.CO2/m²)\n\nIndicateur:\nEges PCE (kg eq.CO2/m²)\n\nProjet:\n866.2\n\nNiveau C1:\n1050"
            "\n\nNiveau C2:\n750\n\nNiveau BDF > Bronze:\n900\n\nNiveau BDF > Argent:\n800")
    return _chunk(text, page=2, kind="table", table_type="generic", table={"table_type": "generic"})


class TestClaimReduction:
    def _guard_a(self, answer, *extra, question=Q_A):
        return _guard(answer, _native_row("Pompe A", **ROW_A), *extra, question=question, pages={})

    def test_r07_structure_keeps_the_proven_value_only(self):
        d = _guard("Pour l'indicateur Eges PCE, la valeur du Projet est de 866,2 kg eq.CO2/m², le niveau C1 de "
                   "1 451 kg eq.CO2/m² et le niveau C2 de 4 781 kg eq.CO2/m² [1].", _eges_row(), question=Q_EGES, pages={})
        assert d.verdict == NE.VERDICT_REDACT and d.reduced == 1
        assert "Pour l'indicateur Eges PCE, la valeur du Projet est de 866,2 kg eq.CO2/m² [1]." in d.answer
        assert "1 451" not in d.answer and "4 781" not in d.answer

    def test_prompt_illustration_object_value_and_unit_are_kept(self):
        d = self._guard_a("La pompe A développe 866,2 W, son prix est de 750 € et sa masse est de 1050 kg [1].")
        assert "La pompe A développe 866,2 W [1]." in d.answer
        assert "750" not in d.answer and "1050" not in d.answer and d.reduced == 1

    def test_three_claims_only_one_proven(self):
        d = self._guard_a("La puissance de la pompe A est de 866,2 W, la masse de la pompe A de 1050 kg, "
                          "la hauteur de la pompe A de 1250 mm [1].")
        assert "La puissance de la pompe A est de 866,2 W [1]." in d.answer
        assert "1050" not in d.answer and "1250" not in d.answer

    @pytest.mark.parametrize("answer", [
        pytest.param("La puissance de la pompe A est de 866,2 W, supérieure à 750 W [1].", id="comparison"),
        pytest.param("La puissance de la pompe A est de 866,2 W, soit 1050 W au total [1].", id="sum"),
        pytest.param("La puissance de la pompe A est de 866,2 W, pour un ratio de 1050 sur 750 [1].", id="ratio"),
        pytest.param("La puissance de la pompe A est comprise entre 750 W et 866,2 W, la masse 1050 kg [1].", id="range"),
        pytest.param("La puissance de la pompe A va de 750 à 866,2 W, la masse 1050 kg [1].", id="range-a"),
        pytest.param("Les puissances des pompes A et B sont respectivement 866,2 W et 1050 W, masse 75,5 kg [1].",
                     id="respectively"),
        pytest.param("La puissance de la pompe A est de 866,2 W, en hausse de 12,5 % [1].", id="percent"),
        pytest.param("La puissance de la pompe A est de 866,2 W, la marge est de -12,5 W [1].", id="negative"),
        pytest.param("Si la pompe A tourne, sa puissance est de 866,2 W, la masse 1050 kg [1].", id="conditional"),
        pytest.param("La masse est de 1050 kg, sa puissance est de 866,2 W [1].", id="dependent-start"),
        pytest.param("La pompe A (fiche 31490) développe 866,2 W, la masse 1050 kg [1].", id="identifier-in-part"),
        pytest.param("La puissance des 2 pompes A est de 866,2 W, la masse 1050 kg [1].", id="single-digit-kept"),
        pytest.param("La puissance de la pompe A est de 866,2 W, pour 3 unités, la masse 1050 kg [1].",
                     id="qualifier-after-claim"),
        pytest.param("La puissance de la pompe A est de 866,2 W et la masse de 1050 kg [1].", id="no-boundary"),
    ])
    def test_inseparable_propositions_stay_removed_whole(self, answer):
        d = self._guard_a(answer)
        assert d.reduced == 0 and "866,2" not in d.answer

    def test_reduced_text_must_still_prove_the_value(self):
        """Prouvée seulement grâce à un mot de la partie RETIRÉE (« pompe B ») : refus."""
        d = _guard("La puissance de la pompe A est de 912,4 W, la pompe B a une masse de 1050 kg [1].",
                   _native_row("Pompe A", **ROW_A), _native_row("Pompe B", **ROW_B),
                   question="Quelle est la puissance de la pompe A ?", pages={})
        assert d.reduced == 0 and "912,4" not in d.answer

    def test_only_citations_of_proving_sources_are_kept(self):
        d = _guard("La puissance de la pompe A est de 866,2 W, la masse de la pompe A est de 1050 kg [1].",
                   _native("Le budget prévisionnel est de 12 500 €.", page=9), _native_row("Pompe A", **ROW_A),
                   question=Q_A, pages={})
        assert "La puissance de la pompe A est de 866,2 W [2]." in d.answer and "[1]" not in d.answer

    def test_proving_citation_is_kept_when_present(self):
        d = _guard("La puissance de la pompe A est de 866,2 W [2], la masse de la pompe A est de 1050 kg [1].",
                   _native("Le budget prévisionnel est de 12 500 €.", page=9), _native_row("Pompe A", **ROW_A),
                   question=Q_A, pages={})
        assert d.answer.endswith("La puissance de la pompe A est de 866,2 W [2].")

    def test_wrong_column_and_wrong_unit_are_not_kept(self):
        d = self._guard_a("La puissance de la pompe A est de 750 W, la masse de la pompe A est de 1050 kg [1].")
        assert d.verdict == NE.VERDICT_BLOCK
        d = self._guard_a("La puissance de la pompe A est de 866,2 €, la masse de la pompe A est de 1050 kg [1].")
        assert d.verdict == NE.VERDICT_BLOCK

    def test_fully_proven_fully_refused_and_numberless_answers_are_unchanged(self):
        ok = "La puissance de la pompe A est de 866,2 W [1]."
        assert self._guard_a(ok).answer == ok
        assert self._guard_a("La masse de la pompe A est de 1050 kg, sa hauteur de 1250 mm [1].").verdict == NE.VERDICT_BLOCK
        plain = "La pompe A est décrite dans le tableau [1]."
        d = self._guard_a(plain)
        assert (d.answer, d.reduced) == (plain, 0)

    def test_guard_summary_reports_reductions(self):
        d = self._guard_a("La pompe A développe 866,2 W, son prix est de 750 € et sa masse est de 1050 kg [1].")
        assert d.to_dict()["propositions_reduced"] == 1 and d.to_dict()["propositions_removed"] == 1


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-RESPONSE-HARDENING-4 — frontières de phrase après un caractère fermant
# --------------------------------------------------------------------------- #
def _total_row():
    """Ligne structurée dont le CONTEXTE (libellé de section) porte un nombre — structure du cas réel."""
    text = ("Context: Group 7 storage 10\n\nRow: TOTAL USEFUL AREA PROGRAMME\n\nActivity:\nTOTAL USEFUL AREA PROGRAMME"
            "\n\nTotal SU m²:\n1351")
    return _chunk(text, page=1, kind="table", table_type="generic", table={"table_type": "generic"})


Q_TOTAL = "Quelle est la valeur de « Total SU m² » pour la ligne « TOTAL USEFUL AREA PROGRAMME » ?"


class TestSentenceBoundaries:
    @pytest.mark.parametrize("text, parts", [
        ("dans le contexte « Group 7 storage 10 ». La valeur est de 1351.", 2),
        ("la valeur est « 1351 ». La suite.", 2),
        ("selon le tableau (page 1). La valeur est de 1351.", 2),
        ("il écrit « fin. » Puis la valeur 1351.", 2),
        ("il écrit \"fin.\" Puis la valeur 1351.", 2),
        ("La valeur est de 866,2 kW. Puis 1351.", 2),
        ("La puissance est de 866,2 kW et la masse de 3,5 kg.", 1),
        ("Voir p. 3 La valeur est de 1351.", 1),
        ("Le fichier rapport.pdf contient 1351 lignes.", 1),
        ("La valeur (env. 5 m) est stable.", 1),
    ])
    def test_split(self, text, parts):
        assert len(NE._split(text)[0]) == parts

    def test_real_structure_keeps_the_proven_value_of_the_second_sentence(self):
        """Structure du cas réel (R16, flux) : un nombre non prouvé dans un libellé de section, puis la
        valeur prouvée dans la phrase suivante, séparées par « ». ». Avant : retirées ensemble."""
        answer = ("Dans les sources fournies, la ligne « TOTAL USEFUL AREA PROGRAMME » apparaît dans le document [1], "
                  "dans le contexte « Group 7 storage 10 ». La valeur indiquée pour « Total SU m² » est de **1351** [1].")
        d = _guard(answer, _total_row(), question=Q_TOTAL, pages={})
        assert "La valeur indiquée pour « Total SU m² » est de **1351** [1]." in d.answer
        assert "storage 10" not in d.answer and d.verdict == NE.VERDICT_REDACT
        assert [c["status"] for c in d.claims if c["value"] == "1351"] == [NE.CLAIM_SUPPORTED]


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-RESPONSE-HARDENING-4 — attribution explicite d'une ligne à une source citée
# --------------------------------------------------------------------------- #
def _pump(label, power, page=4, doc=DOC_A):
    return _native_row(label, page=page, doc=doc, **{"Puissance (W)": power, "Réf.": "5101"})


Q_PUMP = "Quelle est la puissance (W) de l'équipement « Pompe A » ?"


class TestRowAttribution:
    def test_wrong_row_citations_are_dropped_and_valid_ones_kept(self):
        """Structure du cas réel (R10, flux) : la ligne déclarée présente dans des sources qui sont
        d'autres lignes, partageant une valeur."""
        d = _guard("La ligne « Pompe A » est présente dans les sources [1], [2] et [3], dans le même lot.",
                   _pump("Pompe A", "866,2"), _pump("Pompe B", "866,2"), _pump("Pompe AB", "866,2"), question=Q_PUMP, pages={})
        assert "est présente dans les sources [1], dans le même lot." in d.answer
        assert "[2]" not in d.answer and "[3]" not in d.answer and d.citations_dropped == 1

    def test_row_attributed_to_a_source_without_it_is_removed_with_its_other_claims(self):
        """Structure du cas réel (R16, synchrone) : la phrase attribue aussi d'autres éléments (titres)
        à la source fautive : pas de « réparation » par une autre source, la phrase est retirée."""
        other = _native("Tableau des surfaces utiles du bâtiment B, version 2. Hall 67,74.", page=3, doc=DOC_B)
        answer = ("Une référence similaire est présente dans [2] : « Tableau des surfaces utiles du bâtiment B », "
                  "qui inclut une ligne intitulée « TOTAL USEFUL AREA PROGRAMME ». La valeur est de 1351 [1].")
        d = _guard(answer, _total_row(), other, question=Q_TOTAL, pages={})
        assert "Une référence similaire" not in d.answer and "1351 [1]" in d.answer
        assert d.attributions_removed == 1 and "attribuant une ligne" in d.answer

    def test_right_value_in_the_wrong_document_is_removed_never_recited(self):
        """NATIVE-TABLE-SAFETY-6 : plus aucun remplacement de citation, même par l'unique source qui
        prouve la ligne (constat réel : le remplacement changeait le sens de la phrase)."""
        d = _guard("La puissance (W) pour la ligne « Pompe A » est de 866,2 W, selon le document [2].",
                   _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert "selon le document" not in d.answer and "[1]" not in d.answer and d.attributions_removed == 1

    def test_ambiguous_attribution_is_never_given_a_new_citation(self):
        """Deux sources contiennent la ligne (représentations équivalentes) : aucune n'est choisie."""
        d = _guard("La ligne « Pompe A » figure dans le document [3].",
                   _pump("Pompe A", "866,2"), _pump("Pompe A", "866,2", page=5),
                   _native("Note de synthèse sans tableau.", page=9, doc=DOC_B), question=Q_PUMP, pages={})
        assert "figure dans le document" not in d.answer

    def test_out_of_range_citation_is_invalid(self):
        d = _guard("La ligne « Pompe A » figure dans [1] et [7].", _pump("Pompe A", "866,2"), question=Q_PUMP, pages={})
        assert "figure dans [1]." in d.answer and "[7]" not in d.answer

    def test_similar_labels_are_not_interchangeable(self):
        """« Pompe A » n'est pas dans la ligne « Pompe AB » (mots entiers) : la phrase est retirée,
        jamais réattribuée à la source qui porte la ligne nommée."""
        d = _guard("La ligne « Pompe A » figure dans [1].", _pump("Pompe AB", "866,2"), _pump("Pompe A", "866,2"),
                   question=Q_PUMP, pages={})
        assert "figure dans" not in d.answer and "[2]" not in d.answer and d.attributions_removed == 1

    @pytest.mark.parametrize("answer", [
        "La ligne « Pompe A » est présente dans la source [1].",
        "La puissance de la pompe A est de 866,2 W [1].",
        "Le tableau récapitule les équipements [1].",
        "Le document [2] ne contient pas de ligne « Pompe A ».",
        "Cette ligne figure dans la section « Pompes du lot 2 » [1].",
    ])
    def test_valid_narrative_negated_and_section_sentences_are_unchanged(self, answer):
        d = _guard(answer, _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert answer in d.answer and d.attributions_removed == 0 and d.citations_dropped == 0

    def test_summary_reports_attribution_corrections(self):
        d = _guard("La ligne « Pompe A » est présente dans les sources [1] et [2].",
                   _pump("Pompe A", "866,2"), _pump("Pompe B", "866,2"), question=Q_PUMP, pages={})
        assert d.to_dict()["citations_dropped"] == 1 and d.to_dict()["attributions_removed"] == 0


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-SAFETY-6 — preuve numérique par ligne de texte natif ; garde de citation réparée.
# Libellés et valeurs SYNTHÉTIQUES, structure des cas réels (R05, R06, R10, R11, R16).

def _row_question(label, column="Surface utile"):
    return f"Quelle est la valeur de « {column} » pour la ligne « {label} » ?"


class TestNativeTextRowEvidence:
    """Constat réel (R06, flux) : « Local CTA 1 38,74 » certifiait 38,74 pour « Local technique CFA »
    (un seul mot commun). La ligne de texte qui porte la valeur doit porter le libellé désigné."""

    @pytest.mark.parametrize("asked, line, value", [
        pytest.param("Salle technique Nord", "Salle Sud 1 38,74 38,74", "38,74", id="local-cta-vs-cfa"),
        pytest.param("Pompe A", "Pompe B 866,2", "866,2", id="pompe-a-vs-b"),
        pytest.param("Pompe A", "Pompe AB 866,2", "866,2", id="pompe-a-vs-ab"),
        pytest.param("Niveau C1", "Niveau C2 1 000", "1 000", id="c1-vs-c2"),
        pytest.param("Lot 6", "Lot 16 4 250", "4 250", id="lot6-vs-lot16"),
    ])
    def test_neighbour_row_never_proves_the_asked_row(self, asked, line, value):
        # Forme réelle de la réponse (R06) : « pour « X » » sans le mot « ligne » — hors garde d'attribution.
        answer = f"La valeur de « Surface utile » pour « {asked} » est de {value} [1]."
        d = _guard(answer, _native(f"Tableau des surfaces\n{line}", page=3), _ocr("x", page=4),
                   pages={}, question=_row_question(asked))
        assert d.verdict != NE.VERDICT_PASS and value not in d.answer

    @pytest.mark.parametrize("asked, line, value", [
        pytest.param("Salle technique Nord", "Salle technique Nord 1 9,03", "9,03", id="exact-row"),
        pytest.param("Pompe A", "Pompe A 866,2", "866,2", id="pompe-a"),
        pytest.param("Lot 6", "Lot 6 4 250", "4 250", id="lot6"),
    ])
    def test_the_designated_row_still_proves_its_value(self, asked, line, value):
        answer = f"La valeur de « Surface utile » pour « {asked} » est de {value} [1]."
        d = _guard(answer, _native(f"Tableau des surfaces\n{line}", page=3), _ocr("x", page=4),
                   pages={}, question=_row_question(asked))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_truncated_label_is_never_numeric_proof(self):
        """La tolérance de troncature (bloc B) ne vaut que pour les citations."""
        answer = "La valeur de « Surface utile » pour « Isol / Indivi » est de 42,5 [1]."
        d = _guard(answer, _native("Tableau\nIsolant Individuel 42,5", page=3), _ocr("x", page=4),
                   pages={}, question=_row_question("Isol / Indivi"))
        assert d.verdict != NE.VERDICT_PASS

    def test_narrative_native_text_keeps_its_rule(self):
        answer = "Le budget prévisionnel du lot façade est de 12 500 € [1]."
        d = _guard(answer, _native("Le budget prévisionnel du lot façade est de 12 500 €, hors aléas de 3 % et 2 options.", page=3),
                   _ocr("x", page=4), pages={}, question="Quel est le budget prévisionnel du lot façade ?")
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_narrative_sentence_keeps_its_rule_even_with_a_designated_row(self):
        """Constat au rejeu réel (R08, R14) : un seuil ou une référence de norme cités dans une PHRASE ne
        sont pas des lignes de tableau ; la question désigne une ligne, la phrase reste vérifiée par recouvrement."""
        answer = "Le projet vise un niveau Eges inférieur au seuil de 900 kgCO2/m² [1]."
        d = _guard(answer, _native("Le maître d'ouvrage exige le respect d'un niveau Eges inférieur à 900 kgCO2/m² sur l'opération.", page=3),
                   _ocr("x", page=4), pages={}, question=_row_question("Eges / SO", column="Niveau C1"))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_short_neighbour_line_is_tabular(self):
        """Prudence : une ligne courte sans ponctuation de phrase est traitée comme tabulaire."""
        answer = "La valeur de « Surface utile » pour « Salle technique Nord » est de 38,74 [1]."
        d = _guard(answer, _native("Récapitulatif\nla salle Sud fait 38,74", page=3), _ocr("x", page=4),
                   pages={}, question=_row_question("Salle technique Nord"))
        assert d.verdict != NE.VERDICT_PASS

    def test_tabular_line_without_designated_row_is_not_proof(self):
        """Ligne d'allure tabulaire, aucune ligne désignée : la colonne de la valeur est indéterminable."""
        answer = "La surface du hall est de 38,74 m² [1]."
        d = _guard(answer, _native("Hall 1 38,74 12,50 51,24", page=3), _ocr("x", page=4),
                   pages={}, question="Quelle est la surface du hall ?")
        assert d.verdict != NE.VERDICT_PASS


class TestCitationGuardRepair:
    def test_citation_in_an_incidental_relation_is_not_checked(self):
        """Structure réelle (R06, synchrone) : [3] porte une autre relation (même projet), dans une
        parenthèse incidente ; la ligne est attribuée à [1] qui la contient."""
        answer = ("Dans le tableau du document [1] (qui correspond au même projet que le document [2], vu la "
                  "cohérence des données), la ligne « Pompe A » est mentionnée avec 866,2 W.")
        d = _guard(answer, _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert answer in d.answer and d.attributions_removed == 0 and d.citations_dropped == 0

    @pytest.mark.parametrize("answer", [
        "Le document [2] ne contient pas de ligne « Pompe A ».",
        "Une note est présente dans [2], mais sans ligne exactement intitulée « Pompe A ».",
        "Le document [2] n'indique pas la ligne « Pompe A ».",
        "La note [2] est rédigée sans référence à la ligne « Pompe A ».",
        "La note [2] ne comporte aucune ligne « Pompe A ».",
        "On constate l'absence de la ligne « Pompe A » dans [2].",
        "La note [2] ne permet pas d'établir la ligne « Pompe A ».",
        "Il manque une confirmation du document [2] pour valider que la ligne « Pompe A » y figure.",
    ])
    def test_negated_or_conditional_attributions_are_out_of_scope(self, answer):
        d = _guard(answer, _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert answer in d.answer and d.attributions_removed == 0 and d.citations_dropped == 0

    def test_negation_after_the_attribution_does_not_shield_it(self):
        """Structure réelle (R05, flux) : « [2] et [5] mentionnent la catégorie « X », mais sans valeur » —
        la négation porte sur la valeur, pas sur la présence : [5] invalide est retirée."""
        d = _guard("Les documents [1] et [2] mentionnent la catégorie « Pompe A », mais sans fournir de valeur.",
                   _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert "Les documents [1] mentionnent" in d.answer and "[2]" not in d.answer and d.citations_dropped == 1

    def test_truncated_label_is_tolerated_when_unique(self):
        """Structure réelle (R11) : « … / Indivi » pour « … / Individuel »."""
        src = _native_row("Mur double", page=4, **{"Type": "Individuel", "Lot": "6"})
        answer = "La valeur de « Lot » pour la ligne « Mur double / Indivi » est 6 [1]."
        d = _guard(answer, src, question="Quel est le lot de la ligne « Mur double » ?", pages={})
        assert answer in d.answer and d.attributions_removed == 0

    def test_ambiguous_truncation_is_not_tolerated(self):
        src = _native_row("Mur double", page=4, **{"Type": "Individuel", "Mode": "Individualisé"})
        d = _guard("La ligne « Mur double / Indivi » figure dans [1].", src, question="Quelle ligne ?", pages={})
        assert "figure dans" not in d.answer and d.attributions_removed == 1

    @pytest.mark.parametrize("answer", [
        "Le document « Synthèse Pompe A.pdf » est cité dans [2].",
        "Le tableau « Pompe A » figure dans [2].",
        "La section « Pompe A » est décrite dans [2].",
    ])
    def test_document_table_and_section_labels_are_not_row_labels(self, answer):
        d = _guard(answer, _pump("Pompe A", "866,2"), _native("Note de synthèse sans tableau.", page=9, doc=DOC_B),
                   question=Q_PUMP, pages={})
        assert answer in d.answer and d.attributions_removed == 0

    def test_category_attribution_is_checked_like_a_row(self):
        d = _guard("La catégorie « Pompe A » figure dans [2].", _pump("Pompe A", "866,2"),
                   _native("Note de synthèse sans tableau.", page=9, doc=DOC_B), question=Q_PUMP, pages={})
        assert "figure dans" not in d.answer and d.attributions_removed == 1

    def test_parenthesis_right_after_the_label_is_attached(self):
        d = _guard("La ligne « Pompe A » (source [2]) vaut 866,2 W.", _pump("Pompe A", "866,2"),
                   _native("Note de synthèse sans tableau.", page=9, doc=DOC_B), question=Q_PUMP, pages={})
        assert "vaut 866,2" not in d.answer and d.attributions_removed == 1


# --------------------------------------------------------------------------- #
# NATIVE-TABLE-RECOVERY-9 — affirmations d'absence contredites par les sources.
# Structure du cas réel (GENERATION-8, R05 flux) ; libellés et valeurs SYNTHÉTIQUES.

A6 = "A6 surfaces.pdf"


def _a6_sources(extra_native=""):
    """[1] texte natif page 3 du document A6 (porte la ligne) ; [2] vignette OCR page 1 du même document (ne la porte
    pas : le contexte entre dans le périmètre) ; [3] autre document, tableau qui porte un libellé voisin."""
    page3 = "Catégories\nLocal technique CFA 1 9,03 9,03\n3 Local ménage 4 3,85 15,38" + extra_native
    return [_native(page3, page=3, filename=A6),
            _ocr("Tableau détaillé des surfaces utiles et SDP", page=1, filename=A6),
            _native_row("C 1.1 Local entretien", page=1, doc=DOC_B, **{"Code": "00-04"})]


Q_MENAGE = "Quelle est la valeur de « Surface utile » pour la ligne « Local ménage » ?"


def _absence_guard(answer, sources=None, question=Q_MENAGE):
    return NE.guard_answer(answer, question=question, sources=NE.annotate_numeric_evidence(
        sources or _a6_sources(), table_pages_by_doc={}))


class TestFalseAbsence:
    def test_real_r05_false_absence_is_not_published(self):
        """Forme réelle (G8, R05 flux) : document nommé par son fichier + [1] ; [1] porte la ligne."""
        answer = ("Je ne trouve pas de mention de « Local ménage » dans le document « A6 surfaces.pdf » [1]. "
                  "Je ne peux donc pas fournir la valeur demandée.")
        d = _absence_guard(answer)
        assert "Je ne trouve pas de mention" not in d.answer and "Je ne peux donc pas fournir" in d.answer
        assert d.absences_removed == 1 and d.verdict == NE.VERDICT_REDACT and d.to_dict()["absences_removed"] == 1

    @pytest.mark.parametrize("answer", [
        pytest.param("Le document « A6 surfaces.pdf » ne contient pas de ligne « Local ménage ».", id="document-scope"),
        pytest.param("La ligne « Local ménage » n'apparaît pas dans le tableau [1].", id="cited-page"),
        pytest.param("Aucune mention de « Local ménage » n'est présente dans les sources fournies.", id="corpus-scope"),
        pytest.param("Aucune mention de « LOCAL MENAGE » dans les sources fournies.", id="case-and-accents"),
        pytest.param("Aucune mention de « Local-ménage » dans [1].", id="punctuation"),
        pytest.param("Le tableau [1] mentionne « Local technique CFA », mais pas « Local ménage ».", id="mais-pas"),
        pytest.param("Les seules mentions de « Local ménage » se trouvent dans le document [3].", id="false-exclusivity"),
    ])
    def test_contradicted_absence_is_removed(self, answer):
        d = _absence_guard(answer + " La question porte sur une surface.")
        assert answer not in d.answer and "La question porte sur une surface." in d.answer and d.absences_removed == 1

    @pytest.mark.parametrize("answer", [
        pytest.param("La source [2] ne contient pas de ligne « Local ménage ».", id="true-absence-other-page"),
        pytest.param("Le document « Note.pdf » ne contient pas de ligne « Local ménage ».", id="true-absence-other-document"),
        pytest.param("Aucune mention de « Local entretien » dans [1].", id="other-label-not-equivalent"),
        pytest.param("Aucune mention de « Local ménage sous-sol » dans [1].", id="longer-label"),
        pytest.param("Aucune mention de « Local ménage » dans [7].", id="out-of-range-citation"),
        pytest.param("Les catégories de ce tableau incluent les chambres, mais pas « Local ménage ».", id="unresolved-scope-alone"),
        pytest.param("Si la ligne « Local ménage » n'apparaît pas dans [1], la valeur reste inconnue.", id="hypothetical"),
        pytest.param("La ligne « Local ménage » n'apparaît-elle pas dans [1] ?", id="question"),
        pytest.param("Il manque une preuve structurelle pour la ligne « Local ménage » [1].", id="negation-on-proof"),
        pytest.param("La colonne de la valeur n'est pas certifiée pour la ligne « Local ménage » [1].", id="negation-on-column"),
        pytest.param("La valeur de « Surface utile » pour « Local ménage » n'est pas présente dans le document [1].", id="negation-on-value"),
        pytest.param("Les documents [1] et [3] ne mentionnent pas de « Local ménage » avec une valeur certifiée.", id="value-qualifier"),
        pytest.param("Les sources ne contiennent pas le document « A6 surfaces.pdf ».", id="file-name-is-not-a-row"),
        pytest.param("Je ne peux pas confirmer que cette valeur correspond à la surface.", id="no-quoted-object"),
        pytest.param("Le tableau [1] mentionne « Local ménage », mais pas « Local entretien ».", id="mixed-true-negative"),
        pytest.param("Les seules mentions de « Local ménage » se trouvent dans le document [1].", id="true-exclusivity"),
    ])
    def test_legitimate_or_unprovable_negations_are_kept(self, answer):
        d = _absence_guard(answer)
        assert answer in d.answer and d.absences_removed == 0

    def test_presence_read_on_an_image_never_contradicts(self):
        """La vignette OCR porte le libellé, le texte natif non : pas de preuve de présence fiable."""
        sources = [_native("Catégories\nChambres 12 15,00", page=3, filename=A6),
                   _ocr("Local ménage 4 3,85", page=1, filename=A6)]
        answer = "Aucune mention de « Local ménage » dans le document « A6 surfaces.pdf »."
        assert answer in _absence_guard(answer, sources).answer

    def test_close_labels_are_not_interchangeable(self):
        sources = [_native("Équipements\nPompe AB 866,2", page=3, filename=A6), _ocr("x", page=4, filename=A6)]
        answer = "Aucune mention de « Pompe A » dans les sources fournies."
        assert answer in _absence_guard(answer, sources, question="Quelle ligne ?").answer

    def test_unresolved_scope_follows_a_demonstrated_contradiction_on_the_same_label(self):
        answer = ("Je ne trouve pas de mention de « Local ménage » dans le document « A6 surfaces.pdf » [1]. "
                  "Les catégories de ce tableau incluent les chambres, mais pas « Local ménage ». "
                  "Les catégories de ce tableau excluent les parkings, mais pas « Local entretien ».")
        d = _absence_guard(answer)
        assert "Local ménage" not in d.answer.split("\n\n", 1)[-1] and "Local entretien" in d.answer
        assert d.absences_removed == 2

    def test_proven_value_is_kept_beside_a_removed_absence(self):
        q = "Quelle est la valeur de « Surface utile » pour la ligne « Local technique CFA » ?"
        answer = ("La valeur de « Surface utile » pour « Local technique CFA » est de 9,03 [1]. "
                  "Aucune mention de « Local ménage » n'est présente dans les sources fournies.")
        d = _absence_guard(answer, question=q)
        assert "9,03 [1]" in d.answer and "Aucune mention" not in d.answer and d.absences_removed == 1

    def test_false_absence_with_a_number_is_removed_and_the_number_never_shown(self):
        q = "Quelle est la valeur de « Surface utile » pour la ligne « Local technique CFA » ?"
        answer = "Aucune mention de « Local ménage » dans [1], seule la valeur 38,74 y figure."
        d = _absence_guard(answer, question=q)
        assert "38,74" not in d.answer

    def test_only_a_false_absence_falls_back_to_the_existing_abstention(self):
        d = _absence_guard("Aucune mention de « Local ménage » n'est présente dans les sources fournies [1].")
        assert d.verdict == NE.VERDICT_BLOCK and "Local ménage" not in d.answer

    def test_outside_the_guard_perimeter_nothing_changes(self):
        """Hors périmètre (aucune source lue sur image ni tableau) : le flux n'est pas tamponné, la réponse
        est inchangée — limite documentée."""
        answer = "Aucune mention de « Local ménage » n'est présente dans les sources fournies."
        d = NE.guard_answer(answer, question=Q_MENAGE, sources=NE.annotate_numeric_evidence(
            [_native("3 Local ménage 4 3,85 15,38", page=3, filename=A6)], table_pages_by_doc={}))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)


class TestStructuredProvenancePolicy:
    """une sortie StructX n'est une preuve chiffrée que si le Gateway la DÉCLARE
    qualifiée ; sinon elle reste recherchable et citable, sans certifier aucun nombre. Valeurs synthétiques."""

    ROW = staticmethod(lambda: _row("Pompe de relevage", **{"Puissance installée (kW)": "18,5"}, Tension="400"))
    ANSWER = "La puissance installée de la pompe de relevage est de 18,5 kW [1]."

    @pytest.mark.parametrize("provenance", [
        pytest.param(UNQUALIFIED, id="experimental-unqualified"),
        pytest.param({**QUALIFIED, "experimental": True}, id="qualified-but-experimental"),
        pytest.param({**QUALIFIED, "qualified_for_production": False}, id="not-qualified"),
        pytest.param({**QUALIFIED, "extraction_status": "partial"}, id="partial-extraction"),
        pytest.param({"experimental": False}, id="qualification-field-missing"),
        pytest.param({**QUALIFIED, "qualified_for_production": "true"}, id="string-is-not-true"),
        pytest.param({**QUALIFIED, "confidence": 0.99, "qualified_for_production": False}, id="confidence-is-not-proof"),
        pytest.param(QUALIFIED_UNCERTIFIED, id="qualified-engine-but-no-evidence-v1"),
        pytest.param({**QUALIFIED, "evidence": {**QUALIFIED["evidence"], "certified": False}}, id="certified-false"),
        pytest.param({**QUALIFIED, "evidence": {**QUALIFIED["evidence"], "certified": "true"}}, id="certified-string"),
        pytest.param({**QUALIFIED, "evidence": None}, id="evidence-missing"),
        pytest.param({**QUALIFIED, "evidence": {**QUALIFIED["evidence"], "numeric_claims_supported": False}},
                     id="numeric-claims-not-supported"),
        pytest.param({**QUALIFIED, "qualification": {"business_numeric_use_authorized": False}}, id="business-use-denied"),
        pytest.param({**QUALIFIED, "evidence": {**QUALIFIED["evidence"], "consistency_status": "anomalies_detected"}},
                     id="document-anomaly"),
        pytest.param({**QUALIFIED, "item_consistency": {"status": "anomalies_detected", "anomalies": ["ragged_rows"]}},
                     id="item-anomaly"),
        pytest.param({**QUALIFIED, "evidence": {**QUALIFIED["evidence"], "consistency_status": "not_applicable"}},
                     id="consistency-not-applicable"),
    ])
    def test_unqualified_structured_value_is_never_numeric_proof(self, provenance):
        d = _guard(self.ANSWER, _structured(self.ROW(), page=5, provenance=provenance), question=Q_POWER)
        assert d.verdict == NE.VERDICT_BLOCK and "18,5" not in d.answer

    def test_unqualified_structured_source_stays_citable_and_flagged(self):
        [a] = _annotated(_structured(self.ROW(), page=5, provenance=UNQUALIFIED))
        meta = a["source"]["meta"]
        assert meta[NE.META_KEY] == NE.STRUCTURED_UNQUALIFIED and meta["requires_visual_check"] is True
        assert NE.context_is_protected([a])
        assert "Pompe de relevage" in a["text"]  # contenu inchangé : recherché, présenté, cité

    def test_qualified_structured_value_on_the_right_row_and_column_passes(self):
        d = _guard(self.ANSWER, _structured(self.ROW(), page=5), question=Q_POWER)
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, self.ANSWER)

    def test_unqualified_structured_plus_independent_native_text_passes_on_the_text(self):
        d = _guard(self.ANSWER, _structured(self.ROW(), page=5, provenance=UNQUALIFIED),
                   _native("La pompe de relevage a une puissance installée de 18,5 kW.", page=7), question=Q_POWER)
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, self.ANSWER)
        assert all(p["status"] != NE.STRUCTURED_UNQUALIFIED for p in d.claims[0]["proof"])

    def test_describe_only_value_is_still_never_proof(self):
        d = _guard(self.ANSWER, _describe("Graphique : la pompe de relevage atteint environ 18,5 kW.", page=4),
                   question=Q_POWER)
        assert d.verdict == NE.VERDICT_BLOCK

    def test_value_estimated_from_an_unqualified_chart_is_not_proof(self):
        chart = "Graphique (extraction structurée expérimentale)\n- 2024 : 18,5\n- 2025 : 21,0"
        d = _guard("En 2024, la valeur est de 18,5 [1].", _structured(chart, page=5, provenance=UNQUALIFIED, task="chart"),
                   question="Quelle est la valeur en 2024 ?")
        assert d.verdict == NE.VERDICT_BLOCK and "18,5" not in d.answer

    def test_native_text_value_unaffected(self):
        answer = "La tension est de 400 V [1]."
        d = _guard(answer, _native("La tension nominale est de 400 V.", page=2), _ocr("bruit", page=3))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

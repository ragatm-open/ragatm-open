"""QUANTIFICATION-NEGATION — non-quantification contredite par le passage CITÉ.

Constat réel (FINAL-QUALIFICATION-13, B001, sync et flux) : « While the report notes that "most of the …
matched DB may be classified as VC investments" [1], it does not quantify this share » alors que la même
proposition du fragment récupéré de [1] porte « (43 %) », coupé de l'extrait par la fenêtre de contexte.

Contrat : la contradiction n'est démontrée que si la phrase CITE mot pour mot (>= 6 mots) un passage de la
source qu'elle désigne et que la PROPOSITION de la source qui contient ce passage porte un nombre du type nié.
Un nombre ailleurs dans le fragment ne suffit pas. Contrôle actif aussi hors périmètre numérique. Textes
SYNTHÉTIQUES.
"""
from __future__ import annotations

from apps.rag_core.services.query import numeric_evidence as NE

FULL = ("Key findings\n▪ The survey covers two panels of firms.\n"
        "▪ Most of the deals recorded in the joint panel can be counted as seed financing (38 %) under our "
        "definition, while the others mainly fall into debt (21 %) and grants (6 %).\n"
        "▪ Most seed deals fall into the first round (52 %).")
EXCERPT = ("Key findings\n▪ The survey covers two panels of firms.\n"
           "▪ Most of the deals recorded in the joint panel can be counted as seed financing…")
QUESTION = "What share of the deals in the joint panel counts as seed financing under the study's definition?"
QUOTE = '"most of the deals recorded in the joint panel can be counted as seed financing"'


def _src(text=EXCERPT, full=FULL, *, page=22, ocr=False, doc="d1"):
    chunk = {"chunk_id": f"c{page}", "text": text,
             "source": {"document_id": doc, "page_number": page, "filename": "survey.pdf",
                        "meta": {"source_is_ocr": ocr}}}
    if full is not None:
        chunk[NE.RETRIEVED_TEXT_KEY] = full
    return chunk


def _guard(answer, *chunks, question=QUESTION, language="en"):
    sources = NE.annotate_numeric_evidence(list(chunks), table_pages_by_doc={})
    return NE.guard_answer(answer, question=question, sources=sources, language=language)


class TestAnchoredContradiction:
    def test_false_non_quantification_with_quoted_passage_is_removed(self):
        answer = (f"The survey defines seed financing broadly [1]. While the report notes that {QUOTE} [1], "
                  "it does not quantify this share.")
        d = _guard(answer, _src())
        assert d.verdict == NE.VERDICT_REDACT and d.quantifications_removed == 1
        body = d.answer.split("\n\n", 1)[1]
        assert "does not quantify" not in body and body == "The survey defines seed financing broadly [1]."
        assert d.answer.startswith("⚠️ 1 statement(s) claiming that a source does not quantify")
        assert "38" not in d.answer  # aucune valeur injectée

    def test_same_object_negations_of_the_answer_are_removed_too(self):
        answer = (f"While it is stated that {QUOTE} [1], no exact proportion is given.\n"
                  "The sources do not specify the exact share of deals in the joint panel.\n"
                  "Therefore, the precise share remains unknown.\n"
                  "There is no breakdown of how many deals in the joint panel are seed deals.\n"
                  "The report does not quantify the employment effects of grants [2].")
        d = _guard(answer, _src(), _src("Grants support employment in small firms.", None, page=40, doc="d2"))
        assert d.quantifications_removed == 4
        assert "employment effects of grants [2]" in d.answer  # autre objet, autre source : conservée

    def test_french_pronominal_negation(self):
        answer = ("L'étude couvre deux panels d'entreprises [1]. "
                  f"Le rapport indique que {QUOTE.replace(chr(34), '« ', 1).replace(chr(34), ' »')} [1], "
                  "mais ne la quantifie pas.")
        d = _guard(answer, _src(), question="Quelle part des opérations relève du financement d'amorçage ?",
                   language="fr")
        assert d.quantifications_removed == 1
        assert d.answer.startswith("⚠️ 1 affirmation(s) selon lesquelles une source ne chiffre pas")
        assert d.answer.endswith("L'étude couvre deux panels d'entreprises [1].")

    def test_answer_reduced_to_nothing_becomes_the_controlled_abstention(self):
        d = _guard(f"While the report notes that {QUOTE} [1], it does not quantify this share.", _src())
        assert d.verdict == NE.VERDICT_BLOCK and "quantify" not in d.answer

    def test_citation_absent_searches_the_context(self):
        d = _guard(f"While the report notes that {QUOTE}, it does not quantify this share.", _src())
        assert d.quantifications_removed == 1

    def test_protected_context_applies_the_same_check(self):
        table = {"chunk_id": "t1", "text": "Row: Panel A\n\nPanel:\nPanel A\n\nDeals:\n120",
                 "source": {"document_id": "d9", "page_number": 3, "filename": "annex.pdf",
                            "meta": {"kind": "table", "table": {}}}}
        d = _guard(f"While the report notes that {QUOTE} [1], it does not quantify this share.", _src(), table)
        assert NE.context_is_protected(NE.annotate_numeric_evidence([_src(), table], table_pages_by_doc={}))
        assert d.quantifications_removed == 1


class TestNotContradicted:
    def test_true_non_quantification_is_kept(self):
        full = FULL.replace(" (38 %)", "")
        answer = f"While the report notes that {QUOTE} [1], it does not quantify this share."
        d = _guard(answer, _src(full=full))
        assert (d.verdict, d.answer) == (NE.VERDICT_PASS, answer)

    def test_number_elsewhere_in_the_fragment_is_not_a_contradiction(self):
        """La proposition citée ne porte aucun nombre ; « 52 % » est dans une AUTRE proposition."""
        full = FULL.replace(" (38 %)", "")
        answer = f"While the report notes that {QUOTE} [1], it does not quantify this share."
        assert _guard(answer, _src(full=full)).verdict == NE.VERDICT_PASS

    def test_count_in_the_clause_does_not_contradict_a_share_negation(self):
        full = FULL.replace("(38 %)", "(1 204 deals)")
        answer = f"While the report notes that {QUOTE} [1], it does not give the percentage."
        assert _guard(answer, _src(full=full)).verdict == NE.VERDICT_PASS

    def test_negation_without_quoted_anchor_is_kept(self):
        answer = "The sources do not provide the share of deals in the joint panel that are seed financing [1]."
        assert _guard(answer, _src()).verdict == NE.VERDICT_PASS

    def test_short_quote_is_not_an_anchor(self):
        answer = 'While "most" deals may be seed financing [1], the report does not quantify this share.'
        assert _guard(answer, _src()).verdict == NE.VERDICT_PASS

    def test_property_negation_is_not_a_quantification(self):
        answer = f"While the report notes that {QUOTE} [1], it does not specify whether this share includes grants."
        assert _guard(answer, _src()).verdict == NE.VERDICT_PASS

    def test_other_cited_source_is_out_of_scope(self):
        other = _src("Unrelated text about grants.", "Unrelated text about grants.", page=90, doc="d2")
        answer = f"While the report notes that {QUOTE} [2], it does not quantify this share."
        assert _guard(answer, _src(), other).verdict == NE.VERDICT_PASS

    def test_incoherent_full_fragment_is_not_used(self):
        """Le fragment entier n'est consulté que s'il contient l'extrait transmis (CONTEXT-GROUNDING-11)."""
        answer = f"While the report notes that {QUOTE} [1], it does not quantify this share."
        d = _guard(answer, _src(full="A different page entirely. " + FULL.replace("Key findings", "Summary")))
        assert d.verdict == NE.VERDICT_PASS

    def test_ocr_source_is_never_presence_evidence(self):
        answer = f"While the report notes that {QUOTE} [1], it does not quantify this share."
        assert _guard(answer, _src(text=FULL, full=None, ocr=True)).quantifications_removed == 0

    def test_relation_inversion_is_not_detected_documented_limit(self):
        """B002 (limite DOCUMENTÉE, pas une garantie) : bonne valeur, mauvais sujet, citation exacte — aucun
        contrôle déterministe ne relie aujourd'hui sujet et valeur dans un texte narratif."""
        src = _src("EU investors cooperate more frequently with US (67 %) than Chinese (4 %) ones.", None)
        answer = "67 % of mixed deals with a US partner are completed jointly with EU investors [1]."
        assert _guard(answer, src).verdict == NE.VERDICT_PASS

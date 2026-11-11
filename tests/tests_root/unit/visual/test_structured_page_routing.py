"""routage des pages entières vers la tâche ``table``.

Constat PILOT-2 (staging, documents réels) : une page entière est décrite par Vision comme
« un rapport de l'Insee… » ; la première phrase ne nomme ni tableau ni graphique, mais la
liste ``Objects:`` du contrat ``describe`` en contient un (« - Tableau (count: 1; …) »). Le
routeur, qui ne lisait que la première phrase, classait OTHER : aucun appel, tableau perdu,
alors que le moteur l'extrayait à 20/20.

Règle : la tête nominale de la première phrase garde la priorité (comportement inchangé) ;
SEULEMENT si elle ne nomme rien, les NOMS d'objets de la liste ``Objects:`` décident
(tableau avant graphique). TABLE-RECOVERY-1 : à défaut, le corps de la description et les
ATTRIBUTS d'objets qui nomment un tableau (page réelle observée, p. 3 : objet « Section 5. … »
d'attributs « tableau avec colonnes ») routent vers ``table``, sauf si un graphique y est nommé.
Le texte visible (OCR de la page) ne déclenche jamais rien. Le routage n'ouvre aucune
publication : preuve géométrique, validation de grille et politique de tâches restent appliquées
en aval.
"""
from __future__ import annotations

from apps.rag_core.services.ingest.visual import structured_auto as A

REPORT_PAGE = """Visual type: photograph
Description: L'image montre un rapport sur les créations d'entreprises en novembre 2025.
Objects:
- Logo (count: 1)
- Titre principal (count: 1; attributes: Hausse modérée des créations)
- Graphique (count: 1; attributes: lignes bleues et rouges)
- Tableau (count: 1; attributes: Évolution mensuelle par type)
- Texte descriptif (count: 1; attributes: variations saisonnières)
Visible text:
- Hausse modérée des créations"""

TEXT_PAGE_WITH_TWO_TABLES = """Visual type: photograph
Description: L'image contient un document textuel en français.
Objects:
- titre principal (count: 1; attributes: texte)
- paragraphe (count: 3; attributes: texte)
- tableau (count: 2; attributes: texte, chiffres)
Visible text:
- Rebond des créations"""

GRAPH_ONLY_REPORT = """Visual type: photograph
Description: L'image montre un document de l'Insee intitulé 'Comptes trimestriels'.
Objects:
- titre principal (count: 1; attributes: texte)
- graphique (count: 1; attributes: variation trimestrielle)"""

WORD_ONLY_IN_ATTRIBUTES = """Visual type: photograph
Description: L'image montre un document avec du texte.
Objects:
- texte (count: 1; attributes: le tableau ci-dessous présente les résultats)
Visible text:
- Tableau 3 : résultats détaillés"""

NO_OBJECTS = """Visual type: photograph
Description: L'image montre un document de l'Insee avec un titre et du texte."""


class TestPageRouting:
    def test_report_page_with_table_object_is_routed_to_table(self):
        cls, proof = A.classify_from_describe("photo", REPORT_PAGE)
        assert cls == A.CLASS_TABLE
        assert proof == "describe.objects:'tableau'"

    def test_table_object_after_a_chart_object_still_routes_table(self):
        """Le graphique est cité avant le tableau : la page reste routée vers la tâche table."""
        assert A.classify_from_describe("photo", REPORT_PAGE)[0] == A.CLASS_TABLE

    def test_several_tables_in_object_list_are_routed(self):
        cls, proof = A.classify_from_describe("photo", TEXT_PAGE_WITH_TWO_TABLES)
        assert cls == A.CLASS_TABLE and proof == "describe.objects:'tableau'"

    def test_graph_only_page_is_never_routed_as_table(self):
        cls, proof = A.classify_from_describe("photo", GRAPH_ONLY_REPORT)
        assert cls == A.CLASS_CHART and proof == "describe.objects:'graphique'"

    def test_table_named_in_attributes_routes_a_candidate(self):
        """TABLE-RECOVERY-1 (constat réel) : candidat seulement — la région reste à prouver en aval."""
        cls, proof = A.classify_from_describe("photo", WORD_ONLY_IN_ATTRIBUTES)
        assert (cls, proof) == (A.CLASS_TABLE, "describe.body_or_attributes:'tableau'")

    def test_word_only_in_visible_text_routes_nothing(self):
        text = ("Visual type: photograph\nDescription: L'image montre un document avec du texte.\n"
                "Objects:\n- texte (count: 1; attributes: paragraphes)\nVisible text:\n- Tableau 3 : résultats détaillés")
        assert A.classify_from_describe("photo", text)[0] == A.CLASS_OTHER

    def test_table_in_a_later_description_sentence_routes_a_candidate(self):
        text = ("Visual type: photograph\nDescription: L'image montre une page de rapport. "
                "Elle contient un tableau des composants par lot.\nObjects:\n- en-tête (count: 1)")
        assert A.classify_from_describe("photo", text) == (A.CLASS_TABLE, "describe.body_or_attributes:'tableau'")

    def test_chart_named_anywhere_blocks_the_body_rule(self):
        for text in (
            "Visual type: photograph\nDescription: Une page de rapport. Un tableau et un graphique en barres.",
            "Visual type: photograph\nDescription: Une page de rapport.\nObjects:\n"
            "- bloc (count: 1; attributes: tableau, courbes de variation)",
            "Visual type: photograph\nDescription: Une page de rapport.\nObjects:\n"
            "- graphique (count: 1)\n- texte (count: 1; attributes: renvoie au tableau 2)",
        ):
            assert A.classify_from_describe("photo", text)[0] != A.CLASS_TABLE

    def test_electrical_panel_in_attributes_is_not_a_table(self):
        text = ("Visual type: photograph\nDescription: L'image montre un local technique.\n"
                "Objects:\n- armoire (count: 1; attributes: tableau électrique, disjoncteurs)")
        assert A.classify_from_describe("photo", text)[0] == A.CLASS_OTHER

    def test_page_without_any_table_word_stays_other(self):
        """Constat réel (page 5 du même document) : aucun signal dans la description ni les objets -> non routée."""
        text = ("Visual type: photograph\nDescription: L'image montre un document technique.\nObjects:\n"
                "- Projet (count: 1; attributes: texte)\n- Date (count: 1)\n- Applicateur (count: 1)")
        assert A.classify_from_describe("photo", text)[0] == A.CLASS_OTHER

    def test_no_object_list_keeps_current_safe_behavior(self):
        assert A.classify_from_describe("photo", NO_OBJECTS)[0] == A.CLASS_OTHER

    def test_first_sentence_head_keeps_priority(self):
        """Comportement historique inchangé : la phrase nomme un graphique -> CHART, même si
        la liste d'objets cite aussi un tableau."""
        text = ("Visual type: photograph\nDescription: L'image contient un graphique et du texte.\n"
                "Objects:\n- graphique (count: 1)\n- tableau (count: 1)")
        assert A.classify_from_describe("photo", text) == (A.CLASS_CHART, "describe.description:'graphique'")

    def test_electrical_panel_object_is_not_a_table(self):
        text = ("Visual type: photograph\nDescription: L'image montre un local technique.\n"
                "Objects:\n- tableau électrique (count: 1)")
        assert A.classify_from_describe("photo", text)[0] == A.CLASS_OTHER

    def test_visual_kind_still_wins(self):
        assert A.classify_from_describe("chart", REPORT_PAGE) == (A.CLASS_CHART, "describe.visual_kind=chart")

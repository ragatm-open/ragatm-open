"""Lot E4 — classificateur analytique Excel : le corpus qui décide de son utilité.

Une garde trop stricte rend Excel inutilisable ; trop laxiste, elle laisse passer un
chiffre faux présenté comme sûr. Tout se joue donc dans les **faux positifs** : le mot
« total » dans « le total **indiqué sur la ligne** Atlas » est un LIBELLÉ, pas une
opération. Ce fichier est plus long que le classificateur — c'est voulu.
"""
import pytest

from apps.chat.services.excel_guard import (
    ANSWER_KIND,
    INTENT_DOCUMENT_LOOKUP,
    INTENT_EXHAUSTIVE_AGGREGATION,
    INTENT_EXHAUSTIVE_COUNT,
    INTENT_GLOBAL_COMPARISON,
    INTENT_GROUPING,
    INTENT_RANKING,
    build_refusal,
    classify_excel_query,
    guard,
    question_mentions_workbook,
    sources_cite_excel,
)


def _d(q, *, ctx=True):
    return classify_excel_query(q, workbook_context=ctx)


# --------------------------------------------------------------------------- #
# BLOQUÉ — la question exige de parcourir tout le classeur
# --------------------------------------------------------------------------- #
class TestBlocked:
    @pytest.mark.parametrize(("q", "intent"), [
        # Agrégations
        ("Calcule le total du budget dans Budget.xlsx.", INTENT_EXHAUSTIVE_AGGREGATION),
        ("Additionne toute la colonne Budget du classeur.", INTENT_EXHAUSTIVE_AGGREGATION),
        ("Quelle est la moyenne des dépenses dans ce classeur ?", INTENT_EXHAUSTIVE_AGGREGATION),
        ("Fais la somme des montants de la feuille Budget.", INTENT_EXHAUSTIVE_AGGREGATION),
        ("Quel est le cumul de toutes les lignes du classeur ?", INTENT_EXHAUSTIVE_AGGREGATION),
        ("Donne la médiane des budgets de ce classeur.", INTENT_EXHAUSTIVE_AGGREGATION),
        # Comptages exhaustifs
        ("Combien de projets y a-t-il dans toutes les feuilles ?", INTENT_EXHAUSTIVE_COUNT),
        ("Combien de lignes ont le statut En retard dans ce classeur ?", INTENT_EXHAUSTIVE_COUNT),
        ("Recense toutes les lignes du classeur.", INTENT_EXHAUSTIVE_COUNT),
        ("Quel est le nombre de projets dans le classeur ?", INTENT_EXHAUSTIVE_COUNT),
        # Classements
        ("Classe les agences par chiffre d'affaires dans Budget.xlsx.", INTENT_RANKING),
        ("Donne-moi le top 10 des responsables de ce classeur.", INTENT_RANKING),
        ("Quel est le projet le plus coûteux du classeur ?", INTENT_RANKING),
        ("Quelle feuille contient la valeur la moins élevée de tout le fichier ?", INTENT_RANKING),
        # Regroupements
        ("Quel est le budget total par direction dans ce classeur ?", INTENT_GROUPING),
        ("Regroupe les coûts par région dans Budget.xlsx.", INTENT_GROUPING),
        ("Ventile les montants par responsable dans la feuille Synthèse.", INTENT_GROUPING),
        ("Donne la répartition des dépenses par service du classeur.", INTENT_GROUPING),
        # Comparaisons globales
        ("Compare exhaustivement 2025 et 2026 dans ce classeur.", INTENT_GLOBAL_COMPARISON),
        ("Compare toutes les agences de la feuille Synthèse.", INTENT_GLOBAL_COMPARISON),
    ])
    def test_analytic_questions_are_blocked(self, q, intent):
        d = _d(q)
        assert d.applies is True, q
        assert d.intent == intent, f"{q} -> {d.intent}"
        assert guard(q) is not None

    def test_grouping_captures_the_dimension(self):
        """Amorce P2 : on capture CE QUE l'utilisateur voulait, sans plan d'exécution."""
        d = _d("Quel est le budget total par direction dans ce classeur ?")
        assert d.intent == INTENT_GROUPING
        assert d.groups == ("direction",)
        assert d.scope == "whole_workbook"

    def test_exhaustive_scope_raises_confidence(self):
        vague = _d("Quel est le projet le plus coûteux de la feuille ?")
        explicit = _d("Quel est le projet le plus coûteux de tout le classeur ?")
        assert vague.applies and explicit.applies
        assert explicit.confidence == "high"
        assert explicit.scope == "whole_workbook"


# --------------------------------------------------------------------------- #
# AUTORISÉ — lecture d'un fait, même si les mots ressemblent à du calcul
# --------------------------------------------------------------------------- #
class TestAllowedLocalReads:
    @pytest.mark.parametrize("q", [
        # Lectures directes
        "Quel est le budget du projet Atlas dans Budget.xlsx ?",
        "Qui est responsable de la ligne « Migration ERP » du classeur ?",
        "Que contient la feuille Risques ?",
        "Résume la feuille Synthèse.",
        "Donne-moi les lignes concernant le projet Orion dans ce classeur.",
        "Quelle est la date du projet Orion dans la feuille Synthèse ?",
        # 🔴 LE piège : des mots d'agrégat qui sont en réalité des LIBELLÉS
        "Quel est le total indiqué sur la ligne Atlas dans le classeur ?",
        "Lis la cellule intitulée Total général.",
        "Quel est le montant maximum autorisé indiqué dans la feuille ?",
        "Le champ « Moyenne » de la feuille vaut-il 12 ?",
        "Quelle est la valeur de la colonne Total pour Orion dans le classeur ?",
        "Montre-moi la ligne « Classement 2025 » de la feuille Synthèse.",
        "Quel est le nombre inscrit dans la cellule B12 ?",
        # Comparaisons LOCALES
        "Le budget d'Atlas dépasse-t-il 100 000 € dans ce classeur ?",
        "Combien vaut le budget d'Atlas dans la feuille Synthèse ?",
        "Quels projets ont le statut En retard dans les extraits du classeur ?",
    ])
    def test_documentary_questions_pass_through(self, q):
        d = _d(q)
        assert d.applies is False, f"FAUX POSITIF : {q} -> {d.intent} ({d.reason})"
        assert guard(q) is None

    def test_a_label_is_not_an_operation(self):
        """La distinction centrale du lot, isolée."""
        assert _d("Quel est le total indiqué sur la ligne Atlas ?").applies is False
        assert _d("Calcule le total de la feuille Atlas.").applies is True

    def test_cell_reference_is_always_a_read(self):
        assert _d("Quelle valeur figure en B12 de la feuille Synthèse ?").applies is False

    def test_an_explicit_verb_is_never_rescued_by_a_read_marker(self):
        """« Calcule le total indiqué » reste un CALCUL : le verbe l'emporte."""
        assert _d("Calcule le total indiqué sur toutes les lignes du classeur.").applies is True


# --------------------------------------------------------------------------- #
# HORS CONTEXTE CLASSEUR — la condition B manque, on ne bloque rien
# --------------------------------------------------------------------------- #
class TestNonExcelQuestions:
    @pytest.mark.parametrize("q", [
        "Quelle est la moyenne nationale du chômage ?",
        "Classe les risques mentionnés dans ce rapport PDF.",
        "Combien de documents parlent de cybersécurité ?",
        "Calcule le total des amendes citées dans le rapport.",
        "Quel est le budget total par direction ?",          # ambigu -> pas de blocage
    ])
    def test_no_workbook_context_no_block(self, q):
        """Un `.xlsx` qui traîne ailleurs dans l'espace ne doit RIEN faire basculer."""
        d = classify_excel_query(q, workbook_context=False)
        assert d.applies is False
        assert d.reason == "no_workbook_context"
        assert d.intent == INTENT_DOCUMENT_LOOKUP
        assert guard(q, thread=None) is None

    def test_the_same_question_blocks_once_the_workbook_is_named(self):
        q = "Quel est le budget total par direction"
        assert classify_excel_query(q, workbook_context=False).applies is False
        assert classify_excel_query(q + " dans Budget.xlsx ?", workbook_context=True).applies is True


class TestWorkbookContextDetection:
    @pytest.mark.parametrize("q", [
        "Calcule le total dans Budget_2026.xlsx",
        "Quel est le total de ce classeur ?",
        "Que dit la feuille Synthèse ?",
        "Quelle est la valeur de la cellule B12 ?",
        "Ouvre le fichier Excel",
        "What is the total in the spreadsheet?",
        "Show me the sheet Synthèse",
    ])
    def test_detected(self, q):
        assert question_mentions_workbook(q) is True

    @pytest.mark.parametrize("q", [
        "Quelle est la moyenne nationale du chômage ?",
        "Résume le rapport annuel.",
        "Combien de documents parlent de cybersécurité ?",
    ])
    def test_not_detected(self, q):
        assert question_mentions_workbook(q) is False

    def test_thread_context_from_a_previous_excel_citation(self):
        """« Le budget total par direction ? » posé APRÈS une réponse issue d'un classeur."""
        excel_sources = [{"filename": "B.xlsx", "meta": {"kind": "excel_table",
                                                         "sheet_name": "Synthèse"}}]
        pdf_sources = [{"filename": "R.pdf", "meta": {}}]
        assert sources_cite_excel(excel_sources) is True
        assert sources_cite_excel(pdf_sources) is False
        assert sources_cite_excel(None) is False
        assert sources_cite_excel([{"meta": {"kind": "table"}}]) is False   # tableau PDF (P3)


# --------------------------------------------------------------------------- #
# Variantes : accents, casse, anglais, ponctuation
# --------------------------------------------------------------------------- #
class TestVariants:
    @pytest.mark.parametrize("q", [
        "CALCULE LE TOTAL DU CLASSEUR",
        "calcule   le  total   du   classeur",
        "Calcule le total du classeur !!!",
        "Quelle est la mediane des budgets du classeur ?",     # sans accent
        "Quelle est la médiane des budgets du classeur ?",
        "Regroupe les couts par region dans le classeur",      # sans accents
        "Calculate the total budget by department in Budget.xlsx.",
        "Rank all projects by cost in the spreadsheet.",
        "How many rows are in the whole workbook?",
        "Group the costs by region in the sheet.",
    ])
    def test_blocked_variants(self, q):
        assert _d(q).applies is True, q

    @pytest.mark.parametrize("q", [
        "What value is shown for Atlas in the spreadsheet?",
        "Read the cell B12 of the sheet.",
        "Quel est le montant affiché pour Atlas dans le classeur ?",
    ])
    def test_allowed_variants(self, q):
        assert _d(q).applies is False, q


# --------------------------------------------------------------------------- #
# Réponse de refus
# --------------------------------------------------------------------------- #
class TestRefusal:
    def test_message_is_honest_and_actionable(self):
        d = _d("Quel est le budget total par direction dans ce classeur ?")
        answer, suggestions = build_refusal(d)

        assert "calcul exhaustif" in answer
        assert "pas encore disponible" in answer
        # Le message dit POURQUOI, pas seulement « non ».
        assert "extraits retrouvés" in answer
        assert len(suggestions) == 2

    def test_suggestions_never_reintroduce_the_forbidden_calculation(self):
        for q in (
            "Calcule le total du classeur.",
            "Classe les agences du classeur par chiffre d'affaires.",
            "Regroupe les coûts par région du classeur.",
            "Combien de lignes dans tout le classeur ?",
            "Compare toutes les agences du classeur.",
        ):
            _, suggestions = build_refusal(_d(q))
            joined = " ".join(suggestions).lower()
            for forbidden in ("calcule", "total de", "somme", "moyenne", "classe ",
                              "top ", "regroupe"):
                assert forbidden not in joined, f"{q} -> {suggestions}"

    def test_grouping_suggestion_uses_the_detected_dimension(self):
        d = _d("Quel est le budget total par direction dans ce classeur ?")
        _, suggestions = build_refusal(d)
        assert any("direction" in s for s in suggestions)

    def test_answer_kind_is_stable(self):
        assert ANSWER_KIND == "excel_analytics_unavailable"


# --------------------------------------------------------------------------- #
# Robustesse
# --------------------------------------------------------------------------- #
class TestRobustness:
    @pytest.mark.parametrize("q", ["", "   ", "?", "…"])
    def test_empty_or_noise_never_blocks(self, q):
        assert _d(q).applies is False
        assert guard(q) is None

    def test_guard_never_raises_on_a_broken_thread(self):
        class Broken:
            pass

        assert guard("Calcule le total du classeur.", thread=Broken()) is not None

    def test_par_exemple_is_not_a_grouping_dimension(self):
        """« par exemple », « par rapport à » ne sont pas des dimensions."""
        d = _d("Quel est le total indiqué, par exemple sur la ligne Atlas du classeur ?")
        assert d.applies is False

"""Tests purs des heuristiques d'intent (inventory / explanatory / comparative)."""
import pytest

from apps.rag_core.services.query.heuristics import (
    is_comparative_question,
    is_explanatory_question,
    is_inventory_question,
)


@pytest.mark.parametrize(
    "q,expected",
    [
        ("Liste des startups", True),
        ("Quels pitchs sont disponibles", True),
        ("tous les decks", True),
        ("Liste des produits", False),  # pas de collection reconnue
        ("les startups", False),  # pas d'intent liste
        ("", False),
        (None, False),
    ],
)
def test_is_inventory_question(q, expected):
    assert is_inventory_question(q) is expected


@pytest.mark.parametrize(
    "q,expected",
    [
        ("Explique le concept", True),
        ("Comment ça marche ?", True),
        ("Présente l'entreprise", True),
        ("Résume le document", True),
        ("Quelle est la capitale ?", False),
        ("", False),
    ],
)
def test_is_explanatory_question(q, expected):
    assert is_explanatory_question(q) is expected


# --- Demandes ANALYTIQUES : verbe de tête, livrable demandé, interrogatif ----
#
# Ces formulations recevaient le gabarit factuel (« réponse courte et directe ») :
# mesuré sur le corpus de dev, la politique `ok` produit une médiane de 252 caractères
# contre 2 080 en `explain`. La détection reposait sur une liste de mots-clés qui ne
# contenait ni « analyse », ni « rapport », ni « synthèse », ni « développe ».
@pytest.mark.parametrize(
    "q",
    [
        # (1) verbe analytique en tête de demande
        "Analyse les risques.",
        "Analyse les principaux risques.",
        "Développe les impacts sur les acteurs concernés.",
        "Détaille les enjeux du projet.",
        "Explique les causes de ces écarts.",
        "Présente les risques, leurs causes, impacts et contrôles.",
        "Résume les principaux constats et leurs impacts.",
        "Justifie le choix retenu.",
        "Commente les résultats du contrôle.",
        # amorce de politesse : la demande reste une demande
        "Peux-tu analyser les risques du dispositif ?",
        "Merci de détailler les contrôles de second niveau.",
        # (2) verbe de production + livrable analytique
        "Fais une analyse approfondie et structurée.",
        "Fais une synthèse exécutive du dossier.",
        "Rédige un rapport détaillé sur les contrôles.",
        "Prépare une analyse structurée des enjeux.",
        "Établis un bilan des actions correctives.",
        # (3) interrogatif explicatif
        "Pourquoi ces écarts ont-ils été constatés ?",
        "En quoi ce dispositif est-il pertinent ?",
        "Dans quelle mesure les contrôles couvrent-ils le risque ?",
    ],
)
def test_demandes_analytiques_reconnues(q):
    assert is_explanatory_question(q) is True


# --- Anti-faux-positifs : le MOT ne suffit pas, la FORME décide ---------------
#
# « rapport », « synthèse », « impact » sont des objets documentaires ordinaires. Une
# question de nom, de date, de montant ou de dénombrement qui les mentionne reste
# factuelle — sans quoi la moitié du corpus basculerait en réponse développée.
@pytest.mark.parametrize(
    "q",
    [
        "Quel est le nom du rapport ?",
        "Quelle est la date du rapport ?",
        "Quel est le montant de l'impact financier ?",
        "Combien de contrôles sont mentionnés ?",
        "Combien de rapports ont été produits ?",
        # Participe passé : un fait rapporté, pas une consigne. Le repliage des accents
        # rend « rédigé » identique à « rédige » : seuls la position et l'accent final
        # les séparent.
        "Qui a rédigé la synthèse ?",
        "Qui a analysé les écarts ?",
        "Le rapport détaillé a été publié le 3 mai.",
        # Questions factuelles simples du périmètre produit.
        "Quel est le montant du contrat ?",
        "Quelle est la date de mise en production ?",
        "Qui est responsable du contrôle ?",
    ],
)
def test_questions_factuelles_restent_factuelles(q):
    assert is_explanatory_question(q) is False


@pytest.mark.parametrize(
    "q",
    [
        # Deux objets NOMMÉS de part et d'autre d'une relation comparative explicite.
        # La doctrine complète (et ses limites assumées) vit dans
        # tests/tests_root/unit/test_comparative_intent.py.
        "Quelles sont les principales différences entre les architectures de reporting de Tessaly et de Valteria ?",
        "quelle est la difference entre Tessaly et Valteria",  # sans accent
        "Tessaly versus Valteria",
        "Tessaly vs Valteria",
        "Quels écarts entre le périmètre A et le périmètre B ?",
        # Le VERBE « comparer » en tête de demande est en soi l'intention, même quand
        # les objets restent sous-entendus : la politique `compare` s'applique, mais
        # `objects` reste None (aucune expansion déterministe). Cf. la doctrine dans
        # test_comparative_intent.py § « demande explicite, objets implicites ».
        "Compare les deux dispositifs de reporting",
        "Compare les risques entre les deux dispositifs.",
        # « en quoi X diffère de Y » : l'amorce interrogative distingue la DEMANDE de
        # comparaison du constat d'écart (« quels éléments diffèrent de la procédure ? »).
        "En quoi le dispositif Valteria diffère-t-il de celui de Tessaly ?",
    ],
)
def test_is_comparative_question_true(q):
    assert is_comparative_question(q) is True


@pytest.mark.parametrize(
    "q",
    [
        "Quelle est la capitale ?",
        "Liste des startups",
        "Explique le dispositif de maintenance",
        # Pas de faux positif par sous-chaîne : « avs » ne contient pas le mot « vs »,
        # « comparateur » n'est pas « comparer ».
        "Quel est le rôle de la division avs ?",
        # Le verbe « comparer » employé par un TIERS (sujet en tête) n'est pas une
        # demande : c'est la description de ce que fait un document.
        "Le rapport compare plusieurs valeurs.",
        "Quel est le taux comparé à l'année précédente ?",
        "La colonne comparaison est-elle renseignée ?",
        "",
        None,
        # --- Doctrine P0 : intention comparative SANS deux objets identifiables -----
        # Ces formulations étaient acceptées par la première version (déclenchement sur
        # un lexème isolé) : elles imposaient un gabarit comparatif à des questions dont
        # les objets ne sont pas nommés, ce qui poussait le modèle à comparer les
        # SOURCES entre elles. Le repli (politique standard/explicative) est plus sûr.
        "comparaison des architectures",
        "Tableau comparatif des deux approches",
        "Quels sont les points communs entre les deux dispositifs ?",
        "Y a-t-il des similitudes entre ces deux notes ?",
        "Quels écarts entre les deux méthodologies ?",
        # Vocabulaire de CONSTAT : un écart, un avantage ou une distinction ne sont pas
        # une demande de comparaison.
        "Quels sont les avantages de cette approche ?",
        "Quels inconvénients sont documentés ?",
        "Comment distinguer les deux périmètres ?",
        # Limite assumée, TOUJOURS en vigueur : sans l'amorce « en quoi », « X diffère
        # de Y » reste indistinguable d'un écart à un attendu.
        "Quels éléments diffèrent de la procédure attendue ?",
    ],
)
def test_is_comparative_question_false(q):
    assert is_comparative_question(q) is False


# ===========================================================================
# Lot d'ENRICHISSEMENT FR/EN — parité de couverture
# ===========================================================================
#
# Avant ce lot, le rappel `explain` mesuré sur le corpus versionné valait 0,602 en
# français et 0,018 en anglais : les listes de verbes, de livrables et
# d'interrogatifs étaient exclusivement françaises. Ces tests verrouillent le
# miroir anglais ET les formulations françaises qui manquaient.
from apps.rag_core.services.query.heuristics import analytic_intent_family  # noqa: E402


@pytest.mark.parametrize(
    "q",
    [
        # verbes analytiques français absents avant le lot
        "Évalue la solidité du dispositif.",
        "Apprécie l'efficacité des contrôles.",
        "Examine les causes des écarts.",
        "Étudie les impacts opérationnels.",
        "Discute les limites de la procédure.",
        "Interprète les constats.",
        "Expose les limites du dispositif.",
        "Éclaire-moi sur la portée du contrôle.",
        "Diagnostique les faiblesses du plan.",
        # locutions figées
        "Mets ces éléments en perspective.",
        "Tire les enseignements de ces résultats.",
        "Dégage les implications pour l'organisation.",
        "Fais ressortir les points clés.",
        "Donne-moi une lecture critique de la situation.",
        # interrogatifs analytiques
        "Que peut-on en déduire ?",
        "Que révèlent ces écarts ?",
        "Quelles sont les implications de ce constat ?",
        "Pour quelles raisons ce contrôle a-t-il échoué ?",
        # formes nominales
        "Synthèse des principaux risques.",
        "Rapport détaillé sur les contrôles.",
        "État des lieux de la documentation.",
        "Vue d'ensemble des principaux risques.",
        # ellipses autonomes
        "Une analyse plus poussée ?",
        "Plus de détail ?",
    ],
)
def test_demandes_analytiques_francaises(q):
    assert is_explanatory_question(q) is True


@pytest.mark.parametrize(
    "q",
    [
        # verbes analytiques — le miroir anglais n'existait pas du tout
        "Explain the framework.",
        "Analyze the main risks.",
        "Analyse the main risks.",          # variante britannique
        "Assess the robustness of the framework.",
        "Evaluate the effectiveness of the controls.",
        "Review the findings.",
        "Examine the root causes.",
        "Investigate the discrepancies.",
        "Discuss the limitations.",
        "Interpret the results.",
        "Describe the control process.",
        "Elaborate on the reporting obligations.",
        "Clarify the scope of the control.",
        "Outline the key findings.",
        "Critically assess the governance model.",
        # interrogatifs
        "What can be inferred from these findings?",
        "To what extent is the framework robust?",
        "Why was this control introduced?",
        "How does the validation chain work?",
        "What do these discrepancies reveal?",
        # synthèse / rapport — 0/20 avant le lot
        "Summarize the document.",
        "Summarise the document.",          # variante britannique
        "Provide an executive summary.",
        "Prepare a briefing note.",
        "Draft a detailed report.",
        "Write an assessment of the framework.",
        # formes nominales et ellipses
        "Executive summary of the key risks.",
        "Detailed report on the control framework.",
        "Overview of the main discrepancies.",
        "More detail?",
        "A deeper analysis?",
    ],
)
def test_demandes_analytiques_anglaises(q):
    assert is_explanatory_question(q) is True


@pytest.mark.parametrize(
    "q",
    [
        # noms de documents — le mot analytique désigne un objet, pas une demande
        "Quel est le nom du rapport ?",
        "Qui a produit l'analyse ?",
        "Quelle est la date de la synthèse ?",
        "Combien de diagnostics sont disponibles ?",
        "Quel est le titre de la synthèse ?",
        # verbe analytique à la 3e personne : c'est un CONSTAT
        "Le rapport compare plusieurs valeurs.",
        "Le dispositif analyse les événements.",
        # faux positif corrigé par ce lot : « contexte » seul ne déclenche plus
        "Le contexte réglementaire a-t-il changé ?",
        # négation
        "Ne compare pas les dispositifs ; donne leur date.",
        "Sans analyser les causes, liste les incidents.",
        # discours rapporté
        "Le document indique : « analyse les risques ».",
        "La note cite « explique les modalités de calcul ».",
    ],
)
def test_negatifs_francais_restent_factuels(q):
    assert is_explanatory_question(q) is False


@pytest.mark.parametrize(
    "q",
    [
        "What is the report name?",
        "Who wrote the analysis?",
        "The report compares several values.",
        "The system analyzes the events.",
        "How many reports were produced?",
        "How much is the financial impact?",
        "How long is the contract?",
        "How often is the control performed?",
        "Has the regulatory context changed?",
        "Do not compare the frameworks; give their dates.",
        "Without analyzing the causes, list the incidents.",
        "The document says “analyze the risks”.",
        "The note quotes “explain the calculation basis”.",
    ],
)
def test_negatifs_anglais_restent_factuels(q):
    assert is_explanatory_question(q) is False


# --- le CONTEXTE reste atteignable, mais seulement s'il est DEMANDÉ -----------
def test_le_contexte_demande_reste_explicatif():
    """Le mot seul ne déclenche plus ; le livrable demandé, si."""
    assert is_explanatory_question("Le contexte réglementaire a-t-il changé ?") is False
    assert is_explanatory_question("Présente le contexte réglementaire.") is True
    assert is_explanatory_question("Explique le contexte de la décision.") is True
    assert is_explanatory_question("Provide the regulatory context.") is True


# --- EXPLICABILITÉ : quelle famille de règle a tranché ? ----------------------
@pytest.mark.parametrize(
    "q,famille",
    [
        ("Pourquoi ce contrôle existe-t-il ?", "interrogative"),
        ("Why was this control introduced?", "interrogative"),
        ("Mets ces éléments en perspective.", "idiom"),
        ("Explique en détail le dispositif.", "marker"),
        ("Analyse les principaux risques.", "analytic_verb"),
        ("Analyze the main risks.", "analytic_verb"),
        ("Rédige un rapport détaillé sur les contrôles.", "production_deliverable"),
        ("Draft a detailed report on the controls.", "production_deliverable"),
        ("Synthèse des principaux risques.", "nominal_form"),
        ("Executive summary of the key risks.", "nominal_form"),
        ("Et les impacts ?", "ellipsis"),
        ("And the implications?", "ellipsis"),
        ("Quel est le nom du rapport ?", None),
    ],
)
def test_famille_de_regle_identifiable(q, famille):
    """Un faux positif doit pouvoir être imputé à UNE règle, pas au tout."""
    assert analytic_intent_family(q) == famille


# --- la négation est évaluée PAR PROPOSITION, pas globalement -----------------
def test_la_negation_ne_neutralise_que_sa_proposition():
    """« Ne compare pas … mais explique … » demande bien une explication."""
    assert is_explanatory_question(
        "Ne compare pas les dispositifs, mais explique leurs différences.") is True
    assert is_comparative_question(
        "Ne compare pas les dispositifs, mais explique leurs différences.") is False
    assert is_explanatory_question(
        "Ne compare pas les dispositifs ; analyse uniquement leurs risques.") is True


def test_une_demande_portant_sur_une_citation_reste_une_demande():
    """La garde vise la pseudo-consigne CITÉE, pas l'objet cité d'une vraie demande."""
    assert is_explanatory_question(
        "Analyse la phrase « les contrôles sont insuffisants ».") is True
    assert is_explanatory_question(
        "Le document indique : « analyse les risques ».") is False

# backend/apps/help/catalog.py
"""Catalogue éditorial des capacités RagATM — source de vérité de l'aide (Lot 1).

Nommé ``catalog`` et non ``capabilities`` : ``apps/llm_router/capabilities.py``
désigne déjà les capacités des *providers LLM*, tout autre chose.

INVARIANTS
----------
- **Jamais de LLM.** Le Markdown est produit par une fonction pure et déterministe.
- **Adossé au code, pas à la doc.** Une capacité n'est listée que si elle existe
  réellement (chronologie, comparaison, rapports : absents du code -> absents d'ici).
- **Jamais de mutation par phrase.** Les actions mutantes sont structurellement non
  déclenchables par une phrase (``registry.register`` impose
  ``natural_language_enabled=False => matchers=()``). Elles sont donc décrites comme
  des *gestes d'interface*, jamais comme des exemples à taper.
- **Rien de technique en sortie.** Aucun nom de flag, aucune route, aucun provider.

Lot 1 : catalogue statique, sans résolution de disponibilité (permissions, documents
présents, flags) — c'est l'objet du Lot 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """Une entrée du catalogue.

    ``action_id`` ancre l'entrée sur ``apps.actions.registry.ACTION_REGISTRY`` : c'est
    ce qui empêche le catalogue de promettre une capacité que le code ne sait pas faire.
    ``None`` = geste d'interface (non déclenchable par une phrase).
    """

    id: str
    category: str
    title: str
    description: str
    examples: tuple[str, ...] = ()
    action_id: str | None = None
    conversational: bool = True
    order: int = 0
    #: Corps Markdown LITTÉRAL, rendu tel quel entre la description et les exemples.
    #: Nécessaire depuis L3/L4 : expliquer un choix, des états ou une limite demande
    #: plus qu'une phrase et une liste d'exemples. Aucune donnée utilisateur n'y entre.
    body: str = ""
    #: Libellé COURT pour l'aide de niveau 1 (« Poser une question »). Le ``title``
    #: reste le libellé long, employé en tête de section dans les sous-commandes.
    short_title: str = ""
    #: Phrase COURTE pour l'aide de niveau 1. La ``description`` peut faire trois
    #: lignes ; ici, une seule, sinon le niveau 1 redevient le mur qu'on démonte.
    summary: str = ""
    # Réservé au backend — jamais sérialisé ni rendu (cf. Lot 2).
    feature_flag: str | None = field(default=None, repr=False)


#: Analyses globales RÉELLEMENT disponibles — clés du registre d'analyse
#: (``apps.rag_core.analysis.registry``). Un test vérifie qu'elles y sont bien
#: enregistrées : l'aide ne peut donc pas promettre une analyse que le code ne sait
#: pas exécuter.
AVAILABLE_GLOBAL_ANALYSES: tuple[str, ...] = ("corpus.global_search",)

#: Analyses globales annoncées comme NON disponibles, avec leur libellé utilisateur.
#: Un test vérifie qu'AUCUNE de ces clés n'est enregistrée. Le jour où l'une d'elles
#: est implémentée, le test échoue tant qu'elle n'a pas quitté cette liste — c'est ce
#: qui empêche l'aide de rester périmée, dans un sens comme dans l'autre.
UNAVAILABLE_GLOBAL_ANALYSES: tuple[tuple[str, str], ...] = (
    ("corpus.summary", "résumé intégral non thématique de tous les documents"),
    ("corpus.kpi_inventory", "inventaire structuré de KPI"),
    ("corpus.entity_extraction", "extraction globale d'entités"),
    ("corpus.timeline", "chronologie complète"),
    ("corpus.comparison", "comparaison structurée de tous les documents"),
    ("corpus.contradictions", "recherche automatique de contradictions"),
    ("corpus.report", "génération automatique de rapports métier"),
)


def _unavailable_lines() -> str:
    return "\n".join(f"- {label} ;" for _key, label in UNAVAILABLE_GLOBAL_ANALYSES[:-1]) + (
        f"\n- {UNAVAILABLE_GLOBAL_ANALYSES[-1][1]}."
    )


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id="ask_documents",
        category="questions",
        title="Poser des questions sur vos documents",
        description=(
            "Je réponds à partir des documents de l'espace sélectionné et je cite mes sources."
        ),
        examples=(
            "Quels sont les principaux risques du dossier ?",
            "Quelle est la durée du contrat ?",
            "Quels engagements sont mentionnés dans ce document ?",
        ),
        action_id="search.answer",
        order=10,
        short_title="Poser une question",
        summary="je réponds à partir des documents disponibles, en citant mes sources.",
    ),
    Capability(
        id="list_documents",
        category="documents",
        title="Lister les documents",
        description=(
            "Je peux retrouver les documents qui mentionnent un sujet ou correspondent "
            "à un filtre documentaire. Je vous rends alors une **liste de documents**, "
            "et non le détail de ce qu'ils disent."
        ),
        examples=(
            "Liste les documents sur Nordexia.",
            "Quels documents parlent de cybersécurité ?",
            "Quels fichiers mentionnent le projet Atlas ?",
        ),
        action_id="search.list_documents",
        order=20,
        short_title="Lister des documents",
        summary="je retrouve les fichiers liés à un sujet.",
    ),
    Capability(
        id="summarize",
        category="synthese",
        title="Résumer un document ou une page",
        description="Je peux résumer un document précis, ou une page précise.",
        examples=(
            "Résume Rapport.pdf.",
            "Résume la page 3.",
            "Fais une synthèse de ce document.",
        ),
        action_id="document.summarize",
        order=30,
        short_title="Résumer",
        summary="je résume un document précis, ou une page précise.",
    ),
    Capability(
        id="news",
        category="actualites",
        title="Rechercher des actualités",
        description=(
            "Je peux rechercher des informations récentes concernant une entité, "
            "lorsque cette fonction est disponible dans votre environnement."
        ),
        examples=(
            "Donne-moi les actualités récentes sur UniCredit.",
            "Quoi de neuf sur Nordexia ?",
        ),
        action_id="search.news",
        order=40,
        short_title="Rechercher des actualités",
        # Ne jamais promettre une fonction qui peut être absente de l'environnement.
        summary=(
            "je cherche des informations récentes sur une entité, lorsque cette "
            "fonction est disponible."
        ),
    ),
    # -----------------------------------------------------------------------
    # Analyses globales (L3/L4)
    # -----------------------------------------------------------------------
    Capability(
        id="global_search",
        category="corpus",
        title="Analyser l'ensemble du corpus",
        description=(
            "Je détecte les demandes qui portent sur **tous** les documents de l'espace, "
            "et non sur un document en particulier."
        ),
        body=(
            "Pour que je puisse lancer une analyse globale, indiquez les deux à la fois :\n"
            "\n"
            "- que la recherche porte sur l'ensemble du corpus ;\n"
            "- le sujet précis à rechercher.\n"
            "\n"
            "Sans sujet précis, je ne peux pas lancer d'analyse globale : je vous "
            "demanderai de préciser ce que vous cherchez."
        ),
        examples=(
            "Recherche dans toute la base les informations sur la cybersécurité.",
            "Analyse dans tous les documents ce qui concerne le projet Atlas.",
            "Que dit l'ensemble du corpus sur la qualité de service ?",
            "Recense dans tous les documents les informations relatives aux délais.",
            "Search the whole corpus for information about data protection.",
        ),
        action_id="corpus.global_request",
        order=50,
        short_title="Analyser tout le corpus",
        summary="je parcours l’ensemble des documents sur un sujet précis.",
    ),
    Capability(
        id="quick_or_global",
        category="corpus",
        title="Choisir entre réponse rapide et analyse globale",
        description=(
            "Face à une demande portant sur tout le corpus, je vous propose deux modes. "
            "Rien ne se lance sans votre confirmation explicite."
        ),
        body=(
            "### Réponse rapide\n"
            "\n"
            "- s'appuie sur un nombre limité de sources ;\n"
            "- répond plus vite ;\n"
            "- reste **explicitement partielle** ;\n"
            "- ne garantit pas le parcours de tous les documents.\n"
            "\n"
            "### Analyse globale\n"
            "\n"
            "- parcourt les contenus textuels exploitables à l'aide d'un préfiltrage lexical ;\n"
            "- s'exécute en arrière-plan ;\n"
            "- affiche sa progression ;\n"
            "- peut être annulée ;\n"
            "- reprend après un rechargement de la page ;\n"
            "- indique la couverture atteinte et les documents exclus ;\n"
            "- cite chaque élément produit."
        ),
        action_id=None,
        conversational=False,  # explication d'un choix, pas une phrase à taper
        order=55,
    ),
    Capability(
        id="track_analysis",
        category="corpus",
        title="Suivre une analyse globale",
        description="Une analyse globale peut apparaître dans les états suivants :",
        body=(
            "- **En attente** : l'analyse est enregistrée et attend son exécution.\n"
            "- **En cours** : les passages candidats sont recherchés puis traités.\n"
            "- **Terminée** : le résultat sourcé est disponible.\n"
            "- **Partielle** : une partie des passages n'a pas pu être traitée.\n"
            "- **Annulée** : l'analyse a été interrompue à votre demande.\n"
            "- **Échouée** : l'analyse n'a pas pu aboutir.\n"
            "\n"
            "Vous pouvez continuer à utiliser le chat pendant qu'une analyse s'exécute. "
            "Si vous rechargez la page, la carte d'analyse réapparaît dans le fil, avec "
            "son état à jour — aucune analyse n'est relancée en double."
        ),
        action_id=None,
        conversational=False,
        order=60,
    ),
    Capability(
        id="coverage",
        category="corpus",
        title="Comprendre la couverture",
        description=(
            "La couverture décrit ce qui a **réellement** été parcouru et traité. "
            "Je distingue notamment :"
        ),
        body=(
            "- les documents considérés ;\n"
            "- les documents textuellement exploitables ;\n"
            "- les documents candidats ;\n"
            "- les documents traités ;\n"
            "- les documents ayant produit un résultat ;\n"
            "- les extraits textuels parcourus ;\n"
            "- les extraits candidats ;\n"
            "- les extraits réellement traités ;\n"
            "- les documents exclus, et la raison de leur exclusion.\n"
            "\n"
            "Une couverture documentaire complète ne garantit pas que toutes les "
            "formulations sémantiquement équivalentes ont été reconnues."
        ),
        action_id=None,
        conversational=False,
        order=65,
    ),
    Capability(
        id="global_limits",
        category="corpus",
        title="Limites de la recherche globale",
        description="L'analyse globale repose d'abord sur une recherche **lexicale**, sur les mots.",
        body=(
            "Elle peut donc ne pas reconnaître :\n"
            "\n"
            "- certains synonymes ;\n"
            "- certaines paraphrases ;\n"
            "- certaines variantes linguistiques ;\n"
            "- certaines formulations très éloignées des mots recherchés.\n"
            "\n"
            "Une requête trop large peut être refusée, afin d'éviter un traitement "
            "coûteux ou trompeur. Dans ce cas, précisez le sujet ou ajoutez des termes "
            "distinctifs."
        ),
        action_id=None,
        conversational=False,
        order=70,
    ),
    Capability(
        id="global_unavailable",
        category="corpus",
        title="Analyses globales non encore disponibles",
        description=(
            "Les fonctions suivantes **ne sont pas** proposées aujourd'hui comme "
            "analyses globales :"
        ),
        body=(
            _unavailable_lines()
            + "\n\nSi vous formulez une telle demande, je ferai ce que la recherche "
            "globale sait faire : retrouver et citer les passages liés aux mots de votre "
            "demande. Je ne produirai pas pour autant l'analyse structurée correspondante."
        ),
        action_id=None,
        conversational=False,
        order=75,
    ),
    # -----------------------------------------------------------------------
    Capability(
        id="format",
        category="format",
        title="Choisir le format de la réponse",
        description="Je peux structurer la réponse si vous le demandez explicitement.",
        examples=(
            "Présente la réponse sous forme de tableau.",
            "Fais une synthèse en cinq points.",
            "Sépare les constats, les risques et les recommandations.",
        ),
        action_id=None,
        conversational=False,  # préférence de format, pas une action du registre
        order=110,
    ),
    Capability(
        id="spreadsheets",
        category="documents",
        title="Classeurs Excel",
        description=(
            "Les fichiers Excel **`.xlsx`** peuvent être indexés et interrogés comme "
            "des documents. Les réponses citent la **feuille** et la **plage de cellules**."
        ),
        body=(
            "Ce que je sais faire :\n"
            "\n"
            "- retrouver une ligne ou une cellule précise ;\n"
            "- citer le fichier, la **feuille** et les **cellules** utilisées ;\n"
            "- résumer le contenu d'une feuille.\n"
            "\n"
            "Ce que je ne sais **pas encore** garantir :\n"
            "\n"
            "- les **sommes, moyennes, classements, regroupements et comptages** "
            "portant sur **tout un classeur** : je ne vois que les extraits retrouvés, "
            "jamais toutes les lignes. Je préfère vous le dire plutôt que d'additionner "
            "ce que je vois et de présenter le résultat comme sûr ;\n"
            "- le **recalcul des formules** : je lis la dernière valeur enregistrée par "
            "le tableur. Si le classeur n'a pas été recalculé, une formule peut apparaître "
            "vide, et l'ingestion vous en avertit.\n"
            "\n"
            "Formats **non pris en charge** : `.xlsm` (macros), `.xls` (format ancien), "
            "`.ods`. Enregistrez-les en `.xlsx`."
        ),
        action_id=None,
        conversational=False,
        order=35,
    ),
    Capability(
        id="sources",
        category="sources",
        title="Remonter aux sources",
        description="Chaque réponse sourcée indique les extraits utilisés.",
        body=(
            "Vous pouvez :\n"
            "\n"
            "- ouvrir le document d'origine ;\n"
            "- consulter la page concernée ;\n"
            "- vérifier l'extrait cité ;\n"
            "- voir plusieurs sources pour une même affirmation.\n"
            "\n"
            "Dans une analyse globale, tout élément affiché est associé à au moins une "
            "source valide."
        ),
        examples=(),
        action_id=None,
        conversational=False,  # geste d'interface
        order=120,
    ),
)

_TITLE = "Que pouvez-vous faire dans RagATM ?"
_INTRO = (
    "Posez votre question normalement : je réponds à partir des documents de "
    "l'espace sélectionné, et je cite mes sources."
)

_BY_ID: dict[str, Capability] = {cap.id: cap for cap in CAPABILITIES}

# ---------------------------------------------------------------------------
# Aide à deux niveaux (Lot 2)
#
# Le niveau 1 ne montre que les capacités PRINCIPALES : ce que l'utilisateur peut
# taper. Le détail (états, couverture, limites) vit dans les sous-commandes — il
# n'est pas supprimé, il cesse simplement de noyer l'essentiel.
# ---------------------------------------------------------------------------

#: Capacités affichées dans l'aide de niveau 1, dans cet ordre.
HELP_PRIMARY: tuple[str, ...] = (
    "ask_documents",
    "list_documents",
    "summarize",
    "news",
    "global_search",
)

#: Sujet canonique -> capacités détaillées. Volontairement explicite plutôt que
#: dérivé de ``category`` : « limites » prélève 2 des 6 entrées de la catégorie
#: ``corpus``, qu'aucun regroupement automatique ne saurait séparer.
HELP_TOPICS: dict[str, tuple[str, ...]] = {
    "analyse": (
        "global_search",
        "quick_or_global",
        "track_analysis",
        "coverage",
    ),
    "documents": (
        "ask_documents",
        "list_documents",
        "summarize",
        "spreadsheets",
    ),
    "sources": (
        "sources",
        "format",
    ),
    "limites": (
        "global_limits",
        "spreadsheets",
        "global_unavailable",
    ),
}

#: Alias -> sujet canonique. Liste COURTE et fermée : chaque entrée est un mot
#: qu'un utilisateur tape spontanément, pas une tentative de couvrir la langue.
HELP_TOPIC_ALIASES: dict[str, str] = {
    "analyse": "analyse",
    "analyses": "analyse",
    "analysis": "analyse",
    "documents": "documents",
    "document": "documents",
    "docs": "documents",
    "sources": "sources",
    "source": "sources",
    "limites": "limites",
    "limite": "limites",
    "limits": "limites",
}

#: Titre de la réponse d'une sous-commande.
_TOPIC_TITLES: dict[str, str] = {
    "analyse": "Analyser l'ensemble du corpus",
    "documents": "Interroger vos documents",
    "sources": "Sources et format des réponses",
    "limites": "Ce que je ne sais pas (encore) faire",
}

#: Renvoi final d'une sous-commande. L'analyse renvoie vers les limites : c'est là
#: que vit l'honnêteté fonctionnelle, et elle ne doit pas se perdre en route.
_TOPIC_FOOTERS: dict[str, str] = {
    "analyse": (
        "Pour les limites de la recherche globale et les analyses qui ne sont pas "
        "encore proposées : `/help limites`."
    ),
    "documents": "Pour revenir à la vue d'ensemble : `/help`.",
    "sources": "Pour revenir à la vue d'ensemble : `/help`.",
    "limites": "Pour revenir à la vue d'ensemble : `/help`.",
}

#: Libellé de chaque sous-commande dans le bloc « Aller plus loin ».
_TOPIC_TEASERS: tuple[tuple[str, str], ...] = (
    ("analyse", "l'analyse globale : modes, progression, couverture"),
    ("documents", "questions, recherche et résumés"),
    ("sources", "citations et formats de réponse"),
    ("limites", "ce que je ne sais pas (encore) faire"),
)


def resolve_help_topic(topic: str | None) -> str | None:
    """Sujet brut -> sujet canonique. Un sujet inconnu vaut « pas de sujet ».

    C'est ici, et nulle part ailleurs, que se décide ce qu'est un sujet valide :
    le matcher se contente de transporter le mot tapé.
    """
    if not topic:
        return None
    return HELP_TOPIC_ALIASES.get(topic.strip().lower())


def _render_capability(cap: Capability) -> list[str]:
    lines = [f"## {cap.title}", cap.description]
    if cap.body:
        lines += ["", cap.body]
    if cap.examples:
        lines += ["", "Exemples :", ""]
        lines += [f"- `{example}`" for example in cap.examples]
    lines.append("")
    return lines


def _render_overview() -> list[str]:
    """Aide de niveau 1. Courte par construction : cinq capacités, un exemple chacune.

    Aucun tableau : le rendu impose aux tableaux une largeur minimale de 640 px,
    ce qui forcerait un défilement horizontal sur téléphone.
    """
    lines = [f"# {_TITLE}", "", _INTRO, ""]

    for cap_id in HELP_PRIMARY:
        cap = _BY_ID[cap_id]
        # `\` en fin de ligne = saut de ligne dur en GFM : l'exemple tombe sous son
        # libellé, sans tableau et sans HTML.
        lines.append(f"- **{cap.short_title}** — {cap.summary}\\")
        lines.append(f"  `{cap.examples[0]}`")

    lines += ["", "## Aller plus loin", ""]
    lines += [f"- `/help {topic}` — {teaser}" for topic, teaser in _TOPIC_TEASERS]
    return lines


def _render_topic(topic: str) -> list[str]:
    lines = [f"# {_TOPIC_TITLES[topic]}", ""]
    for cap_id in HELP_TOPICS[topic]:
        lines += _render_capability(_BY_ID[cap_id])
    lines.append(_TOPIC_FOOTERS[topic])
    return lines


def render_help_markdown(topic: str | None = None) -> str:
    """Rend l'aide en Markdown GFM. Pure, déterministe, sans donnée utilisateur.

    ``topic`` absent, vide ou INCONNU -> aide de niveau 1. Un sujet que l'on ne
    reconnaît pas n'est jamais une erreur : c'est quelqu'un qui cherche de l'aide.

    Aucune interpolation de contenu utilisateur : le Markdown est intégralement
    littéral, donc non injectable.
    """
    canonical = resolve_help_topic(topic)
    lines = _render_overview() if canonical is None else _render_topic(canonical)
    return "\n".join(lines)

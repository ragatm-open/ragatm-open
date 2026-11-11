"""Lot 1 — détection déterministe de l'intention d'aide (T1, T2, T3, T14, T15)."""
from __future__ import annotations

import pytest

from apps.actions.matchers import HelpMatcher
from apps.actions.normalize import normalize
from apps.actions.registry import ACTION_REGISTRY, allowed_actions_for_llm
from apps.actions.router import ActionRouter
from apps.help.catalog import (
    AVAILABLE_GLOBAL_ANALYSES,
    CAPABILITIES,
    HELP_PRIMARY,
    HELP_TOPIC_ALIASES,
    HELP_TOPICS,
    UNAVAILABLE_GLOBAL_ANALYSES,
    render_help_markdown,
)

from .fixtures_help import (
    HELP_NEGATIVES,
    HELP_NEGATIVES_EXPECTED_ACTION,
    HELP_POSITIVES,
)

#: Les cinq sorties possibles de l'aide. Plusieurs invariants (aucune fuite
#: technique, aucune promesse d'exhaustivité, aucun tableau) doivent tenir sur
#: TOUTES, pas seulement sur celle qu'on regarde.
_ALL_TOPICS = (None, "analyse", "documents", "sources", "limites")

_MATCHER = HelpMatcher()


def _match(text: str):
    return _MATCHER.match(norm=normalize(text), original=text)


# ---------------------------------------------------------------------------
# T1 — positifs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", HELP_POSITIVES)
def test_help_matcher_positives(question: str):
    res = _match(question)
    assert res is not None, f"non détecté : {question!r}"
    assert res.action == "help.capabilities"
    assert res.detection_method in ("explicit_command", "rule")


def test_exact_commands_are_flagged_as_explicit_command():
    assert _match("/aide").detection_method == "explicit_command"
    assert _match("?").detection_method == "explicit_command"
    # Tolérance à la ponctuation terminale, sans regex globale sur « ? ».
    assert _match("aide ?").detection_method == "explicit_command"


def test_natural_phrasings_are_flagged_as_rule():
    assert _match("que puis-je faire ?").detection_method == "rule"


# ---------------------------------------------------------------------------
# T2 — négatifs (aucun faux positif)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", HELP_NEGATIVES)
def test_help_matcher_negatives(question: str):
    assert _match(question) is None, f"faux positif : {question!r}"


def test_bare_question_mark_never_matches_a_real_question():
    """« ? » seul est une commande ; un « ? » DANS une question ne l'est pas."""
    assert _match("?") is not None
    assert _match("quelle est la durée du contrat ?") is None


# ---------------------------------------------------------------------------
# T3 — priorité du routeur : l'aide gagne, sans casser l'existant
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", HELP_POSITIVES)
def test_router_routes_help(question: str):
    plan = ActionRouter().route(question)
    assert plan.action == "help.capabilities", f"{question!r} -> {plan.action}"


@pytest.mark.parametrize("question,expected", HELP_NEGATIVES_EXPECTED_ACTION)
def test_router_preserves_existing_intents(question: str, expected: str):
    plan = ActionRouter().route(question)
    assert plan.action == expected, f"{question!r} -> {plan.action} (attendu {expected})"


@pytest.mark.parametrize("question", HELP_NEGATIVES)
def test_router_never_routes_negatives_to_help(question: str):
    assert ActionRouter().route(question).action != "help.capabilities"


# ---------------------------------------------------------------------------
# Registre
# ---------------------------------------------------------------------------
def test_help_action_registered_read_only():
    d = ACTION_REGISTRY["help.capabilities"]
    assert d.destructive is False
    assert d.natural_language_enabled is True
    assert d.feature_flag is None
    assert d.permission.level == "read"


def test_help_is_never_proposed_to_the_llm_classifier():
    """L'aide est 100 % déterministe : elle ne doit pas entrer dans la liste fermée."""
    assert "help.capabilities" not in allowed_actions_for_llm()
    assert ACTION_REGISTRY["help.capabilities"].llm_proposable is False


# ---------------------------------------------------------------------------
# T14 — ancrage du catalogue sur le registre (anti-divergence)
# ---------------------------------------------------------------------------
def test_catalog_entries_are_anchored_on_the_registry():
    for cap in CAPABILITIES:
        if cap.action_id is None:
            # Geste d'interface : doit être explicitement marqué non conversationnel.
            assert cap.conversational is False, cap.id
            continue
        assert cap.action_id in ACTION_REGISTRY, cap.action_id
        defn = ACTION_REGISTRY[cap.action_id]
        assert defn.natural_language_enabled is True, cap.action_id
        assert defn.destructive is False, cap.action_id


def test_catalog_never_promises_a_mutation_by_phrase():
    mutations = {
        name for name, d in ACTION_REGISTRY.items() if not d.natural_language_enabled
    }
    assert {c.action_id for c in CAPABILITIES}.isdisjoint(mutations)


def test_catalog_ids_are_unique():
    ids = [c.id for c in CAPABILITIES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# T15 — aucune fuite technique dans le Markdown rendu
# ---------------------------------------------------------------------------
_FORBIDDEN = (
    "ACTIONS_ROUTER_ENABLED",
    "RAG_REPHRASING_SUGGESTIONS_ENABLED",
    "DOCUMENT_OCR_ENABLED",
    "/api/",
    "handler_key",
    "audit_event",
    "AI_GATEWAY",
    "OLLAMA",
)


@pytest.mark.parametrize("topic", _ALL_TOPICS)
def test_markdown_exposes_no_technical_name(topic):
    """Vrai pour TOUTES les sorties, pas seulement le niveau 1."""
    upper = render_help_markdown(topic).upper()
    for needle in _FORBIDDEN:
        assert needle.upper() not in upper, needle


@pytest.mark.parametrize("topic", _ALL_TOPICS)
def test_markdown_is_deterministic_and_non_empty(topic):
    assert render_help_markdown(topic) == render_help_markdown(topic)
    md = render_help_markdown(topic)
    assert md.startswith("# ")
    assert len(md.strip()) > 200


@pytest.mark.parametrize("topic", _ALL_TOPICS)
def test_no_markdown_table_anywhere(topic):
    """Le rendu impose 640 px de large aux tableaux : sur mobile, ils défileraient
    horizontalement. L'aide ne doit donc en contenir aucun."""
    assert "|---" not in render_help_markdown(topic).replace(" ", "")


def test_markdown_does_not_announce_absent_capabilities():
    """Chronologie / comparaison / rapports n'existent pas dans le code.

    Ils sont NOMMÉS dans l'aide — mais uniquement pour dire qu'ils ne sont pas
    disponibles. Le test est désormais plus strict qu'avant : ces mots ne doivent
    apparaître dans AUCUNE autre sortie que la section « non encore disponibles »
    de `/help limites`.
    """
    limites = render_help_markdown("limites")
    section = limites.split("## Analyses globales non encore disponibles", 1)
    assert len(section) == 2, "la section des capacités indisponibles a disparu"
    unavailable_block = section[1].lower()

    ailleurs = " ".join(
        render_help_markdown(t) for t in (None, "analyse", "documents", "sources")
    ).lower()
    ailleurs += section[0].lower()

    for absent in ("chronologie", "comparaison structurée", "rapports métier", "kpi"):
        assert absent in unavailable_block, absent
        assert absent not in ailleurs, f"{absent} est annoncé hors de la section 'non disponibles'"


# ---------------------------------------------------------------------------
# HELP2..HELP6 — l'aide reflète les capacités RÉELLEMENT livrées (L3/L4)
#
# Le détail a quitté le niveau 1 pour les sous-commandes : ces tests sont reciblés,
# pas affaiblis. Rien de ce qui était garanti ne cesse de l'être.
# ---------------------------------------------------------------------------
def test_topic_analyse_documents_the_global_analysis():  # HELP2
    md = render_help_markdown("analyse")
    for section in (
        "## Analyser l'ensemble du corpus",
        "## Choisir entre réponse rapide et analyse globale",
        "### Réponse rapide",
        "### Analyse globale",
        "## Suivre une analyse globale",
        "## Comprendre la couverture",
    ):
        assert section in md, section
    # …et renvoie vers les limites, qui vivent désormais ailleurs.
    assert "`/help limites`" in md


def test_topic_limites_carries_the_limits_and_the_unavailable_analyses():  # HELP2
    md = render_help_markdown("limites")
    assert "## Limites de la recherche globale" in md
    assert "## Analyses globales non encore disponibles" in md


def test_topic_analyse_explains_progress_cancel_resume_and_citations():  # HELP2 (suite)
    md = render_help_markdown("analyse").lower()
    assert "affiche sa progression" in md
    assert "peut être annulée" in md
    assert "rechargement" in md
    assert "cite chaque élément produit" in md
    # Les six états d'une analyse.
    for state in ("en attente", "en cours", "terminée", "partielle", "annulée", "échouée"):
        assert f"**{state}**" in md, state


def test_topic_analyse_carries_l4_compatible_examples():  # HELP3
    md = render_help_markdown("analyse")
    assert "Recherche dans toute la base les informations sur" in md
    assert "Que dit l'ensemble du corpus sur" in md


_MISLEADING = (
    "exhaustif",
    "exhaustive",
    "exhaustivité garantie",
    "toutes les informations ont été trouvées",
    "toutes les formulations ont été reconnues",
    "aucune information n'a été manquée",
)


@pytest.mark.parametrize("topic", _ALL_TOPICS)
def test_markdown_never_promises_exhaustiveness(topic):  # HELP4
    md = render_help_markdown(topic).lower()
    for needle in _MISLEADING:
        assert needle not in md, needle


def test_topic_analyse_says_explicitly_the_opposite():  # HELP4 (suite)
    assert "ne garantit pas que toutes les formulations" in render_help_markdown("analyse")


# ---------------------------------------------------------------------------
# Lot 2 — aide à deux niveaux
# ---------------------------------------------------------------------------
def test_overview_is_short_and_scannable():
    md = render_help_markdown()
    assert len(md) <= 1500, f"aide de niveau 1 trop longue : {len(md)} caractères"
    assert len(md.splitlines()) <= 25
    # Une seule section secondaire : « Aller plus loin ».
    assert md.count("\n## ") == 1


def test_overview_lists_the_five_capabilities_with_one_example_each():
    md = render_help_markdown()
    for cap_id in HELP_PRIMARY:
        cap = next(c for c in CAPABILITIES if c.id == cap_id)
        assert f"**{cap.short_title}**" in md, cap_id
        assert f"`{cap.examples[0]}`" in md, cap_id


def test_overview_advertises_every_subcommand():
    """Une sous-commande que personne ne peut deviner ne sert à rien : le niveau 1
    EST le menu."""
    md = render_help_markdown()
    for topic in HELP_TOPICS:
        assert f"`/help {topic}`" in md, topic


def test_overview_carries_no_detail():
    """Le niveau 1 ne doit contenir ni les états, ni la couverture, ni les limites."""
    md = render_help_markdown().lower()
    for detail in (
        "en file d'attente",
        "comprendre la couverture",
        "documents exclus",
        "synonymes",
        "paraphrases",
        "inventaire structuré de kpi",
        "**en attente**",
    ):
        assert detail not in md, detail


@pytest.mark.parametrize("alias,canonical", sorted(HELP_TOPIC_ALIASES.items()))
def test_every_alias_resolves_to_its_topic(alias, canonical):
    assert render_help_markdown(alias) == render_help_markdown(canonical)


@pytest.mark.parametrize("unknown", ["bidule", "", "   ", "analyse globale", "ANALYSEZ"])
def test_unknown_topic_falls_back_to_the_overview(unknown):
    """Un sujet inconnu n'est jamais une erreur : c'est quelqu'un qui cherche de l'aide."""
    assert render_help_markdown(unknown) == render_help_markdown()


def test_topic_is_case_insensitive():
    assert render_help_markdown("ANALYSE") == render_help_markdown("analyse")


def test_no_capability_is_orphaned():
    """Aucune capacité ne doit disparaître de l'aide en changeant de niveau.

    L'union « capacités du niveau 1 » + « capacités des sous-commandes » doit couvrir
    la totalité du catalogue. C'est ce test qui garantit que restructurer n'a rien
    supprimé.
    """
    reachable = set(HELP_PRIMARY)
    for ids in HELP_TOPICS.values():
        reachable |= set(ids)

    everything = {c.id for c in CAPABILITIES}
    assert everything - reachable == set(), "capacités devenues inatteignables"


def test_every_topic_id_exists_in_the_catalog():
    known = {c.id for c in CAPABILITIES}
    for topic, ids in HELP_TOPICS.items():
        for cap_id in ids:
            assert cap_id in known, f"{topic} -> {cap_id} inconnu du catalogue"
    for cap_id in HELP_PRIMARY:
        assert cap_id in known, cap_id


def test_unavailable_analyses_are_really_absent_from_the_registry():  # HELP5 / HELP6
    """L'aide ne peut pas mentir sur les capacités, dans un sens comme dans l'autre.

    Le jour où l'un de ces types est implémenté et enregistré, ce test échoue tant
    qu'il n'a pas quitté la liste des indisponibles.
    """
    from apps.rag_core.analysis.registry import is_known_analysis_type

    for key, _label in UNAVAILABLE_GLOBAL_ANALYSES:
        assert not is_known_analysis_type(key), f"{key} est enregistré mais annoncé indisponible"


def test_available_analyses_are_really_registered():  # HELP6
    from apps.rag_core.analysis.registry import GLOBAL_SEARCH, is_known_analysis_type

    assert AVAILABLE_GLOBAL_ANALYSES == (GLOBAL_SEARCH,)
    for key in AVAILABLE_GLOBAL_ANALYSES:
        assert is_known_analysis_type(key), key


# ---------------------------------------------------------------------------
# Lot 2 — sous-commandes : reconnues UNIQUEMENT derrière une forme slashée
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,topic",
    [
        ("/help analyse", "analyse"),
        ("/help analysis", "analysis"),
        ("/help documents", "documents"),
        ("/help docs", "docs"),
        ("/help sources", "sources"),
        ("/help source", "source"),
        ("/help limites", "limites"),
        ("/help limits", "limits"),
        ("/aide analyse", "analyse"),
        ("/aide limites", "limites"),
        ("/help Analyse", "analyse"),      # normalisation : casse
        ("/help analyse ?", "analyse"),    # ponctuation terminale tolérée
        ("/help bidule", "bidule"),        # sujet inconnu : transporté quand même
    ],
)
def test_slash_subcommands_carry_the_topic(text, topic):
    m = _match(text)
    assert m is not None, text
    assert m.action == "help.capabilities"
    assert m.detection_method == "explicit_command"
    assert m.parameters == {"topic": topic}


@pytest.mark.parametrize("text", ["/help", "/aide", "/?", "?", "aide", "help", "/exemples"])
def test_historic_commands_still_answer_without_a_topic(text):
    m = _match(text)
    assert m is not None, text
    assert m.action == "help.capabilities"
    assert m.parameters == {}, "aucun sujet ne doit être inventé"


@pytest.mark.parametrize(
    "text",
    [
        # Le sujet ne se lit QUE derrière une barre oblique. Sans elle, ces phrases
        # restent des demandes potentiellement documentaires.
        "aide résumé",
        "help analyse",
        "analyse",
        "analyse le contrat",
        "aide-moi à résumer ce document",
        "liste les documents sur Nordexia",
        "résume la page 3",
        "documents",
        "sources",
    ],
)
def test_bare_words_are_never_read_as_subcommands(text):
    m = _match(text)
    if m is not None:
        # « aide » seul reste l'aide historique — mais jamais avec un sujet.
        assert m.parameters == {}, f"{text!r} a été lu comme une sous-commande"


@pytest.mark.parametrize(
    "text",
    ["/help analyse", "/help bidule", "/aide limites", "/help documents"],
)
def test_slash_help_never_reaches_the_rag(text):
    """Preuve : une forme slashée est TOUJOURS de l'aide, jamais une question.

    Le routeur complet est interrogé — c'est lui qui déciderait d'un `search.answer`.
    """
    assert ActionRouter().route(text).action == "help.capabilities"


def test_slash_help_is_immune_to_the_document_guard():
    """« /help » l'emporte sur le garde-fou documentaire : sans cela, un sujet inconnu
    formulé comme une phrase documentaire retomberait dans le RAG."""
    m = _match("/help résumer ce document")
    assert m is not None and m.action == "help.capabilities"


def test_natural_document_requests_survive_untouched():
    """La non-régression qui compte le plus : le Lot 2 n'a pas élargi le matcher."""
    assert ActionRouter().route("aide-moi à résumer ce document").action == "document.summarize"
    assert ActionRouter().route("liste les documents sur Nordexia").action == "search.list_documents"

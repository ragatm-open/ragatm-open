"""Clarification « réponse rapide / analyse complète » — rendus déterministes.

Module PUR : aucune dépendance Django, aucun accès base, aucun appel IA. Toute
sortie est une fonction du seul état passé en argument.

Règles éditoriales, verrouillées par test (docs/coverage-semantics.md) :

- une réponse rapide est TOUJOURS déclarée partielle ;
- l'analyse complète n'existe pas encore : on le dit, on ne le laisse pas croire ;
- aucune promesse d'exhaustivité, sémantique ou métier ;
- aucun nom de fichier, de provider, de modèle, de flag ni d'endpoint.
"""
from __future__ import annotations

from apps.rag_core.domain.coverage import (
    Coverage,
    CoverageExclusion,
    CoverageScope,
    compute_coverage_ratio,
)

# Avertissement déterministe préfixé à toute réponse rapide. Injecté par le
# backend APRÈS génération : le LLM ne peut pas le supprimer, même s'il affirme
# dans son texte avoir « analysé tous les documents ».
PARTIAL_COVERAGE_WARNING = (
    "> **Couverture partielle**\n"
    ">\n"
    "> Cette réponse est fondée sur les extraits les plus pertinents et ne constitue "
    "pas un parcours complet du corpus."
)

PARTIAL_COVERAGE_WARNING_EN = (
    "> **Partial coverage**\n"
    ">\n"
    "> This answer is based on the most relevant excerpts and is not a complete "
    "walkthrough of the corpus."
)


def localized_answer_prefix(prefix: str, answer_language: str | None) -> str:
    """Traduit un préfixe déterministe connu vers la langue de réponse résolue.

    Le préfixe est choisi par la couche HTTP, qui ne connaît PAS encore la langue
    résolue (elle dépend du fil et de la préférence, arbitrés dans le service). Plutôt
    que de dupliquer la résolution dans la vue, on traduit ici, au moment où la cible
    est connue. L'ensemble des préfixes possibles est fermé : tout texte inconnu
    ressort inchangé.
    """
    if answer_language == "en" and (prefix or "").strip() == PARTIAL_COVERAGE_WARNING:
        return PARTIAL_COVERAGE_WARNING_EN
    return prefix


def _fr_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def prepend_partial_coverage_warning(answer: str) -> str:
    """Encadre une réponse rapide de son avertissement de couverture.

    Idempotent : ré-appliquer la fonction n'empile pas deux avertissements.
    """
    text = answer or ""
    if text.lstrip().startswith(PARTIAL_COVERAGE_WARNING.split("\n", 1)[0]):
        return text
    return f"{PARTIAL_COVERAGE_WARNING}\n\n{text}"


def render_analysis_launched_markdown(*, statistics: dict, coverage: Coverage) -> str:
    """Accusé de lancement d'une analyse globale. Aucune durée n'est promise :
    elle n'est pas mesurée."""
    content = statistics.get("content") or {}
    lines = [
        "## Analyse globale lancée",
        "",
        "L’analyse parcourt actuellement les contenus textuels exploitables du "
        "périmètre.",
        "",
        f"- **Documents considérés :** {_fr_int(int(coverage.documents_considered))}",
        f"- **Documents actuellement exploitables :** {_fr_int(int(coverage.documents_analyzed))}",
        f"- **Extraits textuels :** {_fr_int(int(content.get('chunks_total') or 0))}",
        "",
        "Vous pouvez continuer à utiliser le chat pendant son exécution.",
        "",
        "La recherche lexicale peut ne pas reconnaître toutes les formulations "
        "équivalentes du sujet.",
    ]
    return "\n".join(lines)


def render_corpus_mode_choice_markdown(
    *, statistics: dict, coverage: Coverage, global_search_available: bool = False
) -> str:
    """Message de clarification. Chiffres issus des statistiques RÉELLES du RagSpace.

    Ne parle que de compteurs agrégés : aucun nom de fichier, aucun extrait.
    """
    content = statistics.get("content") or {}

    considered = int(coverage.documents_considered)
    exploitable = int(coverage.documents_analyzed)
    excluded = max(0, considered - exploitable)
    chunks = int(content.get("chunks_total") or 0)
    pages = int(content.get("pages_numbered_total") or 0)

    lines = ["## Cette demande porte sur l’ensemble du corpus", ""]

    if considered == 0:
        lines += [
            "Cet espace ne contient aucun document pour le moment.",
            "",
            "Ajoutez des documents, puis reformulez votre demande.",
        ]
        return "\n".join(lines)

    lines += ["L’espace contient actuellement :", ""]
    lines.append(f"- **{_fr_int(considered)} documents considérés**")
    lines.append(f"- **{_fr_int(exploitable)} documents exploitables**")
    if excluded > 0:
        lines.append(f"- **{_fr_int(excluded)} documents non analysables**")
    lines.append(f"- **{_fr_int(chunks)} extraits textuels disponibles**")
    if pages > 0:
        lines.append(f"- **{_fr_int(pages)} pages numérotées**")

    lines += [
        "",
        "Choisissez un mode :",
        "",
        "### Réponse rapide",
        "",
        "Je répondrai à partir des extraits les plus pertinents.",
        "",
        "- résultat en quelques secondes ;",
        "- couverture partielle ;",
        "- ne garantit pas le parcours de tous les documents.",
        "",
        "### Analyse complète",
        "",
    ]

    if global_search_available:
        lines += [
            "Une analyse globale peut être lancée. Elle interrogera tous les "
            "contenus textuels exploitables avec une couverture mesurée.",
            "",
            "- traitement plus long ;",
            "- résultat persistant ;",
            "- progression et périmètre affichés.",
            "",
            # Un préfiltre lexical n'est pas une garantie sémantique. On le dit.
            "La recherche lexicale peut ne pas reconnaître toutes les formulations "
            "équivalentes du sujet.",
        ]
    else:
        # Sans sujet identifiable, le moteur n'a rien à chercher : on ne propose
        # pas un bouton qui ne mènerait nulle part.
        lines += [
            "Je parcourrais le périmètre documentaire avec une couverture mesurée.",
            "",
            "**Cette forme d’analyse complète n’est pas encore disponible.**",
            "",
            "Pour lancer une analyse globale, précisez le sujet à rechercher dans "
            "l’ensemble des documents.",
        ]

    return "\n".join(lines)


def render_full_analysis_unavailable_markdown() -> str:
    """Réponse déterministe au choix « analyse complète ». Aucun traitement lancé."""
    return "\n".join([
        "## Analyse complète",
        "",
        "Cette fonctionnalité n’est pas encore disponible dans cette version.",
        "",
        "La base de couverture est prête, mais le moteur d’analyse asynchrone n’est "
        "pas encore activé.",
        "",
        "Vous pouvez choisir `réponse rapide` pour obtenir immédiatement une réponse "
        "fondée sur les extraits les plus pertinents.",
    ])


def build_quick_answer_coverage(
    *,
    rag_space_id,
    generated_at,
    corpus_fingerprint: str,
    documents_total: int,
    documents_considered: int,
    sources: list,
) -> Coverage:
    """Couverture RÉELLE d'une réponse rapide, fondée sur les sources retenues.

    Point non négociable : ``documents_analyzed`` est le nombre de documents
    **effectivement présents dans les sources envoyées au LLM** — jamais le nombre
    de documents READY de l'espace.

    « READY avec extraits » ≠ « analysé pour cette requête ». Réutiliser la
    couverture de ``/stats/`` ici reviendrait à présenter un top-k comme un
    parcours du corpus : c'est exactement l'erreur que ce lot supprime.
    """
    doc_ids = set()
    for s in sources or []:
        did = None
        if isinstance(s, dict):
            did = s.get("document_id") or (s.get("source") or {}).get("document_id")
        if did:
            doc_ids.add(str(did))

    analyzed = len(doc_ids)
    chunks = len(sources or [])
    not_analyzed = max(0, documents_considered - analyzed)

    exclusions: tuple[CoverageExclusion, ...] = ()
    if not_analyzed > 0:
        # Ces documents ne sont pas « en échec » : ils n'ont simplement pas été
        # retenus par le retrieval. C'est une LIMITE de traitement, pas un défaut
        # du corpus — d'où `limit_applied` et non `no_text_content`.
        exclusions = (CoverageExclusion(reason="limit_applied", count=not_analyzed),)

    return Coverage(
        scope=CoverageScope(
            rag_space_id=str(rag_space_id),
            generated_at=generated_at,
            corpus_fingerprint=corpus_fingerprint,
            filters_applied=(),
        ),
        documents_total=documents_total,
        documents_considered=documents_considered,
        documents_analyzed=analyzed,
        documents_with_results=analyzed,
        documents_excluded=exclusions,
        pages_analyzed=0,  # non mesurable depuis les sources retenues
        chunks_analyzed=chunks,
        items_extracted=0,
        items_without_source=0,
        coverage_level="B" if chunks > 0 else "A",
        # Une réponse rapide n'est JAMAIS complète. Même si le retrieval avait
        # ramené un extrait de chaque document, il n'aurait pas lu tout leur texte.
        coverage_complete=False,
        coverage_ratio=compute_coverage_ratio(
            documents_analyzed=analyzed, documents_considered=documents_considered
        ),
        limits_applied=("top_k",),
    )

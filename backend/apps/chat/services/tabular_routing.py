"""Routage d'une question analytique vers le moteur tabulaire.

Ce module est la charnière entre la garde historique et le nouveau moteur. Sa règle
tient en une phrase : **une question analytique tabulaire ne retombe JAMAIS vers le RAG
vectoriel.** Elle est soit calculée par le moteur, soit refusée explicitement.

    garde armée + moteur activé + question traduisible → moteur tabulaire
    garde armée + moteur activé + question non traduisible → refus existant
    garde armée + moteur désactivé → refus existant (comportement historique)

Le troisième cas est le défaut : `TABULAR_QUERY_ENABLED=0`. Tant que le flag est à 0,
ce module est inerte et le comportement du produit est strictement inchangé.

Ce qu'on ne fait pas ici, délibérément : rattraper une erreur du moteur en répondant
depuis les extraits. Un `empty_selection` ou un `formula_result_unavailable` sont des
réponses, pas des échecs à contourner — les convertir en réponse vectorielle
reproduirait exactement le défaut que la garde existe pour empêcher.
"""
from __future__ import annotations

import logging

from apps.rag_core.tabular import config as CFG
from apps.rag_core.tabular import errors as E

logger = logging.getLogger(__name__)

ANSWER_KIND_TABULAR = "tabular"


def try_tabular_answer(*, user, rag_space_id, question: str, answer_language: str | None = None) -> dict | None:
    """Tente une réponse tabulaire. `None` = « le refus existant s'applique ».

    Ne lève jamais : le routage ne doit pas pouvoir casser le chat. Un échec du moteur
    rend la main à la garde, qui refuse — la dégradation va toujours vers le refus,
    jamais vers une réponse approximative.
    """
    if not CFG.query_enabled():
        return None

    from apps.rag_core.tabular import service as TSVC

    try:
        result = TSVC.answer_question(
            user=user, rag_space_id=rag_space_id, question=question)
    except E.TabularClarificationRequired as exc:
        # Une clarification EST une réponse utile : elle nomme les candidats au lieu
        # de laisser l'utilisateur face à un refus opaque.
        return {
            "answer": _clarification_text(exc, answer_language),
            "answer_kind": ANSWER_KIND_TABULAR,
            "tabular": exc.to_dict(),
            "status": "ok",
        }
    except E.TabularError as exc:
        logger.info("tabular routing: refus %s — repli sur la garde", exc.code)
        return None
    except Exception:  # pragma: no cover - le routage ne casse jamais le chat
        logger.exception("tabular routing: échec inattendu — repli sur la garde")
        return None

    return {
        "answer": _narrate(result, answer_language),
        "answer_kind": ANSWER_KIND_TABULAR,
        "tabular": result.to_dict(),
        "status": "ok",
    }


def _clarification_text(exc: E.TabularClarificationRequired, answer_language: str | None = None) -> str:
    if answer_language == "en":
        default = "A clarification is needed to answer."
    else:
        default = "Une précision est nécessaire pour répondre."
    lignes = [exc.question or default]
    if exc.candidates:
        lignes.append("")
        lignes.extend(f"- {c}" for c in exc.candidates)
    return "\n".join(lignes)


def _narrate(result, answer_language: str | None = None) -> str:
    """Phrase de restitution DÉTERMINISTE, construite depuis le résultat calculé.

    Aucun LLM ici, même quand `TABULAR_NARRATION_ENABLED=1` : la narration LLM
    éventuelle intervient en aval et ne peut que reformuler ce texte. Le chiffre
    affiché vient du moteur, il n'est ni recalculé ni réécrit.

    `answer_language == "en"` produit la variante anglaise ; tout autre cas (fr /
    None / flag OFF) conserve le texte français À L'OCTET PRÈS. Les valeurs, unités,
    noms de feuille/fichier (données) ne sont jamais traduits.
    """
    ev = result.evidence
    cible = ev.get("target_column") or ""
    unite = f" {result.unit}" if result.unit else ""
    portee = f"{ev.get('sheet_name', '')} ({ev.get('data_range', '')})"

    if answer_language == "en":
        if result.value is not None:
            mesure = f" of “{cible}”" if cible else ""
            phrase = f"{_libelle(result.operation, 'en')}{mesure}: **{result.value}{unite}**"
        else:
            phrase = f"{_libelle(result.operation, 'en')}: {result.row_count} row(s)"
        detail = (f"Computed by RagATM over {result.row_count} row(s) of {portee}, "
                  f"from the values of file “{ev.get('filename', '')}”.")
        return f"{phrase}\n\n{detail}"

    if result.value is not None:
        mesure = f" de « {cible} »" if cible else ""
        phrase = (f"{_libelle(result.operation)}{mesure} : "
                  f"**{result.value}{unite}**")
    else:
        phrase = f"{_libelle(result.operation)} : {result.row_count} ligne(s)"

    detail = (f"Calculé par RagATM sur {result.row_count} ligne(s) de {portee}, "
              f"à partir des valeurs du fichier « {ev.get('filename', '')} ».")
    return f"{phrase}\n\n{detail}"


_LIBELLES = {
    "sum": "Somme",
    "average": "Moyenne",
    "count_rows": "Nombre de lignes",
    "count_distinct": "Nombre de valeurs distinctes",
    "minimum": "Minimum",
    "maximum": "Maximum",
    "argmin": "Ligne de valeur minimale",
    "argmax": "Ligne de valeur maximale",
    "top_n": "Premières lignes",
    "bottom_n": "Dernières lignes",
    "filter_rows": "Lignes retenues",
}

_LIBELLES_EN = {
    "sum": "Sum",
    "average": "Average",
    "count_rows": "Row count",
    "count_distinct": "Distinct value count",
    "minimum": "Minimum",
    "maximum": "Maximum",
    "argmin": "Row of minimum value",
    "argmax": "Row of maximum value",
    "top_n": "Top rows",
    "bottom_n": "Bottom rows",
    "filter_rows": "Matching rows",
}


def _libelle(operation: str, lang: str | None = None) -> str:
    table = _LIBELLES_EN if lang == "en" else _LIBELLES
    return table.get(operation, operation)

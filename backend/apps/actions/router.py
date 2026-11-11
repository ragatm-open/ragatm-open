# backend/apps/actions/router.py
"""Routeur central d'intentions — remplace l'ordre implicite dispersé (vue/service).

Priorité déterministe EXPLICITE (audit §13), reproduisant le comportement actuel
observable pour garantir la parité lors du futur câblage :

    1. document.summarize   (aujourd'hui : pré-détection frontend)
    2. search.list_documents(aujourd'hui : regex dans la vue)
    3. search.news          (aujourd'hui : garde regex dans le service)
    4. search.answer        (défaut RAG)

Collisions documentées et TESTÉES :
- listing + news (« Liste les dernières actualités sur Nordexia ») -> **listing gagne**
  (préserve le comportement actuel où la vue tranche avant le service). Un futur
  lot pourra basculer vers une clarification après calibration.

Garde-fous (audit §12) : si le texte est explicatif / nié / cité, toute action
non-fallback est rétrogradée vers ``search.answer`` (jamais d'action arbitraire).

La classification LLM (§14) est un *seam* injecté (``llm_classifier``), **désactivé
par défaut** : ce lot reste 100 % déterministe. Le LLM ne pourra que PROPOSER une
intention, jamais l'exécuter.
"""
from __future__ import annotations

import re
from typing import Callable

from . import guards
from .matchers import (
    GlobalCorpusMatcher,
    HelpMatcher,
    ListDocumentsMatcher,
    NewsGateMatcher,
    SummaryMatcher,
)
from .normalize import normalize
from .plan import ActionContext, ActionPlan

# Ordre de priorité (hors fallback), figé et testé.
# ``HelpMatcher`` est en tête : l'aide produit est une commande explicite, elle
# prime sur toute interprétation documentaire. Il porte sa propre garde interne
# (``_HELP_DENY_RE``), donc « aide-moi à résumer ce document » ne matche pas ici
# et redescend normalement vers ``document.summarize``.
#
# ``GlobalCorpusMatcher`` vient juste après l'aide, et AVANT résumé/listing :
# « résume tous les documents » est une demande GLOBALE, pas un résumé
# mono-document. Il porte ses propres gardes (portée restreinte, restriction
# thématique), donc « résume Rapport.pdf » et « liste tous les documents sur Nordexia »
# ne matchent pas ici et redescendent vers document.summarize / search.list_documents.
_PRIORITY: tuple = (
    HelpMatcher(),
    GlobalCorpusMatcher(),
    SummaryMatcher(),
    ListDocumentsMatcher(),
    NewsGateMatcher(),
)

# Actions dont la détection est explicite et auto-gardée : les garde-fous
# génériques (explicatif/nié/cité) ne doivent PAS les rétrograder. Sans cela,
# « comment utiliser RagATM ? » serait vu comme « explicatif » et renverrait au RAG.
#
# ``corpus.global_request`` est exempté pour la même raison, et pour une seconde,
# décisive : « NE te limite PAS aux sources les plus pertinentes » est une
# NÉGATION portant sur un déclencheur (`_TRIGGER_RE` contient « tou[ts] les »).
# Sans exemption, cette demande globale explicite serait rétrogradée vers le RAG
# top-k — précisément le comportement que ce lot supprime.
_GUARD_EXEMPT = {"help.capabilities", "corpus.global_request"}

# Le classifieur ne peut ESCALADER que vers ces deux lectures (news reste géré par
# chat_query ; search.answer/none => repli déterministe ; mutations impossibles).
_CLASSIFIER_ESCALATABLE = {"search.list_documents", "document.summarize"}

# Garde de déclenchement du classifieur : VERBES/synonymes de résumé ou de listing
# que les regex strictes n'ont pas tranchés (topo, condensé, énumère, lister…).
# Volontairement PAS de simple nom « document/fichier » (présent dans des questions
# factuelles nominales) ni de vocabulaire news (chat_query s'en charge) -> 0 appel
# classifieur sur le chemin nominal.
# NB : racines (enumer, condens, abreg, recens…) => PAS de \b final, sinon
# « énumère|enumere » échouerait (r et e sont tous deux « word »).
_CLASSIFY_HINT_RE = re.compile(
    r"\b(resum|synth|topo|abreg|condens|tl;?dr|points?\s+cles|"
    r"liste|lister|enumer|recens|inventaire)"
)

# Signature du classifieur : (texte_normalisé, actions_autorisées[, candidate_count]) -> classification|None
LlmClassifier = Callable[..., "dict | None"]


CLARIFY_MESSAGE = (
    "Je n’ai pas bien compris votre demande. Souhaitez-vous une réponse à partir des "
    "documents, la liste des documents sur un sujet, ou le résumé d’un document précis ?"
)


def _clarify_plan(candidates, reason: str) -> ActionPlan:
    return ActionPlan(
        action="clarification",
        detection_method="llm",
        parameters={"reason": reason},
        candidates=candidates,
        needs_clarification=True,
        clarification_message=CLARIFY_MESSAGE,
    )


def decide_from_classification(*, deterministic_candidates, classification) -> ActionPlan:
    """Politique de décision backend (§3/§6/§7) — le score LLM ne décide JAMAIS seul.

    Cette fonction n'est appelée QUE lorsqu'un indice d'action (garde) est présent.
    Donc, en cas d'échec/incohérence, on CLARIFIE (plutôt que de RAG silencieusement) :
      - escalatable (listing/summarize) valide  -> action
      - intent=none valide                       -> search.answer (l'utilisateur ne
        voulait finalement pas d'action)
      - timeout / JSON invalide / indisponible   -> clarification générique
      - intention valide mais non actionnable     -> clarification (incohérente)
    Une cible de résumé ambiguë est clarifiée en aval (resolver), jamais exécutée.
    """
    cand = tuple(c.action for c in deterministic_candidates)

    if not classification:
        return _clarify_plan(cand, reason="classifier_unavailable")

    intent = classification.get("intent")

    if intent in _CLASSIFIER_ESCALATABLE:
        params: dict = {}
        if intent == "document.summarize":
            ref = (classification.get("parameters") or {}).get("document_reference")
            params = {"ref_kind": "filename", "document_reference": ref} if ref else {"ref_kind": "implicit"}
        # listing : terme ré-extrait par le handler historique (parité) -> params vides.
        return ActionPlan(action=intent, detection_method="llm", parameters=params, score=classification.get("score"))

    if intent == "none":
        return ActionPlan(action="search.answer", detection_method="fallback", candidates=cand)

    # intent valide (search.answer / search.news) mais un indice d'action était présent
    # -> incohérent : on clarifie plutôt que d'exécuter/RAG à l'aveugle.
    return _clarify_plan(cand, reason="incoherent_intent")


class ActionRouter:
    def __init__(self, *, llm_classifier: LlmClassifier | None = None) -> None:
        # Désactivé par défaut : détection purement déterministe.
        self._llm = llm_classifier

    def route(self, text: str, context: ActionContext | None = None) -> ActionPlan:
        original = text or ""
        norm = normalize(original)

        if not norm:
            return ActionPlan(action="search.answer", detection_method="fallback")

        # 1) Règles déterministes dans l'ordre de priorité.
        matches = [
            mr
            for matcher in _PRIORITY
            if (mr := matcher.match(norm=norm, original=original)) is not None
        ]
        # Un matcher « ambigu » (ex. garde news) est un CANDIDAT : signal faible qui
        # exige une confirmation (classifieur LLM, lot 4) avant d'être committé.
        committed = [m for m in matches if not m.ambiguous]

        # Garde-fous : explicatif / nié / cité -> rétrograde vers search.answer.
        suppressed, reason = guards.suppresses_action(original)

        if committed:
            primary = committed[0]  # ordre de priorité déjà appliqué
            if suppressed and primary.action not in _GUARD_EXEMPT:
                return ActionPlan(
                    action="search.answer",
                    detection_method="fallback",
                    parameters={"suppressed_from": primary.action, "reason": reason},
                )
            candidates = tuple(m.action for m in matches if m.action != primary.action)
            return ActionPlan(
                action=primary.action,
                detection_method=primary.detection_method,
                parameters=dict(primary.parameters),
                candidates=candidates,
                score=primary.score,
            )

        # 2) Classification LLM structurée — UNIQUEMENT sur formulation ambiguë (garde
        # de vocabulaire résumé/listing) et jamais si un garde-fou s'applique. Le
        # chemin nominal (question factuelle, sans indice) N'APPELLE PAS le classifieur.
        if (
            not suppressed
            and self._llm is not None
            and _CLASSIFY_HINT_RE.search(norm)
        ):
            from .registry import allowed_actions_for_llm

            classification = self._llm(norm, allowed_actions_for_llm(), len(matches))
            return decide_from_classification(
                deterministic_candidates=matches, classification=classification
            )

        # 3) Défaut sûr : RAG. On expose les candidats ambigus non confirmés.
        candidates = tuple(m.action for m in matches)
        return ActionPlan(
            action="search.answer",
            detection_method="fallback",
            candidates=candidates,
        )

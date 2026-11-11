# backend/apps/rag_core/services/answer_contract.py
"""Contrat de réponse : ABSTENTION et citations RÉELLEMENT émises.

Pourquoi ce module existe
-------------------------
Le chat confondait **trois populations de sources** en une seule :

1. les sources **retrouvées** par le retrieval (pool final de candidats) ;
2. les sources **transmises** au modèle (après diversification + budget de prompt) ;
3. les sources **réellement citées** par le modèle dans sa réponse.

La ligne « Sources: [1], [2], … » était reconstruite depuis (2) et ajoutée
**inconditionnellement**, y compris sous une abstention (« Je ne sais pas. »). L'interface
affichait alors « N sources citées ✓ » sous une réponse qui n'en citait aucune : un signal de
confiance FAUX, exactement là où le produit devait signaler son incertitude.

Ce module est la source unique de vérité pour :

- la **détection d'abstention** (FR/EN, robuste, jamais une simple égalité de chaîne) ;
- l'**extraction des citations** réellement présentes dans le texte du modèle ;
- le **contrat structuré** exposé au frontend et à l'audit.

Il est pur (aucun accès base, aucun réglage Django) : testable unitairement et réutilisable par
le chat, la mémoire de conversation (anti-anchoring) et l'évaluation offline.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence

# Ligne « Sources: … » / « Source : … » émise par le modèle (ou héritée d'un tour antérieur).
SOURCES_LINE_RE = re.compile(r"(?im)^\s*sources?\s*:\s*.*$")

# Référence de citation. Bornée à 3 chiffres : au-delà, ce n'est plus une citation.
CITATION_RE = re.compile(r"\[(\d{1,3})\]")

# Lignes ajoutées APRÈS génération (avertissement « couverture partielle », bandeau egress).
# Elles n'appartiennent pas à la réponse du modèle et ne doivent pas masquer une abstention.
_PREFIX_LINE_RE = re.compile(r"(?m)^\s*(?:⚠️|⚠|ℹ️).*$")

# Reste de texte toléré autour d'une formule d'abstention. Au-delà, la réponse porte une
# substance propre : ce n'est plus une abstention mais une réponse partielle, qui a le droit
# de citer ses sources. Calibré pour laisser passer une phrase courte de contenu réel.
ABSTENTION_RESIDUAL_MAX_CHARS = 60

# Formules d'abstention, sur texte NORMALISÉ (minuscules, sans accents, espaces compactés).
# Le principe est volontairement le suivant : couvrir les tournures, PAS énumérer des phrases
# exactes. Toute formule ajoutée ici doit venir avec un test dans
# tests/tests_root/unit/test_answer_contract.py.
_ABSTENTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # --- français -------------------------------------------------------------
        r"je ne sais pas",
        r"je ne dispose (?:pas|d'aucune)[^.]{0,40}informations?",
        r"je n'ai pas (?:assez |suffisamment )?(?:d'|de )?informations?",
        r"(?:pas|aucune?) (?:assez )?(?:d'|de )?informations? (?:suffisantes?|pertinentes?|disponibles?)",
        r"pas assez (?:d'|de )informations?",
        r"informations? insuffisantes?",
        r"aucune information[^.]{0,40}(?:disponible|presente|trouvee|permet)",
        r"impossible de repondre",
        r"je ne peux pas (?:y )?repondre",
        r"ne permettent pas de repondre",
        r"ne permet pas de repondre",
        r"(?:sources|documents|extraits)[^.]{0,60}ne (?:contiennent|mentionnent|fournissent|precisent) (?:pas|aucune?)",
        r"(?:sources|documents|extraits) (?:fournis |disponibles |ci-dessous )?ne (?:permettent|permet)",
        # --- anglais (politique de langue de réponse) -----------------------------
        r"i (?:don't|do not) know",
        r"i (?:cannot|can't|am unable to) answer",
        r"(?:not enough|insufficient) information",
        r"no (?:relevant|sufficient) information",
        r"(?:sources|documents|excerpts)[^.]{0,60}do not (?:allow|contain|provide|mention)",
    )
)


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _normalize(text: str) -> str:
    """Minuscules, sans accents, apostrophes unifiées, espaces compactés."""
    out = _strip_accents(text or "").lower()
    out = out.replace("’", "'").replace("‘", "'").replace("`", "'")
    return re.sub(r"\s+", " ", out).strip()


def strip_sources_line(answer: str) -> str:
    """Retire la/les ligne(s) « Sources: … » d'une réponse (modèle ou tour antérieur)."""
    if not isinstance(answer, str):
        return ""
    out = SOURCES_LINE_RE.sub("", answer).strip()
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _answer_core(answer: str) -> str:
    """Texte du MODÈLE seul : sans ligne « Sources: », sans bandeau ajouté en aval."""
    core = strip_sources_line(answer)
    core = _PREFIX_LINE_RE.sub("", core)
    return core.strip()


def is_abstention(answer: str) -> bool:
    """La réponse est-elle une abstention (« je ne sais pas », « sources insuffisantes ») ?

    Robustesse volontaire — la détection ne repose PAS sur une égalité avec une phrase
    unique :

    - texte normalisé (casse, accents, apostrophes, espaces) ;
    - la formule peut apparaître n'importe où, pas seulement en tête ;
    - une réponse SUBSTANTIELLE qui mentionne au passage « les sources ne permettent pas de
      répondre sur tel point » n'est PAS une abstention : on mesure le texte RESTANT une fois
      les formules et les marqueurs de citation retirés (``ABSTENTION_RESIDUAL_MAX_CHARS``).
    """
    core = _answer_core(answer)
    if not core:
        # Rien à afficher : c'est une non-réponse, quelle qu'en soit la cause.
        return True

    residual = _normalize(core)
    matched = False
    for pattern in _ABSTENTION_PATTERNS:
        residual, n = pattern.subn(" ", residual)
        if n:
            matched = True
    if not matched:
        return False

    residual = CITATION_RE.sub(" ", residual)
    residual = re.sub(r"[\s.,;:!?()\[\]«»\"'\-–—/]+", " ", residual).strip()
    return len(residual) <= ABSTENTION_RESIDUAL_MAX_CHARS


def extract_cited_source_ids(answer: str, *, max_id: int) -> list[int]:
    """Identifiants [n] RÉELLEMENT cités par le modèle, bornés au nombre de sources.

    Une référence hors bornes (« [12] » avec 8 sources) est une hallucination de citation :
    elle est écartée, jamais affichée. Résultat trié et dédupliqué.
    """
    if max_id <= 0:
        return []
    found = {int(m) for m in CITATION_RE.findall(answer or "")}
    return sorted(i for i in found if 1 <= i <= max_id)


#: Raisons d'arrêt reconnues, et ce qu'elles signifient pour la troncature.
#: Tout autre libellé est traité comme INCONNU — pas comme « complet ».
_RAISONS_ARRET = {"length": True, "stop": False}


def declared_stop_reason(raw: Any) -> str | None:
    """Raison d'arrêt DÉCLARÉE par le fournisseur, ou ``None`` s'il n'en dit rien.

    Deux formes selon le fournisseur : ``done_reason`` (AI Gateway et Ollama, y compris
    l'événement terminal du flux) et ``choices[0].finish_reason`` (OpenAI). Les deux
    portent le même vocabulaire utile : ``length`` contre ``stop``.
    """
    if not isinstance(raw, dict):
        return None
    raison = raw.get("done_reason")
    if not raison:
        choix = raw.get("choices")
        if isinstance(choix, list) and choix and isinstance(choix[0], dict):
            raison = choix[0].get("finish_reason")
    raison = str(raison or "").strip().lower()
    return raison or None


def answer_was_truncated(usage: Any, num_predict: Any, *, raw: Any = None) -> bool:
    """La génération s'est-elle arrêtée sur le PLAFOND plutôt que d'elle-même ?

    **Signal de premier rang : la raison d'arrêt déclarée.** Vérifié le 23/08/2026
    contre la Gateway de production, en régime synchrone comme en flux :

        num_predict=16    -> done_reason='length'  completion_tokens=16   texte coupé
        num_predict=4096  -> done_reason='stop'    completion_tokens=832  texte complet

    Ce champ est au contrat de `/v1/generate` (`GenerateResponse.done_reason`) et à
    l'événement terminal de `/v1/generate/stream`. Ce module a d'abord affirmé le
    contraire — « la Gateway ne renvoie aucune raison d'arrêt » — pour n'avoir regardé
    que ``done``, qui vaut ``True`` dans les deux cas. L'audit AIGW-WORKLOAD-ISOLATION
    l'a corrigé.

    **Repli : l'arithmétique.** Quand aucune raison n'est déclarée, atteindre exactement
    le plafond ne peut pas être une coïncidence de fin de phrase — c'est l'arrêt forcé.
    Le repli reste nécessaire : tous les fournisseurs ne déclarent pas, et il couvre la
    réponse d'un provider muet.

    Pourquoi ce signal est nécessaire. La ligne « Sources: [1], [2] » est ajoutée APRÈS
    la génération (cf. `finalize_answer`) : une réponse coupée en plein mot reçoit donc
    un pied de page bien formé et un badge de citations. Elle est indistinguable d'une
    réponse complète — y compris pour son lecteur. Mesuré sur 200 réponses réelles :
    6 % en production, 20 % en staging.

    Rend ``False`` si l'information manque : on ne signale pas une troncature qu'on n'a
    pas constatée.
    """
    raison = declared_stop_reason(raw)
    if raison in _RAISONS_ARRET:
        # Le fournisseur a répondu à la question posée. On ne recalcule pas par-dessus :
        # un compteur qui contredirait la raison déclarée serait un défaut à corriger
        # chez lui, pas une raison de le désavouer ici.
        return _RAISONS_ARRET[raison]

    if not isinstance(usage, dict):
        # Un `usage` malformé ne doit pas faire échouer le chemin de réponse : la
        # détection est un CONFORT de diagnostic, jamais une condition de service.
        return False
    try:
        plafond = int(num_predict)
        produits = int(usage.get("completion_tokens"))
    except (TypeError, ValueError):
        return False
    return plafond > 0 and produits >= plafond


def format_cited_sources_line(cited_source_ids: Sequence[int]) -> str:
    """Ligne « Sources: [x], [y] » construite depuis les citations RÉELLES (vide si aucune)."""
    ids = [int(i) for i in (cited_source_ids or [])]
    if not ids:
        return ""
    return "Sources: " + ", ".join(f"[{i}]" for i in ids)


@dataclass(frozen=True)
class AnswerSourcesContract:
    """Contrat explicite des trois populations de sources + statut d'abstention.

    Exposé au frontend (clé ``sources_contract``) pour qu'aucune couche d'affichage n'ait à
    redevenir devin : l'interface n'a plus à déduire « citée » de « retrouvée ».
    """

    retrieved_sources_count: int
    prompt_sources_count: int
    cited_source_ids: tuple[int, ...]
    answer_is_abstention: bool
    #: La génération a-t-elle été coupée par le plafond de tokens ? ``None`` quand
    #: l'information n'est pas disponible — on ne signale pas ce qu'on n'a pas constaté.
    answer_truncated: bool | None = None

    @property
    def cited_sources_count(self) -> int:
        return len(self.cited_source_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieved_sources_count": int(self.retrieved_sources_count),
            "prompt_sources_count": int(self.prompt_sources_count),
            "cited_source_ids": list(self.cited_source_ids),
            "cited_sources_count": self.cited_sources_count,
            "answer_is_abstention": bool(self.answer_is_abstention),
            "answer_truncated": (None if self.answer_truncated is None
                                 else bool(self.answer_truncated)),
        }


def build_answer_contract(
    answer: str,
    *,
    prompt_sources_count: int,
    retrieved_sources_count: int | None = None,
    force_abstention: bool = False,
    answer_truncated: bool | None = None,
) -> AnswerSourcesContract:
    """Contrat calculé depuis le texte BRUT du modèle (avant toute réécriture)."""
    n_prompt = max(0, int(prompt_sources_count or 0))
    n_retrieved = int(retrieved_sources_count) if retrieved_sources_count is not None else n_prompt
    n_retrieved = max(n_retrieved, n_prompt)

    abstention = bool(force_abstention) or is_abstention(answer)
    # Une abstention ne cite RIEN : même si le modèle a laissé traîner un « [2] », il n'a rien
    # affirmé qu'une source puisse étayer.
    cited = () if abstention else tuple(extract_cited_source_ids(answer, max_id=n_prompt))

    return AnswerSourcesContract(
        retrieved_sources_count=n_retrieved,
        prompt_sources_count=n_prompt,
        cited_source_ids=cited,
        answer_is_abstention=abstention,
        answer_truncated=answer_truncated,
    )


def finalize_answer(
    answer: str,
    *,
    prompt_sources_count: int,
    retrieved_sources_count: int | None = None,
    force_abstention: bool = False,
    answer_truncated: bool | None = None,
) -> tuple[str, AnswerSourcesContract]:
    """Texte final + contrat.

    Règles (cf. docs/06-ai/answerability.md) :

    - **abstention** → aucune ligne « Sources: » ajoutée, aucune citation revendiquée ;
    - **réponse citant [a], [b]** → ligne reconstruite avec CES identifiants uniquement
      (jamais l'énumération complète du top-k) ;
    - **réponse sans citation** → aucune ligne ajoutée : on n'invente pas une traçabilité que
      le modèle n'a pas fournie.
    """
    contract = build_answer_contract(
        answer,
        prompt_sources_count=prompt_sources_count,
        retrieved_sources_count=retrieved_sources_count,
        force_abstention=force_abstention,
        answer_truncated=answer_truncated,
    )
    body = strip_sources_line(answer)
    line = format_cited_sources_line(contract.cited_source_ids)
    text = (body + "\n\n" + line).strip() if line else body.strip()
    return text, contract

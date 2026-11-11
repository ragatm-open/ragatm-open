"""Politique de langue de réponse IA (P0) — résolveur commun.

Décide la langue de GÉNÉRATION, indépendamment de la locale d'interface
(`preferred_locale`) et sans jamais toucher au retrieval, à l'answerability, aux
citations ni aux extraits source. Contrat :

    answer_language (préférence)  : "auto" | "fr" | "en"
    résolution                    : forcé fr/en
                                    → détection de la dernière question
                                    → langue précédente du thread (si indéterminable)
                                    → français (repli historique)

La politique est GARDÉE par un flag plateforme (`RAG_ANSWER_LANGUAGE_POLICY_ENABLED`,
défaut OFF) : tant qu'il est désactivé, `resolve_target` renvoie ``None`` et les
constructeurs de prompt conservent le comportement français historique, à l'octet
près. La préférence utilisateur (`answer_language`) reste distincte du flag.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from django.conf import settings

SUPPORTED = ("fr", "en")
DEFAULT_ANSWER_LANGUAGE = "fr"
ANSWER_LANGUAGE_CHOICES = ("auto", "fr", "en")

_WORD_RE = re.compile(r"[a-zàâäçéèêëîïôöùûüœ]+", re.IGNORECASE)
_ACCENTS_RE = re.compile(r"[àâäçéèêëîïôöùûüœ]", re.IGNORECASE)

# Marqueurs de mots-outils (grammaticaux) : discriminants et stables. On ne cherche
# pas une détection linguistique exhaustive — juste FR vs EN sur une question courte.
_FR_MARKERS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "est", "sont",
    "quel", "quelle", "quels", "quelles", "quoi", "comment", "pourquoi", "combien",
    "où", "que", "qui", "dans", "pour", "avec", "sur", "au", "aux", "ce", "cette",
    "ces", "son", "sa", "ses", "leur", "plus", "moins", "très", "entre", "quelles",
}
_EN_MARKERS = {
    "the", "a", "an", "of", "and", "or", "is", "are", "was", "were", "what", "which",
    "who", "how", "why", "where", "when", "in", "for", "with", "on", "this", "that",
    "these", "those", "between", "most", "more", "less", "were", "identified", "at",
    "significant", "available", "discrepancies", "recent", "close",
}


def policy_enabled() -> bool:
    """Flag plateforme. OFF par défaut → comportement historique français."""
    return bool(getattr(settings, "RAG_ANSWER_LANGUAGE_POLICY_ENABLED", False))


def detect_question_language(question: str) -> Optional[str]:
    """Détecte ``fr`` / ``en`` d'après les mots-outils et les accents.

    Renvoie ``None`` quand la question ne porte AUCUN signal linguistique
    (identifiant, nombre, tokens isolés) : l'appelant enchaîne alors sur la langue
    précédente du thread puis le français.
    """
    q = (question or "").lower()
    words = _WORD_RE.findall(q)
    if not words:
        return None
    fr = sum(1 for w in words if w in _FR_MARKERS)
    en = sum(1 for w in words if w in _EN_MARKERS)
    if _ACCENTS_RE.search(q):
        fr += 2
    if fr == 0 and en == 0:
        return None  # indéterminable : aucun mot-outil, pas d'accent
    return "en" if en > fr else "fr"


def coerce_pref(value: object) -> str:
    """Ramène une préférence à ``auto|fr|en`` (défaut ``auto``)."""
    return value if value in ANSWER_LANGUAGE_CHOICES else "auto"


def resolve_answer_language(
    *,
    pref: str,
    question: str,
    thread_prev_language: Optional[str] = None,
    ui_locale: Optional[str] = None,
) -> str:
    """Résout la langue de réponse EFFECTIVE (``fr`` | ``en``) selon la doctrine.

    Ordre : ``pref`` forcé (``fr``/``en``) → détection de la question → langue
    précédente du thread → ``ui_locale`` (``preferred_locale``) en DERNIER repli →
    ``fr``.

    ``ui_locale`` n'est PAS un synonyme de la préférence de réponse : c'est un repli
    ultime, utile seulement quand il n'y a NI question exploitable NI historique
    (typiquement un résumé de document déclenché hors conversation). Les appelants qui
    veulent une stricte indépendance vis-à-vis de l'interface ne le passent pas.
    """
    p = coerce_pref(pref)
    if p in SUPPORTED:
        return p
    detected = detect_question_language(question)
    if detected in SUPPORTED:
        return detected
    if thread_prev_language in SUPPORTED:
        return thread_prev_language
    if ui_locale in SUPPORTED:
        return ui_locale
    return DEFAULT_ANSWER_LANGUAGE


def resolve_target(
    *,
    pref: str,
    question: str,
    thread_prev_language: Optional[str] = None,
    ui_locale: Optional[str] = None,
) -> Optional[str]:
    """Cible concrète pour les constructeurs de prompt, ou ``None`` si la politique
    est désactivée (→ prompts français historiques inchangés).
    """
    if not policy_enabled():
        return None
    return resolve_answer_language(
        pref=pref,
        question=question,
        thread_prev_language=thread_prev_language,
        ui_locale=ui_locale,
    )


# Directive de langue explicite, courte, préposée au prompt final quand la cible est
# l'anglais. Le français reste la langue ambiante (aucune directive → octets
# identiques à l'historique). Les citations/extraits/noms de fichiers restent dans
# leur langue d'origine.
DIRECTIVE_EN = (
    "IMPORTANT — Respond ENTIRELY in English: all section titles, structure and prose "
    "must be written in English. Keep verbatim quotations, citation markers ([1], [2], …), "
    "source excerpts and file names in their original language.\n\n"
)


def directive_for(target: Optional[str]) -> str:
    """Directive de langue à préposer (vide pour fr / None)."""
    return DIRECTIVE_EN if target == "en" else ""


# --- Observabilité ----------------------------------------------------------
# Diagnostiquer « pourquoi ai-je eu du français ? » exigeait jusqu'ici un accès shell
# au conteneur : ni les logs ni l'audit n'exposaient la moindre étape de la décision.
# Ce journal rend la chaîne complète lisible. Il ne porte NI la question, NI le
# prompt, NI le contenu documentaire — uniquement des codes de langue et des drapeaux.
_logger = logging.getLogger(__name__)


def log_language_decision(
    *,
    surface: str,
    pref: str,
    question: str = "",
    thread_prev_language: Optional[str] = None,
    ui_locale: Optional[str] = None,
    resolved: Optional[str],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    **extra,
) -> None:
    """Journalise la décision de langue. Fail-open : jamais bloquant pour une réponse."""
    try:
        payload = {
            "event": "answer_language_decision",
            "surface": surface,
            "policy_enabled": policy_enabled(),
            "requested_answer_language": coerce_pref(pref),
            # Longueur seule : discrimine « question trop courte pour être détectée »
            # sans jamais exposer le texte de la question.
            "question_len": len(question or ""),
            "detected_question_language": detect_question_language(question),
            "thread_prev_language": thread_prev_language,
            "ui_locale": ui_locale,
            "resolved_answer_language": resolved,
            # Variante de prompt RÉELLEMENT construite : `legacy_fr` signale que la
            # politique est OFF et qu'aucune directive de langue n'a été envoyée.
            "prompt_variant": "en" if resolved == "en" else ("fr" if resolved == "fr" else "legacy_fr"),
            "provider": provider,
            "model": model,
        }
        payload.update(extra)
        _logger.info("answer_language_decision %s", payload)
    except Exception:  # pragma: no cover - l'observabilité ne casse jamais une réponse
        pass

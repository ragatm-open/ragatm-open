"""Score de priorité d'un feedback.

Ce que le score essaie de faire : remonter en tête les signalements qui portent
une information VÉRIFIABLE, et non ceux qui expriment le plus d'agacement.

Deux règles écartées volontairement, malgré leur présence dans la proposition
initiale :

- « beaucoup de sources consultées » — ce signal corrèle mal avec la qualité du
  défaut : une question large consulte beaucoup de sources sans être un incident ;
- « commentaire long = signalement de qualité » — cela pénalise les utilisateurs
  peu bavards, pas les mauvais signalements.

Le malus ne porte donc PAS sur l'absence de commentaire (H19 le proposait) mais
sur l'absence de **tout signal objectif**, ce qui est mesurable.

Découpage en deux temps, imposé par la contrainte de latence :

- :func:`compute_base_priority` — synchrone, sans I/O coûteuse, appelé sur le
  chemin du clic ;
- :func:`compute_objective_bonus` — asynchrone (tâche Celery), il vérifie en
  base ce que l'utilisateur a *déclaré*. Un « l'information est dans mes
  documents » non vérifié reste une opinion ; vérifié, c'est un fait.
"""
from __future__ import annotations

import logging

from apps.feedback.models import AnswerFeedback

logger = logging.getLogger(__name__)

#: Bornes de sécurité — le champ est un ``SmallIntegerField``, et un score qui
#: dérive ne doit jamais pouvoir faire échouer une écriture de feedback.
SCORE_MIN = -10
SCORE_MAX = 20


def _clamp(value: int) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, int(value)))


def compute_base_priority(
    *,
    kind: str,
    policy_hist: str,
    rating: int,
    dedup_fingerprint: str,
    expected_document: str = "",
    expected_term: str = "",
    distinct_reporters: int = 0,
) -> int:
    """Priorité calculable SANS lecture coûteuse (chemin synchrone du clic).

    ``distinct_reporters`` est fourni par l'appelant, qui le lit via l'index
    ``dedup_fingerprint`` — un ``COUNT`` sur index, pas un balayage.
    """
    if int(rating) == AnswerFeedback.RATING_UP:
        # Un pouce haut n'est pas un incident : il n'entre pas dans la file de
        # triage, et lui donner une priorité brouillerait le classement.
        return 0

    score = 0
    if kind == AnswerFeedback.KIND_INFO_IN_DOCS:
        score += 3
    elif kind == AnswerFeedback.KIND_WRONG_SOURCES:
        score += 2

    # Plusieurs utilisateurs DISTINCTS sur la même empreinte : le signal le plus
    # fiable du lot, car il n'est pas manipulable par une personne seule.
    if int(distinct_reporters or 0) >= 2:
        score += 2

    if (policy_hist or "") == "no_answer":
        score += 2

    has_objective_signal = bool(
        (expected_document or "").strip()
        or (expected_term or "").strip()
        or kind
        in (
            AnswerFeedback.KIND_INFO_IN_DOCS,
            AnswerFeedback.KIND_WRONG_SOURCES,
            AnswerFeedback.KIND_WRONG_LANGUAGE,
        )
        or (policy_hist or "") == "no_answer"
    )
    if not has_objective_signal:
        score -= 1

    return _clamp(score)


def count_distinct_reporters(dedup_fingerprint: str, *, including_pseudonym: str = "") -> int:
    """Nombre de pseudonymes DISTINCTS ayant signalé la même empreinte.

    ``including_pseudonym`` est ajouté à l'ensemble : au moment du clic, la
    ligne courante n'est pas encore écrite, et un simple ``+1`` compterait deux
    fois un utilisateur qui a déjà signalé le même cas.
    """
    if not dedup_fingerprint:
        return 1 if including_pseudonym else 0
    seen = set(
        AnswerFeedback.objects.filter(
            dedup_fingerprint=dedup_fingerprint, rating=AnswerFeedback.RATING_DOWN
        )
        .values_list("user_pseudonym", flat=True)
        .distinct()
    )
    if including_pseudonym:
        seen.add(including_pseudonym)
    seen.discard("")
    return len(seen)


def compute_objective_bonus(feedback: AnswerFeedback) -> int:
    """Bonus VÉRIFIÉ en base (chemin asynchrone).

    +1 si le terme déclaré par l'utilisateur existe réellement dans un chunk de
    l'espace concerné. C'est ce qui transforme « l'information est dans mes
    documents » d'une déclaration en un fait — et c'est précisément le cas
    « Nordexia Conseil » : le terme EST indexé, la réponse a pourtant été
    « I don't know ».

    Ne lève jamais : un bonus est un confort de triage, pas une garantie.
    """
    term = (feedback.expected_term or "").strip()
    if not term or not feedback.rag_space_id_hist:
        return 0
    try:
        from apps.rag_core.models import Chunk

        exists = Chunk.objects.filter(
            rag_space_id=feedback.rag_space_id_hist, text__icontains=term[:200]
        ).exists()
        return 1 if exists else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "feedback.priority objective bonus skipped error_code=%s",
            type(exc).__name__,
        )
        return 0


def recompute_priority(feedback: AnswerFeedback, *, context_bonus: int = 0) -> int:
    """Priorité complète : base + bonus objectif + bonus de contexte conversationnel."""
    base = compute_base_priority(
        kind=feedback.kind,
        policy_hist=feedback.policy_hist,
        rating=feedback.rating,
        dedup_fingerprint=feedback.dedup_fingerprint,
        expected_document=feedback.expected_document,
        expected_term=feedback.expected_term,
        distinct_reporters=count_distinct_reporters(
            feedback.dedup_fingerprint, including_pseudonym=feedback.user_pseudonym
        ),
    )
    if feedback.rating == AnswerFeedback.RATING_UP:
        return 0
    return _clamp(base + compute_objective_bonus(feedback) + int(context_bonus or 0))

# backend/apps/actions/guards.py
"""Garde-fous anti-faux-positifs (audit §11.1, §12).

Objectif : qu'une phrase *explicative*, *niée* ou *citée* ne déclenche jamais une
action. En cas de doute, on rétrograde vers ``search.answer`` (jamais d'action
arbitraire). Les gardes opèrent sur le texte ORIGINAL (indices stables) avec des
classes de caractères tolérantes aux accents/casse.
"""
from __future__ import annotations

import re

# Marqueurs de modalité explicative ("comment ...", "explique ...", etc.).
_EXPLANATORY_RE = re.compile(
    r"\b(comment|explique[rz]?|expliqu|que\s+signifie|qu['e]\s*est[- ]ce|pourquoi|"
    r"documentation|exemple[s]?|definition|d[ée]finir|c['e]\s*est\s+quoi)\b",
    re.IGNORECASE,
)

# Déclencheurs d'action (larges) pour positionner les gardes.
# NB : ce sont des *racines* (resum, synthes, liste, supprim, actualit...) : pas de
# ``\b`` de fin, sinon « résum|er » échouerait (m et e sont tous deux « word »).
_TRIGGER_RE = re.compile(
    r"\b(r[ée]sum|synth[eè]s|synth[ée]tis|points?\s+cl[ée]s?|liste|tou[ts]\s+les|"
    r"quel(?:le)?s|actualit|news|quoi\s+de\s+neuf|mise\s*[àa]\s*jour|supprim|efface)",
    re.IGNORECASE,
)

# Négations proches du déclencheur.
_NEG_BEFORE_RE = re.compile(r"\b(ne|n['e]|sans|jamais|aucun[e]?)\b", re.IGNORECASE)
_NEG_PHRASE_RE = re.compile(
    r"\bje\s+ne\s+(veux|souhaite|voudrais)\s+pas\b|\bne\s+pas\b|\bpas\s+besoin\b",
    re.IGNORECASE,
)

_NEG_WINDOW = 30  # caractères avant le déclencheur

# Portées citées : « ... », " ... ", ' ... '.
_QUOTED_SPANS_RE = re.compile(r"[«\"“']([^«»\"“”']{0,200})[»\"”']")


def _first_trigger_pos(text: str) -> int | None:
    m = _TRIGGER_RE.search(text)
    return m.start() if m else None


def is_explanatory(text: str) -> bool:
    """Vrai si un marqueur explicatif *gouverne* le déclencheur (le précède)."""
    tpos = _first_trigger_pos(text)
    if tpos is None:
        return False
    for m in _EXPLANATORY_RE.finditer(text):
        if m.start() < tpos:
            return True
    return False


def is_negated(text: str) -> bool:
    """Vrai si une négation apparaît juste avant le déclencheur (fenêtre courte)."""
    tpos = _first_trigger_pos(text)
    if tpos is None:
        return False
    if _NEG_PHRASE_RE.search(text[:tpos]):
        return True
    window = text[max(0, tpos - _NEG_WINDOW): tpos]
    return bool(_NEG_BEFORE_RE.search(window))


def is_quoted(text: str) -> bool:
    """Vrai si le déclencheur tombe à l'intérieur d'une portée citée."""
    tpos = _first_trigger_pos(text)
    if tpos is None:
        return False
    for m in _QUOTED_SPANS_RE.finditer(text):
        # groupe 1 = contenu cité
        if m.start(1) <= tpos < m.end(1):
            return True
    return False


def suppresses_action(text: str) -> tuple[bool, str | None]:
    """Le texte doit-il être rétrogradé vers ``search.answer`` ?

    Retourne ``(True, raison)`` si explicatif / nié / cité, sinon ``(False, None)``.
    """
    if is_quoted(text):
        return True, "quoted"
    if is_negated(text):
        return True, "negation"
    if is_explanatory(text):
        return True, "explanatory"
    return False, None

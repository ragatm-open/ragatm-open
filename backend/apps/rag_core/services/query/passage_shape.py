"""Bruit STRUCTUREL du contexte : passages interrogatifs et passages-titres.

Ce que ce module fait — et ce qu'il ne fait pas
-----------------------------------------------
> Cette pénalité ne classe **pas** les anomalies, insuffisances ou constats. Elle réduit
> uniquement le poids de passages **structurellement peu affirmatifs**.

Aucun vocabulaire métier n'y figure : ni terme d'anomalie, ni terme d'insuffisance, ni
lexique négatif, ni nom d'entité, de système ou de document. Les signaux sont
typographiques et syntaxiques.

Pourquoi
--------
Mesuré sur deux corpus (questions de constats, 37 questions, 2 corpus) :

- les passages interrogatifs et les titres occupent **19 % du top-8** ;
- dans le pool lexical, les **questions du corpus se classent MIEUX que les réponses**
  (rang moyen 11,1 contre 13,8) : elles reprennent les mots de la requête ;
- les titres viennent surtout du leg vectoriel (**26 %** de ce pool) ;
- une pénalité de 0,75 ramène ces passages de 18 % à 2 % du top-8 sans dégrader le
  rappel mesuré, en ~0 ms ;
- l'exclusion pure ne fait pas mieux et interdirait une question documentaire qui
  porterait elle-même l'information.

D'où la doctrine : **en cas de doute, on conserve le score d'origine.** Un passage
ambigu ou mixte n'est jamais pénalisé, aucun candidat n'est jamais supprimé.

Limite assumée
--------------
Les métadonnées ``heading`` et ``section`` sont **absentes du corpus** (0 chunk sur
27 086 en dev). ``title_like`` est donc une heuristique **structurelle**, pas une
métadonnée documentaire — d'où son nom, et d'où l'exigence de plusieurs signaux
convergents avant de pénaliser.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any

from apps.rag_core.domain.ranking import sort_candidates

logger = logging.getLogger(__name__)

# --- Bornes du facteur -------------------------------------------------------
#: Pénalité PLANCHER. En deçà, la « dévalorisation » deviendrait une exclusion de fait —
#: hors doctrine de ce lot (non destructif).
MIN_PENALTY_FACTOR = 0.50
#: 1,00 = neutre. C'est aussi le mode de désactivation de secours.
MAX_PENALTY_FACTOR = 1.00
DEFAULT_PENALTY_FACTOR = 0.75

#: Un passage « court » au sens typographique. Volontairement bas : la brièveté SEULE ne
#: pénalise jamais (cf. `classify_passage`).
SHORT_PASSAGE_MAX_WORDS = 25

#: Contenu affirmatif résiduel toléré dans un passage majoritairement interrogatif.
#: Au-delà, le passage porte une réponse : il n'est plus du bruit.
MAX_RESIDUAL_AFFIRMATIVE_WORDS = 25

#: Part d'unités interrogatives à partir de laquelle l'interrogatif DOMINE.
MIN_INTERROGATIVE_RATIO = 0.6


class PassageShape(str, Enum):
    """Forme structurelle d'un passage (valeurs stables, journalisables)."""

    NORMAL = "normal"
    QUESTION = "question"
    TITLE_LIKE = "title_like"


@dataclass(frozen=True)
class PassageShapeDecision:
    """Décision EXPLICABLE : la forme, la confiance, le facteur et les raisons."""

    shape: PassageShape
    confidence: float
    penalty_factor: float
    reasons: tuple[str, ...] = ()

    @property
    def penalized(self) -> bool:
        return self.penalty_factor < MAX_PENALTY_FACTOR


NORMAL_DECISION = PassageShapeDecision(
    shape=PassageShape.NORMAL, confidence=1.0, penalty_factor=MAX_PENALTY_FACTOR
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-•*–—]|\d+[.)]|[a-z][.)])\s+", re.IGNORECASE)
_TERMINAL_PUNCT = (".", "!", "?", "…", ":", ";")

#: Marqueurs typographiques de titre. Purement STRUCTURELS (numérotation, libellés de
#: structure documentaire) : aucun terme métier.
#: Numérotation de section. Ce qui suit doit être une LETTRE : sans cette exigence,
#: « 1 234,56 EUR » — une valeur, pas un titre — serait pris pour une section numérotée.
#: Deux formes acceptées : numéro pointé (« 3.2 Contrôles ») ou numéro suivi d'un
#: séparateur (« 1. Introduction », « IV — Synthèse »). Un millésime nu (« 2025 Bilan »)
#: n'est donc pas un titre.
_NUMBERED_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:[IVXLC]{1,5}|\d{1,2})(?:\.\d{1,2}){1,3}\s+[^\W\d_]"
    r"|"
    r"(?:[IVXLC]{1,5}|\d{1,3})\s*[.)\-–—]\s+[^\W\d_]"
    r")"
)
_STRUCTURAL_LABEL_RE = re.compile(
    r"^\s*(?:sommaire|table des mati|annexe|appendice|appendix|chapitre|section|partie|"
    r"titre|synthese|synthesis|summary|overview|figure|tableau|table|graphique|schema|"
    r"diapositive|slide|introduction|conclusion|preambule|glossaire)\b",
    re.IGNORECASE,
)
#: Débuts interrogatifs génériques (FR + EN). SIGNAUX, jamais des règles : ils ne
#: comptent que sur une unité qui se termine par un point d'interrogation.
_INTERROGATIVE_OPENERS = {
    # français
    "qui", "que", "qu", "quoi", "quel", "quelle", "quels", "quelles", "comment",
    "pourquoi", "quand", "ou", "combien", "est", "existe", "peut", "peux", "pouvez",
    "y", "faut", "doit", "doivent", "avez", "avons", "sont", "a", "ont",
    # anglais
    "who", "what", "which", "how", "why", "when", "where", "is", "are", "was", "were",
    "does", "do", "did", "can", "could", "should", "would", "will", "has", "have", "may",
}


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in folded if unicodedata.category(c) != "Mn")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _units(text: str) -> list[str]:
    """Unités de texte : lignes, puis phrases à l'intérieur des lignes.

    Une liste à puces est faite de LIGNES, pas de phrases : la découper uniquement sur
    la ponctuation finale ferait d'une liste de questions une unité unique.
    """
    out: list[str] = []
    pending = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT_RE.split(line):
            part = part.strip()
            if not part:
                continue
            # Un énumérateur (« 1. », « 2) », « — ») n'est pas une unité de sens : la
            # découpe sur la ponctuation finale le détacherait de sa question et
            # diluerait le ratio interrogatif d'une liste numérotée de questions.
            if not _words(part):
                pending = f"{pending} {part}".strip()
                continue
            out.append(f"{pending} {part}".strip() if pending else part)
            pending = ""
    if pending:
        out.append(pending)
    return out


def _is_interrogative_unit(unit: str) -> bool:
    """Unité interrogative : se TERMINE par « ? » et porte au moins deux mots.

    Exiger la position finale écarte les « ? » internes d'une URL, d'une formule ou
    d'une chaîne technique — qui ne font pas d'un passage une question.
    """
    stripped = _BULLET_RE.sub("", unit).strip().rstrip("»\"')]")
    if not stripped.endswith("?"):
        return False
    return len(_words(stripped)) >= 2


def _has_interrogative_opener(unit: str) -> bool:
    words = _words(_fold(_BULLET_RE.sub("", unit)))
    return bool(words) and words[0] in _INTERROGATIVE_OPENERS


def _caps_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 8:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def classify_passage(
    text: str, *, penalty_factor: float = DEFAULT_PENALTY_FACTOR
) -> PassageShapeDecision:
    """Classe un passage. **En cas de doute : `NORMAL`.**

    `QUESTION` : l'interrogatif DOMINE (≥ 60 % des unités) et il ne reste presque aucun
    contenu affirmatif. Une réponse substantielle contenant une question — citée,
    rhétorique, ou suivie de sa réponse — reste `NORMAL`.

    `TITLE_LIKE` : passage court, SANS ponctuation de fin de phrase, ET portant un
    marqueur typographique de titre (numérotation, libellé de structure, capitales).
    La brièveté seule ne suffit jamais : « Le contrôle est réalisé mensuellement. » est
    court et affirmatif — il reste `NORMAL`.
    """
    factor = normalize_penalty_factor(penalty_factor)
    raw = text or ""
    units = _units(raw)
    if not units:
        return NORMAL_DECISION

    # --- 1. Interrogatif ----------------------------------------------------
    interrogative = [u for u in units if _is_interrogative_unit(u)]
    ratio = len(interrogative) / len(units)
    residual_words = sum(len(_words(u)) for u in units if u not in interrogative)

    if interrogative and ratio >= MIN_INTERROGATIVE_RATIO and residual_words <= MAX_RESIDUAL_AFFIRMATIVE_WORDS:
        reasons = []
        if len(units) == 1:
            reasons.append("single_question")
        else:
            reasons.append("majority_interrogative_sentences")
        if len(interrogative) >= 2 and any(_BULLET_RE.match(u) for u in units):
            reasons.append("interrogative_list")
        if any(_has_interrogative_opener(u) for u in interrogative):
            reasons.append("interrogative_opener")
        return PassageShapeDecision(
            shape=PassageShape.QUESTION,
            confidence=round(ratio, 2),
            penalty_factor=factor,
            reasons=tuple(reasons),
        )

    # --- 2. Assimilable à un titre ------------------------------------------
    words = _words(raw)
    body = raw.strip()
    is_short = 0 < len(words) <= SHORT_PASSAGE_MAX_WORDS
    ends_like_sentence = body.endswith((".", "!", "?", "…"))

    # TROIS signaux convergents, et pas un de plus : passage court, ET sans proposition
    # affirmative (aucune ponctuation de fin de phrase), ET marqueur typographique.
    #
    # Le nombre de LIGNES n'en fait délibérément pas partie : mesuré sur le corpus, les
    # en-têtes réels tiennent sur plusieurs lignes (« 1 POINT MARCHÉ » puis la date, un
    # titre de diapositive suivi de « Extrait »…). L'exiger sur une seule ligne rendait
    # le mécanisme totalement inerte — 0 déclenchement sur 8 000 chunks.
    if is_short and not ends_like_sentence:
        markers: list[str] = []
        if _NUMBERED_PREFIX_RE.match(body):
            markers.append("numbered_title_like")
        if _STRUCTURAL_LABEL_RE.match(_fold(body)):
            markers.append("structural_label")
        if _caps_ratio(body) >= 0.7:
            markers.append("uppercase_heading")
        if markers:
            markers.append("short_nominal_heading")
            return PassageShapeDecision(
                shape=PassageShape.TITLE_LIKE,
                confidence=round(min(1.0, len(markers) / 3), 2),
                penalty_factor=factor,
                reasons=tuple(markers),
            )

    return NORMAL_DECISION


def normalize_penalty_factor(value: Any) -> float:
    """Facteur borné à [0,50 ; 1,00]. Valeur illisible ⇒ neutre (1,00).

    Le repli est NEUTRE et non « par défaut » : une configuration cassée ne doit jamais
    dégrader silencieusement un classement. Le system check, lui, refuse la valeur au
    démarrage.
    """
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return MAX_PENALTY_FACTOR
    if factor != factor:  # NaN
        return MAX_PENALTY_FACTOR
    return max(MIN_PENALTY_FACTOR, min(MAX_PENALTY_FACTOR, factor))


# ---------------------------------------------------------------------------
# Application au pool de candidats
# ---------------------------------------------------------------------------
#: Clé de classement de la diversification (`diversify._rrf_key`). La pénalité agit
#: EXCLUSIVEMENT dessus : le `score` brut reste intact, donc les seuils
#: (`should_answer`) et la confiance faible (`low_confidence`) sont inchangés par
#: construction.
_RANKING_FIELD = "fusion_score"
_ORIGINAL_FIELD = "fusion_score_before_penalty"


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ranking_key(chunk: dict[str, Any]) -> tuple[float, float]:
    return (_f(chunk.get(_RANKING_FIELD)), _f(chunk.get("score")))


def apply_structural_penalty(
    chunks: list[dict[str, Any]],
    *,
    factor: float,
    top_k: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Réordonne le pool en dévalorisant le bruit structurel. NON DESTRUCTIF.

    Aucun candidat n'est retiré : seul l'ordre change, et uniquement via la clé de
    classement de la diversification. Le score d'origine est conservé sur chaque
    candidat (`fusion_score_before_penalty`) pour l'audit.

    Renvoie ``(pool réordonné, agrégats)``. Les agrégats ne contiennent que des
    compteurs — jamais de texte documentaire.
    """
    normalized = normalize_penalty_factor(factor)
    audit: dict[str, Any] = {
        "applied": False,
        "factor": normalized,
        "candidates": len(chunks or []),
        "question_detected": 0,
        "title_like_detected": 0,
        "candidates_penalized": 0,
    }
    if not chunks:
        return list(chunks or []), audit
    if normalized >= MAX_PENALTY_FACTOR:
        # Neutre : on ne touche à rien, pas même à l'ordre.
        return list(chunks), audit

    # Pool mono-modalité : `fusion_score` est absent et la diversification retombe sur
    # `score`. On matérialise alors la clé pour TOUS les candidats (facteur 1,00 pour
    # les passages normaux) : l'ordre relatif est conservé, seule la pénalité le change.
    has_ranking_field = any(chunk.get(_RANKING_FIELD) is not None for chunk in chunks)

    out: list[dict[str, Any]] = []
    for chunk in chunks:
        decision = classify_passage(chunk.get("text") or "", penalty_factor=normalized)
        updated = dict(chunk)
        base = _f(chunk.get(_RANKING_FIELD)) if has_ranking_field else _f(chunk.get("score"))
        if decision.penalized:
            audit["candidates_penalized"] += 1
            if decision.shape is PassageShape.QUESTION:
                audit["question_detected"] += 1
            else:
                audit["title_like_detected"] += 1
            updated["passage_shape"] = decision.shape.value
            updated["structural_penalty_applied"] = True
            updated["structural_penalty_factor"] = decision.penalty_factor
            updated["structural_penalty_reasons"] = list(decision.reasons)
            updated[_ORIGINAL_FIELD] = base
            # Facteur UNIQUE : un passage à la fois interrogatif et assimilable à un
            # titre n'est pénalisé qu'une seule fois (jamais 0,75 × 0,75).
            updated[_RANKING_FIELD] = base * decision.penalty_factor
        elif not has_ranking_field:
            updated[_RANKING_FIELD] = base
        out.append(updated)

    audit["applied"] = audit["candidates_penalized"] > 0
    reordered = sort_candidates(out, relevance_key=_ranking_key)
    if top_k > 0:
        # Compteurs AVANT/APRÈS sur la fenêtre réellement transmise : c'est la seule
        # mesure qui dise si la pénalité a libéré des créneaux de contexte.
        audit["top_k_noise_before"] = _noise_count(chunks[:top_k], normalized)
        audit["top_k_noise_after"] = _noise_count(reordered[:top_k], normalized)

    return reordered, audit


def _noise_count(chunks: list[dict[str, Any]], factor: float) -> int:
    return sum(
        1
        for chunk in chunks
        if classify_passage(chunk.get("text") or "", penalty_factor=factor).penalized
    )

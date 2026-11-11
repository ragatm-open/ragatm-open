"""Résolution d'une colonne depuis la formulation de l'utilisateur.

Le mode d'échec que ce module existe pour empêcher : **une réponse numériquement
exacte, calculée sur la mauvaise colonne.** Elle est indétectable pour l'utilisateur —
le chiffre a l'air juste — et c'est pourquoi la spécification la classe critique.

D'où deux principes :

1. **Priorité stricte et vérifiable.** Exact > normalisé > alias > lexical > flou. Un
   niveau ne s'exécute que si le précédent n'a rien donné d'unique. Le niveau qui a
   tranché est conservé (`match_level`) et exposé : une résolution doit pouvoir
   s'expliquer.

2. **La dominance est requise.** Le meilleur candidat doit dépasser le second d'une
   marge configurable. Deux colonnes proches (« Montant net comptable » /
   « Montant net payé ») ne se départagent pas toutes seules : c'est une
   `clarification_required`, jamais un choix par position.

RapidFuzz n'intervient qu'au niveau 5, ne peut jamais élire seul une colonne ambiguë,
et son score est plafonné sous celui d'une correspondance exacte : un « à peu près »
ne doit pas pouvoir battre un « exactement ».
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

from apps.rag_core.tabular import errors as E
from apps.rag_core.tabular import types as T

#: Niveaux de correspondance, du plus fort au plus faible. Exposés dans la provenance.
LEVEL_EXACT = "exact_header"
LEVEL_NORMALIZED = "normalized_header"
LEVEL_ALIAS = "business_alias"
LEVEL_LEXICAL = "lexical"
LEVEL_FUZZY = "fuzzy"

#: Score attribué par niveau. L'écart entre `exact` et `fuzzy` est délibérément large :
#: aucune combinaison de correspondances floues ne doit rattraper une exacte.
_LEVEL_SCORE = {
    LEVEL_EXACT: 1.00,
    LEVEL_NORMALIZED: 0.95,
    LEVEL_ALIAS: 0.90,
    LEVEL_LEXICAL: 0.80,
    LEVEL_FUZZY: 0.70,
}

#: Plafond du score flou. Un flou parfait (100) vaut 0.70 : sous le seuil lexical.
_FUZZY_CEILING = 0.70
#: Sous ce ratio RapidFuzz, le candidat n'est même pas proposé.
_FUZZY_FLOOR_RATIO = 75.0

#: Alias métier. Volontairement CONSERVATEUR : un alias erroné produit exactement le
#: défaut qu'on cherche à éviter. On n'y met que des équivalences non ambiguës.
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "ca": ("chiffre_d_affaires", "chiffre_affaires", "ca"),
    "chiffre_d_affaires": ("ca", "chiffre_affaires"),
    "montant": ("montant", "somme"),
    "quantite": ("qte", "quantite", "nombre"),
    "prix_unitaire": ("pu", "prix_unitaire"),
    "date": ("date",),
    "reference": ("ref", "reference"),
    "region": ("region",),
}


@dataclass(frozen=True)
class ColumnCandidate:
    """Une colonne du catalogue, telle que le résolveur la voit."""

    column_id: UUID
    original_name: str
    normalized_name: str
    position: int
    inferred_type: str
    unit: str = ""


@dataclass
class ColumnMatch:
    candidate: ColumnCandidate
    score: float
    level: str
    reasons: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    s = (text or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", _norm(text)) if len(t) > 1}


def _fuzzy_ratio(a: str, b: str) -> float:
    """Ratio RapidFuzz, ou repli stdlib si la bibliothèque est absente.

    L'absence de RapidFuzz **dégrade** la résolution, elle ne la casse pas : le moteur
    doit rester fonctionnel sans dépendance optionnelle. Le repli est plus lent mais
    donne un ordre de grandeur comparable.
    """
    try:
        from rapidfuzz import fuzz
        return float(fuzz.token_set_ratio(a, b))
    except ImportError:  # pragma: no cover - RapidFuzz est une dépendance cœur
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() * 100.0


def resolve_column(
    phrase: str,
    candidates: list[ColumnCandidate],
    *,
    min_score: float,
    min_margin: float,
    required_types: frozenset[str] | None = None,
    aliases: dict[str, tuple[str, ...]] | None = None,
    what: str = "colonne",
) -> ColumnCandidate:
    """Élit UNE colonne, ou lève une demande de clarification.

    `required_types` filtre par compatibilité AVANT le classement : demander une somme
    n'a de sens que sur une colonne numérique, et proposer une colonne texte comme
    candidate à une somme n'aiderait personne à trancher.
    """
    if not candidates:
        raise E.TabularError(E.ERR_COLUMN_NOT_FOUND, f"aucune {what} disponible")

    pool = candidates
    if required_types:
        typed = [c for c in candidates if c.inferred_type in required_types]
        if not typed:
            raise E.TabularError(
                E.ERR_INCOMPATIBLE_COLUMN_TYPE,
                f"aucune {what} compatible avec l'opération demandée",
                available=[c.original_name for c in candidates[:10]])
        pool = typed

    matches = _score_all(phrase, pool, aliases or DEFAULT_ALIASES)
    matches = [m for m in matches if m.score >= min_score]

    if not matches:
        raise E.TabularClarificationRequired(
            E.ERR_COLUMN_NOT_FOUND,
            f"aucune {what} ne correspond à « {phrase} »",
            question=f"Quelle {what} souhaitez-vous utiliser ?",
            candidates=[c.original_name for c in pool[:10]])

    matches.sort(key=lambda m: (-m.score, m.candidate.normalized_name))
    best = matches[0]

    # Homonymes stricts : deux colonnes portant le même nom normalisé ne peuvent pas
    # être départagées par un score — il est identique par construction.
    homonyms = [m for m in matches
                if m.candidate.normalized_name == best.candidate.normalized_name]
    if len(homonyms) > 1:
        raise E.TabularClarificationRequired(
            E.ERR_DUPLICATE_COLUMN_NAME,
            f"plusieurs colonnes portent le nom « {best.candidate.original_name} »",
            question=f"Quelle {what} souhaitez-vous utiliser ?",
            candidates=[f"{m.candidate.original_name} (position {m.candidate.position})"
                        for m in homonyms])

    if len(matches) > 1:
        margin = best.score - matches[1].score
        if margin < min_margin:
            # Le cas « Montant net comptable » vs « Montant net payé ». Trancher ici
            # produirait une réponse exacte sur la mauvaise colonne.
            close = [m for m in matches if best.score - m.score < min_margin]
            raise E.TabularClarificationRequired(
                E.ERR_AMBIGUOUS_COLUMN,
                f"plusieurs colonnes correspondent à « {phrase} » sans dominance claire",
                question=f"Quelle {what} souhaitez-vous utiliser ?",
                candidates=[m.candidate.original_name for m in close])

    return best.candidate


def _score_all(phrase: str, candidates: list[ColumnCandidate],
               aliases: dict[str, tuple[str, ...]]) -> list[ColumnMatch]:
    want_raw = _norm(phrase)
    want_norm = T.normalize_header(phrase)
    want_tokens = _tokens(phrase)

    out: list[ColumnMatch] = []
    for c in candidates:
        best: ColumnMatch | None = None

        def offer(level: str, score: float, why: str):
            nonlocal best
            if best is None or score > best.score:
                best = ColumnMatch(candidate=c, score=score, level=level, reasons=[why])

        # 1 — en-tête exact
        if _norm(c.original_name) == want_raw:
            offer(LEVEL_EXACT, _LEVEL_SCORE[LEVEL_EXACT], "en-tête identique")

        # 2 — en-tête normalisé
        if c.normalized_name == want_norm and want_norm:
            offer(LEVEL_NORMALIZED, _LEVEL_SCORE[LEVEL_NORMALIZED], "en-tête normalisé")

        # 3 — alias métier
        for key, values in aliases.items():
            if want_norm in (key,) + tuple(values) and c.normalized_name in (key,) + tuple(values):
                offer(LEVEL_ALIAS, _LEVEL_SCORE[LEVEL_ALIAS], f"alias métier « {key} »")
                break

        # 4 — recouvrement lexical : tous les mots demandés présents dans l'en-tête.
        #     « montant net » ⊂ « Montant net comptable ». Le score décroît avec les
        #     mots EN TROP côté colonne : « Montant net » bat « Montant net payé ».
        cand_tokens = _tokens(c.original_name)
        if want_tokens and want_tokens <= cand_tokens:
            extra = len(cand_tokens - want_tokens)
            offer(LEVEL_LEXICAL, _LEVEL_SCORE[LEVEL_LEXICAL] - 0.02 * extra,
                  "tous les termes demandés sont présents")

        # 5 — flou (RapidFuzz), SIGNAL SECONDAIRE. Plafonné sous le niveau lexical :
        #     il peut départager, jamais élire contre une correspondance structurelle.
        ratio = _fuzzy_ratio(want_raw, _norm(c.original_name))
        if ratio >= _FUZZY_FLOOR_RATIO:
            offer(LEVEL_FUZZY, _FUZZY_CEILING * (ratio / 100.0),
                  f"proximité lexicale {ratio:.0f}%")

        if best is not None:
            out.append(best)
    return out

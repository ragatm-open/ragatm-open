"""Politique d'admission du retrieval — module PUR (lot 4).

Aucune dépendance Django, aucun accès base, aucun appel réseau. Il ne connaît que
des nombres et des chaînes, ce qui le rend testable exhaustivement aux bornes.

## Ce que ce module corrige

`domain/policies.py::should_answer` décide par un **seuil absolu, en booléen**, et
avec un ordre de priorité qui a une conséquence rarement énoncée : *dès qu'un signal
FTS existe, `RAG_MIN_SCORE` n'est plus appliqué du tout*. En mode hybride — la
configuration réelle des deux environnements — la décision se réduit donc à
`ts_rank >= RAG_MIN_TEXT_RANK`, soit 0,02. Le seuil vectoriel, lui, vaut 0,08 : à ce
niveau, il n'écarte pratiquement rien.

Mesuré au lot 3 sur le corpus pilote : les scores des positifs et des négatifs se
**recouvrent**. Un seuil absolu seul ne peut donc pas les séparer, quel que soit le
point où on le place. Ce qui les distingue mieux, c'est la **séparation** entre le
premier et le deuxième résultat : un corpus qui contient vraiment la réponse la fait
ressortir ; un corpus qui ne la contient pas rend une grappe de passages également
médiocres.

## Trois états, pas deux

Un booléen force à ranger l'ambigu avec le certain ou avec le vide. ``UNCERTAIN``
existe pour que le pipeline puisse répondre prudemment plutôt que d'avoir à choisir
entre affirmer et se taire. Il n'est **jamais** compté comme une acceptation.

## Nature des scores — à ne pas mélanger

- ``top_vector_score`` : ``1/(1+distance_cosinus)`` — borné dans ]0, 1], monotone
  croissant avec la similarité. Ce n'est PAS un cosinus : le seuil 0,76 équivaut à
  ``cos >= 0,684``.
- ``top_text_rank``    : ``ts_rank`` PostgreSQL — non borné en pratique, valeurs
  typiques 0,01 à 0,1. Incommensurable avec le précédent.

La marge n'est calculée **qu'entre scores de même nature** (vectoriel avec
vectoriel). Comparer une marge vectorielle à un ts_rank n'aurait aucun sens, et le
module ne l'autorise pas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# --- Décisions (valeurs STABLES : trace, logs, métriques, API) ---------------
ACCEPT = "accept"
UNCERTAIN = "uncertain"
REJECT = "reject"

# --- Politiques disponibles --------------------------------------------------
#: Comportement historique, à l'octet près. Conservé pour le rollback ET pour la
#: comparaison : sans bras de référence, une amélioration n'est pas mesurable.
POLICY_LEGACY = "legacy"
#: Seuil absolu, mais appliqué aux DEUX modalités (contrairement à `legacy`, où le
#: FTS court-circuite le seuil vectoriel).
POLICY_SCORE_ONLY = "score_only"
#: Seuil absolu + séparation top-1/top-2. Deux états seulement.
POLICY_SCORE_AND_MARGIN = "score_and_margin"
#: Seuil + marge + zone d'incertitude explicite. Trois états.
POLICY_TIERED = "tiered"

ADMISSION_POLICIES: frozenset[str] = frozenset(
    {POLICY_LEGACY, POLICY_SCORE_ONLY, POLICY_SCORE_AND_MARGIN, POLICY_TIERED}
)

# --- Traitement du signal lexical -------------------------------------------
#: `independent` : un FTS suffisant suffit à admettre (comportement historique).
TEXT_POLICY_INDEPENDENT = "independent"
#: `combined`    : le FTS corrobore, il ne décide pas seul en présence d'un signal
#: vectoriel faible. C'est la correction du contournement décrit plus haut.
TEXT_POLICY_COMBINED = "combined"
#: `disabled`    : le lexical n'entre pas dans l'admission (diagnostic).
TEXT_POLICY_DISABLED = "disabled"

TEXT_ADMISSION_POLICIES: frozenset[str] = frozenset(
    {TEXT_POLICY_INDEPENDENT, TEXT_POLICY_COMBINED, TEXT_POLICY_DISABLED}
)

# --- Modes de retrieval ------------------------------------------------------
MODE_VECTOR = "vector"
MODE_TEXT = "text"
MODE_HYBRID = "hybrid"
MODE_EMPTY = "empty"

# --- Raisons (valeurs STABLES) ----------------------------------------------
REASON_NO_RESULT = "no_result"
REASON_INSUFFICIENT_SOURCES = "insufficient_sources"
REASON_FOREIGN_PIPELINE = "foreign_pipeline"
REASON_VECTOR_CONFIDENT = "vector_confident"
REASON_VECTOR_AMBIGUOUS = "vector_ambiguous"
REASON_VECTOR_TOO_WEAK = "vector_too_weak"
REASON_VECTOR_NO_SEPARATION = "vector_no_separation"
REASON_TEXT_CONFIDENT = "text_confident"
REASON_TEXT_TOO_WEAK = "text_too_weak"
REASON_TEXT_ONLY_UNCORROBORATED = "text_only_uncorroborated"
REASON_HYBRID_CONFIRMED = "hybrid_confirmed"
REASON_HYBRID_AMBIGUOUS = "hybrid_ambiguous"
REASON_HYBRID_WEAK = "hybrid_weak"
REASON_LEGACY = "legacy_should_answer"


def _finite(value: Any) -> float:
    """Coercition SÛRE. Une valeur non finie ou non numérique vaut 0.

    Volontairement silencieuse : un score invalide est une donnée dégradée, pas un
    bug du décideur. Le traiter comme nul le fait tomber sous n'importe quel seuil,
    donc vers le refus — la direction prudente. Un NaN propagé, lui, rendrait toutes
    les comparaisons fausses **sans** lever, et l'admission deviendrait aléatoire.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


@dataclass(frozen=True)
class RetrievalEvidence:
    """Ce que le retrieval a réellement observé. Aucune décision ici."""

    source_count: int = 0
    top_vector_score: float = 0.0
    second_vector_score: float = 0.0
    third_vector_score: float = 0.0
    top_text_rank: float = 0.0
    top_any_score: float = 0.0
    median_vector_score: float = 0.0
    distinct_document_count: int = 0
    pipeline_family: str = ""
    expected_pipeline_family: str = ""

    @property
    def vector_margin(self) -> float:
        """Séparation top-1/top-2, entre scores de MÊME nature. Jamais négative.

        Un seul résultat vectoriel => marge égale au score lui-même : il n'y a aucun
        concurrent, la séparation est totale. Rendre 0 ici ferait basculer en
        ``UNCERTAIN`` tous les corpus ne contenant qu'un passage pertinent — un
        rejet dû à la forme du corpus, pas à la qualité de la réponse.
        """
        if self.top_vector_score <= 0.0:
            return 0.0
        if self.second_vector_score <= 0.0:
            return self.top_vector_score
        return max(0.0, self.top_vector_score - self.second_vector_score)

    @property
    def margin_top1_top3(self) -> float:
        if self.top_vector_score <= 0.0:
            return 0.0
        if self.third_vector_score <= 0.0:
            return self.top_vector_score
        return max(0.0, self.top_vector_score - self.third_vector_score)

    @property
    def retrieval_mode(self) -> str:
        has_vec = self.top_vector_score > 0.0
        has_txt = self.top_text_rank > 0.0
        if self.source_count <= 0:
            return MODE_EMPTY
        if has_vec and has_txt:
            return MODE_HYBRID
        if has_vec:
            return MODE_VECTOR
        if has_txt:
            return MODE_TEXT
        return MODE_EMPTY

    @property
    def pipeline_is_consistent(self) -> bool:
        """Sans famille attendue déclarée, on n'affirme rien : espace non gouverné."""
        if not self.expected_pipeline_family:
            return True
        return self.pipeline_family == self.expected_pipeline_family

    def to_log_fields(self) -> dict[str, Any]:
        """Champs journalisables. Ni question, ni texte de chunk, ni vecteur."""
        return {
            "retrieval_mode": self.retrieval_mode,
            "source_count": int(self.source_count),
            "distinct_document_count": int(self.distinct_document_count),
            "top_vector_score": round(self.top_vector_score, 6),
            "second_vector_score": round(self.second_vector_score, 6),
            "vector_margin": round(self.vector_margin, 6),
            "top_text_rank": round(self.top_text_rank, 6),
            "pipeline_family": self.pipeline_family,
        }


@dataclass(frozen=True)
class AdmissionThresholds:
    """Seuils EFFECTIFS. Validés à la construction : un seuil incohérent est refusé.

    Refuser plutôt que corriger : ramener silencieusement une valeur dans les bornes
    ferait tourner une politique que l'exploitant ne croit pas avoir configurée.
    """

    policy: str = POLICY_LEGACY
    min_sources: int = 1
    min_score: float = 0.76
    min_margin: float = 0.0
    uncertain_score: float = 0.0
    min_text_rank: float = 0.0
    text_policy: str = TEXT_POLICY_INDEPENDENT

    def __post_init__(self) -> None:
        if self.policy not in ADMISSION_POLICIES:
            raise ValueError(
                f"RAG_ADMISSION_POLICY inconnue: {self.policy!r} "
                f"(attendu: {', '.join(sorted(ADMISSION_POLICIES))})"
            )
        if self.text_policy not in TEXT_ADMISSION_POLICIES:
            raise ValueError(
                f"RAG_TEXT_ADMISSION_POLICY inconnue: {self.text_policy!r} "
                f"(attendu: {', '.join(sorted(TEXT_ADMISSION_POLICIES))})"
            )
        if int(self.min_sources) < 1:
            raise ValueError("RAG_MIN_SOURCES doit être un entier strictement positif")
        for name, v in (("RAG_MIN_SCORE", self.min_score),
                        ("RAG_MIN_MARGIN", self.min_margin),
                        ("RAG_UNCERTAIN_SCORE", self.uncertain_score)):
            f = float(v)
            if not math.isfinite(f) or not (0.0 <= f <= 1.0):
                raise ValueError(f"{name} doit être un flottant fini dans [0, 1] (reçu {v!r})")
        if self.uncertain_score > self.min_score:
            raise ValueError(
                f"RAG_UNCERTAIN_SCORE ({self.uncertain_score}) doit être <= RAG_MIN_SCORE "
                f"({self.min_score}) : la zone d'incertitude vit SOUS le seuil d'acceptation."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy, "min_sources": int(self.min_sources),
            "min_score": float(self.min_score), "min_margin": float(self.min_margin),
            "uncertain_score": float(self.uncertain_score),
            "min_text_rank": float(self.min_text_rank), "text_policy": self.text_policy,
        }


@dataclass(frozen=True)
class AdmissionDecision:
    """Verdict + sa justification. `status` ∈ {accept, uncertain, reject}."""

    status: str
    reason: str
    policy_name: str
    thresholds: dict[str, Any] = field(default_factory=dict)
    observed_scores: dict[str, Any] = field(default_factory=dict)
    fallback_allowed: bool = False

    @property
    def is_accept(self) -> bool:
        return self.status == ACCEPT

    @property
    def is_answerable(self) -> bool:
        """ACCEPT ou UNCERTAIN. `UNCERTAIN` n'est PAS une acceptation.

        Distinct de ``is_accept`` à dessein : le pipeline peut répondre prudemment
        sur un UNCERTAIN, mais aucune métrique de faux positifs ne doit le compter
        comme une admission.
        """
        return self.status in (ACCEPT, UNCERTAIN)

    def to_log_fields(self) -> dict[str, Any]:
        return {"decision": self.status, "reason": self.reason, "policy": self.policy_name}


# ---------------------------------------------------------------------------
# Décideur
# ---------------------------------------------------------------------------
def decide(evidence: RetrievalEvidence, thresholds: AdmissionThresholds) -> AdmissionDecision:
    """Applique la politique configurée. Fonction PURE, sans effet de bord.

    Ordre des conditions, du plus structurel au plus circonstanciel :

    1. cohérence de pipeline  — un vecteur d'un autre modèle n'est pas comparable ;
    2. nombre de sources      — en dessous du minimum, rien à évaluer ;
    3. politique              — la logique propre à chaque mode.

    Les deux premières s'appliquent à TOUTES les politiques, y compris `legacy` :
    ce sont des invariants de gouvernance, pas des réglages de qualité.
    """
    e, t = evidence, thresholds
    obs = e.to_log_fields()

    def _d(status: str, reason: str, *, fallback: bool = False) -> AdmissionDecision:
        return AdmissionDecision(
            status=status, reason=reason, policy_name=t.policy,
            thresholds=t.to_dict(), observed_scores=obs, fallback_allowed=fallback,
        )

    if not e.pipeline_is_consistent:
        return _d(REJECT, REASON_FOREIGN_PIPELINE)
    if e.source_count <= 0:
        return _d(REJECT, REASON_NO_RESULT)
    if e.source_count < int(t.min_sources):
        return _d(REJECT, REASON_INSUFFICIENT_SOURCES)

    if t.policy == POLICY_LEGACY:
        return _legacy(e, t, _d)
    if t.policy == POLICY_SCORE_ONLY:
        return _score_only(e, t, _d)
    if t.policy == POLICY_SCORE_AND_MARGIN:
        return _score_and_margin(e, t, _d)
    return _tiered(e, t, _d)


def _legacy(e: RetrievalEvidence, t: AdmissionThresholds, _d) -> AdmissionDecision:
    """Reproduit `should_answer` EXACTEMENT : FTS prioritaire, seuil absolu, booléen.

    Y compris son défaut : dès qu'un ts_rank existe, le seuil vectoriel n'est jamais
    consulté. C'est ce qui rend le rollback réel plutôt qu'« équivalent ».
    """
    if e.top_text_rank > 0.0:
        ok = e.top_text_rank >= t.min_text_rank
        return _d(ACCEPT if ok else REJECT, REASON_LEGACY)
    if e.top_vector_score > 0.0:
        ok = e.top_vector_score >= t.min_score
        return _d(ACCEPT if ok else REJECT, REASON_LEGACY)
    ok = e.top_any_score >= t.min_score
    return _d(ACCEPT if ok else REJECT, REASON_LEGACY)


def _text_verdict(e: RetrievalEvidence, t: AdmissionThresholds) -> str | None:
    """Verdict du seul signal lexical, ou None si le lexical ne participe pas."""
    if t.text_policy == TEXT_POLICY_DISABLED or e.top_text_rank <= 0.0:
        return None
    return ACCEPT if e.top_text_rank >= t.min_text_rank else REJECT


def _score_only(e, t, _d) -> AdmissionDecision:
    """Seuil absolu sur les DEUX modalités — sans le court-circuit de `legacy`."""
    txt = _text_verdict(e, t)
    if e.top_vector_score > 0.0:
        if e.top_vector_score >= t.min_score:
            return _d(ACCEPT, REASON_VECTOR_CONFIDENT)
        if txt == ACCEPT and t.text_policy == TEXT_POLICY_INDEPENDENT:
            return _d(ACCEPT, REASON_TEXT_CONFIDENT)
        return _d(REJECT, REASON_VECTOR_TOO_WEAK)
    if txt == ACCEPT:
        return _d(ACCEPT, REASON_TEXT_CONFIDENT)
    if txt == REJECT:
        return _d(REJECT, REASON_TEXT_TOO_WEAK)
    return _d(REJECT, REASON_VECTOR_TOO_WEAK)


def _score_and_margin(e, t, _d) -> AdmissionDecision:
    """Seuil ET séparation. Deux états : ce qui n'est pas net est refusé."""
    txt = _text_verdict(e, t)
    if e.top_vector_score > 0.0:
        if e.top_vector_score >= t.min_score and e.vector_margin >= t.min_margin:
            return _d(ACCEPT, REASON_VECTOR_CONFIDENT)
        if e.top_vector_score >= t.min_score:
            return _d(REJECT, REASON_VECTOR_NO_SEPARATION)
        if txt == ACCEPT and t.text_policy == TEXT_POLICY_INDEPENDENT:
            return _d(ACCEPT, REASON_TEXT_CONFIDENT)
        return _d(REJECT, REASON_VECTOR_TOO_WEAK)
    if txt == ACCEPT:
        return _d(ACCEPT, REASON_TEXT_CONFIDENT)
    return _d(REJECT, REASON_TEXT_TOO_WEAK)


def _tiered(e, t, _d) -> AdmissionDecision:
    """Trois états, et le lexical CORROBORE au lieu de contourner.

    Le point qui distingue cette politique de `legacy` : un ts_rank suffisant ne
    suffit plus à admettre quand le vectoriel, lui, est faible. Il rend
    ``UNCERTAIN`` — la réponse existe peut-être, mais rien ne le confirme.
    """
    txt = _text_verdict(e, t)
    text_confident = txt == ACCEPT
    text_present = txt is not None

    vec_present = e.top_vector_score > 0.0
    vec_strong = vec_present and e.top_vector_score >= t.min_score
    vec_separated = e.vector_margin >= t.min_margin
    vec_plausible = vec_present and e.top_vector_score >= t.uncertain_score

    if not vec_present:
        # Lexical seul. `independent` l'accepte, `combined` ne le fait pas : un
        # ts_rank élevé signale une correspondance de MOTS, pas de sens.
        if text_confident:
            if t.text_policy == TEXT_POLICY_INDEPENDENT:
                return _d(ACCEPT, REASON_TEXT_CONFIDENT)
            return _d(UNCERTAIN, REASON_TEXT_ONLY_UNCORROBORATED, fallback=True)
        return _d(REJECT, REASON_TEXT_TOO_WEAK)

    if vec_strong and vec_separated:
        if text_present and not text_confident and t.text_policy == TEXT_POLICY_COMBINED:
            # Conflit : le vectoriel affirme, le lexical ne corrobore pas.
            return _d(UNCERTAIN, REASON_HYBRID_AMBIGUOUS, fallback=True)
        return _d(ACCEPT, REASON_HYBRID_CONFIRMED if text_confident else REASON_VECTOR_CONFIDENT)

    if vec_strong and not vec_separated:
        # Bon score, mauvaise séparation : une grappe de passages équivalents. C'est
        # exactement la signature d'un corpus qui ne contient PAS la réponse.
        return _d(UNCERTAIN, REASON_VECTOR_NO_SEPARATION, fallback=True)

    if vec_plausible:
        if text_confident and t.text_policy != TEXT_POLICY_DISABLED:
            return _d(UNCERTAIN, REASON_HYBRID_AMBIGUOUS, fallback=True)
        return _d(UNCERTAIN, REASON_VECTOR_AMBIGUOUS, fallback=True)

    if text_confident and t.text_policy == TEXT_POLICY_INDEPENDENT:
        return _d(ACCEPT, REASON_TEXT_CONFIDENT)
    if text_confident:
        return _d(UNCERTAIN, REASON_TEXT_ONLY_UNCORROBORATED, fallback=True)
    return _d(REJECT, REASON_HYBRID_WEAK if text_present else REASON_VECTOR_TOO_WEAK)


# ---------------------------------------------------------------------------
# Construction de l'évidence depuis les candidats du retrieval
# ---------------------------------------------------------------------------
def build_evidence(
    chunks: list[dict[str, Any]] | None,
    *,
    expected_pipeline_family: str = "",
) -> RetrievalEvidence:
    """Extrait l'évidence d'une liste de candidats. Ne classe pas, ne décide pas.

    Les scores vectoriels sont triés ICI plutôt que de faire confiance à l'ordre
    d'entrée : après fusion RRF et diversification, la liste est ordonnée par
    ``fusion_score``, pas par score vectoriel. Prendre `chunks[1]` pour le second
    score vectoriel donnerait une marge fausse — et une marge fausse est pire qu'une
    marge absente, parce qu'elle décide quand même.
    """
    rows = [c for c in (chunks or []) if isinstance(c, dict)]
    if not rows:
        return RetrievalEvidence(expected_pipeline_family=expected_pipeline_family)

    vec = sorted((_finite(c.get("vector_score")) for c in rows), reverse=True)
    vec = [v for v in vec if v > 0.0]
    if not vec:
        # Liste mono-modalité pré-fusion : `score` porte alors le vectoriel.
        vec = sorted((_finite(c.get("score")) for c in rows if "text_score" not in c), reverse=True)
        vec = [v for v in vec if v > 0.0]
    txt = max((_finite(c.get("text_score")) for c in rows), default=0.0)
    if txt <= 0.0:
        txt = max((_finite(c.get("text_rank")) for c in rows), default=0.0)
    any_score = max((_finite(c.get("score")) for c in rows), default=0.0)

    fams = {str(c.get("pipeline_family") or "") for c in rows}
    fams.discard("")
    docs = {str((c.get("source") or {}).get("document_id") or c.get("document_id") or "") for c in rows}
    docs.discard("")

    return RetrievalEvidence(
        source_count=len(rows),
        top_vector_score=vec[0] if vec else 0.0,
        second_vector_score=vec[1] if len(vec) > 1 else 0.0,
        third_vector_score=vec[2] if len(vec) > 2 else 0.0,
        top_text_rank=txt,
        top_any_score=any_score,
        median_vector_score=(vec[len(vec) // 2] if vec else 0.0),
        distinct_document_count=len(docs),
        # Plusieurs familles dans un même lot = incohérence : on rend la valeur
        # observée telle quelle, et `pipeline_is_consistent` la refusera.
        pipeline_family=(next(iter(fams)) if len(fams) == 1 else ("|".join(sorted(fams)) if fams else "")),
        expected_pipeline_family=expected_pipeline_family,
    )

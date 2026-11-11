"""Mécanisme d'*answerability* déterministe, sans LLM (P1.8).

Distinction fondamentale :

    pertinence retrieval  ≠  answerability

Un chunk peut être *thématiquement proche* de la question sans contenir la **preuve
demandée** (la mesure / valeur / propriété / relation). Le seuil `RAG_MIN_SCORE` seul ne
sépare pas ces cas (no-answer plausibles) : sur `demo-fr-extended`, no_answer_accuracy
plafonne à ~0.13. Ce module décide, à partir de la question et des chunks récupérés
(texte + score), si les preuves suffisent — **déterministe, explicable, sérialisable**,
aucun appel au LLM de génération.

Signal central : **couverture de preuve pondérée par l'IDF**. Les mots *cadres* de la
question (« quel », « conditions », …) pèsent peu ; le **terme discriminant** (la
propriété demandée : « sla », « prix », « consommation », …) pèse fort. Si ce terme est
absent des preuves — alors que le sujet, lui, est présent — la question est déclarée
no-answer. Complété par un contrôle de **valeur structurante** (port/IPv4/version/…) et
d'**identifiant fort**.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Sequence

STRATEGY_NONE = "none"
STRATEGY_RETRIEVAL_THRESHOLD = "retrieval_threshold"
STRATEGY_EVIDENCE_RULES = "evidence_rules"
STRATEGY_COMPOSITE = "composite"
STRATEGIES = (STRATEGY_NONE, STRATEGY_RETRIEVAL_THRESHOLD, STRATEGY_EVIDENCE_RULES, STRATEGY_COMPOSITE)

ANSWERABILITY_VERSION = "1"

# Raisons de refus stables.
REASON_NO_CANDIDATE = "no_retrieval_candidate"
REASON_SCORE_LOW = "retrieval_score_too_low"
REASON_MISSING_IDENTIFIER = "missing_requested_identifier"
REASON_MISSING_VALUE = "missing_structured_value"
REASON_MISSING_PROPERTY = "missing_requested_property"
REASON_THEMATIC_ONLY = "only_thematic_overlap"
REASON_MULTI_SOURCE = "insufficient_multi_source_evidence"

# --- Normalisation / tokenisation (pliage d'accents pour la robustesse morphologique) --
_APOS = {"’": "'", "ʼ": "'", "‘": "'", "`": "'"}


def fold(text: str) -> str:
    """Minuscules + NFKD sans accents + apostrophes unifiées (matching robuste)."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    for a, b in _APOS.items():
        t = t.replace(a, b)
    return t.lower()


# Tokens « mots » : on découpe sur tout non-alphanumérique (donc sur les tirets et les
# élisions « sont-elles », « d'ollama »). Les identifiants (REF-2026-04) sont extraits
# séparément par _ID_PATTERNS, qui conservent les tirets.
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(fold(text))


# Mots vides + mots « cadres » d'une question (non discriminants).
STOPWORDS: frozenset[str] = frozenset(
    """
    le la les un une des du de d au aux a l et ou ni mais donc or car que qui quoi dont
    ou où quel quelle quels quelles est sont etre est-ce ce cet cette ces son sa ses
    leur leurs mon ma mes ton ta tes notre nos votre vos on il elle ils elles je tu nous
    vous se sur sous dans par pour avec sans vers chez entre depuis pendant apres avant
    comment combien quand pourquoi lequel laquelle en y ne pas plus moins tres tout tous
    toute toutes meme aussi comme si oui non peut on doit faut quel-est
    utilise utiliser ecoute ecouter sert servir correspond correspondre represente
    represente-t possible fait faire donne donner trouve trouver existe exister propose
    proposer permet permettre concerne concerner porte porter contient contenir
    conditions condition procedure procedures etape etapes etapes marche facon maniere
    moyen cas exemple information informations detail details valeur role roles
    quels-sont quelle-est mise a-jour numero
    confondre distinguer celui celle ceux celles plutot versus contrairement respectivement
    detaille detailler precise preciser indique indiquer
    """.split()
)


def content_terms(question: str) -> list[str]:
    """Termes de contenu (discriminants candidats) : tokens hors stopwords, len >= 3."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens(question):
        if len(tok) < 3 or tok in STOPWORDS:
            continue
        if tok.isdigit() and len(tok) < 3:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# --- Type de valeur demandé + détecteurs de preuve dans les chunks -------------
_VALUE_REQUEST_PATTERNS = (
    ("ipv4", re.compile(r"\badresse ip\b|\bip\b", re.I)),
    ("port", re.compile(r"\bport\b", re.I)),
    ("version", re.compile(r"\bversion\b", re.I)),
    ("file", re.compile(r"\bfichier\b", re.I)),
    ("ref", re.compile(r"\bref[-_]?\d|référence|reference\b", re.I)),
)
# Détecteurs de VALEUR dans les preuves (types fiables uniquement).
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
_FILE_RE = re.compile(r"\b[\w-]+\.(?:ya?ml|json|pdf|txt|csv|md|conf|ini|cfg|py|js|toml|xlsx?|docx?|pptx?)\b", re.I)
_REF_RE = re.compile(r"\b[a-z]{2,}[-_]\d{2,}(?:[-_][a-z0-9]+)*\b", re.I)
_PORT_RE = re.compile(r"\b\d{3,5}\b")


# Mots « type de valeur » — exclus des termes ENTITÉ pour la co-occurrence valeur↔entité.
_VALUE_WORDS: frozenset[str] = frozenset(
    "port ports adresse ip version fichier reference ref numero".split()
)


def requested_value_type(question: str) -> str:
    """Type de valeur structurante demandé (ou 'none'). IPv4 avant port (ambigu)."""
    for name, pat in _VALUE_REQUEST_PATTERNS:
        if pat.search(question):
            return name
    return "none"


def _value_present(value_type: str, evidence: str) -> bool:
    if value_type == "none":
        return True
    if value_type == "ipv4":
        return bool(_IPV4_RE.search(evidence))
    if value_type == "port":
        return bool(_PORT_RE.search(evidence))
    if value_type == "version":
        return bool(_VERSION_RE.search(evidence))
    if value_type == "file":
        return bool(_FILE_RE.search(evidence))
    if value_type == "ref":
        return bool(_REF_RE.search(evidence))
    return True


# Identifiants forts présents dans la QUESTION (à retrouver dans les preuves).
_ID_PATTERNS = (
    re.compile(r"\b[a-z]{2,}[-_]\d{2,}(?:[-_][a-z0-9]+)*\b", re.I),   # REF-2026-04, 2020-1…
    re.compile(r"\bseries [a-z]-\d\b", re.I),                          # Series A-1
    re.compile(r"\b[a-z]+(?:_[a-z]+){2,}\b", re.I),                    # AI_GATEWAY_TIMEOUT_SECONDS
    re.compile(r"\b\d{3,5}\b"),                                        # 8100, 11434
    re.compile(r"\b[\w-]+\.(?:ya?ml|json|conf|cfg)\b", re.I),          # config.yaml
    re.compile(r"\b\d{4}_[a-z0-9_]+\b", re.I),                         # 0028_alter_...
)


def question_identifiers(question: str) -> list[str]:
    q = fold(question)
    found: list[str] = []
    for pat in _ID_PATTERNS:
        for m in pat.finditer(q):
            v = m.group(0)
            if v not in found:
                found.append(v)
    return found


# --- Signaux --------------------------------------------------------------------
@dataclass
class AnswerabilitySignals:
    retrieval_score: float
    score_margin: float
    weighted_coverage: float
    lexical_overlap: float
    value_type: str
    structured_value_present: bool
    identifiers_covered: bool
    n_terms: int
    n_covered: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_score": round(self.retrieval_score, 4),
            "score_margin": round(self.score_margin, 4),
            "weighted_coverage": round(self.weighted_coverage, 4),
            "lexical_overlap": round(self.lexical_overlap, 4),
            "value_type": self.value_type,
            "structured_value_present": self.structured_value_present,
            "identifiers_covered": self.identifiers_covered,
            "n_terms": self.n_terms,
            "n_covered": self.n_covered,
        }


def _idf(term: str, df: dict[str, int], n_docs: int) -> float:
    """IDF lissé, borné [1.0, 5.0] : un terme discriminant jamais vu (la propriété
    demandée absente du corpus) domine la couverture pondérée sans qu'un seul terme
    fréquent puisse la sauver."""
    d = int(df.get(term, 0))
    val = math.log((n_docs + 1) / (d + 1)) + 1.0
    return max(1.0, min(5.0, val))


def _covered(term: str, evid_tokens: set[str], evid_text: str) -> bool:
    """Terme couvert par les preuves : sous-chaîne, ou préfixe partagé (morphologie)."""
    if term in evid_text:
        return True
    pfx = term[: min(len(term), 6)]
    return any(tok.startswith(pfx) for tok in evid_tokens)


def evidence_signals(
    question: str,
    sources: Sequence[dict[str, Any]],
    *,
    df: dict[str, int] | None = None,
    n_docs: int = 1,
    top_k: int = 5,
) -> AnswerabilitySignals:
    df = df or {}
    ranked = [s for s in (sources or []) if isinstance(s, dict)][:top_k]
    scores = [float(s.get("score") or 0.0) for s in ranked]
    best = scores[0] if scores else 0.0
    margin = (scores[0] - scores[1]) if len(scores) >= 2 else best

    terms = content_terms(question)
    weights = {t: _idf(t, df, n_docs) for t in terms}
    den = sum(weights.values())
    vtype = requested_value_type(question)
    ids = question_identifiers(question)

    chunk_texts = [fold(str(s.get("text") or "")) for s in ranked]
    chunk_tokens = [set(tokens(ct)) for ct in chunk_texts]

    # Couverture UNION (multi-doc friendly) : un terme est couvert s'il apparaît dans
    # N'IMPORTE QUEL chunk du top-k.
    num = 0.0
    covered_terms: set[str] = set()
    for t in terms:
        if any(_covered(t, chunk_tokens[i], chunk_texts[i]) for i in range(len(ranked))):
            num += weights[t]
            covered_terms.add(t)
    n_cov = len(covered_terms)
    union_cov = (num / den) if den > 0 else (1.0 if ranked else 0.0)
    lexical_overlap = (n_cov / len(terms)) if terms else (1.0 if ranked else 0.0)

    # Identifiants forts : union (spécifiques, pas de faux positif inter-chunk).
    ids_covered = (
        all(any(i in ct or _covered(i, chunk_tokens[k], ct) for k, ct in enumerate(chunk_texts))
            for i in ids)
        if ids else True
    )

    # Valeur structurante : elle doit CO-OCCURRER, dans UN chunk, avec le terme entité le
    # plus discriminant (évite « une IP » d'un chunk rattachée à l'entité d'un autre —
    # ex. « IP de le poste de développement » alors que l'IP est celle de la gateway).
    if vtype == "none":
        value_present = True
    elif not ranked:
        value_present = False
    else:
        # Ancre = terme entité le plus discriminant PARMI CEUX RÉELLEMENT COUVERTS
        # (un verbe/mot jamais vu, ex. « tourne », n'est pas une ancre fiable).
        entity_terms = [t for t in terms if t not in _VALUE_WORDS and t in covered_terms]
        top_entity = max(entity_terms, key=lambda t: weights[t], default=None)
        value_present = False
        for i in range(len(ranked)):
            if not _value_present(vtype, chunk_texts[i]):
                continue
            if top_entity is None or _covered(top_entity, chunk_tokens[i], chunk_texts[i]):
                value_present = True
                break

    return AnswerabilitySignals(
        retrieval_score=best, score_margin=margin, weighted_coverage=union_cov,
        lexical_overlap=lexical_overlap, value_type=vtype,
        structured_value_present=value_present, identifiers_covered=ids_covered,
        n_terms=len(terms), n_covered=n_cov,
    )


# --- Décision -------------------------------------------------------------------
@dataclass
class AnswerabilityDecision:
    answerable: bool
    confidence: float
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answerable": self.answerable,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "signals": self.signals,
        }


# Presets de règles déterministes (strict / balanced / permissive).
RULE_PRESETS: dict[str, dict[str, float]] = {
    "strict": {"score_floor": 0.72, "coverage_min": 0.70},
    "balanced": {"score_floor": 0.60, "coverage_min": 0.60},
    "permissive": {"score_floor": 0.50, "coverage_min": 0.50},
}

# Poids du score composite (somme = 1).
DEFAULT_COMPOSITE_WEIGHTS: dict[str, float] = {
    "retrieval": 0.20, "coverage": 0.45, "value": 0.20, "identifier": 0.15,
}


def _score_norm(score: float, lo: float = 0.5, hi: float = 1.0) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (score - lo) / (hi - lo)))


def decide(
    signals: AnswerabilitySignals,
    *,
    strategy: str,
    min_score: float = 0.76,
    rule_preset: str = "balanced",
    composite_threshold: float = 0.5,
    weights: dict[str, float] | None = None,
    has_candidate: bool = True,
) -> AnswerabilityDecision:
    sd = signals.to_dict()

    if not has_candidate or signals.n_terms == 0 and signals.retrieval_score == 0.0 and not has_candidate:
        return AnswerabilityDecision(False, 0.0, REASON_NO_CANDIDATE, sd)

    if strategy == STRATEGY_NONE:
        return AnswerabilityDecision(True, 1.0, "", sd)

    if strategy == STRATEGY_RETRIEVAL_THRESHOLD:
        ok = signals.retrieval_score >= min_score
        return AnswerabilityDecision(
            ok, signals.retrieval_score, "" if ok else REASON_SCORE_LOW, sd
        )

    if strategy == STRATEGY_EVIDENCE_RULES:
        cfg = RULE_PRESETS.get(rule_preset, RULE_PRESETS["balanced"])
        if not has_candidate:
            return AnswerabilityDecision(False, 0.0, REASON_NO_CANDIDATE, sd)
        if signals.retrieval_score < cfg["score_floor"]:
            return AnswerabilityDecision(False, signals.weighted_coverage, REASON_SCORE_LOW, sd)
        if not signals.identifiers_covered:
            return AnswerabilityDecision(False, signals.weighted_coverage, REASON_MISSING_IDENTIFIER, sd)
        if not signals.structured_value_present:
            return AnswerabilityDecision(False, signals.weighted_coverage, REASON_MISSING_VALUE, sd)
        if signals.weighted_coverage < cfg["coverage_min"]:
            reason = REASON_MISSING_PROPERTY if signals.lexical_overlap > 0 else REASON_THEMATIC_ONLY
            return AnswerabilityDecision(False, signals.weighted_coverage, reason, sd)
        return AnswerabilityDecision(True, signals.weighted_coverage, "", sd)

    if strategy == STRATEGY_COMPOSITE:
        w = weights or DEFAULT_COMPOSITE_WEIGHTS
        comp = (
            w["retrieval"] * _score_norm(signals.retrieval_score)
            + w["coverage"] * signals.weighted_coverage
            + w["value"] * (1.0 if signals.structured_value_present else 0.0)
            + w["identifier"] * (1.0 if signals.identifiers_covered else 0.0)
        )
        ok = comp >= composite_threshold
        reason = ""
        if not ok:
            if not signals.structured_value_present:
                reason = REASON_MISSING_VALUE
            elif not signals.identifiers_covered:
                reason = REASON_MISSING_IDENTIFIER
            elif signals.weighted_coverage < 0.6:
                reason = REASON_MISSING_PROPERTY
            else:
                reason = REASON_SCORE_LOW
        return AnswerabilityDecision(ok, comp, reason, sd)

    raise ValueError(f"stratégie d'answerability inconnue: {strategy!r}")


def evaluate(
    question: str,
    sources: Sequence[dict[str, Any]],
    *,
    strategy: str,
    df: dict[str, int] | None = None,
    n_docs: int = 1,
    min_score: float = 0.76,
    rule_preset: str = "balanced",
    composite_threshold: float = 0.5,
    weights: dict[str, float] | None = None,
) -> AnswerabilityDecision:
    """Point d'entrée unique : signaux + décision pour une question et ses preuves."""
    has_candidate = bool(sources)
    sig = evidence_signals(question, sources, df=df, n_docs=n_docs)
    if not has_candidate and strategy != STRATEGY_NONE:
        return AnswerabilityDecision(False, 0.0, REASON_NO_CANDIDATE, sig.to_dict())
    return decide(
        sig, strategy=strategy, min_score=min_score, rule_preset=rule_preset,
        composite_threshold=composite_threshold, weights=weights, has_candidate=has_candidate,
    )


def build_corpus_df(docs_texts: Sequence[Sequence[str]]) -> tuple[dict[str, int], int]:
    """Document-frequency des tokens sur le corpus (un « document » = un ensemble de
    chunks). Renvoie (df, n_docs). Utilisé pour pondérer la couverture par l'IDF."""
    df: dict[str, int] = {}
    n = 0
    for chunks in docs_texts:
        n += 1
        seen = set()
        for ch in chunks:
            seen.update(tokens(ch))
        for tok in seen:
            df[tok] = df.get(tok, 0) + 1
    return df, n

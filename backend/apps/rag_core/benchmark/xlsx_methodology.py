"""Filtres MÉTHODOLOGIQUES du benchmark Excel.

Répond à une seule question, de façon déterministe et SANS LLM :

    Ce cas mesure-t-il réellement la capacité du moteur à retrouver une source
    explicite, directe et non ambiguë ?

Un cas ``retrieval_lookup`` valide : réponse matérialisée dans une région source,
question désignant sans ambiguïté cette région, aucun calcul/agrégation/raisonnement
multi-région, mesure et identifiant de ligne explicites. Les cas hors périmètre sont
redirigés (agrégation, multi-région) ou rejetés (en-tête générique, question non
autonome, ambiguïté…), jamais comptés comme un lookup.

Règles explicites, auditables, testables et déterministes. Aucune réécriture
automatique d'une question (accepted / rejected / needs_human_review uniquement).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from apps.rag_core.benchmark.xlsx_cases import fold, is_ambiguous_column

# --------------------------------------------------------------------------- #
# Taxonomie des tâches (§7)
# --------------------------------------------------------------------------- #
TASK_RETRIEVAL_LOOKUP = "retrieval_lookup"
TASK_TABULAR_AGGREGATION = "tabular_aggregation"
TASK_MULTI_REGION = "multi_region_reasoning"
TASK_ANSWER_GENERATION = "answer_generation"
TASK_UNSUPPORTED = "unsupported"

SUPPORTED_TASK_TYPES = frozenset({
    TASK_RETRIEVAL_LOOKUP, TASK_TABULAR_AGGREGATION,
    TASK_MULTI_REGION, TASK_ANSWER_GENERATION,
})

# --------------------------------------------------------------------------- #
# Décisions (§9) — rejet technique / méthodologique / redirection / revue séparés
# --------------------------------------------------------------------------- #
DEC_ACCEPTED = "accepted_retrieval_lookup"
DEC_REJECTED = "rejected"
DEC_REDIRECT_AGGREGATION = "redirected_tabular_aggregation"
DEC_REDIRECT_MULTI_REGION = "redirected_multi_region_reasoning"
DEC_NEEDS_REVIEW = "needs_human_review"

# --------------------------------------------------------------------------- #
# Codes de rejet STABLES (§31) — indépendants du texte des messages
# --------------------------------------------------------------------------- #
GENERIC_OR_INFERRED_HEADER = "generic_or_inferred_header"
HEADER_MAPPING_UNCERTAIN = "header_mapping_uncertain"
REQUIRES_AGGREGATION = "requires_aggregation"
REQUIRES_COUNTING = "requires_counting"
REQUIRES_FORMULA_EVALUATION = "requires_formula_evaluation"
REQUIRES_MULTI_REGION_REASONING = "requires_multi_region_reasoning"
REQUIRES_CROSS_SHEET_REASONING = "requires_cross_sheet_reasoning"
REQUIRES_CROSS_DOCUMENT_REASONING = "requires_cross_document_reasoning"
AMBIGUOUS_ROW_IDENTIFIER = "ambiguous_row_identifier"
AMBIGUOUS_MEASURE_COLUMN = "ambiguous_measure_column"
QUESTION_NOT_SELF_CONTAINED = "question_not_self_contained"
DEICTIC_OR_CONTEXT_DEPENDENT = "deictic_or_context_dependent_question"
QUESTION_CONTAINS_ANSWER = "question_contains_answer"
EXPECTED_VALUE_NOT_DISTINCTIVE = "expected_value_not_distinctive"
EMPTY_OR_NON_INFORMATIVE_VALUE = "empty_or_non_informative_expected_value"
MULTIPLE_MATCHING_REGIONS = "multiple_matching_regions"
QUESTION_GENERATION_UNUSABLE = "question_generation_unusable"
UNSUPPORTED_TASK_TYPE = "unsupported_task_type"

#: Codes « durs » -> rejet. Les autres (ci-dessous) -> revue humaine.
_HARD_REJECT_CODES = frozenset({
    GENERIC_OR_INFERRED_HEADER, REQUIRES_AGGREGATION, REQUIRES_COUNTING,
    REQUIRES_FORMULA_EVALUATION, REQUIRES_MULTI_REGION_REASONING,
    REQUIRES_CROSS_SHEET_REASONING, REQUIRES_CROSS_DOCUMENT_REASONING,
    AMBIGUOUS_ROW_IDENTIFIER, AMBIGUOUS_MEASURE_COLUMN, QUESTION_NOT_SELF_CONTAINED,
    DEICTIC_OR_CONTEXT_DEPENDENT, QUESTION_CONTAINS_ANSWER,
    EMPTY_OR_NON_INFORMATIVE_VALUE, MULTIPLE_MATCHING_REGIONS,
    QUESTION_GENERATION_UNUSABLE, UNSUPPORTED_TASK_TYPE,
})
_REVIEW_CODES = frozenset({HEADER_MAPPING_UNCERTAIN, EXPECTED_VALUE_NOT_DISTINCTIVE})


@dataclass(frozen=True)
class MethodologicalValidationResult:
    accepted: bool
    task_type: str
    decision: str
    rejection_codes: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "task_type": self.task_type,
                "decision": self.decision, "rejection_codes": list(self.rejection_codes),
                "warnings": list(self.warnings)}


# --------------------------------------------------------------------------- #
# Détecteurs — en-têtes génériques (§10)
# --------------------------------------------------------------------------- #
_GENERIC_HEADER_RES = [
    re.compile(r"^col(?:umn|onne)?[_\s-]*\d+$"),
    re.compile(r"^champ[_\s-]*\d+$"),
    re.compile(r"^field[_\s-]*\d+$"),
    re.compile(r"^unnamed:?\s*\d+$"),
]
_GENERIC_HEADER_LITERALS = frozenset({"", "none", "null", "nan", "sans titre", "vide", "-"})


def is_generic_header(header: str) -> bool:
    """En-tête non discriminant (col1, Unnamed: 0, vide…). Insensible casse/accents."""
    h = fold(header)
    if h in _GENERIC_HEADER_LITERALS:
        return True
    return any(rx.match(h) for rx in _GENERIC_HEADER_RES)


# --------------------------------------------------------------------------- #
# Détecteurs — agrégation / comptage / formule (§13-16)
# --------------------------------------------------------------------------- #
# Verbes de CALCUL (la valeur n'est pas matérialisée : il faut la produire).
_COUNTING_RES = [
    re.compile(r"\bcombien\b"), re.compile(r"\bnombre d[e']"), re.compile(r"\bcount\b"),
    re.compile(r"\bhow many\b"),
]
_AGGREGATION_RES = [
    re.compile(r"\bsomme d[e']"), re.compile(r"\btotal d[e']"), re.compile(r"\bmoyenne d[e']"),
    re.compile(r"\bmediane d[e']"), re.compile(r"\bpourcentage d[e']"), re.compile(r"\bratio d[e']"),
    re.compile(r"\becart\b"), re.compile(r"\bclassement\b"), re.compile(r"\btop\s*\d"),
    re.compile(r"\bsum of\b"), re.compile(r"\baverage of\b"), re.compile(r"\bmean of\b"),
    re.compile(r"\bpercentage of\b"),
    # Superlatifs impliquant une comparaison de plusieurs lignes.
    re.compile(r"\ble plus (?:grand|petit|eleve|faible)\b"),
    re.compile(r"\bla plus (?:grande|petite|elevee|faible)\b"),
    re.compile(r"\b(?:maximal|minimal|maximale|minimale)\b"),
    re.compile(r"\b(?:highest|lowest|maximum|minimum)\b"),
]


def detect_aggregation_requirement(question: str, *, aggregate: str | None = None) -> str | None:
    """Renvoie ``requires_counting`` / ``requires_aggregation`` si la question exige un
    calcul (agrégat NON matérialisé), sinon ``None``.

    ``aggregate`` : signal explicite du générateur (count/sum/min/max/avg…) — prioritaire.
    """
    if aggregate:
        return REQUIRES_COUNTING if aggregate == "count" else REQUIRES_AGGREGATION
    q = fold(question)
    if any(rx.search(q) for rx in _COUNTING_RES):
        return REQUIRES_COUNTING
    if any(rx.search(q) for rx in _AGGREGATION_RES):
        return REQUIRES_AGGREGATION
    return None


_FORMULA_RE = re.compile(r"\b(calcul|calcule|recalcul|formule|=somme|=sum|=average)\b")


def requires_formula_evaluation(question: str) -> bool:
    return bool(_FORMULA_RE.search(fold(question)))


# --------------------------------------------------------------------------- #
# Détecteurs — multi-région (§17)
# --------------------------------------------------------------------------- #
_MULTI_REGION_RES = [
    re.compile(r"\bcompare[rz]?\b"), re.compile(r"\bcompar(?:e|aison|er)\b"),
    re.compile(r"\bentre .* et .*\b"), re.compile(r"\bconsolid"), re.compile(r"\bjointure\b"),
]
_CROSS_SHEET_RE = re.compile(r"\b(?:autre|plusieurs) feuille|\bcross[- ]sheet\b")


def detect_multi_region_requirement(question: str) -> str | None:
    q = fold(question)
    if _CROSS_SHEET_RE.search(q):
        return REQUIRES_CROSS_SHEET_REASONING
    if any(rx.search(q) for rx in _MULTI_REGION_RES):
        return REQUIRES_MULTI_REGION_REASONING
    return None


# --------------------------------------------------------------------------- #
# Détecteurs — question autonome / déictique / réponse incluse (§22-24)
# --------------------------------------------------------------------------- #
_DEICTIC_RES = [
    re.compile(r"\bce tableau\b"), re.compile(r"\bcette ligne\b"), re.compile(r"\bcette colonne\b"),
    re.compile(r"\bla colonne suivante\b"), re.compile(r"\bla valeur ci-dessus\b"),
    re.compile(r"\bcelle-ci\b"), re.compile(r"\bcelui-ci\b"),
    re.compile(r"\bvaleur correspondante\b"), re.compile(r"\bla correspondante\b"),
    re.compile(r"\b(?:ligne|colonne|valeur) (?:precedente|suivante)\b"),
]


def has_deictic_reference(question: str) -> bool:
    q = fold(question)
    return any(rx.search(q) for rx in _DEICTIC_RES)


def is_question_self_contained(question: str, *, sheet_name: str, row_identifier: str,
                               measure: str) -> bool:
    """La question doit nommer la feuille (contexte), l'identifiant de ligne ET la
    mesure demandée. Sinon elle n'est pas répondable hors affichage du tableau."""
    q = fold(question)
    if not q or len(q.split()) < 4:
        return False
    for token in (row_identifier, measure):
        t = fold(token)
        if not t or t not in q:
            return False
    return bool(fold(sheet_name)) and fold(sheet_name) in q


def question_contains_answer(question: str, expected_values: list[str]) -> bool:
    q = fold(question)
    for v in expected_values:
        fv = fold(v)
        if fv and len(fv) >= 2 and not is_trivial_value(v) and fv in q:
            return True
    return False


_MAX_QUESTION_CHARS = 320
_MAX_QUESTION_WORDS = 60


def is_question_unusable(question: str) -> bool:
    q = (question or "").strip()
    if len(q) > _MAX_QUESTION_CHARS or len(q.split()) > _MAX_QUESTION_WORDS:
        return True
    # Concaténation brute de nombreux « et » (empilement de désambiguïsateurs).
    return fold(q).count(" et ") >= 4


# --------------------------------------------------------------------------- #
# Détecteurs — valeurs triviales / vides (§25-27)
# --------------------------------------------------------------------------- #
_TRIVIAL_VALUES = frozenset({"oui", "non", "0", "1", "n/a", "na", "ok", "actif",
                             "inactif", "vrai", "faux", "yes", "no", "true", "false"})
_EMPTY_TOKENS = frozenset({"", "-", "—", "–", "n/a", "na", "null", "none", "nan", "."})


def is_trivial_value(value: str) -> bool:
    return fold(value) in _TRIVIAL_VALUES


def is_empty_value(value: str) -> bool:
    v = unicodedata.normalize("NFKC", value or "").strip()
    return fold(v) in _EMPTY_TOKENS


# --------------------------------------------------------------------------- #
# Détecteurs — ambiguïté de ligne / colonne (§18-21)
# --------------------------------------------------------------------------- #
def count_matching_rows(rows: list[list[str]], identity_idx: int, row_identifier: str) -> int:
    fid = fold(row_identifier)
    return sum(1 for r in rows if identity_idx < len(r) and fold(r[identity_idx]) == fid)


def detect_row_ambiguity(
    rows: list[list[str]], *, identity_idx: int, row_identifier: str,
    composite: dict[str, str] | None = None, composite_indices: dict[str, int] | None = None,
) -> bool:
    """L'identifiant de ligne est-il ambigu (présent sur >1 ligne) sans qu'un identifiant
    COMPOSITE ne rende la ligne unique ?"""
    if count_matching_rows(rows, identity_idx, row_identifier) <= 1:
        return False
    if composite and composite_indices:
        # Unicité par la combinaison complète des colonnes d'identité.
        wanted = {k: fold(v) for k, v in composite.items()}
        matches = 0
        for r in rows:
            if all(idx < len(r) and fold(r[idx]) == wanted.get(k, "\x00")
                   for k, idx in composite_indices.items()):
                matches += 1
        return matches != 1
    return True


def detect_measure_ambiguity(measure: str, headers: list[str], question: str) -> bool:
    """La colonne de mesure est-elle ambiguë (Montant / Montant_1, Montant 2024 /
    Montant 2025…) SANS que la question ne nomme l'en-tête complet exact ?"""
    if not is_ambiguous_column(measure, headers):
        return False
    # §21 : accepté si la question cite explicitement l'en-tête complet.
    return fold(measure) not in fold(question)


# --------------------------------------------------------------------------- #
# Classification du task_type (§7-8)
# --------------------------------------------------------------------------- #
def classify_task_type(*, question: str, aggregate: str | None = None) -> str:
    if detect_aggregation_requirement(question, aggregate=aggregate):
        return TASK_TABULAR_AGGREGATION
    if requires_formula_evaluation(question):
        return TASK_TABULAR_AGGREGATION
    if detect_multi_region_requirement(question):
        return TASK_MULTI_REGION
    return TASK_RETRIEVAL_LOOKUP


# --------------------------------------------------------------------------- #
# Classification complète d'un candidat (§32, §37)
# --------------------------------------------------------------------------- #
def classify_xlsx_case(
    *,
    question: str,
    headers: list[str],
    rows: list[list[str]],
    identity_idx: int,
    row_identifier: str,
    measure: str,
    expected_values: list[str],
    aggregate: str | None = None,
    headers_inferred: bool = False,
    composite: dict[str, str] | None = None,
    composite_indices: dict[str, int] | None = None,
) -> MethodologicalValidationResult:
    """Classe un candidat et décide : accepté lookup / redirigé / rejeté / revue.

    Ordre stable (§37) ; les codes s'ACCUMULENT (on ne s'arrête pas au premier), sauf
    redirection de tâche (agrégation / multi-région) qui prime."""
    # 1-2. task_type
    task_type = classify_task_type(question=question, aggregate=aggregate)
    if task_type == TASK_TABULAR_AGGREGATION:
        code = detect_aggregation_requirement(question, aggregate=aggregate) or (
            REQUIRES_FORMULA_EVALUATION if requires_formula_evaluation(question) else REQUIRES_AGGREGATION
        )
        return MethodologicalValidationResult(False, task_type, DEC_REDIRECT_AGGREGATION, (code,))
    if task_type == TASK_MULTI_REGION:
        code = detect_multi_region_requirement(question) or REQUIRES_MULTI_REGION_REASONING
        return MethodologicalValidationResult(False, task_type, DEC_REDIRECT_MULTI_REGION, (code,))

    codes: list[str] = []
    warnings: list[str] = []

    # 3. en-têtes
    if headers_inferred or is_generic_header(measure) or is_generic_header(headers[identity_idx] if identity_idx < len(headers) else ""):
        codes.append(GENERIC_OR_INFERRED_HEADER)

    # 4. identifiant de ligne
    if is_empty_value(row_identifier):
        codes.append(EMPTY_OR_NON_INFORMATIVE_VALUE)
    elif is_generic_header(row_identifier):
        codes.append(GENERIC_OR_INFERRED_HEADER)
    elif detect_row_ambiguity(rows, identity_idx=identity_idx, row_identifier=row_identifier,
                              composite=composite, composite_indices=composite_indices):
        codes.append(AMBIGUOUS_ROW_IDENTIFIER)

    # 5. colonne de mesure
    if detect_measure_ambiguity(measure, headers, question):
        codes.append(AMBIGUOUS_MEASURE_COLUMN)

    # 6. autonomie de la question
    if has_deictic_reference(question):
        codes.append(DEICTIC_OR_CONTEXT_DEPENDENT)
    if not is_question_self_contained(question, sheet_name="", row_identifier=row_identifier, measure=measure):
        # (le contexte feuille est vérifié en amont côté générateur ; ici on exige
        #  au moins identifiant + mesure présents et question non triviale.)
        if not (fold(row_identifier) in fold(question) and fold(measure) in fold(question)
                and len(fold(question).split()) >= 4):
            codes.append(QUESTION_NOT_SELF_CONTAINED)
    if is_question_unusable(question):
        codes.append(QUESTION_GENERATION_UNUSABLE)

    # 8. réponse incluse / distinctivité
    if question_contains_answer(question, expected_values):
        codes.append(QUESTION_CONTAINS_ANSWER)
    non_empty = [v for v in expected_values if not is_empty_value(v)]
    if not non_empty:
        codes.append(EMPTY_OR_NON_INFORMATIVE_VALUE)
    elif all(is_trivial_value(v) for v in non_empty):
        # Valeur triviale (Oui/0/…) : acceptable SEULEMENT si le locator est unique.
        ambiguous_locator = (AMBIGUOUS_ROW_IDENTIFIER in codes or AMBIGUOUS_MEASURE_COLUMN in codes)
        if ambiguous_locator:
            codes.append(EXPECTED_VALUE_NOT_DISTINCTIVE)
        else:
            warnings.append("trivial_value_unique_locator")

    codes = _dedup(codes)
    if any(c in _HARD_REJECT_CODES for c in codes):
        return MethodologicalValidationResult(False, task_type, DEC_REJECTED, tuple(codes), tuple(warnings))
    if any(c in _REVIEW_CODES for c in codes):
        return MethodologicalValidationResult(False, task_type, DEC_NEEDS_REVIEW, tuple(codes), tuple(warnings))
    return MethodologicalValidationResult(True, task_type, DEC_ACCEPTED, (), tuple(warnings))


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

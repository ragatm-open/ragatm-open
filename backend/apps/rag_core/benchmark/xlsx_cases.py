"""Dérivation MÉCANIQUE de cas de benchmark depuis des chunks XLSX (P1.9-b).

Principe : la vérité terrain d'un tableau est **structurelle**, pas sémantique. On lit
la structure réellement indexée (en-têtes, colonne d'identité, colonne de mesure,
cellule) et on en dérive une question dont la réponse est **vérifiable par construction**
— jamais par la réponse du moteur RagATM, jamais par un LLM.

Module **pur** : aucun import Django, aucun accès DB/réseau. Il reçoit le texte du chunk
et sa métadonnée ; la commande fournit les données réelles. Testable sur fixtures
synthétiques (aucun contenu client dans les tests).

Chaque cas produit est marqué ``human_validation_status="pending"`` : la validation
humaine reste obligatoire (tableaux, valeurs numériques, ambiguïtés) — ce module ne
fait que *pré-mâcher* le travail de relecture.
"""
from __future__ import annotations

import random
import re
import unicodedata
from typing import Any

GENERATION_METHOD = "metadata_structural"
STATUS_PENDING = "pending"

# Sous-catégories (cf. taxonomie §4).
SUB_DIRECT_VALUE = "direct_value"
SUB_DATE = "date_or_deadline"
SUB_STATUS = "status_or_category"
SUB_AMBIGUOUS = "column_disambiguation"
SUB_MULTI_COLUMN = "multi_column_row"
SUB_AGGREGATION = "local_aggregation"

# Motifs de rejet.
REJ_NO_TABLE = "no_parsable_table"
REJ_NO_SHEET = "missing_sheet_name"
REJ_NO_HEADERS = "no_usable_headers"
REJ_NO_IDENTITY = "no_identity_column"
REJ_NO_MEASURE = "no_measure_column"
REJ_EMPTY_VALUE = "empty_expected_value"
REJ_VALUE_NOT_IN_CHUNK = "value_not_in_chunk"
REJ_QUESTION_CONTAINS_ANSWER = "question_contains_answer"
REJ_SENSITIVE = "sensitive_value_not_anonymisable"
REJ_AMBIGUOUS_TRUTH = "ambiguous_ground_truth"
REJ_VISUAL_CHECK = "requires_visual_check"


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


# --------------------------------------------------------------------------
# Parsing du tableau markdown porté par le chunk
# --------------------------------------------------------------------------
_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    """(en-têtes, lignes) depuis le tableau markdown d'un chunk. ([], []) si absent."""
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if _SEP_RE.match(s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        rows.append(cells)
    if len(rows) < 2:
        return [], []
    headers, data = rows[0], rows[1:]
    width = len(headers)
    data = [r for r in data if len(r) == width and any(c for c in r)]
    return headers, data


# --------------------------------------------------------------------------
# Typage déterministe des valeurs
# --------------------------------------------------------------------------
_DATE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{1,2}\s+(janv|f[ée]vr|mars|avr|mai|juin|juil|ao[uû]t|sept|oct|nov|d[ée]c)[a-z.]*\s+\d{4})\s*$",
    re.I,
)
_PERCENT_RE = re.compile(r"^\s*-?\d+([.,]\d+)?\s*%\s*$")
_AMOUNT_RE = re.compile(r"^\s*-?[\d\s ]{1,15}([.,]\d{1,2})?\s*(€|eur|euros?|\$|usd)?\s*$", re.I)
_INT_RE = re.compile(r"^\s*-?\d{1,9}\s*$")
_IDENT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._/\-]{2,}$")

VALUE_TYPES = ("date", "percent", "amount", "quantity", "identifier", "status", "string")


def classify_value(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return "string"
    if _DATE_RE.match(v):
        return "date"
    if _PERCENT_RE.match(v):
        return "percent"
    if _INT_RE.match(v):
        return "quantity"
    if _AMOUNT_RE.match(v) and any(ch.isdigit() for ch in v):
        return "amount"
    if _IDENT_RE.match(v):
        return "identifier"
    if len(v) <= 24 and " " not in v.strip():
        return "status"
    return "string"


def column_value_type(rows: list[list[str]], idx: int) -> str:
    """Type dominant d'une colonne (vote majoritaire sur les cellules non vides)."""
    counts: dict[str, int] = {}
    for r in rows:
        if idx < len(r) and (r[idx] or "").strip():
            t = classify_value(r[idx])
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return "string"
    return max(counts.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------
# Confidentialité : détection de valeurs non anonymisables
# --------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\d[\s.-]?){9,14}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")


def is_sensitive(value: str) -> bool:
    """Valeur manifestement personnelle/bancaire -> le cas est écarté (§3)."""
    v = (value or "").strip()
    if not v:
        return False
    return bool(_EMAIL_RE.search(v) or _IBAN_RE.search(v)) or bool(
        _PHONE_RE.fullmatch(v.replace(" ", ""))
    )


# --------------------------------------------------------------------------
# Colonnes lexicalement proches (§9)
# --------------------------------------------------------------------------
_STOP_HDR = frozenset("de du des la le les l d en a au aux et ou".split())


def _tokens(header: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", fold(header)) if t not in _STOP_HDR]


def detect_ambiguous_groups(headers: list[str]) -> dict[str, list[str]]:
    """Regroupe les en-têtes partageant leur *racine* et ne différant que par le mot
    distinctif final (« montant brut » / « montant net »). Sans dépendance NLP.

    Renvoie {racine: [en-têtes...]} pour les groupes d'au moins 2 en-têtes.
    """
    groups: dict[str, list[str]] = {}
    for h in headers:
        toks = _tokens(h)
        if len(toks) < 2:
            continue
        root = " ".join(toks[:-1])  # tout sauf le mot distinctif final
        groups.setdefault(root, []).append(h)
    return {root: hs for root, hs in groups.items() if len(hs) >= 2}


def is_ambiguous_column(header: str, headers: list[str]) -> bool:
    for hs in detect_ambiguous_groups(headers).values():
        if header in hs:
            return True
    return False


# --------------------------------------------------------------------------
# Construction des questions (gabarits explicites — jamais de LLM)
# --------------------------------------------------------------------------
def build_question(*, sub_category: str, sheet: str, measure: str,
                   identity_col: str, row_id: str, measures: list[str] | None = None,
                   aggregate: str | None = None) -> str:
    """Question explicite nommant la colonne demandée (anti-ambiguïté §9)."""
    if sub_category == SUB_MULTI_COLUMN and measures:
        cols = " et ".join(f"« {m} »" for m in measures)
        return (f"Dans la feuille « {sheet} », quelles sont les valeurs de {cols} "
                f"pour le {identity_col} « {row_id} » ?")
    if sub_category == SUB_AGGREGATION and aggregate:
        libelle = {"count": "combien de lignes comporte",
                   "max": "quelle est la valeur maximale de",
                   "min": "quelle est la valeur minimale de"}[aggregate]
        if aggregate == "count":
            return f"Dans la feuille « {sheet} », {libelle} le tableau ?"
        return f"Dans la feuille « {sheet} », {libelle} la colonne « {measure} » ?"
    if sub_category == SUB_DATE:
        return (f"Dans la feuille « {sheet} », quelle date figure en « {measure} » "
                f"pour le {identity_col} « {row_id} » ?")
    if sub_category == SUB_STATUS:
        return (f"Dans la feuille « {sheet} », quel est le « {measure} » "
                f"du {identity_col} « {row_id} » ?")
    if sub_category == SUB_AMBIGUOUS:
        return (f"Dans la feuille « {sheet} », quelle est la valeur de « {measure} » "
                f"(et non d'une autre colonne similaire) pour le {identity_col} « {row_id} » ?")
    return (f"Dans la feuille « {sheet} », quelle est la valeur de « {measure} » "
            f"pour le {identity_col} « {row_id} » ?")


def assign_difficulty(*, sub_category: str, ambiguous: bool, wide_table: bool,
                      n_identity: int) -> str:
    """Difficulté déterministe (§10)."""
    if sub_category in (SUB_MULTI_COLUMN, SUB_AGGREGATION):
        return "hard"
    if ambiguous or wide_table or n_identity > 1:
        return "medium"
    return "easy"


# --------------------------------------------------------------------------
# Validation mécanique (§8)
# --------------------------------------------------------------------------
def validate_case(case: dict[str, Any], chunk_text: str, headers: list[str]) -> list[str]:
    """Renvoie la liste des motifs de rejet (vide = cas exploitable)."""
    errs: list[str] = []
    gt = case.get("ground_truth") or {}

    if not case.get("question"):
        errs.append(REJ_QUESTION_CONTAINS_ANSWER if False else "empty_question")
    if not gt.get("sheet_name"):
        errs.append(REJ_NO_SHEET)
    if not gt.get("identity_columns"):
        errs.append(REJ_NO_IDENTITY)

    measure = gt.get("measure_column")
    measures = gt.get("measure_columns") or ([measure] if measure else [])
    if not measures:
        errs.append(REJ_NO_MEASURE)
    for m in measures:
        if m not in headers:
            errs.append(REJ_NO_MEASURE)
            break

    values = gt.get("expected_values") or ([gt.get("expected_value")] if gt.get("expected_value") else [])
    if not values or not all(str(v).strip() for v in values):
        errs.append(REJ_EMPTY_VALUE)

    folded = fold(chunk_text)
    for v in values:
        if v and fold(str(v)) not in folded:
            errs.append(REJ_VALUE_NOT_IN_CHUNK)
            break

    # La question ne doit pas contenir la réponse (sinon le cas est trivial).
    q = fold(case.get("question", ""))
    for v in values:
        sv = fold(str(v))
        # Le row_identifier apparaît légitimement dans la question ; on ne teste que la
        # VALEUR attendue, et seulement si elle est assez discriminante.
        if sv and len(sv) >= 3 and sv in q and sv != fold(str(gt.get("row_identifier", ""))):
            errs.append(REJ_QUESTION_CONTAINS_ANSWER)
            break

    if not case.get("expected_source_chunk_ids"):
        errs.append("missing_source_chunk")
    if not case.get("fact_group_id"):
        errs.append("missing_fact_group")
    if case.get("human_validation_status") != STATUS_PENDING:
        errs.append("not_marked_pending")
    return errs


# --------------------------------------------------------------------------
# Split calibration / holdout par fact_group_id (§11)
# --------------------------------------------------------------------------
def split_by_fact_group(
    cases: list[dict[str, Any]], *, calibration_ratio: float = 0.70, seed: int = 0
) -> tuple[list[dict], list[dict]]:
    """Split déterministe **par fact_group_id** : jamais par ligne (anti-fuite)."""
    groups = sorted({c["fact_group_id"] for c in cases})
    rng = random.Random(seed)
    rng.shuffle(groups)
    n_cal = int(round(len(groups) * calibration_ratio))
    cal_groups = set(groups[:n_cal])
    cal = [c for c in cases if c["fact_group_id"] in cal_groups]
    hold = [c for c in cases if c["fact_group_id"] not in cal_groups]
    return cal, hold


def split_leaks(calibration: list[dict], holdout: list[dict]) -> set[str]:
    """fact_group_id présents des deux côtés (doit toujours être vide)."""
    return {c["fact_group_id"] for c in calibration} & {c["fact_group_id"] for c in holdout}

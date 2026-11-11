"""Vérité terrain de benchmark Excel — schéma STABLE v2, indépendant des UUID.

Problème résolu : les cas exploratoires référençaient la source par des UUID
d'ingestion (`expected_document_ids`, `expected_source_chunk_ids`, …). Après toute
réingestion / restauration / changement de base, ces UUID ne désignent plus rien —
ce qui a déjà invalidé une ancienne calibration.

Ce module décrit une source attendue de façon **logique et reproductible** :

- `document_ref` : identité par **SHA-256 du fichier source** (jamais l'UUID) ;
- `locator` : région Excel **structurelle** (feuille + plage + bornes) ;
- `evidence` : hash **versionné** d'un texte de région normalisé, pour détecter un
  changement réel de contenu et vérifier la présence du fait ;
- `match_policy` : `any` / `all`.

Ce module NE fait AUCUN retrieval et NE dépend d'AUCUN UUID. La résolution vers les
IDs courants (calculés à l'exécution) vit dans ``stable_source_resolver``.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

STABLE_GROUND_TRUTH_SCHEMA_VERSION = 2
NORMALIZATION_VERSION = 1
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

LOCATOR_KIND_XLSX = "xlsx_region"
MATCH_ANY = "any"
MATCH_ALL = "all"
_MATCH_POLICIES = frozenset({MATCH_ANY, MATCH_ALL})

TASK_RETRIEVAL_LOOKUP = "retrieval_lookup"

# --- Codes d'erreur STABLES (ensemble fermé, exposables) --------------------
ERR_INVALID_SCHEMA_VERSION = "invalid_schema_version"
ERR_INVALID_DOCUMENT_SHA256 = "invalid_document_sha256"
ERR_DOCUMENT_NOT_FOUND = "document_not_found"
ERR_AMBIGUOUS_DOCUMENT_REFERENCE = "ambiguous_document_reference"
ERR_SHEET_NOT_FOUND = "sheet_not_found"
ERR_INVALID_CELL_RANGE = "invalid_cell_range"
ERR_REGION_NOT_FOUND = "region_not_found"
ERR_PARTIAL_REGION_COVERAGE = "partial_region_coverage"
ERR_AMBIGUOUS_REGION = "ambiguous_region"
ERR_EVIDENCE_HASH_MISMATCH = "evidence_hash_mismatch"
ERR_UNSUPPORTED_LOCATOR_KIND = "unsupported_locator_kind"
ERR_UNSUPPORTED_NORMALIZATION_VERSION = "unsupported_normalization_version"
ERR_INVALID_LOCATOR = "invalid_locator"
ERR_INVALID_MATCH_POLICY = "invalid_match_policy"


class StableGroundTruthError(ValueError):
    """Erreur de schéma/validation de vérité terrain. Porte un ``code`` stable."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- #
# Coordonnées Excel
# --------------------------------------------------------------------------- #
_COL_RE = re.compile(r"^[A-Z]+$")
_CELL_RANGE_RE = re.compile(r"^\$?([A-Za-z]+)\$?(\d+):\$?([A-Za-z]+)\$?(\d+)$")


def col_to_num(col: str) -> int:
    """``A`` -> 1, ``Z`` -> 26, ``AA`` -> 27. Lève sur entrée invalide."""
    c = (col or "").strip().upper().replace("$", "")
    if not _COL_RE.match(c):
        raise StableGroundTruthError(ERR_INVALID_CELL_RANGE, f"colonne invalide: {col!r}")
    n = 0
    for ch in c:
        n = n * 26 + (ord(ch) - 64)
    return n


def num_to_col(n: int) -> str:
    """1 -> ``A``, 27 -> ``AA``. Lève si < 1."""
    if n < 1:
        raise StableGroundTruthError(ERR_INVALID_CELL_RANGE, f"index colonne < 1: {n}")
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def normalize_excel_column(col: str) -> str:
    """Colonne canonique : uppercase, sans ``$``. Valide au passage."""
    return num_to_col(col_to_num(col))


def normalize_sheet_name(name: str) -> str:
    """Nom de feuille canonique : NFC + trim externe. **Casse conservée** (Excel
    distingue rarement la casse mais l'auteur du classeur, oui : « Données » ≠ « DONNÉES »
    au niveau affichage). On ne trim que les bords, on préserve l'intérieur."""
    return unicodedata.normalize("NFC", (name or "")).strip()


def parse_cell_range(rng: str) -> tuple[int, int, int, int]:
    """``A12:H20`` -> ``(col_start, row_start, col_end, row_end)`` (1-based).
    Normalise ``$`` absolus. Lève ``invalid_cell_range`` si mal formé ou incohérent."""
    m = _CELL_RANGE_RE.match((rng or "").strip())
    if not m:
        raise StableGroundTruthError(ERR_INVALID_CELL_RANGE, f"plage invalide: {rng!r}")
    c1 = col_to_num(m.group(1))
    r1 = int(m.group(2))
    c2 = col_to_num(m.group(3))
    r2 = int(m.group(4))
    if r1 < 1 or c1 < 1 or r2 < r1 or c2 < c1:
        raise StableGroundTruthError(ERR_INVALID_CELL_RANGE, f"bornes incohérentes: {rng!r}")
    return c1, r1, c2, r2


def normalize_cell_range(rng: str) -> str:
    """Forme canonique ``A1:H12`` (uppercase, sans ``$``)."""
    c1, r1, c2, r2 = parse_cell_range(rng)
    return f"{num_to_col(c1)}{r1}:{num_to_col(c2)}{r2}"


# --------------------------------------------------------------------------- #
# Normalisation textuelle versionnée (evidence)
# --------------------------------------------------------------------------- #
def normalize_benchmark_evidence_text(value: str, version: int = NORMALIZATION_VERSION) -> str:
    """Texte canonique pour l'evidence. Stable Linux/Windows/process/réingestion.

    v1 : Unicode **NFKC** (unifie les formes de compatibilité — ligatures, espaces
    insécables, chiffres pleine largeur — ce qui compte pour des cellules Excel) ;
    CRLF/CR → LF ; espaces de fin de ligne supprimés ; espaces multiples réduits ;
    retours de ligne structurants conservés ; aucune suppression sémantique.
    """
    if version != NORMALIZATION_VERSION:
        raise StableGroundTruthError(
            ERR_UNSUPPORTED_NORMALIZATION_VERSION, f"version={version}"
        )
    s = unicodedata.normalize("NFKC", value or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(lines).strip()


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def is_valid_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and bool(re.fullmatch(r"[0-9a-f]{64}", value))


# --------------------------------------------------------------------------- #
# Modèle typé
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StableDocumentRef:
    content_sha256: str
    filename: str | None = None
    relative_path: str | None = None
    media_type: str | None = XLSX_MEDIA_TYPE

    def validate(self) -> None:
        if not is_valid_sha256(self.content_sha256):
            raise StableGroundTruthError(
                ERR_INVALID_DOCUMENT_SHA256, f"sha256 invalide: {self.content_sha256!r}"
            )


@dataclass(frozen=True)
class ExcelRegionLocator:
    kind: str
    sheet_name: str
    cell_range: str
    row_start: int
    row_end: int
    column_start: str
    column_end: str

    def validate(self) -> None:
        if self.kind != LOCATOR_KIND_XLSX:
            raise StableGroundTruthError(ERR_UNSUPPORTED_LOCATOR_KIND, f"kind={self.kind!r}")
        if not normalize_sheet_name(self.sheet_name):
            raise StableGroundTruthError(ERR_INVALID_LOCATOR, "sheet_name vide")
        c1, r1, c2, r2 = parse_cell_range(self.cell_range)  # lève si mal formé
        cs, ce = col_to_num(self.column_start), col_to_num(self.column_end)
        # Cohérence stricte entre bornes déclarées et cell_range (§12).
        if (self.row_start, self.row_end, cs, ce) != (r1, r2, c1, c2):
            raise StableGroundTruthError(
                ERR_INVALID_LOCATOR,
                f"bornes incohérentes avec cell_range {self.cell_range!r}",
            )

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """(col_start, row_start, col_end, row_end) en indices numériques."""
        return (col_to_num(self.column_start), self.row_start,
                col_to_num(self.column_end), self.row_end)


@dataclass(frozen=True)
class StableEvidence:
    normalization_version: int
    region_text_sha256: str
    expected_value_sha256: str | None = None

    def validate(self) -> None:
        if self.normalization_version != NORMALIZATION_VERSION:
            raise StableGroundTruthError(
                ERR_UNSUPPORTED_NORMALIZATION_VERSION, f"version={self.normalization_version}"
            )
        if not is_valid_sha256(self.region_text_sha256):
            raise StableGroundTruthError(
                ERR_EVIDENCE_HASH_MISMATCH, "region_text_sha256 mal formé"
            )
        if self.expected_value_sha256 is not None and not is_valid_sha256(self.expected_value_sha256):
            raise StableGroundTruthError(
                ERR_EVIDENCE_HASH_MISMATCH, "expected_value_sha256 mal formé"
            )


@dataclass(frozen=True)
class StableGroundTruth:
    document_ref: StableDocumentRef
    locator: ExcelRegionLocator
    evidence: StableEvidence
    match_policy: str = MATCH_ANY

    def validate(self) -> None:
        self.document_ref.validate()
        self.locator.validate()
        self.evidence.validate()
        if self.match_policy not in _MATCH_POLICIES:
            raise StableGroundTruthError(ERR_INVALID_MATCH_POLICY, f"match_policy={self.match_policy!r}")


# --------------------------------------------------------------------------- #
# case_id stable (§25)
# --------------------------------------------------------------------------- #
def build_stable_case_id(
    *,
    document_sha256: str,
    sheet_name: str,
    cell_range: str,
    task_type: str = TASK_RETRIEVAL_LOOKUP,
    measure_columns: list[str] | tuple[str, ...] = (),
    row_identifier: str | None = None,
) -> str:
    """SHA-256 (versionné) de champs LOGIQUES stables. Jamais d'UUID, timestamp,
    position, seed brute ni résultat moteur. Stable après réingestion."""
    measures = "|".join(normalize_benchmark_evidence_text(m) for m in measure_columns)
    key = "\x1f".join([
        str(STABLE_GROUND_TRUTH_SCHEMA_VERSION),
        (document_sha256 or "").lower(),
        normalize_sheet_name(sheet_name),
        normalize_cell_range(cell_range),
        task_type,
        measures,
        normalize_benchmark_evidence_text(row_identifier or ""),
    ])
    return "xlsx-" + sha256_hex(key)[:16]


# --------------------------------------------------------------------------- #
# Relation de plages (§17)
# --------------------------------------------------------------------------- #
def range_relation(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> str:
    """Relation 2D entre deux rectangles ``(c1,r1,c2,r2)`` : exact / contains /
    contained_by / overlaps / disjoint."""
    ac1, ar1, ac2, ar2 = a
    bc1, br1, bc2, br2 = b
    if (ac1, ar1, ac2, ar2) == (bc1, br1, bc2, br2):
        return "exact"
    inter = not (bc1 > ac2 or bc2 < ac1 or br1 > ar2 or br2 < ar1)
    if not inter:
        return "disjoint"
    a_in_b = bc1 <= ac1 and br1 <= ar1 and bc2 >= ac2 and br2 >= ar2
    b_in_a = ac1 <= bc1 and ar1 <= br1 and ac2 >= bc2 and ar2 >= br2
    if b_in_a:
        return "contains"       # a contient b
    if a_in_b:
        return "contained_by"   # a est contenu dans b
    return "overlaps"


# --------------------------------------------------------------------------- #
# (Dé)sérialisation canonique (§41)
# --------------------------------------------------------------------------- #
def to_case_dict(
    *, case_id: str, question: str, rag_space: str, ground_truth: StableGroundTruth,
    task_type: str = TASK_RETRIEVAL_LOOKUP,
) -> dict[str, Any]:
    """Cas officiel v2 (dict), SANS aucun UUID ni cache résolu."""
    ground_truth.validate()
    return {
        "schema_version": STABLE_GROUND_TRUTH_SCHEMA_VERSION,
        "case_id": case_id,
        "rag_space": rag_space,
        "question": question,
        "task_type": task_type,
        "ground_truth": {
            "document_ref": {k: v for k, v in asdict(ground_truth.document_ref).items()},
            "locator": {k: v for k, v in asdict(ground_truth.locator).items()},
            "evidence": {k: v for k, v in asdict(ground_truth.evidence).items()},
            "match_policy": ground_truth.match_policy,
        },
    }


def dump_stable_case(case: dict[str, Any]) -> str:
    """JSON canonique : clés triées, UTF-8, séparateurs déterministes, pas de
    timestamp/UUID/cache. Deux dicts logiquement égaux -> mêmes octets."""
    return json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_ground_truth(obj: dict[str, Any]) -> StableGroundTruth:
    """Construit un ``StableGroundTruth`` typé depuis un dict `ground_truth`. Lève
    ``StableGroundTruthError`` (code stable) sur schéma/valeurs invalides."""
    dr = obj.get("document_ref") or {}
    loc = obj.get("locator") or {}
    ev = obj.get("evidence") or {}
    gt = StableGroundTruth(
        document_ref=StableDocumentRef(
            content_sha256=(dr.get("content_sha256") or "").lower(),
            filename=dr.get("filename"),
            relative_path=dr.get("relative_path"),
            media_type=dr.get("media_type", XLSX_MEDIA_TYPE),
        ),
        locator=ExcelRegionLocator(
            kind=loc.get("kind", LOCATOR_KIND_XLSX),
            sheet_name=loc.get("sheet_name", ""),
            cell_range=loc.get("cell_range", ""),
            row_start=int(loc.get("row_start", 0)),
            row_end=int(loc.get("row_end", 0)),
            column_start=loc.get("column_start", ""),
            column_end=loc.get("column_end", ""),
        ),
        evidence=StableEvidence(
            normalization_version=int(ev.get("normalization_version", 0)),
            region_text_sha256=ev.get("region_text_sha256", ""),
            expected_value_sha256=ev.get("expected_value_sha256"),
        ),
        match_policy=obj.get("match_policy", MATCH_ANY),
    )
    gt.validate()
    return gt


def parse_case(obj: dict[str, Any]) -> StableGroundTruth:
    """Vérifie ``schema_version`` (versions connues UNIQUEMENT) puis parse la GT."""
    v = obj.get("schema_version")
    if v != STABLE_GROUND_TRUTH_SCHEMA_VERSION:
        raise StableGroundTruthError(
            ERR_INVALID_SCHEMA_VERSION,
            f"schema_version inconnue: {v!r} (attendu {STABLE_GROUND_TRUTH_SCHEMA_VERSION})",
        )
    return parse_ground_truth(obj.get("ground_truth") or {})


# --------------------------------------------------------------------------- #
# Contenu canonique d'une région (base de l'evidence)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegionCells:
    """Cellules non vides d'une région, indexées par (row, col) numériques."""

    cells: dict[tuple[int, int], str] = field(default_factory=dict)

    def canonical_text(self) -> str:
        """Sérialisation déterministe, INDÉPENDANTE du découpage en chunks :
        ``R{row}C{col}={valeur normalisée}`` triés par (row, col)."""
        parts = [
            f"R{r}C{c}={normalize_benchmark_evidence_text(v)}"
            for (r, c), v in sorted(self.cells.items())
            if normalize_benchmark_evidence_text(v)
        ]
        return "\n".join(parts)

    def sha256(self) -> str:
        return sha256_hex(self.canonical_text())

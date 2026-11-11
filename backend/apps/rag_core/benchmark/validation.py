"""Contrôles déterministes de cohérence d'un cas de benchmark (P1-B).

Deux niveaux, volontairement séparés :

- ``validate_case_structure`` — **pur** (aucun accès Django/DB) : cohérence interne
  du cas (question, réponse, question ne contenant pas trivialement la réponse) ;
- ``validate_case_against_corpus`` — cohérence **documentaire** via un résolveur
  *injecté* (``CorpusResolver``) : existence du document/chunk attendu, appartenance
  au bon document et au bon RagSpace, texte exploitable, présence déterministe de la
  réponse de référence et de ses valeurs structurantes dans le chunk attendu.

Aucun LLM, aucun embedding, aucun modèle NLI : uniquement de la normalisation et de
la recherche de sous-chaîne / de valeurs structurantes (nombres, dates, versions,
ports, IPv4, noms de fichiers, références alphanumériques).

Convention MVP : la « réponse de référence » d'un cas est portée par le champ
``expected_answer_contains`` (liste de termes attendus). Les cas ``should_answer=false``
(no-answer) ne sont PAS soumis aux contrôles de réponse/document (§2.8).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .dataset import BenchmarkCase

# --- Codes d'erreur stables (identifiants, pas seulement des messages) ---------
CODE_EXPECTED_DOCUMENT_NOT_FOUND = "expected_document_not_found"
CODE_EXPECTED_CHUNK_NOT_FOUND = "expected_chunk_not_found"
CODE_CHUNK_DOCUMENT_MISMATCH = "chunk_document_mismatch"
CODE_CHUNK_RAGSPACE_MISMATCH = "chunk_ragspace_mismatch"
CODE_EMPTY_CHUNK_TEXT = "empty_chunk_text"
CODE_REFERENCE_ANSWER_NOT_FOUND = "reference_answer_not_found"
CODE_STRUCTURED_VALUE_NOT_FOUND = "structured_value_not_found"
CODE_QUESTION_CONTAINS_ANSWER = "question_contains_answer"

# Codes qui rendent un cas OBSOLÈTE (dérive du corpus après réindexation).
OBSOLETE_CODES = frozenset(
    {
        CODE_EXPECTED_DOCUMENT_NOT_FOUND,
        CODE_EXPECTED_CHUNK_NOT_FOUND,
        CODE_CHUNK_DOCUMENT_MISMATCH,
        CODE_CHUNK_RAGSPACE_MISMATCH,
        CODE_EMPTY_CHUNK_TEXT,
    }
)
# Codes qui rendent un cas INVALIDE (erreur d'écriture du benchmark).
INVALID_CODES = frozenset(
    {
        CODE_REFERENCE_ANSWER_NOT_FOUND,
        CODE_STRUCTURED_VALUE_NOT_FOUND,
    }
)

STATUS_VALID = "valid"
STATUS_OBSOLETE = "obsolete"
STATUS_INVALID = "invalid"

# Seuil de significativité pour "question contient trivialement la réponse".
_QUESTION_CONTAINS_MIN_LEN = 4


# --- Résolveur de corpus injecté (implémentation Django fournie par la commande) --
@dataclass(frozen=True)
class ChunkRef:
    chunk_id: str
    document_key: str  # filename (la vérité terrain du dataset = filenames)
    rag_space_id: str
    text: str


@runtime_checkable
class CorpusResolver(Protocol):
    def find_document(self, *, rag_space_id: str, document_key: str) -> bool: ...

    def find_chunk(self, *, rag_space_id: str, chunk_id: str) -> ChunkRef | None: ...

    def document_chunk_texts(self, *, rag_space_id: str, document_key: str) -> list[str]: ...


# --- Résultat de validation ---------------------------------------------------
@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: str  # "error" | "warning"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class CaseValidation:
    case_id: str
    status: str = STATUS_VALID
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        if issue.severity == "error":
            self.errors.append(issue)
        else:
            self.warnings.append(issue)

    def finalize(self) -> "CaseValidation":
        codes = {i.code for i in self.errors}
        if codes & OBSOLETE_CODES:
            self.status = STATUS_OBSOLETE
        elif codes & INVALID_CODES:
            self.status = STATUS_INVALID
        else:
            self.status = STATUS_VALID
        return self

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_VALID

    def error_kind(self) -> str | None:
        """Catégorie d'erreur pour le runner (None si valide)."""
        if self.status == STATUS_OBSOLETE:
            return "obsolete_reference"
        if self.status == STATUS_INVALID:
            return "invalid_dataset_case"
        return None

    def primary_message(self) -> str:
        return self.errors[0].message if self.errors else ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }


# --- Normalisation & valeurs structurantes ------------------------------------
_APOS = {"’": "'", "ʼ": "'", "‘": "'", "`": "'"}
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Minuscules + NFKC + apostrophes unifiées + espaces normalisés.

    Conserve chiffres, points, deux-points et tirets (indispensables pour matcher
    ports, IPv4, versions, dates ISO et noms de fichiers)."""
    t = unicodedata.normalize("NFKC", text or "")
    for k, v in _APOS.items():
        t = t.replace(k, v)
    t = t.lower()
    t = _WS_RE.sub(" ", t)
    return t.strip()


_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_DATE_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_REF_RE = re.compile(r"\b[A-Z]{2,}-\d{2,}(?:-[A-Z0-9]+)*\b")
_FILENAME_RE = re.compile(
    r"\b[\w-]+\.(?:ya?ml|json|pdf|txt|csv|md|png|jpe?g|conf|ini|cfg|py|js|ts|toml|xlsx?|docx?|pptx?)\b",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)+\b")
_DECIMAL_RE = re.compile(r"\b\d+\.\d+\b")
_INTEGER_RE = re.compile(r"\b\d+\b")

# Ordre = priorité ; chaque motif consomme son empreinte pour éviter la sur-extraction
# (ne pas extraire "192" depuis "<hôte-gateway>").
_STRUCTURED_PATTERNS = (
    _IPV4_RE,
    _DATE_ISO_RE,
    _REF_RE,
    _FILENAME_RE,
    _VERSION_RE,
    _DECIMAL_RE,
    _INTEGER_RE,
)


def extract_structured_values(text: str) -> list[str]:
    """Extrait les valeurs structurantes (IPv4, dates, versions, ports/entiers, noms de
    fichiers, références) d'un texte, sans double comptage."""
    values: list[str] = []
    remaining = text or ""
    for pat in _STRUCTURED_PATTERNS:
        for m in pat.finditer(remaining):
            values.append(m.group(0))
        remaining = pat.sub(" ", remaining)
    # dédup en conservant l'ordre
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _is_atomic(term: str) -> bool:
    """Terme « court et atomique » : un seul token, ou une valeur structurante."""
    t = (term or "").strip()
    if not t:
        return False
    if " " in normalize(t):
        return False
    return True


# --- Validation structurelle (pure) -------------------------------------------
def validate_case_structure(case: BenchmarkCase) -> CaseValidation:
    """Contrôles internes au cas, sans corpus (pur)."""
    res = CaseValidation(case_id=case.id)

    if not case.question.strip():
        res.add(ValidationIssue(CODE_REFERENCE_ANSWER_NOT_FOUND, "question vide", "error"))

    if case.is_no_answer:
        return res.finalize()

    # Question contenant trivialement un terme de réponse significatif.
    nq = normalize(case.question)
    for term in case.expected_answer_contains:
        nt = normalize(term)
        if len(nt) >= _QUESTION_CONTAINS_MIN_LEN and nt in nq:
            res.add(
                ValidationIssue(
                    CODE_QUESTION_CONTAINS_ANSWER,
                    f"la question contient déjà le terme de réponse {term!r}",
                    "warning",
                )
            )
    return res.finalize()


# --- Validation documentaire (corpus injecté) ---------------------------------
def validate_case_against_corpus(
    case: BenchmarkCase,
    *,
    rag_space_id: str,
    corpus: CorpusResolver,
) -> CaseValidation:
    """Cohérence documentaire d'un cas positif contre le corpus réel.

    Les cas no-answer ne sont pas soumis aux contrôles de document/chunk/réponse.
    """
    res = CaseValidation(case_id=case.id)
    if case.is_no_answer:
        return res.finalize()

    haystack_parts: list[str] = []

    # 1) Chunks attendus (vérité terrain fine).
    for chunk_id in case.expected_chunk_ids:
        ref = corpus.find_chunk(rag_space_id=rag_space_id, chunk_id=chunk_id)
        if ref is None:
            res.add(
                ValidationIssue(
                    CODE_EXPECTED_CHUNK_NOT_FOUND,
                    f"chunk attendu introuvable: {chunk_id!r}",
                    "error",
                )
            )
            continue
        if str(ref.rag_space_id) != str(rag_space_id):
            res.add(
                ValidationIssue(
                    CODE_CHUNK_RAGSPACE_MISMATCH,
                    f"chunk {chunk_id!r} appartient à un autre RagSpace",
                    "error",
                )
            )
            continue
        if case.expected_documents and ref.document_key not in case.expected_documents:
            res.add(
                ValidationIssue(
                    CODE_CHUNK_DOCUMENT_MISMATCH,
                    f"chunk {chunk_id!r} n'appartient pas au document déclaré "
                    f"({ref.document_key!r} ∉ {list(case.expected_documents)})",
                    "error",
                )
            )
            continue
        if not (ref.text or "").strip():
            res.add(
                ValidationIssue(
                    CODE_EMPTY_CHUNK_TEXT,
                    f"chunk {chunk_id!r} sans texte exploitable",
                    "error",
                )
            )
            continue
        haystack_parts.append(ref.text)

    # 2) Documents attendus (existence + repli du haystack si pas de chunk explicite).
    for doc_key in case.expected_documents:
        if not corpus.find_document(rag_space_id=rag_space_id, document_key=doc_key):
            res.add(
                ValidationIssue(
                    CODE_EXPECTED_DOCUMENT_NOT_FOUND,
                    f"document attendu introuvable: {doc_key!r}",
                    "error",
                )
            )
            continue
        if not case.expected_chunk_ids:
            haystack_parts.extend(
                corpus.document_chunk_texts(rag_space_id=rag_space_id, document_key=doc_key)
            )

    # Si l'existence a échoué, ne pas prétendre valider le contenu.
    if res.errors:
        return res.finalize()

    # 3) Présence déterministe de la réponse de référence et de ses valeurs.
    if haystack_parts:
        _check_reference_presence(case, haystack_parts, res)

    return res.finalize()


def _check_reference_presence(
    case: BenchmarkCase, haystack_parts: list[str], res: CaseValidation
) -> None:
    haystack = normalize(" \n ".join(haystack_parts))
    for term in case.expected_answer_contains:
        raw = str(term).strip()
        if not raw:
            continue
        nt = normalize(raw)
        if nt and nt not in haystack:
            severity = "error" if _is_atomic(raw) else "warning"
            res.add(
                ValidationIssue(
                    CODE_REFERENCE_ANSWER_NOT_FOUND,
                    f"réponse de référence {raw!r} absente du chunk attendu",
                    severity,
                )
            )
        # Valeurs structurantes du terme : toujours bloquantes si absentes.
        for value in extract_structured_values(raw):
            nv = normalize(value)
            if nv and nv not in haystack:
                res.add(
                    ValidationIssue(
                        CODE_STRUCTURED_VALUE_NOT_FOUND,
                        f"valeur structurante {value!r} absente du chunk attendu",
                        "error",
                    )
                )


def validate_case(
    case: BenchmarkCase,
    *,
    rag_space_id: str | None,
    corpus: CorpusResolver | None,
) -> CaseValidation:
    """Combine structure + corpus (si résolveur fourni) en un seul verdict."""
    structural = validate_case_structure(case)
    if corpus is None or rag_space_id is None:
        return structural
    corpus_res = validate_case_against_corpus(
        case, rag_space_id=rag_space_id, corpus=corpus
    )
    merged = CaseValidation(case_id=case.id)
    for issue in structural.errors + structural.warnings:
        merged.add(issue)
    for issue in corpus_res.errors + corpus_res.warnings:
        merged.add(issue)
    return merged.finalize()

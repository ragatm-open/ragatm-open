"""Chargement et validation du dataset de benchmark RAG (format JSONL versionné).

Un cas = une ligne JSON. Schéma (cf. backend/benchmarks/datasets/*.jsonl) :

    {
      "id": "q001",                       # str, requis, unique
      "rag_space": "demo-fr",             # str, requis (nom logique de l'espace)
      "question": "...",                  # str, requis, non vide
      "expected_answer_contains": [...],  # list[str], optionnel
      "expected_documents": [...],        # list[str] (filenames), optionnel
      "expected_chunk_ids": [...],        # list[str], optionnel
      "must_cite": true,                  # bool, optionnel (défaut: should_answer)
      "should_answer": true,              # bool, requis
      "tags": ["fr", "single-doc"]        # list[str], optionnel
    }

La validation est volontairement stricte : un dataset cassé doit échouer tôt,
pas produire des métriques silencieusement fausses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class DatasetError(ValueError):
    """Erreur de schéma/format dans un dataset de benchmark. Porte un ``code`` stable."""

    def __init__(self, message: str, code: str = "invalid_case_schema"):
        super().__init__(message)
        self.code = code


# Codes stables des erreurs de chargement (préflight amont).
ERR_UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
ERR_INVALID_CASE_SCHEMA = "invalid_case_schema"
ERR_UNSUPPORTED_TASK_TYPE = "unsupported_task_type"
ERR_DUPLICATE_CASE_ID = "duplicate_case_id"

TASK_RETRIEVAL_LOOKUP = "retrieval_lookup"


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    rag_space: str
    question: str
    should_answer: bool
    expected_answer_contains: tuple[str, ...] = ()
    expected_documents: tuple[str, ...] = ()
    expected_chunk_ids: tuple[str, ...] = ()
    must_cite: bool = False
    tags: tuple[str, ...] = ()
    # --- Métadonnées d'analyse (dataset étendu P1.7) — optionnelles ---
    category: str = ""          # fact | strong_id | morphology | multi_doc | list | table | no_answer | adversarial
    difficulty: str = ""        # easy | medium | hard
    fact_group_id: str = ""     # regroupe les paraphrases d'un même fait (anti-fuite / diversité)
    no_answer_reason: str = ""  # sous-type no-answer (property_absent, entity_absent, …)
    # Format du document source (benchmark terrain) : pdf | docx | xlsx | txt | …
    # Permet de mesurer le retrieval PAR FORMAT (les classeurs XLSX se comportent
    # très différemment des paragraphes PDF/DOCX).
    format: str = ""
    # --- Provenance de schéma (loader v2) — un cas v1 reste à 1 ---
    # Un cas v2 est produit APRÈS résolution stable : ses ``expected_chunk_ids`` sont
    # des UUID COURANTS résolus en mémoire (jamais écrits dans le dataset canonique).
    source_schema_version: int = 1
    match_policy: str = "any"

    @property
    def is_no_answer(self) -> bool:
        return not self.should_answer

    @property
    def is_multi_doc(self) -> bool:
        return len(self.expected_documents) >= 2


def _as_str_list(value: Any, *, field_name: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DatasetError(f"case {case_id!r}: champ {field_name!r} doit être une liste")
    out: list[str] = []
    for v in value:
        if not isinstance(v, (str, int, float)):
            raise DatasetError(
                f"case {case_id!r}: {field_name!r} contient un élément non scalaire: {v!r}"
            )
        s = str(v).strip()
        if s:
            out.append(s)
    return tuple(out)


def _as_opt_str(value: Any) -> str:
    """Chaîne normalisée (vide si None/non-str)."""
    return value.strip() if isinstance(value, str) else ""


def parse_case(obj: dict[str, Any]) -> BenchmarkCase:
    """Valide et convertit un dict brut en ``BenchmarkCase`` (lève ``DatasetError``)."""
    if not isinstance(obj, dict):
        raise DatasetError(f"chaque ligne doit être un objet JSON, reçu: {type(obj).__name__}")

    cid = obj.get("id")
    if not isinstance(cid, str) or not cid.strip():
        raise DatasetError(f"champ 'id' requis et non vide (reçu: {cid!r})")
    cid = cid.strip()

    rag_space = obj.get("rag_space")
    if not isinstance(rag_space, str) or not rag_space.strip():
        raise DatasetError(f"case {cid!r}: champ 'rag_space' requis et non vide")

    question = obj.get("question")
    if not isinstance(question, str) or not question.strip():
        raise DatasetError(f"case {cid!r}: champ 'question' requis et non vide")

    if "should_answer" not in obj:
        raise DatasetError(f"case {cid!r}: champ 'should_answer' requis (bool)")
    should_answer = obj.get("should_answer")
    if not isinstance(should_answer, bool):
        raise DatasetError(f"case {cid!r}: 'should_answer' doit être un booléen")

    expected_documents = _as_str_list(
        obj.get("expected_documents"), field_name="expected_documents", case_id=cid
    )
    if should_answer and not expected_documents and not obj.get("expected_chunk_ids"):
        # cas positif sans vérité terrain documentaire : recall non calculable
        raise DatasetError(
            f"case {cid!r}: cas 'should_answer=true' doit fournir 'expected_documents' "
            f"ou 'expected_chunk_ids' (sinon recall@k non mesurable)"
        )

    must_cite = obj.get("must_cite")
    if must_cite is None:
        must_cite = bool(should_answer)
    elif not isinstance(must_cite, bool):
        raise DatasetError(f"case {cid!r}: 'must_cite' doit être un booléen")

    return BenchmarkCase(
        id=cid,
        rag_space=rag_space.strip(),
        question=question.strip(),
        should_answer=should_answer,
        expected_answer_contains=_as_str_list(
            obj.get("expected_answer_contains"),
            field_name="expected_answer_contains",
            case_id=cid,
        ),
        expected_documents=expected_documents,
        expected_chunk_ids=_as_str_list(
            obj.get("expected_chunk_ids"), field_name="expected_chunk_ids", case_id=cid
        ),
        must_cite=must_cite,
        tags=_as_str_list(obj.get("tags"), field_name="tags", case_id=cid),
        category=_as_opt_str(obj.get("category")),
        difficulty=_as_opt_str(obj.get("difficulty")),
        fact_group_id=_as_opt_str(obj.get("fact_group_id")),
        no_answer_reason=_as_opt_str(obj.get("no_answer_reason")),
        format=_as_opt_str(obj.get("format")),
    )


def parse_jsonl(lines: Iterable[str]) -> list[BenchmarkCase]:
    """Parse des lignes JSONL en cas validés ; ignore lignes vides et commentaires ``#``."""
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"ligne {lineno}: JSON invalide ({exc})") from exc
        case = parse_case(obj)
        if case.id in seen_ids:
            raise DatasetError(f"ligne {lineno}: id dupliqué {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError("dataset vide : aucun cas valide trouvé")
    return cases


def load_dataset(path: str | Path) -> list[BenchmarkCase]:
    """Charge un fichier JSONL v1 et renvoie la liste des cas validés.

    Compat historique INCHANGÉE : rejette une ligne v2 (pas de champ ``id``/
    ``should_answer``). Le chemin mixte v1/v2 passe par ``load_mixed_dataset``.
    """
    p = Path(path)
    if not p.exists():
        raise DatasetError(f"dataset introuvable: {p}")
    with p.open("r", encoding="utf-8") as fh:
        return parse_jsonl(fh)


# --------------------------------------------------------------------------- #
# Schéma v2 (vérité terrain stable) — cas CHARGÉ, non encore résolu
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoadedV2Case:
    """Cas v2 AVANT résolution : aucune référence UUID courante. La résolution vers
    les IDs de l'ingestion se fait dans ``benchmark.preflight`` (en mémoire)."""

    case_id: str
    rag_space: str
    question: str
    task_type: str
    ground_truth: Any  # stable_ground_truth.StableGroundTruth
    schema_version: int = 2


def detect_schema_version(obj: dict[str, Any]) -> int:
    """Détection EXPLICITE (jamais devinée) : ``schema_version`` absent -> v1 (contrat
    historique) ; ``== 2`` -> v2 ; toute autre valeur -> erreur."""
    if "schema_version" not in obj:
        return 1
    v = obj.get("schema_version")
    if v == 2:
        return 2
    raise DatasetError(
        f"schema_version non supporté: {v!r} (attendu absent ou 2)",
        code=ERR_UNSUPPORTED_SCHEMA_VERSION,
    )


def parse_v2_case(obj: dict[str, Any]) -> LoadedV2Case:
    """Valide un cas v2 et délègue la vérité terrain au modèle typé du lot précédent
    (source de vérité unique pour version/SHA/locator/evidence/match_policy)."""
    from apps.rag_core.benchmark import stable_ground_truth as sgt

    cid = obj.get("case_id")
    if not isinstance(cid, str) or not cid.strip():
        raise DatasetError("cas v2: 'case_id' requis et non vide", code=ERR_INVALID_CASE_SCHEMA)
    cid = cid.strip()
    rag_space = obj.get("rag_space")
    if not isinstance(rag_space, str) or not rag_space.strip():
        raise DatasetError(f"cas {cid!r}: 'rag_space' requis", code=ERR_INVALID_CASE_SCHEMA)
    question = obj.get("question")
    if not isinstance(question, str) or not question.strip():
        raise DatasetError(f"cas {cid!r}: 'question' requise", code=ERR_INVALID_CASE_SCHEMA)
    task_type = obj.get("task_type")
    if not isinstance(task_type, str) or not task_type.strip():
        raise DatasetError(f"cas {cid!r}: 'task_type' requis", code=ERR_INVALID_CASE_SCHEMA)
    if task_type != TASK_RETRIEVAL_LOOKUP:
        raise DatasetError(
            f"cas {cid!r}: task_type non supporté par ce lot: {task_type!r}",
            code=ERR_UNSUPPORTED_TASK_TYPE,
        )
    if "ground_truth" not in obj or not isinstance(obj.get("ground_truth"), dict):
        raise DatasetError(f"cas {cid!r}: 'ground_truth' requis", code=ERR_INVALID_CASE_SCHEMA)
    try:
        gt = sgt.parse_case(obj)  # vérifie schema_version==2 + valide toute la GT
    except sgt.StableGroundTruthError as e:
        raise DatasetError(f"cas {cid!r}: {e}", code=e.code) from e
    return LoadedV2Case(case_id=cid, rag_space=rag_space.strip(), question=question.strip(),
                        task_type=task_type, ground_truth=gt)


def load_mixed_dataset(path: str | Path) -> tuple[list[BenchmarkCase], list[LoadedV2Case]]:
    """Charge un JSONL pouvant mêler cas v1 et v2. Renvoie ``(v1_cases, v2_cases)``.

    Détection explicite du schéma par ligne ; unicité des identifiants VÉRIFIÉE à
    travers les deux familles (``id`` v1 et ``case_id`` v2 partagent l'espace de noms).
    """
    p = Path(path)
    if not p.exists():
        raise DatasetError(f"dataset introuvable: {p}")
    v1: list[BenchmarkCase] = []
    v2: list[LoadedV2Case] = []
    seen: set[str] = set()
    with p.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"ligne {lineno}: JSON invalide ({exc})") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"ligne {lineno}: objet JSON attendu")
            version = detect_schema_version(obj)
            if version == 2:
                case = parse_v2_case(obj)
                key = case.case_id
                if key in seen:
                    raise DatasetError(f"ligne {lineno}: case_id dupliqué {key!r}",
                                       code=ERR_DUPLICATE_CASE_ID)
                seen.add(key)
                v2.append(case)
            else:
                bc = parse_case(obj)
                if bc.id in seen:
                    raise DatasetError(f"ligne {lineno}: id dupliqué {bc.id!r}",
                                       code=ERR_DUPLICATE_CASE_ID)
                seen.add(bc.id)
                v1.append(bc)
    if not v1 and not v2:
        raise DatasetError("dataset vide : aucun cas valide trouvé")
    return v1, v2

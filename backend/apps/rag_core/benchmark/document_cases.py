"""Dérivation mécanique de cas de benchmark depuis des documents textuels (PDF/DOCX) — P1.9-d.

Module **pur** : ni Django, ni I/O, ni réseau, ni LLM, ni accès à l'environnement. Il ne
connaît pas le corpus Mission — il est testable sur fixtures synthétiques.

Principe (identique à `xlsx_cases`, étendu au texte) : la vérité terrain est **lue dans le
chunk**, jamais produite par le moteur RagATM ni par un modèle génératif. On ne cherche pas
à *comprendre* le document : on n'exploite que des faits **explicitement écrits** sous une
forme reconnaissable (« Libellé : valeur », date rattachée à un libellé, identifiant nommé,
liste numérotée sous un en-tête). Tout le reste est rejeté, avec un motif explicite.

Les cas no-answer ne sont **jamais inventés** : l'absence est prouvée par une recherche
exhaustive sur l'intégralité du RagSpace (cf. `prove_absence`), jamais par le retrieval.
"""
from __future__ import annotations

import re
from typing import Any

from apps.rag_core.benchmark.xlsx_cases import (  # noqa: F401 (réexports voulus)
    REJ_EMPTY_VALUE,
    REJ_QUESTION_CONTAINS_ANSWER,
    REJ_SENSITIVE,
    REJ_VALUE_NOT_IN_CHUNK,
    classify_value,
    fold,
    is_sensitive,
    split_by_fact_group,
    split_leaks,
)

GENERATION_METHOD_FACT = "deterministic_source_fact"
GENERATION_METHOD_ABSENCE = "deterministic_absence_check"
STATUS_VALIDATED = "validated"
STATUS_PENDING = "pending"

SCOPE_GLOBAL = "entire_rag_space"

# Sous-catégories answerable (§4.2 / §5.2)
SUB_DIRECT_FACT = "direct_fact"
SUB_DATE = "date_or_deadline"
SUB_IDENTIFIER = "strong_identifier"
SUB_LIST = "list_or_enumeration"
SUB_NUMERIC = "numeric_value"
SUB_RELATION = "explicit_relation"
SUB_PROCEDURE = "procedure_step"

# Sous-types no-answer (§6.2)
NA_PROPERTY_ABSENT = "property_absent"
NA_ENTITY_ABSENT = "entity_absent"
NA_VALUE_ABSENT = "value_absent"
NA_RELATION_ABSENT = "relation_absent"
NA_VERSION_ABSENT = "version_absent"
NA_INVENTED_REFERENCE = "invented_reference"
NA_FEATURE_ABSENT = "feature_absent"

NA_SUB_TYPES = (
    NA_PROPERTY_ABSENT, NA_ENTITY_ABSENT, NA_VALUE_ABSENT, NA_RELATION_ABSENT,
    NA_VERSION_ABSENT, NA_INVENTED_REFERENCE, NA_FEATURE_ABSENT,
)

# Motifs de rejet propres au texte
REJ_NO_FACT = "no_extractable_fact"
REJ_DEGRADED_OCR = "degraded_ocr_text"
REJ_UNREADABLE = "unreadable_extraction"
REJ_TITLE_ONLY = "title_only_chunk"
REJ_INCOMPLETE_TEXT = "incomplete_text"
REJ_PERSON_DATA = "person_data_not_anonymisable"
REJ_FIGURE_REFERENCE = "reference_to_absent_figure"
REJ_VISUAL_CHECK = "requires_visual_check"
REJ_NO_ANCHOR = "no_discriminant_anchor"
REJ_ABSENCE_NOT_PROVEN = "absence_not_proven"
REJ_ABSENCE_SCOPE_NOT_GLOBAL = "absence_scope_not_global"
REJ_SINGLE_DOC_SUFFICIENT = "single_document_sufficient"
REJ_MISSING_SOURCE = "missing_source_chunk"
REJ_MISSING_GROUP = "missing_fact_group"
REJ_NOT_VALIDATED = "not_marked_validated"


# --------------------------------------------------------------------------
# Qualité du texte source (§4.3 — critères d'exclusion)
# --------------------------------------------------------------------------
_FIGURE_RE = re.compile(r"\b(voir|cf\.?|selon)\s+(la\s+)?(figure|schéma|graphique|annexe)\b", re.I)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]{2,}")
_GLYPH_RE = re.compile(r"(?:/\d+\s*){6,}")


def ocr_quality_ratio(text: str) -> float:
    """Part de caractères « plausibles » (lettres, chiffres, ponctuation courante).

    Un texte OCR dégradé accumule des caractères parasites : le ratio s'effondre.
    """
    t = text or ""
    if not t:
        return 0.0
    ok = sum(1 for c in t if c.isalnum() or c.isspace() or c in ".,;:!?'\"()[]%€$/\\-–—_+=&@#*|«»’")
    return ok / len(t)


def is_degraded(text: str, *, min_quality: float = 0.92, min_words: int = 12) -> bool:
    """Texte inexploitable comme source de vérité terrain (OCR dégradé / trop pauvre)."""
    if ocr_quality_ratio(text) < min_quality:
        return True
    return len(_WORD_RE.findall(text or "")) < min_words


def is_glyph_garbage(text: str) -> bool:
    """Extraction PDF ratée : la police n'expose pas de table ToUnicode, l'extracteur a
    émis des **index de glyphes** (`/1 /44 /42 □`) au lieu de caractères.

    Ce n'est pas de l'OCR dégradé — le texte n'a jamais existé. Distinguer les deux
    importe : l'un se rattrape par un ré-OCR, l'autre exige de corriger l'extraction.
    """
    return bool(_GLYPH_RE.search(text or ""))


def is_title_only(text: str, *, max_lines: int = 2, max_chars: int = 120) -> bool:
    """Chunk ne portant qu'un titre : aucun fait à en tirer."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    return len(lines) <= max_lines and len((text or "").strip()) <= max_chars


def references_absent_figure(text: str) -> bool:
    """Renvoie à une figure/annexe non présente dans le texte : réponse non vérifiable."""
    return bool(_FIGURE_RE.search(text or ""))


def fact_is_truncated(text: str, value: str, *, margin: int = 2) -> bool:
    """La VALEUR touche-t-elle un bord du chunk (donc potentiellement amputée) ?

    On ne juge pas le chunk (un chunk de continuation commence légitimement en minuscule
    et porte pourtant des faits complets) : on juge **le fait extrait**.
    """
    t, v = text or "", (value or "").strip()
    if not v:
        return True
    pos = t.find(v)
    if pos < 0:
        return True
    return pos + len(v) >= len(t.rstrip()) - margin


# --------------------------------------------------------------------------
# Confidentialité : libellés porteurs de données personnelles (§14)
# --------------------------------------------------------------------------
_PERSON_LABELS = (
    "redacteur", "interlocuteur", "personne", "responsable", "contact", "destinataire",
    "emetteur", "expediteur", "notification envoyee a", "auteur", "valideur", "approbateur",
    "signataire", "gestionnaire", "correspondant", "demandeur", "beneficiaire",
)


def is_person_label(label: str) -> bool:
    """Un libellé désignant une personne porte un nom : écarté, jamais anonymisé.

    Anonymiser rendrait la question introuvable dans le corpus (l'alias n'y existe pas) et
    détruirait le cas — on préfère perdre le fait.
    """
    f = fold(label)
    return any(p in f for p in _PERSON_LABELS)


# --------------------------------------------------------------------------
# Extraction de faits « Libellé : valeur »
# --------------------------------------------------------------------------
_COLON_RE = re.compile(r"^[ \t]*([A-ZÉÈÀÇÎÔÛ][^:\n]{2,44}?)[ \t]*:[ \t]*(\S[^\n]{0,118})$", re.M)

_STOP_LABEL_WORDS = frozenset("de du des la le les l d en a au aux et ou un une".split())


def _label_words(label: str) -> list[str]:
    return [w for w in fold(label).split() if w not in _STOP_LABEL_WORDS]


def is_plausible_label(label: str) -> bool:
    """Un libellé est court et nominal. Filtre le bruit (phrases capturées par le `:`)."""
    words = fold(label).split()
    if not (1 <= len(words) <= 6):
        return False
    if len(label) > 44:
        return False
    # Un libellé entièrement en capitales sur plusieurs mots est en général un titre de
    # section tronqué, pas un libellé de champ.
    return not (label.isupper() and len(words) > 2)


def extract_label_value_facts(text: str, *, extra_labels: tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Faits « Libellé : valeur » (et « Libellé valeur » pour des libellés connus).

    Renvoie des dicts {label, value, value_type}. Aucun filtrage de confidentialité ici :
    c'est le rôle de `validate_document_case` / de l'appelant (séparation des responsabilités).
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for label, value in _COLON_RE.findall(text or ""):
        label, value = label.strip(), value.strip()
        if not is_plausible_label(label) or not value:
            continue
        if (fold(label), fold(value)) in seen:
            continue
        seen.add((fold(label), fold(value)))
        out.append({"label": label, "value": value, "value_type": classify_value(value)})

    # Libellés connus sans séparateur (« Échéance Février 2026 »), fréquents dans les
    # descriptifs mis en page en deux colonnes puis remis à plat à l'extraction.
    for lab in extra_labels:
        pat = re.compile(rf"^[ \t]*{re.escape(lab)}[ \t]+(\S[^\n]{{0,118}})$", re.M | re.I)
        for value in pat.findall(text or ""):
            value = value.strip()
            if not value or (fold(lab), fold(value)) in seen:
                continue
            seen.add((fold(lab), fold(value)))
            out.append({"label": lab, "value": value, "value_type": classify_value(value)})
    return out


# --------------------------------------------------------------------------
# Listes numérotées
# --------------------------------------------------------------------------
_ITEM_RE = re.compile(r"^[ \t]*(\d{1,2})[.)]\s+(\S.{2,})$", re.M)


def extract_numbered_list(text: str) -> tuple[str, list[str]]:
    """(en-tête, items) d'une liste numérotée. ('', []) si aucune liste exploitable.

    L'en-tête est la dernière ligne non vide **avant** le premier item : sans en-tête
    explicite, la question serait non située — donc non vérifiable.
    """
    items = _ITEM_RE.findall(text or "")
    if len(items) < 2:
        return "", []
    numbers = [int(n) for n, _ in items]
    # Une vraie liste commence à 1 et s'incrémente : sinon ce sont des numéros épars.
    if numbers != list(range(1, len(numbers) + 1)):
        return "", []

    first = _ITEM_RE.search(text)
    before = (text[: first.start()] if first else "").split("\n")
    header = next((ln.strip() for ln in reversed(before) if ln.strip()), "")
    return header, [body.strip() for _, body in items]


# --------------------------------------------------------------------------
# Dates et identifiants
# --------------------------------------------------------------------------
_MONTHS = ("janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre")
_DATE_RE = re.compile(
    rf"\b(\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:\d{{1,2}}\s+)?(?:{_MONTHS})\s+\d{{4}}|[TS][1-4]\s*\d{{4}})\b",
    re.I,
)
_IDENT_RE = re.compile(r"\b([A-Z]{2,6}\d{2,3}(?:[/_-][A-Z0-9_]{2,12})+|[A-Z]{2,4}_[A-Z]{2,3}_\d{2}_[A-Z]{2})\b")
_VERSION_RE = re.compile(r"\bversion\s+(\d+\.\d+)\b", re.I)


def extract_dates(text: str) -> list[str]:
    return list(dict.fromkeys(m.strip() for m in _DATE_RE.findall(text or "")))


def extract_identifiers(text: str) -> list[str]:
    return list(dict.fromkeys(_IDENT_RE.findall(text or "")))


def extract_versions(text: str) -> list[str]:
    return list(dict.fromkeys(_VERSION_RE.findall(text or "")))


# --------------------------------------------------------------------------
# Gabarits de question (§4.3 / §5.3) — déterministes, jamais génératifs
# --------------------------------------------------------------------------
def build_fact_question(*, sub_category: str, label: str, anchor: str, doc_hint: str = "") -> str:
    """Question déterministe. `anchor` situe le fait (référence, section, titre)."""
    where = f" du document « {doc_hint} »" if doc_hint else ""
    a = f" pour « {anchor} »" if anchor else ""
    if sub_category == SUB_DATE:
        return f"Quelle est la date indiquée au champ « {label} »{a}{where} ?"
    if sub_category == SUB_IDENTIFIER:
        return f"Quelle référence est indiquée au champ « {label} »{a}{where} ?"
    if sub_category == SUB_NUMERIC:
        return f"Quelle valeur est indiquée au champ « {label} »{a}{where} ?"
    return f"Que contient le champ « {label} »{a}{where} ?"


def build_list_question(*, header: str, n_items: int, ask_count: bool = False) -> str:
    if ask_count:
        return f"Combien d'éléments sont énumérés sous « {header} » ?"
    return f"Quels sont les {n_items} éléments énumérés sous « {header} » ?"


def build_procedure_question(*, header: str, step_number: int) -> str:
    """Question locale et vérifiable (§5.3) : jamais « résume » ni « quelle est la meilleure »."""
    return f"Quelle est l'étape {step_number} listée sous « {header} » ?"


def build_relation_question(*, subject: str, label: str) -> str:
    return f"Quel élément le document associe-t-il à « {subject} » au titre de « {label} » ?"


def build_multidoc_question(*, subject: str, label_a: str, label_b: str) -> str:
    return (f"Pour « {subject} », quels sont d'une part « {label_a} » et d'autre part "
            f"« {label_b} » ? Indiquez les deux.")


# --------------------------------------------------------------------------
# Difficulté (§8) — déduite de la structure, jamais ajustée pour atteindre une cible
# --------------------------------------------------------------------------
def assign_difficulty(*, sub_category: str, multi_doc: bool, long_chunk: bool,
                      competing_labels: int = 1, anchor_scope: str = "chunk") -> str:
    if multi_doc:
        return "hard"
    # Ancre portée par le document mais absente du chunk cible : le retrieval doit relier
    # la référence au bon passage sans partage lexical. C'est objectivement plus dur.
    if anchor_scope == "document":
        return "hard"
    if sub_category in (SUB_RELATION, SUB_LIST):
        return "hard" if competing_labels > 1 else "medium"
    if sub_category == SUB_PROCEDURE:
        return "medium"
    if competing_labels > 1 or long_chunk:
        return "medium"
    return "easy"


def count_competing_labels(label: str, all_labels: list[str]) -> int:
    """Libellés partageant un mot porteur : « Niveau de risque proposé » vs « … ajusté ».

    Plus il y a de libellés voisins, plus la question doit être précise — et plus le cas est
    difficile pour le retrieval.
    """
    words = set(_label_words(label))
    if not words:
        return 1
    return sum(1 for other in all_labels if words & set(_label_words(other)))


# --------------------------------------------------------------------------
# Preuve d'absence (§6.4) — jamais le retrieval, toujours le corpus entier
# --------------------------------------------------------------------------
def term_variants(term: str) -> list[str]:
    """Variantes simples d'un terme : casse, accents, singulier/pluriel élémentaire.

    Volontairement modeste : une variante manquée produit un faux no-answer, donc on reste
    sur des transformations sûres et on complète par plusieurs termes à l'appel.
    """
    base = fold(term)
    out = {base}
    if base.endswith("s"):
        out.add(base[:-1])
    else:
        out.add(base + "s")
    if base.endswith("ale"):
        out.add(base[:-3] + "aux")
    return sorted(v for v in out if v)


def prove_absence(terms: list[str], folded_corpus: list[str], *,
                  scope: str = SCOPE_GLOBAL) -> dict[str, Any]:
    """Compte les chunks contenant l'un des termes (ou variantes) sur TOUT le corpus.

    `folded_corpus` = textes de **tous** les chunks du RagSpace, déjà normalisés par `fold`.
    matches_found > 0 ⇒ le cas no-answer est faux et doit être rejeté.
    """
    checked: list[str] = []
    for t in terms:
        checked.extend(term_variants(t))
    checked = sorted(set(checked))
    matches = sum(1 for text in folded_corpus if any(v in text for v in checked))
    return {"terms_checked": checked, "matches_found": matches, "scope": scope}


def validate_no_answer_case(case: dict[str, Any]) -> list[str]:
    """Motifs de rejet d'un cas no-answer (vide = exploitable)."""
    errs: list[str] = []
    if case.get("expected_answerability") != "no_answer":
        errs.append("wrong_answerability")
    if case.get("expected_source_chunk_ids"):
        errs.append("no_answer_case_has_sources")
    if case.get("expected_document_ids"):
        errs.append("no_answer_case_has_documents")
    if case.get("sub_category") not in NA_SUB_TYPES:
        errs.append("unknown_no_answer_sub_type")

    proof = ((case.get("ground_truth") or {}).get("absence_check")) or {}
    if not proof or not proof.get("terms_checked"):
        errs.append(REJ_ABSENCE_NOT_PROVEN)
    if proof.get("matches_found", -1) != 0:
        errs.append("term_present_in_corpus")
    if proof.get("scope") != SCOPE_GLOBAL:
        errs.append(REJ_ABSENCE_SCOPE_NOT_GLOBAL)
    if not case.get("fact_group_id"):
        errs.append(REJ_MISSING_GROUP)
    if case.get("human_validation_status") != STATUS_VALIDATED:
        errs.append(REJ_NOT_VALIDATED)
    return errs


# --------------------------------------------------------------------------
# Validation mécanique des cas answerable (§11)
# --------------------------------------------------------------------------
def validate_document_case(case: dict[str, Any], chunk_texts: dict[str, str], *,
                           expected_status: str = STATUS_VALIDATED) -> list[str]:
    """Motifs de rejet d'un cas PDF/DOCX (vide = exploitable).

    `chunk_texts` : {chunk_id: texte} pour **tous** les chunks attendus du cas.
    """
    errs: list[str] = []
    gt = case.get("ground_truth") or {}

    if not case.get("question"):
        errs.append("empty_question")
    if case.get("expected_answerability") != "answerable":
        errs.append("wrong_answerability")

    sources = case.get("expected_source_chunk_ids") or []
    if not sources:
        errs.append(REJ_MISSING_SOURCE)
    if not case.get("expected_document_ids"):
        errs.append("missing_expected_document")
    for cid in sources:
        if cid not in chunk_texts:
            errs.append("expected_chunk_not_found")
            break

    values = gt.get("expected_values") or ([gt["expected_value"]] if gt.get("expected_value") else [])
    if not values or not all(str(v).strip() for v in values):
        errs.append(REJ_EMPTY_VALUE)

    # Chaque valeur doit être littéralement présente dans AU MOINS un chunk attendu.
    folded = {cid: fold(t) for cid, t in chunk_texts.items()}
    for v in values:
        if v and not any(fold(str(v)) in folded.get(cid, "") for cid in sources):
            errs.append(REJ_VALUE_NOT_IN_CHUNK)
            break

    # La question ne doit pas contenir la réponse (sinon le cas ne teste rien).
    q = fold(case.get("question", ""))
    anchor = fold(str(gt.get("anchor", "")))
    for v in values:
        sv = fold(str(v))
        if sv and len(sv) >= 3 and sv in q and sv != anchor:
            errs.append(REJ_QUESTION_CONTAINS_ANSWER)
            break

    for v in values:
        if is_sensitive(str(v)):
            errs.append(REJ_SENSITIVE)
            break
    if is_person_label(str(gt.get("label", ""))):
        errs.append(REJ_PERSON_DATA)

    if not case.get("fact_group_id"):
        errs.append(REJ_MISSING_GROUP)
    if case.get("human_validation_status") != expected_status:
        errs.append(REJ_NOT_VALIDATED)
    return errs


def validate_multidoc_case(case: dict[str, Any], chunk_texts: dict[str, str]) -> list[str]:
    """Un cas multi-doc doit VRAIMENT exiger deux documents (§7.3).

    Si un seul chunk porte toutes les valeurs attendues, le cas est un faux multi-doc.
    """
    errs = validate_document_case(case, chunk_texts)
    sources = case.get("expected_source_chunk_ids") or []
    docs = case.get("expected_document_ids") or []
    if len(docs) < 2 or len(sources) < 2:
        errs.append(REJ_SINGLE_DOC_SUFFICIENT)
        return errs

    gt = case.get("ground_truth") or {}
    values = [fold(str(v)) for v in (gt.get("expected_values") or []) if str(v).strip()]
    folded = {cid: fold(t) for cid, t in chunk_texts.items()}
    # Un chunk qui porte TOUTES les valeurs rend le second document inutile.
    for cid in sources:
        if values and all(v in folded.get(cid, "") for v in values):
            errs.append(REJ_SINGLE_DOC_SUFFICIENT)
            break
    # Chaque source doit apporter au moins une valeur (sinon elle est décorative).
    for cid in sources:
        if values and not any(v in folded.get(cid, "") for v in values):
            errs.append("source_contributes_nothing")
            break
    return errs

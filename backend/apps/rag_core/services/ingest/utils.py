# backend/apps/rag_core/services/ingest/utils.py
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Iterable, Sequence

from django.conf import settings

logger = logging.getLogger(__name__)


def filter_nonblank_specs(specs: Sequence) -> list:
    """Retire les ChunkSpec dont le texte est vide/blanc AVANT embeddings.

    Aucun chunk vide/blanc ne doit atteindre le provider d'embeddings (la garde
    fail-closed de ``embed_texts`` ferait sinon échouer TOUT le document). Le
    filtrage préserve l'ordre : comme la liste ``texts`` est reconstruite à partir
    des specs filtrés, l'alignement ``spec -> vecteur`` (``zip(specs, vectors)``)
    reste garanti.
    """
    from apps.llm_router.embeddings import is_blank_text

    return [s for s in (specs or []) if not is_blank_text(getattr(s, "text", None))]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes((text or "").encode("utf-8"))


def settings_int(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def settings_bool(name: str, default: bool = False) -> bool:
    v = getattr(settings, name, default)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def resolve_chunk_budget(space) -> tuple[int, int]:
    """Taille et chevauchement de fragment EFFECTIFS pour cet espace.

    Une seule règle : ``min(recommandation produit, plafond du MODÈLE)``.

    Trois limites distinctes se rencontrent ici, et les confondre a coûté cher :

    1. **la recommandation produit** — `RAG_CHUNK_SIZE_CHARS` (2 500). C'est la
       granularité choisie pour le RAG, indépendamment de toute contrainte technique ;
    2. **le plafond du modèle** — `GOVERNED_MODEL_MAX_INPUT_CHARS`. Une contrainte
       DURE : au-delà, la Gateway refuse le texte et l'indexation du document ÉCHOUE.
       Elle n'est pas négociable, mais elle n'est pas non plus une cible ;
    3. **le repli conservateur** — `RAG_GOVERNED_CHUNK_SIZE_CHARS` (1 200), employé
       quand le modèle gouverné est INCONNU de la table. « Inconnu » ne vaut jamais
       « sans limite ».

    Ce que ce code faisait avant, et pourquoi c'était faux
    -----------------------------------------------------
    Le plafond était attaché au **provider** : tout espace `ai_gateway` était découpé
    à 1 200. C'était exact tant que le provider n'exposait qu'`fr-en-retrieval`, dont
    c'est bien la limite. `legacy-768` a rompu l'équivalence — il accepte 6 000 — et
    la règle par provider s'est mise à redécouper un corpus sans raison.

    La conséquence n'est pas cosmétique : elle transforme une migration de
    **provenance d'embedding** (mêmes textes, nouveaux vecteurs, identité vérifiée)
    en migration de **structure documentaire** (nouveaux fragments, nouveaux
    identifiants, corpus non comparable à celui d'avant).

    Un espace NON gouverné garde son comportement à l'octet près.
    """
    from apps.llm_router.capabilities import governed_model_max_input_chars
    from apps.llm_router.embeddings import resolve_space_embedding, space_is_governed

    chunk_size = settings_int("RAG_CHUNK_SIZE_CHARS", 2500)
    overlap = settings_int("RAG_CHUNK_OVERLAP_CHARS", 250)

    if space is None or not space_is_governed(space):
        return chunk_size, overlap

    _provider, model = resolve_space_embedding(space)
    plafond = governed_model_max_input_chars(model or "")
    if plafond is None:
        # Modèle gouverné hors table : on se rabat sur la borne conservatrice plutôt
        # que sur l'absence de borne. Le journal existe parce qu'un repli silencieux
        # ferait passer une lacune de déclaration pour une décision.
        plafond = settings_int("RAG_GOVERNED_CHUNK_SIZE_CHARS", 1200)
        logger.warning(
            "ingest.chunk_budget modèle gouverné inconnu de la table de capacités — "
            "repli conservateur appliqué",
            extra={"embedding_model": model or "", "fallback_max_chars": plafond},
        )

    if plafond >= chunk_size:
        # Le modèle suit la granularité produit : rien à écrêter, budget INCHANGÉ.
        return chunk_size, overlap

    governed_overlap = settings_int("RAG_GOVERNED_CHUNK_OVERLAP_CHARS", 180)
    # Garde-fou : jamais un chevauchement qui empêcherait la fenêtre d'avancer.
    governed_overlap = max(0, min(governed_overlap, max(0, plafond - 1)))
    return plafond, governed_overlap


# ---------------------------------------------------------------------
# Document date extraction (from CONTENT, not filename)
# ---------------------------------------------------------------------

_MONTHS_FR: dict[str, int] = {
    "janvier": 1,
    "janv": 1,
    "janv.": 1,
    "fevrier": 2,
    "février": 2,
    "fevr": 2,
    "févr": 2,
    "fevr.": 2,
    "févr.": 2,
    "mars": 3,
    "avril": 4,
    "avr": 4,
    "avr.": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "juil": 7,
    "juil.": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "sept": 9,
    "sept.": 9,
    "octobre": 10,
    "oct": 10,
    "oct.": 10,
    "novembre": 11,
    "nov": 11,
    "nov.": 11,
    "decembre": 12,
    "décembre": 12,
    "dec": 12,
    "déc": 12,
    "dec.": 12,
    "déc.": 12,
}

_MONTHS_EN: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

# Common explicit formats
_RE_ISO = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_RE_DMY_SLASH = re.compile(r"\b(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])/(20\d{2})\b")
_RE_DMY_DASH = re.compile(r"\b(0?[1-9]|[12]\d|3[01])-(0?[1-9]|1[0-2])-(20\d{2})\b")
_RE_DMY_DOT = re.compile(r"\b(0?[1-9]|[12]\d|3[01])\.(0?[1-9]|1[0-2])\.(20\d{2})\b")

# “12 septembre 2025”, “12 sept. 2025”
_RE_FR_TEXT = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])\s+"
    r"(janvier|janv\.?|février|fevrier|févr\.?|fevr\.?|mars|avril|avr\.?|mai|juin|"
    r"juillet|juil\.?|août|aout|septembre|sept\.?|octobre|oct\.?|novembre|nov\.?|"
    r"décembre|decembre|déc\.?|dec\.?)\s+(20\d{2})\b",
    re.IGNORECASE,
)

# “12 September 2025”
_RE_EN_TEXT_DMY = re.compile(
    r"\b(0?[1-9]|[12]\d|3[01])\s+"
    r"(january|jan\.?|february|feb\.?|march|mar\.?|april|apr\.?|may|june|jun\.?|july|jul\.?|"
    r"august|aug\.?|september|sept?\.?|october|oct\.?|november|nov\.?|december|dec\.?)\s+"
    r"(20\d{2})\b",
    re.IGNORECASE,
)

# “September 12, 2025” (comma optional)
_RE_EN_TEXT_MDY = re.compile(
    r"\b(january|jan\.?|february|feb\.?|march|mar\.?|april|apr\.?|may|june|jun\.?|july|jul\.?|"
    r"august|aug\.?|september|sept?\.?|october|oct\.?|november|nov\.?|december|dec\.?)\s+"
    r"(0?[1-9]|[12]\d|3[01])(?:,)?\s+"
    r"(20\d{2})\b",
    re.IGNORECASE,
)

# Optional patterns like "Le 12/09/2025" / "Publié le 12 septembre 2025"
_RE_PREFIX_CONTEXT = re.compile(
    r"\b(publi[eé]\s+le|publication\s*:|date\s*:|le)\b",
    re.IGNORECASE,
)


def _strip_accents_lower(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("\u00a0", " ")
    s = (
        s.replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("ë", "e")
        .replace("à", "a")
        .replace("â", "a")
        .replace("î", "i")
        .replace("ï", "i")
        .replace("ô", "o")
        .replace("ö", "o")
        .replace("ù", "u")
        .replace("û", "u")
        .replace("ü", "u")
        .replace("ç", "c")
    )
    return s


def _month_from_token(tok: str) -> int | None:
    """
    Convert a month token to month number (1-12).
    Supports FR (with/without accents) and EN, accepts abbreviations with '.' and loose prefixes.
    """
    if not tok:
        return None

    t = _strip_accents_lower(tok).replace("\u00a0", " ").strip()
    t = t.replace(".", "")
    if not t:
        return None

    # direct lookups
    if t in _MONTHS_FR:
        return _MONTHS_FR[t]
    if t in _MONTHS_EN:
        return _MONTHS_EN[t]

    # tolerate prefixes (e.g. "septemb", "september", "janv", "févr")
    for k, v in _MONTHS_FR.items():
        kk = _strip_accents_lower(k).replace(".", "")
        if t.startswith(kk):
            return v
    for k, v in _MONTHS_EN.items():
        kk = _strip_accents_lower(k).replace(".", "")
        if t.startswith(kk):
            return v

    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        dt = date(int(y), int(m), int(d))
    except Exception:
        return None

    # Guard rails against garbage
    if dt.year > date.today().year + 2:
        return None
    if dt.year < 2000:
        return None
    return dt


def _collect_candidates(s: str) -> list[tuple[int, date]]:
    """
    Return list of (position, date) candidates found in text.
    """
    candidates: list[tuple[int, date]] = []

    for m in _RE_ISO.finditer(s):
        dt = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            candidates.append((m.start(), dt))

    for m in _RE_DMY_SLASH.finditer(s):
        dt = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if dt:
            candidates.append((m.start(), dt))

    for m in _RE_DMY_DASH.finditer(s):
        dt = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if dt:
            candidates.append((m.start(), dt))

    for m in _RE_DMY_DOT.finditer(s):
        dt = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if dt:
            candidates.append((m.start(), dt))

    for m in _RE_FR_TEXT.finditer(s):
        d = int(m.group(1))
        mo = _month_from_token(m.group(2) or "")
        y = int(m.group(3))
        if mo:
            dt = _safe_date(y, mo, d)
            if dt:
                candidates.append((m.start(), dt))

    for m in _RE_EN_TEXT_DMY.finditer(s):
        d = int(m.group(1))
        mo = _month_from_token(m.group(2) or "")
        y = int(m.group(3))
        if mo:
            dt = _safe_date(y, mo, d)
            if dt:
                candidates.append((m.start(), dt))

    for m in _RE_EN_TEXT_MDY.finditer(s):
        mo = _month_from_token(m.group(1) or "")
        d = int(m.group(2))
        y = int(m.group(3))
        if mo:
            dt = _safe_date(y, mo, d)
            if dt:
                candidates.append((m.start(), dt))

    return candidates


def extract_document_date_from_text(text: str, *, max_chars: int = 40_000) -> date | None:
    """
    Extract a plausible "document date" from raw text (best-effort, conservative).
    Heuristic:
      - only scan first max_chars
      - prefer dates close to contextual words (Publié le / Date / Publication / Le)
      - otherwise, pick earliest match position (headers are usually early)
    """
    if not text:
        return None

    s = str(text).replace("\u00a0", " ")
    s = s[:max_chars]

    candidates = _collect_candidates(s)
    if not candidates:
        return None

    # Prefer candidates that are close to contextual markers
    ctx_positions = [m.start() for m in _RE_PREFIX_CONTEXT.finditer(s)]
    if ctx_positions:
        best: tuple[int, date] | None = None
        best_dist = 10**9
        for pos, dt in candidates:
            dist = min(abs(pos - cpos) for cpos in ctx_positions)
            if dist < best_dist:
                best_dist = dist
                best = (pos, dt)
        if best and best_dist <= 250:
            return best[1]

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def extract_document_date_from_pages(
    pages: Sequence[tuple[int, str]] | Iterable[str],
    *,
    max_pages: int = 2,
    max_chars: int = 60_000,
) -> date | None:
    """
    Extract document date from the first pages of a PDF.

    Accepts either:
      - Sequence[(page_number, text)]
      - Iterable[str] (already page texts)

    We only inspect the first max_pages and first max_chars characters (combined).
    """
    if pages is None:
        return None

    page_texts: list[str] = []
    try:
        for item in pages:  # type: ignore[assignment]
            if isinstance(item, tuple) and len(item) == 2:
                page_texts.append(str(item[1] or ""))
            else:
                page_texts.append(str(item or ""))
            if len(page_texts) >= max_pages:
                break
    except Exception:
        return None

    if not page_texts:
        return None

    joined = "\n\n".join(page_texts).replace("\u00a0", " ")
    joined = joined[:max_chars]

    return extract_document_date_from_text(joined, max_chars=max_chars)

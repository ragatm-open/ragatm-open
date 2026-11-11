# backend/apps/chat/services/chat_sources.py
from __future__ import annotations

from typing import Any

from apps.rag_core.domain.provenance import SOURCE_TYPE_DOMAIN_CORPUS, resolve_source_type
from apps.rag_core.services.query.source_meta import project_source_meta

from .chat_audit import safe_str

from apps.rag_core.services.query.audit import SOURCES_SCHEMA_VERSION  # noqa: E402

# UI hygiene (stream + chat response)
MAX_SNIPPET_CHARS_UI = 320


def snippet_from_text(text: Any, *, max_chars: int = MAX_SNIPPET_CHARS_UI) -> str:
    s = safe_str(text).strip()
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _coerce_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _coerce_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _coerce_iso_date(v: Any) -> str | None:
    """
    Best-effort: normalize a date-like value to ISO string.

    Accepts:
      - "YYYY-MM-DD" (returns as-is if plausible)
      - datetime/date objects (stringified by safe_str)
      - "YYYY-MM-DDTHH:MM:SS..." -> keeps date part
      - any other -> None
    """
    s = safe_str(v).strip()
    if not s:
        return None

    # keep only date part if it's an ISO datetime
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        head = s[:10]
        y, m, d = head[:4], head[5:7], head[8:10]
        if y.isdigit() and m.isdigit() and d.isdigit():
            return head

    return None


def _extract_document_date(ch: dict) -> str | None:
    """
    Pull document_date from common locations:
      - top-level: ch["document_date"]
      - source: ch["source"]["document_date"]
      - source/meta: ch["source"]["meta"]["document_date"]
      - chunk meta: ch["meta"]["document_date"]
    """
    if not isinstance(ch, dict):
        return None

    # 1) top-level
    iso = _coerce_iso_date(ch.get("document_date"))
    if iso:
        return iso

    # 2) source-level
    src = ch.get("source") or {}
    if isinstance(src, dict):
        iso = _coerce_iso_date(src.get("document_date"))
        if iso:
            return iso

        meta = src.get("meta") or {}
        if isinstance(meta, dict):
            iso = _coerce_iso_date(meta.get("document_date"))
            if iso:
                return iso

    # 3) chunk meta
    meta2 = ch.get("meta") or {}
    if isinstance(meta2, dict):
        iso = _coerce_iso_date(meta2.get("document_date"))
        if iso:
            return iso

    return None


def _extract_source_meta(ch: dict) -> dict:
    """
    Best-effort: returns the 'meta' dict from retrieval if present.
    Supports:
      - ch["source"]["meta"]
      - ch["meta"]
    """
    if not isinstance(ch, dict):
        return {}
    src = ch.get("source") or {}
    if isinstance(src, dict):
        meta = src.get("meta") or {}
        if isinstance(meta, dict):
            return meta
    meta2 = ch.get("meta") or {}
    if isinstance(meta2, dict):
        return meta2
    return {}


def normalize_source_for_api(ch: dict, rank: int) -> dict[str, Any]:
    """
    Convert a retrieval chunk into a compact UI-friendly source object
    compatible with your frontend SourceV1 renderer.
    """
    ch = ch or {}
    src = ch.get("source") or {}
    if not isinstance(src, dict):
        src = {}

    filename = safe_str(src.get("filename") or src.get("label") or "").strip() or None
    page_number = _coerce_int(src.get("page_number"))

    # Some retrievals (older code paths) may use top-level page_number/filename
    if page_number is None:
        page_number = _coerce_int(ch.get("page_number"))
    if not filename:
        filename = safe_str(ch.get("filename")).strip() or None

    score = _coerce_float(ch.get("score"))

    # ✅ Date extracted from document content (ISO string if available)
    document_date = _extract_document_date(ch)

    # Métadonnées de CITATION (feuille, plage, titre…). Projetées sur une allowlist :
    # `Chunk.meta` est un JSONField libre qui porte aussi des détails d'implémentation
    # (entités regex sur chaque chunk PDF, offsets de caractères, JSON complet des
    # lignes d'un tableau P3) — inutiles au client et coûteux en charge utile.
    # L'AUDIT applique EXACTEMENT la même projection : les deux ne doivent pas diverger.
    meta = project_source_meta(_extract_source_meta(ch))

    # Typage des sources (corpus métier vs document privé). Une provenance ABSENTE
    # devient `unknown` : l'afficher « Workspace » serait affirmer une origine qu'on
    # n'a pas. Cf. rag_core/domain/provenance.py.
    source_type = resolve_source_type(ch, src)

    out = {
        # v3 : `meta` porte désormais la citation Excel (feuille + plage).
        "schema_version": SOURCES_SCHEMA_VERSION,
        "source_type": source_type,
        "rank": int(rank),
        "document_id": src.get("document_id") or ch.get("document_id"),
        "chunk_id": ch.get("chunk_id"),
        "score": score,
        "filename": filename,
        "page_number": page_number,
        "document_date": document_date,  # ✅ NEW FIELD
        "snippet": snippet_from_text(ch.get("text")),
        "meta": meta if meta else {},  # safe (optional)
    }

    # Métadonnées corpus pour l'affichage des badges côté chat (Phase 9).
    if source_type == SOURCE_TYPE_DOMAIN_CORPUS:
        out["corpus_id"] = src.get("corpus_id") or ch.get("corpus_id")
        out["corpus_name"] = src.get("corpus_name") or ch.get("corpus_name")
        out["business_domain_id"] = src.get("business_domain_id") or ch.get("business_domain_id")
        out["business_domain_name"] = src.get("business_domain_name") or ch.get("business_domain_name")

    return out


def dedup_sources(sources: list[dict]) -> list[dict]:
    """
    Déduplique les sources pour l'API (pas l'audit).

    IMPORTANT: ici, on suppose que `sources` est déjà normalisé
    (filename/page_number/snippet/document_date au top-level).

    ⚠️ Deux plages différentes du MÊME classeur sont deux sources DIFFÉRENTES :
    « Budget.xlsx — Synthèse — B8:F17 » et « Budget.xlsx — Risques — A2:G12 » ne doivent
    jamais fusionner. Le `chunk_id` suffit dans le cas nominal ; la clé de repli (chunks
    sans id) intègre donc feuille + plage, faute de quoi deux extraits distincts d'un
    même fichier — tous deux sans page — s'effondreraient l'un sur l'autre.
    """
    seen: set[tuple] = set()
    out: list[dict] = []

    for s in sources or []:
        if not isinstance(s, dict):
            continue

        cid = safe_str(s.get("chunk_id")).strip()
        if cid:
            key = ("chunk_id", cid)
        else:
            meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
            key = (
                safe_str(s.get("filename")).strip(),
                safe_str(s.get("document_date")).strip(),
                int(s.get("page_number") or 0),
                safe_str(meta.get("sheet_name")).strip(),
                safe_str(meta.get("cell_range")).strip(),
                safe_str(s.get("snippet"))[:80].strip(),
            )

        if key in seen:
            continue
        seen.add(key)
        out.append(s)

    return out


def sources_for_api(retrieval_sources: list[dict]) -> list[dict]:
    normalized = [normalize_source_for_api(ch, i) for i, ch in enumerate(retrieval_sources or [], start=1)]
    normalized = dedup_sources(normalized)
    for i, s in enumerate(normalized, start=1):
        s["rank"] = i
    return normalized

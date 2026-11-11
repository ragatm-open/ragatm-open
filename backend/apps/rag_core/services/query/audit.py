from __future__ import annotations

from typing import Any

from apps.rag_core.domain.provenance import (
    SOURCE_TYPE_DOMAIN_CORPUS as _SOURCE_TYPE_DOMAIN_CORPUS,
    SOURCE_TYPE_UNKNOWN as _SOURCE_TYPE_UNKNOWN,
    SOURCE_TYPE_WORKSPACE as _SOURCE_TYPE_WORKSPACE,
    resolve_source_type,
)

from .settings import MAX_SNIPPET_CHARS_AUDIT, MAX_REQUEST_ID_CHARS
from .source_meta import project_source_meta

# Public contract (shared with API / audit).
# v2 (corpus métier) : ajout de `source_type` et des métadonnées de provenance
# (corpus/domaine pour les sources corpus, workspace/rag_space pour le privé).
# v3 (lot E3, Excel) : ajout de `meta` — la projection des métadonnées de citation
#     (feuille, plage, titre…), IDENTIQUE à celle exposée par l'API. Sans elle, une
#     citation Excel serait affichée mais **non auditable** : le log dirait « source =
#     Budget_2026.xlsx » sans jamais dire QUELLE feuille ni QUELLES cellules.
#     Additif : les logs v1/v2 antérieurs restent lisibles tels quels, et rien n'est migré.
SOURCES_SCHEMA_VERSION = 3

# Énumération CANONIQUE (cf. rag_core/domain/provenance.py) : réexportée ici pour
# les consommateurs historiques, jamais redéfinie.
SOURCE_TYPE_WORKSPACE = _SOURCE_TYPE_WORKSPACE
SOURCE_TYPE_DOMAIN_CORPUS = _SOURCE_TYPE_DOMAIN_CORPUS
SOURCE_TYPE_UNKNOWN = _SOURCE_TYPE_UNKNOWN


def _resolve_source_type(ch: dict[str, Any], source: dict[str, Any]) -> str:
    """source_type effectif d'un chunk. Provenance absente => ``unknown``.

    L'audit ne DEVINE pas : consigner « workspace_document » pour une source dont
    l'origine a été perdue en amont produirait une trace fausse — et c'est précisément
    la trace qu'on relit lors d'un incident de provenance.
    """
    return resolve_source_type(ch, source)


def normalize_sources_for_audit(
    selected_chunks: list[dict[str, Any]] | None,
    *,
    max_snippet_chars: int = MAX_SNIPPET_CHARS_AUDIT,
) -> list[dict[str, Any]]:
    """Contrat public v2 : liste ordonnée de sources TYPÉES (rank 1..N)."""
    normalized: list[dict[str, Any]] = []

    for rank, ch in enumerate(selected_chunks or [], start=1):
        source = ch.get("source") or {}
        if not isinstance(source, dict):
            source = {}

        raw_text = ch.get("text") or ""
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)
        text = raw_text.strip()

        source_type = _resolve_source_type(ch, source)

        # Même projection que l'API (`chat_sources.normalize_source_for_api`) : ce
        # qu'on audite doit être exactement ce que l'utilisateur a vu.
        meta = project_source_meta(
            source.get("meta") if isinstance(source.get("meta"), dict) else ch.get("meta")
        )

        entry = {
            "schema_version": SOURCES_SCHEMA_VERSION,
            "source_type": source_type,
            "rank": rank,
            "document_id": source.get("document_id"),
            "chunk_id": ch.get("chunk_id"),
            "score": float(ch.get("score") or 0.0),
            "filename": source.get("filename"),
            "page_number": source.get("page_number"),
            "snippet": text[:max_snippet_chars],
        }
        # Additif : absent quand il n'y a rien à citer de plus (PDF simple, TXT…),
        # donc les logs restent identiques à v2 pour ces sources.
        if meta:
            entry["meta"] = meta

        if source_type == SOURCE_TYPE_DOMAIN_CORPUS:
            entry["corpus_id"] = source.get("corpus_id")
            entry["corpus_name"] = source.get("corpus_name")
            entry["business_domain_id"] = source.get("business_domain_id")
            entry["business_domain_name"] = source.get("business_domain_name")
        else:
            # Provenance privée (utile à l'audit inter-workspace).
            if source.get("workspace_id") is not None:
                entry["workspace_id"] = source.get("workspace_id")
            if source.get("rag_space_id") is not None:
                entry["rag_space_id"] = source.get("rag_space_id")

        # Champs ADDITIFS, présents uniquement si un rerank a eu lieu. `score` porte la
        # pertinence du retrieval AVANT reranking, `rerank_score` la sortie réelle du
        # cross-encoder et `rerank_rank` le rang qu'il a choisi : la trace peut donc
        # distinguer les trois sans ambiguïté, ce que l'ordinal rendait impossible.
        if ch.get("rerank_score") is not None:
            entry["rerank_score"] = float(ch.get("rerank_score") or 0.0)
        if ch.get("rerank_rank") is not None:
            try:
                entry["rerank_rank"] = int(ch.get("rerank_rank"))
            except (TypeError, ValueError):
                pass
        normalized.append(entry)
    return normalized


def extract_request_id(user) -> str:
    """Best-effort extraction of X-Request-ID."""
    try:
        req = getattr(user, "_request", None)
        if not req:
            return ""
        return (req.META.get("HTTP_X_REQUEST_ID") or "").strip()
    except Exception:
        return ""


def normalize_request_id(value: str | None) -> str:
    if not value:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    return v[:MAX_REQUEST_ID_CHARS]

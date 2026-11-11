# backend/apps/chat/services/intents/news.py
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from apps.llm_router.egress import LlmExecutionContext
from apps.llm_router.router import generate_text
from apps.rag_core.models import Document
from apps.rag_core.repositories.chunk_repository import search_chunks_hybrid

from ..format_prefs import escape_table_cell, wants_table_format
from .common import model_has_field, parse_iso_date, safe_json_extract


_NEWS_HINTS = re.compile(
    r"\b(news|actu|actualit(?:é|e)s?|quoi de neuf|dernier(?:es)?|récent(?:es)?|mise à jour|updates?)\b",
    re.IGNORECASE,
)


def llm_route_news_or_none(*, question: str, llm_provider: str, llm_model: str, context=None) -> dict[str, Any] | None:
    """
    Use the current LLM as a lightweight router for "news" intent.
    Returns a plan dict or None if not news / parsing failed.

    Plan format:
    {
      "intent": "news",
      "term": "Nordexia",
      "time_window_days": 90,
      "top_n_docs": 5
    }
    """
    q = (question or "").strip()
    if not q:
        return None

    # Cheap heuristic gate: only call LLM router if question looks like news/actu
    if not _NEWS_HINTS.search(q):
        return None

    prompt = (
        "Tu es un routeur de requêtes pour un moteur de recherche interne de documents.\n"
        "Ta tâche: détecter si l'utilisateur demande des 'news/actualités' sur une entité.\n"
        "Réponds UNIQUEMENT en JSON valide, sans texte autour.\n\n"
        "Champs:\n"
        "- intent: 'news' ou 'none'\n"
        "- term: entité principale (ex: Nordexia, Valteria) — 1 seule\n"
        "- time_window_days: entier (7 à 365). Par défaut 120.\n"
        "- top_n_docs: entier (3 à 10). Par défaut 5.\n\n"
        f"Question: {q}\n\n"
        "JSON:"
    )

    try:
        out = generate_text(
            prompt=prompt, provider=llm_provider, model=llm_model,
            context=context or LlmExecutionContext.system_local_only(
                operation="chat.news_route"
            ),
            operation="chat.news_route",
        )
        plan = safe_json_extract((out.get("text") or "").strip())
        if not isinstance(plan, dict):
            return None
        if plan.get("intent") != "news":
            return None

        term = (plan.get("term") or "").strip()
        if not term:
            return None

        top_n = plan.get("top_n_docs", 5)
        try:
            top_n = int(top_n)
        except Exception:
            top_n = 5
        top_n = max(3, min(10, top_n))

        tw = plan.get("time_window_days", 120)
        try:
            tw = int(tw)
        except Exception:
            tw = 120
        tw = max(7, min(365, tw))

        return {"intent": "news", "term": term, "top_n_docs": top_n, "time_window_days": tw}
    except Exception:
        return None


def fetch_documents_meta(*, rag_space_id, document_ids: list[str]) -> dict[str, dict[str, Any]]:
    """
    Returns mapping: doc_id -> {filename, created_at, document_date(iso or None)}
    Works with schema variants:
      - Document.document_date (preferred)
      - optional meta/metadata JSON fields (only if present)
    """
    if not document_ids:
        return {}

    has_doc_date = model_has_field(Document, "document_date")
    has_meta = model_has_field(Document, "meta")
    has_metadata = model_has_field(Document, "metadata")

    fields = ["id", "filename", "created_at"]
    if has_doc_date:
        fields.append("document_date")
    if has_meta:
        fields.append("meta")
    if has_metadata:
        fields.append("metadata")

    rows = Document.objects.filter(rag_space_id=rag_space_id, id__in=document_ids).values(*fields)

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        did = str(r.get("id"))
        iso: str | None = None

        if has_doc_date:
            dd = r.get("document_date")
            if isinstance(dd, date):
                iso = dd.isoformat()
            elif isinstance(dd, str) and dd.strip():
                iso = dd.strip()

        if not iso and has_meta:
            meta = r.get("meta")
            if isinstance(meta, dict):
                v = meta.get("document_date")
                if isinstance(v, str) and v.strip():
                    iso = v.strip()

        if not iso and has_metadata:
            md = r.get("metadata")
            if isinstance(md, dict):
                v = md.get("document_date")
                if isinstance(v, str) and v.strip():
                    iso = v.strip()

        out[did] = {
            "filename": r.get("filename") or "",
            "created_at": r.get("created_at"),
            "document_date": iso if parse_iso_date(iso) else None,
        }

    return out


def update_log_sources_best_effort(*, log_id: str | None, sources: list[dict[str, Any]]) -> None:
    """
    In news mode, we create the log via query_rag() (to keep audit pipeline),
    but the *true* sources shown to user are from the news retriever.

    This function makes RagQueryLog.sources consistent with what we returned.
    Best-effort: never raises.
    """
    if not log_id:
        return
    try:
        from apps.audit.models import RagQueryLog

        update_kwargs: dict[str, Any] = {"sources": sources}
        if model_has_field(RagQueryLog, "sources_schema_version"):
            update_kwargs["sources_schema_version"] = 1
        RagQueryLog.objects.filter(id=log_id).update(**update_kwargs)
    except Exception:
        return


def build_news_sources_from_retriever(
    *,
    rag_space_id,
    term: str,
    top_n_docs: int,
    time_window_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    News retriever (strictly grounded) that favors recent documents via Document.document_date,
    but uses indexed retrieval (hybrid/FTS) instead of icontains.

    Strategy:
    1) Retrieve candidate chunks with search_chunks_hybrid(question=term).
    2) Group by document_id (keep best score chunk per doc).
    3) Enrich with Document.document_date.
    4) Filter by time window if it doesn't wipe everything.
    5) Sort: document_date desc (null last), then score desc.
    6) Return facts + retrieval_sources compatible with sources_for_api().
    """
    term_norm = (term or "").strip()
    if not term_norm:
        return ([], [])

    top_n_docs = max(3, min(10, int(top_n_docs or 5)))
    window_days = max(7, min(365, int(time_window_days or 120)))
    cutoff = date.today() - timedelta(days=window_days)

    candidates = search_chunks_hybrid(
        rag_space_id=rag_space_id,
        question=term_norm,
        top_k=max(80, top_n_docs * 25),
        min_text_rank=0.0,  # recall first; filter after
    ) or []

    if not candidates:
        return ([], [])

    best_by_doc: dict[str, dict[str, Any]] = {}
    doc_ids: list[str] = []

    for c in candidates:
        src = (c or {}).get("source") or {}
        did = src.get("document_id") or src.get("document") or c.get("document_id")
        if not did:
            continue
        did = str(did)
        doc_ids.append(did)

        prev = best_by_doc.get(did)
        if prev is None:
            best_by_doc[did] = c
        else:
            try:
                if float(c.get("score") or 0.0) > float(prev.get("score") or 0.0):
                    best_by_doc[did] = c
            except Exception:
                pass

    doc_ids = list(dict.fromkeys(doc_ids))
    docs_meta = fetch_documents_meta(rag_space_id=rag_space_id, document_ids=doc_ids)

    items: list[tuple[date | None, float, str, dict[str, Any]]] = []
    for did, chunk in best_by_doc.items():
        meta = docs_meta.get(did) or {}
        dt = parse_iso_date(meta.get("document_date"))
        score = float(chunk.get("score") or 0.0)
        items.append((dt, score, did, chunk))

    within = [it for it in items if it[0] is not None and it[0] >= cutoff]
    if within:
        items = within

    # recent first (None last), then score
    items.sort(key=lambda it: (it[0] is None, -(it[0].toordinal() if it[0] else 0), -it[1]))
    items = items[:top_n_docs]

    facts: list[dict[str, Any]] = []
    retrieval_sources: list[dict[str, Any]] = []

    for rank, (dt, score, did, chunk) in enumerate(items, start=1):
        meta = docs_meta.get(did) or {}
        filename = meta.get("filename") or ((chunk.get("source") or {}).get("filename") or "")
        doc_date = dt.isoformat() if dt else None

        snippet = (chunk.get("text") or "").strip()
        if len(snippet) > 900:
            snippet = snippet[:900].rstrip() + "…"

        page_number = None
        try:
            page_number = (chunk.get("source") or {}).get("page_number")
        except Exception:
            page_number = None

        facts.append(
            {
                "rank": rank,
                "term": term_norm,
                "document_date": doc_date,
                "filename": filename,
                "page_number": page_number,
                "snippet": snippet,
            }
        )

        src = dict(chunk.get("source") or {})
        src.setdefault("meta", {})
        if doc_date:
            if isinstance(src["meta"], dict):
                src["meta"]["document_date"] = doc_date
            src["document_date"] = doc_date

        retrieval_sources.append(
            {
                "chunk_id": chunk.get("chunk_id") or chunk.get("id") or "",
                "text": snippet,
                "score": score,
                "source": src,
                "document_date": doc_date,
            }
        )

    return facts, retrieval_sources


def summarize_news_with_llm(
    *,
    question: str,
    term: str,
    facts: list[dict[str, Any]],
    llm_provider: str,
    llm_model: str,
    context=None,
    answer_language: str | None = None,
) -> str:
    """
    Strict grounded summarization: LLM only sees facts extracted from retriever.

    ``answer_language == "en"`` produit la variante anglaise (message, format, prompt) ;
    tout autre cas (fr / None / flag OFF) conserve le comportement français.
    """
    en = answer_language == "en"
    if not facts:
        if en:
            return (
                f"I found no information in your documents mentioning **{term}** "
                "to produce news."
            )
        return (
            f"Je n’ai trouvé aucune information dans vos documents qui mentionne **{term}** "
            "pour produire des news."
        )

    as_table = wants_table_format(question)

    if as_table:
        # Les valeurs sont pré-échappées : un '|' dans un nom de fichier ou un
        # extrait décalerait sinon toute la ligne du tableau.
        facts = [
            {
                key: escape_table_cell(value) if isinstance(value, str) else value
                for key, value in fact.items()
            }
            for fact in facts
        ]

    facts_json = json.dumps(facts, ensure_ascii=False)

    if en:
        if as_table:
            format_block = (
                "Format:\n"
                "- Respond with a valid GFM Markdown table.\n"
                "- Mandatory columns: | Date | Summary | Source |\n"
                "- One physical line per table row.\n"
                "- Mandatory separator line after the header: |---|---|---|\n"
                "- No paragraph or list inside a cell.\n"
                "- Concise cells.\n"
                "- '|' characters inside a cell must be escaped with '\\|'.\n"
                "- Source in the format: File name, p. X\n"
                "- Reverse chronological order (most recent first).\n"
                "- 5 rows maximum unless otherwise requested.\n\n"
            )
        else:
            format_block = (
                "Format:\n"
                "- 5 bullets maximum.\n"
                "- Each bullet: **YYYY-MM-DD** (or 'unknown date') — short summary (1-2 lines) — (File, p.X)\n\n"
            )
        prompt = (
            "You are an assistant that summarises information sourced EXCLUSIVELY from internal documents.\n"
            "Strict rules:\n"
            "- Use ONLY the facts provided in FACTS.\n"
            "- Do not complete with your own knowledge. Do not guess.\n"
            "- If a date is missing, write exactly: 'unknown date'.\n"
            "- Do not cite any external source.\n\n"
            "Goal:\n"
            "- Produce a 'News' section genuinely oriented towards the most recent documents.\n\n"
            f"{format_block}"
            f"User question: {question}\n"
            f"Main term: {term}\n\n"
            f"FACTS: {facts_json}\n\n"
            "Answer:"
        )
    else:
        if as_table:
            format_block = (
                "Format:\n"
                "- Réponds avec un tableau Markdown GFM valide.\n"
                "- Colonnes obligatoires: | Date | Résumé | Source |\n"
                "- Une ligne physique par ligne du tableau.\n"
                "- Ligne de séparation obligatoire après l'en-tête: |---|---|---|\n"
                "- Aucun paragraphe ni liste dans une cellule.\n"
                "- Cellules concises.\n"
                "- Les caractères '|' à l'intérieur d'une cellule doivent être échappés avec '\\|'.\n"
                "- Source au format: Nom du fichier, p. X\n"
                "- Ordre chronologique inverse (le plus récent en premier).\n"
                "- 5 lignes maximum sauf demande contraire.\n\n"
            )
        else:
            format_block = (
                "Format:\n"
                "- 5 puces maximum.\n"
                "- Chaque puce: **YYYY-MM-DD** (ou 'date inconnue') — résumé court (1-2 lignes) — (Fichier, p.X)\n\n"
            )
        prompt = (
            "Tu es un assistant qui résume des informations issues EXCLUSIVEMENT de documents internes.\n"
            "Règles strictes:\n"
            "- Tu ne dois utiliser QUE les faits fournis dans FACTS.\n"
            "- Ne complète pas avec ta connaissance. Ne devine pas.\n"
            "- Si une date est absente, écris exactement: 'date inconnue'.\n"
            "- Ne cite pas de source externe.\n\n"
            "Objectif:\n"
            "- Produire une section 'News' réellement orientée vers les documents les plus récents.\n\n"
            f"{format_block}"
            f"Question utilisateur: {question}\n"
            f"Terme principal: {term}\n\n"
            f"FACTS: {facts_json}\n\n"
            "Réponse:"
        )

    out = generate_text(
        prompt=prompt, provider=llm_provider, model=llm_model,
        # `facts_json` contient des EXTRAITS DE CORPUS (P0-C #12) : fail-closed.
        context=context or LlmExecutionContext.system_local_only(
            operation="chat.news_summary"
        ),
        operation="chat.news_summary",
    )
    return (out.get("text") or "").strip() or "Je n’ai pas réussi à résumer les informations disponibles."

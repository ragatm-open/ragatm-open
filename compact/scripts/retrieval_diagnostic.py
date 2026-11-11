"""Diagnostic de retrieval Compact (qualification interne, §9.3) — s'exécute DANS le conteneur backend :

    docker compose cp fixtures/corpus/manifest.json backend:/tmp/manifest.json
    docker compose exec -T -e DIAG_RAG_SPACE_ID=<uuid> -e DIAG_MANIFEST=/tmp/manifest.json \
        backend python manage.py shell -c "$(cat scripts/retrieval_diagnostic.py)"
    docker compose cp backend:/tmp/retrieval-diagnostic.json diag.json   # (les journaux vont sur stdout)

Pour chaque question synthétique du manifeste, mesure SÉPARÉMENT, sans modifier aucun
réglage : (1) le leg lexical FTS ; (2) le leg vectoriel AVANT seuil (top-k brut avec
similarités) et APRÈS le seuil effectif `RAG_MIN_SCORE` ; (3) l'identité d'embeddings
utilisée ; (4) la présence des documents attendus dans chaque leg. Le but est d'établir
si une réponse `partial`/vide relève de l'indexation, de l'identité du pipeline, du
retrieval, du seuil, de la fusion ou de la génération — pas de « réparer » en changeant
un seuil. Aucun appel de génération ici (les legs et le seuil suffisent au diagnostic) ;
le reranking et la génération sont observés par `scripts/e2e_qualification.py`.
Sortie : JSON écrit dans DIAG_OUT (défaut /tmp/retrieval-diagnostic.json, à récupérer par
`docker compose cp`) — aucun contenu documentaire, seulement noms de fichiers, identifiants et scores.
"""
import json
import os

from django.conf import settings

from apps.llm_router.embeddings import embed_texts_with_identity, resolve_space_embedding, space_is_governed
from apps.llm_router.egress import LlmExecutionContext
from apps.rag_core.domain.policies import DEFAULT_MIN_SCORE
from apps.rag_core.models import Document, RagSpace
from apps.rag_core.repositories.chunk_repository import search_chunks_hybrid, search_chunks_vector

try:
    from apps.llm_router.embeddings import TASK_QUERY
except Exception:  # pragma: no cover
    TASK_QUERY = "query"

space = RagSpace.objects.get(id=os.environ["DIAG_RAG_SPACE_ID"])
manifest = json.load(open(os.environ.get("DIAG_MANIFEST", "/tmp/manifest.json")))
questions = manifest["questions"] if isinstance(manifest, dict) else manifest
top_k = int(os.environ.get("DIAG_TOP_K", str(max(getattr(settings, "RAG_VECTOR_TOP_K", 8), 20))))
min_score = float(getattr(space, "min_score", None) or 0) if getattr(settings, "RAG_SPACE_MIN_SCORE_ENABLED", False) else DEFAULT_MIN_SCORE
min_text_rank = float(getattr(settings, "RAG_MIN_TEXT_RANK", 0.0) or 0.0)
provider, model = resolve_space_embedding(space)
governed = space_is_governed(space)
doc_names = {str(d.id): (d.filename or d.title or str(d.id)) for d in Document.objects.filter(rag_space=space)}


def _doc_of(ch):
    src = ch.get("source") if isinstance(ch.get("source"), dict) else {}
    for k in ("document_id", "doc_id"):
        if ch.get(k):
            return str(ch[k])
        if src.get(k):
            return str(src[k])
    return str((ch.get("document") or {}).get("id") or "")


def _summ(chunks):
    return [
        {"doc": doc_names.get(_doc_of(c), _doc_of(c)), "chunk_id": str(c.get("chunk_id") or c.get("id") or ""),
         "score": round(float(c.get("score") or 0), 4), "page": c.get("page")}
        for c in chunks
    ]


out = {
    "space": str(space.id), "embedding": {"provider": provider, "model": model, "governed": governed},
    "settings": {"RAG_MIN_SCORE_effective": min_score, "RAG_MIN_TEXT_RANK": min_text_rank, "top_k": top_k,
                 "RAG_VECTOR_TOP_K": getattr(settings, "RAG_VECTOR_TOP_K", None),
                 "RAG_RERANKER_POOL_K": getattr(settings, "RAG_RERANKER_POOL_K", None)},
    "documents": sorted(doc_names.values()), "questions": [],
}
for q in questions:
    rec = {"id": q.get("id"), "question_chars": len(q.get("question", "")), "expected_docs": q.get("expected_docs", []),
           "answerable": q.get("answerable")}
    try:
        fts = search_chunks_hybrid(rag_space_id=space.id, question=q["question"], top_k=top_k, min_text_rank=min_text_rank)
        rec["fts"] = {"n": len(fts), "top": _summ(fts[:5]),
                      "expected_present": sorted({doc_names.get(_doc_of(c), "") for c in fts} & set(q.get("expected_docs", [])))}
    except Exception as exc:  # noqa: BLE001 - diagnostic
        rec["fts"] = {"error": type(exc).__name__}
    try:
        emb = embed_texts_with_identity([q["question"]], dim=settings.RAG_EMBEDDING_DIM, provider=provider, model=model,
                                        task=TASK_QUERY, rag_space_id=space.id,
                                        context=LlmExecutionContext.for_rag_space(space, operation="embedding.query"))
        fam = getattr(emb, "family", None)
        raw = search_chunks_vector(rag_space_id=space.id, query_embedding=emb.vectors[0], top_k=top_k,
                                   embedding_provider=provider, embedding_model=model,
                                   pipeline_family=fam if governed else None)
        kept = [c for c in raw if float(c.get("score") or 0) >= min_score]
        rec["vector"] = {"pipeline_family": fam, "n_raw": len(raw), "n_after_threshold": len(kept),
                         "best_score": round(float(raw[0]["score"]), 4) if raw else None,
                         "top_raw": _summ(raw[:5]),
                         "expected_present_raw": sorted({doc_names.get(_doc_of(c), "") for c in raw} & set(q.get("expected_docs", []))),
                         "expected_present_after_threshold": sorted({doc_names.get(_doc_of(c), "") for c in kept} & set(q.get("expected_docs", [])))}
        if raw and not kept:
            rec["diagnosis"] = "vector_leg_emptied_by_threshold"
        elif not raw:
            rec["diagnosis"] = "vector_leg_no_candidates (indexation/identité)"
        elif rec["vector"]["expected_present_after_threshold"] or rec["fts"].get("expected_present"):
            rec["diagnosis"] = "expected_doc_reachable (fusion/rerank/génération à observer)"
        else:
            rec["diagnosis"] = "expected_doc_not_in_legs (retrieval)"
    except Exception as exc:  # noqa: BLE001
        rec["vector"] = {"error": type(exc).__name__}
    out["questions"].append(rec)
with open(os.environ.get("DIAG_OUT", "/tmp/retrieval-diagnostic.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print("DIAG_WRITTEN", os.environ.get("DIAG_OUT", "/tmp/retrieval-diagnostic.json"))

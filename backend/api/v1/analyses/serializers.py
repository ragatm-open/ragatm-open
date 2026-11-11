"""Serializers des analyses globales.

Le client ne peut fournir QUE : ``analysis_type``, ``parameters``, ``request_id``.
Tout le reste (statut, progression, couverture, résultat, empreinte, coût,
demandeur) est calculé côté serveur — un champ inattendu est rejeté, pas ignoré.
"""
from __future__ import annotations

from rest_framework import serializers

from apps.rag_core.analysis.registry import is_known_analysis_type
from apps.rag_core.analysis.service import is_stale
from apps.rag_core.models.corpus_analysis_job import REQUEST_ID_MAX_LENGTH

#: Champs que le client ne doit JAMAIS pouvoir poser.
FORBIDDEN_INPUT_FIELDS = frozenset({
    "status", "phase", "progress", "progress_current", "progress_total",
    "progress_percent", "coverage", "result", "corpus_fingerprint", "preflight",
    "requested_by", "cost_usd", "tokens_in", "tokens_out", "result_version",
    "expires_at", "started_at", "finished_at", "error_code", "error_message",
})


class AnalysisCreateSerializer(serializers.Serializer):
    analysis_type = serializers.CharField(max_length=64)
    parameters = serializers.DictField(required=False, default=dict)
    request_id = serializers.CharField(
        max_length=REQUEST_ID_MAX_LENGTH, required=False, allow_blank=True, default=""
    )

    def validate_analysis_type(self, value: str) -> str:
        # Registre FERMÉ : la clé est validée ici, aucun chemin d'import n'est
        # jamais résolu depuis une valeur fournie par le client.
        if not is_known_analysis_type(value):
            raise serializers.ValidationError("unknown_analysis_type")
        return value

    def validate(self, attrs):
        provided = set(self.initial_data or {})
        leaked = provided & FORBIDDEN_INPUT_FIELDS
        if leaked:
            raise serializers.ValidationError(
                {f: "read_only" for f in sorted(leaked)}
            )

        # Paramètres autorisés PAR TYPE. Le client ne peut fixer ni top_k, ni
        # batch_size, ni provider, ni modèle, ni température, ni fallback, ni
        # configuration FTS brute : ce sont des décisions serveur.
        from apps.rag_core.analysis.registry import GLOBAL_SEARCH

        params = attrs.get("parameters") or {}
        if attrs.get("analysis_type") == GLOBAL_SEARCH:
            # `language` = langue de RECHERCHE (config FTS). `answer_language` = langue
            # de RÉPONSE (auto|fr|en). Sémantiques DISTINCTES (cf. P2). `search_language`
            # est accepté comme alias explicite de `language` (rétrocompat conservée).
            unknown = set(params) - {"query", "language", "search_language", "answer_language"}
            if unknown:
                raise serializers.ValidationError(
                    {"parameters": [f"unsupported: {p}" for p in sorted(unknown)]}
                )
            from apps.rag_core.analysis.global_search import has_topic, normalize_query

            q = normalize_query(str(params.get("query") or ""))
            if not q or not has_topic(q):
                # Une recherche globale sans sujet n'a rien à chercher.
                raise serializers.ValidationError(
                    {"parameters": ["global_search_topic_required"]}
                )
            search_language = params.get("search_language") or params.get("language") or "auto"
            answer_language = params.get("answer_language") or "auto"
            attrs["parameters"] = {
                "query": q,
                # Conservé sous `language` pour la rétrocompat du handler FTS.
                "language": search_language,
                "answer_language": answer_language,
            }
        elif params:
            raise serializers.ValidationError({"parameters": ["unsupported"]})

        return attrs


def serialize_progress(job) -> dict:
    return {
        "phase": job.phase,
        "current": job.progress_current,
        "total": job.progress_total,
        "percent": job.progress_percent,
    }


def serialize_job(job, *, include_stale: bool = True) -> dict:
    """Payload de détail. N'expose ni trace, ni task id, ni queue, ni worker, ni
    provider, ni nom de fonction interne."""
    return {
        "analysis_id": str(job.id),
        "rag_space_id": str(job.rag_space_id),
        "analysis_type": job.analysis_type,
        "parameters": job.parameters or {},
        "status": job.status,
        "progress": serialize_progress(job),
        "preflight": job.preflight or {},
        "coverage": job.coverage,
        "result": job.result,
        "error_code": job.error_code or None,
        # Message SÛR uniquement. La trace vit dans les logs serveur.
        "error_message": job.error_message or None,
        "corpus_fingerprint": job.corpus_fingerprint,
        # None tant que le job n'est pas terminal : pas de recalcul d'empreinte à
        # chaque tour de polling.
        "is_stale": is_stale(job) if include_stale else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "tokens_in": job.tokens_in,
        "tokens_out": job.tokens_out,
        "cost_usd": float(job.cost_usd) if job.cost_usd is not None else None,
    }

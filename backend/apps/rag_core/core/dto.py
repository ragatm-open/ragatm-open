"""DTOs stables de la façade RagATM_Core (PR 2).

Contrat de retour figé, indépendant des structures internes de
``services/query`` et ``services/ingest``. Les endpoints HTTP (/core/*, PR 3) et
le branchement de RagATM App (PR 4) consommeront CES types, jamais directement les
dicts internes — ce qui autorise un refactor interne sans casser les appelants.

Note ingestion : on réutilise volontairement ``services.ingest.types.IngestResult``
(déjà figé, frozen dataclass) plutôt que d'en dupliquer un — la façade renvoie ce
type tel quel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalResult:
    """Résultat de récupération de passages — SANS génération de réponse.

    La génération de la réponse finale n'est jamais faite ici (elle reste à la
    charge de RagATM App aujourd'hui, et du serveur IA à terme). ``retrieve``
    délègue à ``query_rag`` qui ne fait que de la recherche (embedding de la
    requête + FTS/vecteur), jamais d'appel LLM de génération.
    """

    status: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    prompt: str | None = None
    log_id: str | None = None
    answer_kind: str | None = None
    # Charge utile brute de query_rag (forward-compat / échappatoire). Permet à
    # PR 4 d'atteindre une parité exacte avec le comportement actuel.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_answerable(self) -> bool:
        """True si la récupération a fourni des passages exploitables."""
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "sources": self.sources,
            "prompt": self.prompt,
            "log_id": self.log_id,
            "answer_kind": self.answer_kind,
        }
        # P1-A0 (additif, non sensible) : résumé SÛR de la trace de retrieval, si
        # présent dans le payload brut. Permet de distinguer un vide légitime d'une
        # dégradation via /core/retrieve, SANS exposer durées, question ni détail SQL.
        summary = self._retrieval_summary()
        if summary is not None:
            payload["retrieval"] = summary
        return payload

    def _retrieval_summary(self) -> dict[str, Any] | None:
        raw = self.raw if isinstance(self.raw, dict) else {}
        trace = raw.get("retrieval_trace")
        if not isinstance(trace, dict):
            return None
        legs = trace.get("legs") or []
        return {
            "schema_version": trace.get("schema_version"),
            "outcome": trace.get("outcome"),
            "degraded": bool(trace.get("degraded")),
            "legs": {
                leg.get("name"): leg.get("status")
                for leg in legs
                if isinstance(leg, dict) and leg.get("name")
            },
        }


@dataclass
class DocumentStatusResult:
    """État d'un document. Distingue explicitement statut technique et métier."""

    document_id: str
    found: bool
    # Statut TECHNIQUE d'ingestion : PENDING/INGESTING/READY/FAILED.
    status: str = ""
    # Statut MÉTIER (champ PR 1, distinct de `status`).
    document_status: str = ""
    chunks_count: int = 0
    error_message: str = ""
    filename: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "found": self.found,
            "status": self.status,
            "document_status": self.document_status,
            "chunks_count": self.chunks_count,
            "error_message": self.error_message,
            "filename": self.filename,
        }


@dataclass
class HealthResult:
    """Santé du moteur documentaire (sans aucun secret)."""

    ok: bool
    embedding_dim: int
    providers: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "embedding_dim": self.embedding_dim,
            "providers": self.providers,
        }

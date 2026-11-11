"""Couverture de pipeline d'embeddings d'un RagSpace (lot 3, §12).

Répond à une seule question, mais celle qui décide si un pilote est réussi :
**tous les chunks interrogeables d'un espace ont-ils bien été produits par le pipeline
que cet espace déclare ?**

## Pourquoi ce service plutôt qu'une requête ad hoc

Parce que la réponse « oui » et la réponse « je n'ai trouvé aucun chunk » se
ressemblent beaucoup quand on les lit dans un terminal. Un décompte à zéro incohérent
sur un espace vide n'est pas une réussite de migration : c'est une absence de mesure.
Les états sont donc CALCULÉS et nommés, jamais laissés à l'interprétation.

## Ce que ce service ne fait pas

Il n'écrit rien. Aucune méthode ne modifie un chunk, un document ou un espace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from django.db.models import Count, Q

from apps.llm_router.embeddings import resolve_space_embedding, space_is_governed


def _family(value: str | None) -> str:
    """Famille attendue telle que fournie (`modele@digest8`), normalisée. Elle n'est
    jamais DÉRIVÉE d'un `pipeline_id` : celui-ci est opaque."""
    return (value or "").strip()

# --- États STABLES, consommés par les commandes et l'exploitation -------------
COVERAGE_CONSISTENT = "consistent"
COVERAGE_PARTIAL = "partial_reindex"
COVERAGE_MISMATCH = "pipeline_mismatch"
COVERAGE_REINDEX_REQUIRED = "reindex_required"
COVERAGE_NO_CHUNKS = "no_chunks"
COVERAGE_NOT_GOVERNED = "not_governed"


@dataclass(frozen=True)
class PipelineCoverage:
    """Décompte des chunks d'un espace face au pipeline ATTENDU, et son verdict."""

    rag_space_id: str
    governed: bool
    expected_family: str = ""
    total_chunks: int = 0
    expected_chunks: int = 0
    other_pipeline_chunks: int = 0
    null_pipeline_chunks: int = 0
    total_documents: int = 0
    fully_conformant_documents: int = 0
    partially_conformant_documents: int = 0
    non_conformant_documents: int = 0

    # --- Périmètre DÉCLARÉ (exclusions explicites) --------------------------
    #
    # Un document peut être hors périmètre pour une raison connue et assumée — par
    # exemple un classeur dont les fragments tabulaires dépassent la fenêtre du
    # modèle et que la Gateway refuserait. Le sortir du décompte n'est légitime
    # QUE s'il est nommé : sans cela, « 100 % conforme » voudrait dire « 100 % de
    # ce que j'ai choisi de regarder », ce qui n'est pas une mesure.
    #
    # Ces documents ne sont JAMAIS réétiquetés : leurs chunks gardent
    # `pipeline_id = NULL`. Ils sont comptés à part, pas effacés.
    excluded_documents: int = 0
    excluded_chunks: int = 0

    @property
    def status(self) -> str:
        """Verdict CALCULÉ, du plus structurel au plus circonstanciel."""
        if not self.governed:
            return COVERAGE_NOT_GOVERNED
        if self.total_chunks == 0:
            return COVERAGE_NO_CHUNKS
        if self.expected_chunks == self.total_chunks:
            return COVERAGE_CONSISTENT
        if self.expected_chunks == 0:
            # Aucun chunk conforme : soit tout vient d'un autre pipeline identifié
            # (mismatch avéré), soit tout est historique et non identifiable.
            return COVERAGE_MISMATCH if self.other_pipeline_chunks else COVERAGE_REINDEX_REQUIRED
        return COVERAGE_PARTIAL

    @property
    def vector_retrieval_available(self) -> bool:
        """Le leg vectoriel a-t-il au moins un chunk sûr à interroger ?"""
        return not self.governed or self.expected_chunks > 0

    @property
    def is_pilot_acceptable(self) -> bool:
        """Critère d'acceptation du pilote (§18 étape 5) : 100 %, sans exception.

        Volontairement plus strict que ``vector_retrieval_available`` : un pilote
        partiellement migré n'est pas un pilote réussi dont on discuterait le taux,
        c'est un espace dont une partie du fonds est devenue invisible.

        Porte sur le PÉRIMÈTRE DÉCLARÉ : les documents explicitement exclus n'y
        entrent pas. C'est légitime parce qu'ils sont nommés et comptés
        (``excluded_documents``) — un verdict de 100 % qui tairait ses exclusions
        n'en serait pas un.
        """
        return (
            self.governed
            and self.total_chunks > 0
            and self.expected_chunks == self.total_chunks
            and self.other_pipeline_chunks == 0
            and self.null_pipeline_chunks == 0
            and self.partially_conformant_documents == 0
            and self.non_conformant_documents == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rag_space_id": self.rag_space_id,
            "governed": self.governed,
            "expected_pipeline_family": self.expected_family,
            "excluded_documents": self.excluded_documents,
            "excluded_chunks": self.excluded_chunks,
            "total_chunks": self.total_chunks,
            "expected_pipeline_chunks": self.expected_chunks,
            "other_pipeline_chunks": self.other_pipeline_chunks,
            "null_pipeline_chunks": self.null_pipeline_chunks,
            "total_documents": self.total_documents,
            "fully_conformant_documents": self.fully_conformant_documents,
            "partially_conformant_documents": self.partially_conformant_documents,
            "non_conformant_documents": self.non_conformant_documents,
            "status": self.status,
            "vector_retrieval_available": self.vector_retrieval_available,
            "pilot_acceptable": self.is_pilot_acceptable,
        }


def observed_pipeline_families(rag_space) -> dict[str, int]:
    """Familles de pipeline RÉELLEMENT présentes dans l'espace, et leur volume.

    Sert à répondre « avec quoi cet espace est-il indexé ? » sans présumer de la
    réponse. ``""`` représente les chunks à ``NULL`` (historiques).
    """
    from apps.rag_core.models import Chunk

    rows = (
        Chunk.objects.filter(rag_space_id=getattr(rag_space, "id", rag_space))
        .values("pipeline_family")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    return {(r["pipeline_family"] or ""): int(r["n"]) for r in rows}


def expected_pipeline_family(rag_space, *, probe: bool = False) -> str:
    """Famille ATTENDUE pour cet espace.

    Sans ``probe``, elle est DÉDUITE des chunks déjà présents : c'est la famille
    majoritaire portée par l'espace. On ne la fabrique pas à partir du modèle déclaré —
    ce serait exactement la reconstruction que le lot interdit.

    Avec ``probe=True``, un appel d'embedding RÉEL (une chaîne neutre, sans contenu
    documentaire) demande son identité à la Gateway. C'est la seule façon de connaître
    la famille attendue d'un espace **vide**, avant toute indexation.
    """
    if probe:
        from apps.llm_router.egress import LlmExecutionContext
        from apps.llm_router.embedding_pipeline import TASK_QUERY
        from apps.llm_router.embeddings import embed_texts_with_identity

        provider, model = resolve_space_embedding(rag_space)
        batch = embed_texts_with_identity(
            ["pipeline probe"],
            provider=provider,
            model=model,
            task=TASK_QUERY,
            rag_space_id=getattr(rag_space, "id", None),
            # La sonde n'envoie qu'une chaîne neutre, mais elle passe par le même
            # provider que le reste de l'espace : elle obéit donc à la même politique.
            context=LlmExecutionContext.for_rag_space(
                rag_space, operation="embedding.query"
            ),
        )
        return batch.family

    families = {k: v for k, v in observed_pipeline_families(rag_space).items() if k}
    if not families:
        return ""
    return max(families.items(), key=lambda kv: kv[1])[0]


def compute_coverage(
    rag_space,
    *,
    expected_family: str | None = None,
    excluded_document_ids: Iterable[Any] | None = None,
) -> PipelineCoverage:
    """Couverture de pipeline de l'espace. LECTURE SEULE.

    ``expected_family`` explicite prime ; à défaut, elle est déduite des chunks
    présents (cf. ``expected_pipeline_family``).

    ``excluded_document_ids`` retire des documents du PÉRIMÈTRE mesuré. Le verdict
    porte alors sur le périmètre déclaré, et les exclus sont comptés à part
    (``excluded_documents`` / ``excluded_chunks``) — jamais effacés, jamais
    réétiquetés. Une exclusion muette transformerait « conforme » en « conforme sur
    ce que j'ai bien voulu regarder ».
    """
    from apps.rag_core.models import Chunk, Document

    space_id = str(getattr(rag_space, "id", rag_space))
    governed = space_is_governed(rag_space)

    family = _family(expected_family) if expected_family else expected_pipeline_family(rag_space)

    excluded = [str(x) for x in (excluded_document_ids or []) if str(x).strip()]

    all_chunk_qs = Chunk.objects.filter(rag_space_id=space_id)
    chunk_qs = all_chunk_qs.exclude(document_id__in=excluded) if excluded else all_chunk_qs
    totals = chunk_qs.aggregate(
        total=Count("id"),
        expected=Count("id", filter=Q(pipeline_family=family)) if family else Count("id", filter=Q(pk=None)),
        nulls=Count("id", filter=Q(pipeline_family__isnull=True)),
    )
    total = int(totals["total"] or 0)
    expected = int(totals["expected"] or 0)
    nulls = int(totals["nulls"] or 0)
    other = max(0, total - expected - nulls)

    # Décompte DOCUMENTAIRE : un document dont une partie seulement des chunks est
    # conforme est le cas le plus dangereux — il reste `READY`, donc il a l'air sain,
    # alors qu'une partie de son contenu est invisible au vectoriel.
    doc_rows = (
        chunk_qs.values("document_id")
        .annotate(
            n=Count("id"),
            ok=Count("id", filter=Q(pipeline_family=family)) if family else Count("id", filter=Q(pk=None)),
        )
    )
    full = partial = none_ = 0
    for row in doc_rows:
        n, ok = int(row["n"]), int(row["ok"] or 0)
        if ok == n:
            full += 1
        elif ok == 0:
            none_ += 1
        else:
            partial += 1

    doc_qs = Document.objects.filter(rag_space_id=space_id)
    return PipelineCoverage(
        rag_space_id=space_id,
        governed=governed,
        expected_family=family,
        total_chunks=total,
        expected_chunks=expected,
        other_pipeline_chunks=other,
        null_pipeline_chunks=nulls,
        total_documents=doc_qs.exclude(id__in=excluded).count() if excluded else doc_qs.count(),
        fully_conformant_documents=full,
        partially_conformant_documents=partial,
        non_conformant_documents=none_,
        excluded_documents=doc_qs.filter(id__in=excluded).count() if excluded else 0,
        excluded_chunks=all_chunk_qs.filter(document_id__in=excluded).count() if excluded else 0,
    )

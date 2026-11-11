"""Cohérence d'identité d'embeddings d'un corpus — comptage et transitions.

Ce module fait ce que le domaine ne peut pas faire : lire l'état documentaire en base.
La RÈGLE (ce qu'est une identité, quand elle diverge, quel verdict en découle) vit dans
``apps.corpus.domain.embedding_identity``.

Aucune écriture ici sauf ``apply_identity_change``, qui ne touche QUE des drapeaux —
jamais un vecteur, jamais un chunk, jamais un fichier.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import Q

from apps.corpus.domain.embedding_identity import (
    ConsistencyReport,
    EmbeddingIdentity,
    CorpusEmbeddingIdentityError,
    normalize_model,
    normalize_provider,
)

logger = logging.getLogger(__name__)


def embedding_dimension() -> int:
    """Dimension attendue — celle de la colonne pgvector, figée par configuration."""
    return int(getattr(settings, "RAG_EMBEDDING_DIM", 768))


def declared_identity(corpus) -> EmbeddingIdentity | None:
    """Identité DÉCLARÉE par le corpus, ou ``None`` s'il n'est pas vectorisé.

    ``None`` n'est pas une erreur : un corpus sans provider n'a jamais été vectorisé.
    Une identité incomplète (provider posé, modèle vide) lève, comme à l'ingestion.
    """
    provider = normalize_provider(getattr(corpus, "embedding_provider", ""))
    model = normalize_model(getattr(corpus, "embedding_model", ""))
    if not provider and not model:
        return None
    return EmbeddingIdentity(provider=provider, model=model, dimension=embedding_dimension())


def compatible_documents_filter(identity: EmbeddingIdentity) -> Q:
    """Clause des documents dont les vecteurs sont RÉPUTÉS comparables à ``identity``.

    Quatre conditions simultanées, aucune facultative : même empreinte (donc même
    provider, modèle, dimension et version de pipeline), pas de réindexation en attente,
    et statut exploitable. L'empreinte seule suffirait, mais expliciter provider/modèle
    rend la clause lisible dans un `EXPLAIN` et dans les journaux.
    """
    return Q(
        embedding_fingerprint=identity.fingerprint,
        embedding_provider=identity.provider,
        embedding_model=identity.model,
        embedding_dimension=identity.dimension,
        reindex_required=False,
    )


def consistency_report(corpus, *, identity: EmbeddingIdentity | None = None) -> ConsistencyReport:
    """Décompte documentaire d'un corpus. LECTURE SEULE, une seule requête agrégée."""
    from apps.corpus.models import DomainCorpusDocument
    from apps.rag_core.domain.eligibility import ELIGIBLE_DOCUMENT_STATUS

    if identity is None:
        try:
            identity = declared_identity(corpus)
        except CorpusEmbeddingIdentityError:
            identity = None

    ready = DomainCorpusDocument.objects.filter(
        corpus_id=getattr(corpus, "id", corpus), status=ELIGIBLE_DOCUMENT_STATUS
    )
    total = ready.count()
    if total == 0 or identity is None:
        # Sans identité déclarée, aucun document ne peut être déclaré compatible : on
        # ne compare pas à une référence qui n'existe pas.
        unknown = ready.filter(embedding_fingerprint="").count() if total else 0
        return ConsistencyReport(
            ready=total, compatible=0, incompatible=total - unknown, unknown=unknown,
            reindex_required=ready.filter(reindex_required=True).count() if total else 0,
        )

    compatible = ready.filter(compatible_documents_filter(identity)).count()
    unknown = ready.filter(embedding_fingerprint="").count()
    reindex = ready.filter(reindex_required=True).count()
    # Incompatible = ni compatible, ni sans empreinte : une empreinte PRÉSENTE et
    # DIFFÉRENTE. C'est le seul cas qui prouve une divergence, par opposition à
    # l'ignorance.
    incompatible = total - compatible - unknown
    return ConsistencyReport(
        ready=total, compatible=compatible, incompatible=max(0, incompatible),
        unknown=unknown, reindex_required=reindex,
    )


def identity_changed(corpus, *, provider: str | None, model: str | None) -> bool:
    """Le couple demandé diffère-t-il de celui persisté ? Comparaison NORMALISÉE.

    Un PATCH qui renvoie la même valeur (ou la même à la casse près) ne doit rien
    déclencher : marquer 12 documents à réindexer parce qu'un formulaire a renvoyé son
    propre état serait une régression, pas une garde.
    """
    return (
        normalize_provider(provider) != normalize_provider(corpus.embedding_provider)
        or normalize_model(model) != normalize_model(corpus.embedding_model)
    )


def apply_identity_change(corpus) -> dict:
    """Marque les documents à réindexer et invalide la calibration devenue étrangère.

    N'écrit QUE des drapeaux et un booléen de calibration. Les vecteurs, les chunks et
    les fichiers sont laissés intacts : le rollback est un retour de valeur, pas une
    réindexation.

    La calibration est désactivée plutôt que supprimée — une décision de retrieval prise
    hier doit rester explicable, et réactiver une version reste possible si l'identité
    redevient la sienne.
    """
    from apps.corpus.models import DomainCorpusAdmissionCalibration, DomainCorpusDocument

    affected = DomainCorpusDocument.objects.filter(corpus=corpus).exclude(
        reindex_required=True
    ).update(reindex_required=True)

    invalidated = 0
    for row in DomainCorpusAdmissionCalibration.objects.filter(corpus=corpus, is_active=True):
        if (
            normalize_provider(row.embedding_provider) != normalize_provider(corpus.embedding_provider)
            or normalize_model(row.embedding_model) != normalize_model(corpus.embedding_model)
        ):
            DomainCorpusAdmissionCalibration.objects.filter(id=row.id).update(is_active=False)
            invalidated += 1

    logger.warning(
        "corpus_embedding_identity_changed",
        extra={"corpus_id": str(corpus.id), "affected_documents": affected,
               "calibrations_invalidated": invalidated},
    )
    return {"affected_documents": int(affected), "calibrations_invalidated": int(invalidated)}


def active_calibration_state(corpus) -> tuple[bool, str | None, int | None]:
    """``(valide, code d'erreur, version)`` de la calibration active. LECTURE SEULE.

    On rejoue la vérification d'identité RÉELLE du chemin de retrieval plutôt que de
    comparer des champs à la main : une divergence entre le contrôle affiché à
    l'exploitant et le contrôle appliqué au retrieval serait pire que pas de contrôle.
    """
    from apps.rag_core.domain.corpus_admission import CorpusAdmissionCalibrationError

    from .admission import load_active_calibration

    try:
        calibration = load_active_calibration(getattr(corpus, "id", corpus))
    except CorpusAdmissionCalibrationError as exc:
        return False, exc.code, None
    if calibration is None:
        return False, None, None
    try:
        calibration.ensure_matches(
            corpus_id=getattr(corpus, "id", corpus),
            embedding_provider=getattr(corpus, "embedding_provider", ""),
            embedding_model=getattr(corpus, "embedding_model", ""),
            embedding_dimension=embedding_dimension(),
        )
    except CorpusAdmissionCalibrationError as exc:
        return False, exc.code, calibration.version
    return True, None, calibration.version

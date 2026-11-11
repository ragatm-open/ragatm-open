"""Porte d'admission calibrée des corpus métier — chargement, décision, filtrage.

Ce module fait ce que le domaine ne peut pas faire : lire la calibration persistée et
l'identité d'embeddings du corpus, puis appliquer la décision aux listes de candidats.
La RÈGLE, elle, vit dans ``apps.rag_core.domain.corpus_admission`` — pure et testable
terme à terme.

Décision PAR CORPUS, pas globale
--------------------------------
Un workspace peut être associé à plusieurs corpus. Le signal, la calibration et donc la
décision sont propres à chacun : décider globalement laisserait un corpus hors sujet
entrer dans la fusion à la faveur du signal d'un autre. Pour un workspace mono-corpus —
le cas mesuré — les deux formulations coïncident exactement.

La porte est TOUT-OU-RIEN par corpus : admis, ses deux legs (lexical et vectoriel)
votent ; rejeté, aucun de ses candidats n'entre dans la fusion. Aucun filtrage chunk par
chunk : rien dans les mesures ne le soutient.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from apps.rag_core.domain.corpus_admission import (
    AdmissionDecision,
    CorpusAdmissionCalibrationError,
    LogisticAdmissionCalibration,
    REASON_FEATURE_DISABLED,
    decide_admission,
)

logger = logging.getLogger(__name__)


def load_active_calibration(corpus_id: Any) -> LogisticAdmissionCalibration | None:
    """Calibration ACTIVE d'un corpus, ou ``None`` s'il n'en a aucune.

    ``None`` n'est pas une autorisation : c'est l'absence d'information, que la
    décision traduit en REJET explicite (``calibration_missing``). Une calibration
    présente mais illisible remonte une erreur typée — jamais un ``None`` déguisé, qui
    confondrait « pas encore calibré » et « calibration corrompue ».
    """
    from apps.corpus.models import DomainCorpusAdmissionCalibration

    row = (
        DomainCorpusAdmissionCalibration.objects.filter(corpus_id=corpus_id, is_active=True)
        .order_by("-version")
        .first()
    )
    if row is None:
        return None
    return LogisticAdmissionCalibration.from_payload(row.as_payload(), corpus_id=corpus_id)


def _corpus_id_of(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return None
    src = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
    value = src.get("corpus_id") or candidate.get("corpus_id")
    return str(value) if value else None


def corpus_ids_in_legs(legs) -> list[str]:
    """Corpus ayant RÉELLEMENT produit des candidats, dans un ordre déterministe.

    C'est le bon périmètre pour la porte, et il évite une requête d'autorisation
    supplémentaire : un corpus sans aucun candidat n'a rien à admettre ni à rejeter.
    Un corpus qui n'a produit que du lexical y figure — et sera rejeté faute de signal,
    ce qui est précisément l'intention.
    """
    seen: list[str] = []
    for candidates in (
        getattr(legs, "text", None),
        getattr(legs, "vector", None),
        getattr(legs, "vector_raw", None),
    ):
        for candidate in candidates or []:
            cid = _corpus_id_of(candidate)
            if cid and cid not in seen:
                seen.append(cid)
    return sorted(seen)


@dataclass
class AdmissionOutcome:
    """Résultat de la porte : décisions par corpus + candidats retenus."""

    decisions: dict[str, AdmissionDecision] = field(default_factory=dict)
    admitted_corpus_ids: set[str] = field(default_factory=set)
    candidate_count_before: int = 0
    candidate_count_after: int = 0

    @property
    def any_admitted(self) -> bool:
        return bool(self.admitted_corpus_ids)


def evaluate_admission(
    *,
    corpus_ids: Sequence[str],
    vector_candidates: Sequence[Any] | None,
    vector_leg_healthy: bool,
    enabled: bool,
    threshold: float,
    embedding_dimension: int,
) -> dict[str, AdmissionDecision]:
    """Une décision par corpus autorisé. Ne filtre rien : décide seulement."""
    ids = [str(c) for c in (corpus_ids or [])]
    if not ids:
        return {}
    if not enabled:
        return {
            cid: AdmissionDecision(admitted=True, reason=REASON_FEATURE_DISABLED)
            for cid in ids
        }

    from apps.corpus.models import DomainCorpus

    identities = {
        str(cid): (provider, model)
        for cid, provider, model in DomainCorpus.objects.filter(id__in=ids).values_list(
            "id", "embedding_provider", "embedding_model"
        )
    }

    by_corpus: dict[str, list[Any]] = {cid: [] for cid in ids}
    for candidate in vector_candidates or []:
        cid = _corpus_id_of(candidate)
        if cid in by_corpus:
            by_corpus[cid].append(candidate)

    decisions: dict[str, AdmissionDecision] = {}
    for cid in ids:
        provider, model = identities.get(cid, (None, None))
        try:
            calibration = load_active_calibration(cid)
        except CorpusAdmissionCalibrationError as exc:
            # Calibration présente mais inexploitable : c'est un REJET nommé, pas une
            # absence. L'exploitant doit pouvoir distinguer les deux.
            logger.error(
                "corpus_admission_calibration_error",
                extra={"error_code": exc.code, "corpus_id": cid},
            )
            from apps.rag_core.domain.corpus_admission import REASON_CALIBRATION_INVALID

            decisions[cid] = AdmissionDecision(
                admitted=False, reason=REASON_CALIBRATION_INVALID,
                threshold=float(threshold), error_code=exc.code,
            )
            continue
        decisions[cid] = decide_admission(
            enabled=True,
            threshold=float(threshold),
            vector_candidates=by_corpus.get(cid) or [],
            vector_leg_healthy=bool(vector_leg_healthy),
            calibration=calibration,
            corpus_id=cid,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dimension=int(embedding_dimension),
        )
    return decisions


def apply_admission(legs, decisions: dict[str, AdmissionDecision]) -> AdmissionOutcome:
    """Retire des legs les candidats des corpus REJETÉS. Ne réordonne rien.

    Les listes sont reconstruites dans leur ordre d'origine : la porte décide QUI
    participe, jamais dans quel ordre. Un candidat sans ``corpus_id`` — impossible par
    construction du SQL corpus, donc symptôme d'une anomalie — est retiré : rattaché à
    aucune décision, il contournerait la porte.
    """
    outcome = AdmissionOutcome(
        decisions=dict(decisions or {}),
        admitted_corpus_ids={cid for cid, d in (decisions or {}).items() if d.admitted},
        candidate_count_before=len(getattr(legs, "merged", []) or []),
    )
    if not decisions or all(
        d.reason == REASON_FEATURE_DISABLED for d in decisions.values()
    ):
        # Porte désactivée : on ne touche à RIEN, pas même aux candidats mal formés.
        # Le rollback par le flag doit reproduire l'existant à l'octet près.
        outcome.candidate_count_after = outcome.candidate_count_before
        return outcome

    def _keep(candidates):
        return [
            c for c in (candidates or [])
            if _corpus_id_of(c) in outcome.admitted_corpus_ids
        ]

    legs.text = _keep(legs.text)
    legs.vector = _keep(legs.vector)
    legs.merged = _keep(legs.merged)
    if hasattr(legs, "vector_raw"):
        legs.vector_raw = _keep(legs.vector_raw)
    outcome.candidate_count_after = len(legs.merged)
    return outcome

"""Admission calibrée d'un corpus métier — signal, calibrateur, décision.

Module PUR : aucune dépendance Django, aucune I/O, aucun accès base. Il ne connaît
ni les corpus ni le retrieval, seulement des scores, des coefficients et une décision.
C'est ce qui rend la règle vérifiable terme à terme.

Pourquoi une porte d'admission
------------------------------
Mesuré sur 70 questions annotées (35 pertinentes corpus / 35 non), en environnement
cloné : **sans porte, 52 % des passages du top-8 viennent du corpus métier même sur les
questions manifestement hors sujet**. La pondération α ne corrige pas cela — elle réduit
la contamination et le service utile dans le même rapport.

Un seul signal discrimine (ROC-AUC 0.962, seuil stable σ ≤ 0.004 sur trois schémas de
validation croisée) : la **moyenne des dix meilleures similarités du leg vectoriel du
corpus**. Les autres ont été mesurés et écartés :

===========================  =======  ================================================
signal                       ROC-AUC  verdict
===========================  =======  ================================================
corpus_vector.top10_mean      0.962   retenu
corpus_vector.top1            0.948   corrélé, moins stable
corpus_fts.top10_mean         0.424   ANTI-corrélé — `ts_rank` récompense les mots
                                      rares, pas la pertinence thématique
workspace_vector.top10_mean   0.654   l'espace privé répond partout
workspace_fts.top10_mean      0.478   non informatif
marge privé/corpus            0.772   instable (change de signe entre plis)
===========================  =======  ================================================

Pourquoi une calibration et non un seuil brut
---------------------------------------------
Le seuil mesuré (≈ 0,645) n'a de sens que pour le couple (corpus, modèle
d'embeddings) sur lequel il a été mesuré : l'échelle du corpus OpenAI observée
(0,550–0,790) passe **sous le minimum** de l'espace privé nomic (0,667). Un seuil brut
transposé d'un corpus à l'autre serait un nombre sans référent. La calibration transforme
le score en PROBABILITÉ, seule grandeur comparable d'un corpus à l'autre ; le réglage
d'exploitation porte alors sur la probabilité.

Calibrateur : logistique à une variable. Interprétable, deux paramètres, robuste au
faible volume (l'isotonique surajuste à 70 observations : σ du seuil ×8), sérialisable,
et il rend directement la probabilité qu'attend la politique.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Identité du signal
# ---------------------------------------------------------------------------
#: Nom STABLE du signal. Persisté dans la calibration : une calibration entraînée sur un
#: autre signal doit être refusée, pas réinterprétée.
SIGNAL_CORPUS_VECTOR_TOP10_MEAN = "corpus_vector_top10_mean"

#: Nombre de candidats agrégés par le signal. Changer ce nombre change le signal :
#: il fait partie de son identité, d'où sa présence dans le nom.
SIGNAL_TOP_N = 10

#: Type de calibrateur implémenté en production. Un type inconnu est REFUSÉ.
CALIBRATOR_LOGISTIC = "logistic"

#: Version du format de calibration persistée.
CALIBRATION_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Vocabulaire de décision (valeurs STABLES, consommées par la trace et l'exploitation)
# ---------------------------------------------------------------------------
REASON_FEATURE_DISABLED = "feature_disabled"
REASON_PROBABILITY_ABOVE_THRESHOLD = "probability_above_threshold"
REASON_PROBABILITY_BELOW_THRESHOLD = "probability_below_threshold"
REASON_CALIBRATION_MISSING = "calibration_missing"
REASON_CALIBRATION_INVALID = "calibration_invalid"
REASON_SIGNAL_UNAVAILABLE = "signal_unavailable"
REASON_VECTOR_LEG_ERROR = "vector_leg_error"
REASON_NO_VECTOR_CANDIDATES = "no_vector_candidates"

ERROR_CALIBRATION_MISSING = "corpus_admission_calibration_missing"
ERROR_CALIBRATION_INVALID = "corpus_admission_calibration_invalid"
ERROR_SIGNAL_UNAVAILABLE = "corpus_admission_signal_unavailable"


class CorpusAdmissionCalibrationError(ValueError):
    """Calibration inutilisable : identité incohérente, coefficients absents ou non finis.

    Toujours une ERREUR, jamais un repli. Une calibration qui ne correspond plus au
    couple (corpus, provider, modèle, dimension, signal) décrirait une échelle qui
    n'existe plus : l'appliquer produirait une probabilité crédible et fausse — le même
    piège que la cascade de modèles d'embeddings globale (cf.
    ``CorpusEmbeddingConfigurationError``).
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------
def admission_signal_value(candidates: Sequence[Any] | None) -> float | None:
    """Moyenne des ``SIGNAL_TOP_N`` meilleurs scores. ``None`` si indisponible.

    Le tri est fait ICI plutôt que supposé : le leg vectoriel arrive trié par le SQL,
    mais il traverse ``merge_candidates`` (fusion multi-passes par ``chunk_id``) qui ne
    garantit pas l'ordre. Le signal est de toute façon défini comme « la moyenne des dix
    meilleures similarités » — une définition indépendante de l'ordre reçu.

    Moins de dix candidats : on moyenne ce qu'on a. Le signal reste défini, il est
    simplement estimé sur moins d'observations — la calibration, elle, ne change pas.
    Aucun candidat : le signal n'existe pas, et ``None`` force l'appelant à décider
    explicitement (il refusera l'admission).
    """
    scores: list[float] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        raw = candidate.get("score")
        if not _is_finite(raw):
            # Un score non fini rendrait la moyenne — donc la probabilité — absurde.
            raise CorpusAdmissionCalibrationError(
                ERROR_SIGNAL_UNAVAILABLE, f"score non fini dans le leg vectoriel ({raw!r})"
            )
        scores.append(float(raw))
    if not scores:
        return None
    scores.sort(reverse=True)
    kept = scores[:SIGNAL_TOP_N]
    return sum(kept) / len(kept)


# ---------------------------------------------------------------------------
# Calibrateur
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LogisticAdmissionCalibration:
    """Calibrateur logistique à une variable, lié à une identité de corpus.

    ``p = sigmoid(intercept + coefficient × signal)``

    L'identité (corpus, provider, modèle, dimension, signal) n'est pas décorative :
    elle est VÉRIFIÉE à chaque usage. Réencoder un corpus avec un autre modèle change
    l'échelle des similarités ; la calibration doit alors être refusée, pas appliquée.
    """

    corpus_id: str
    signal: str
    calibrator_type: str
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    intercept: float
    coefficient: float
    version: int
    schema_version: int = CALIBRATION_SCHEMA_VERSION

    # -- construction ------------------------------------------------------
    @classmethod
    def from_payload(cls, payload: Any, *, corpus_id: Any) -> "LogisticAdmissionCalibration":
        """Construit depuis un dict persisté, en REFUSANT tout ce qui est douteux."""
        if not isinstance(payload, dict):
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID, "payload de calibration absent ou non structuré"
            )
        schema_version = payload.get("version", CALIBRATION_SCHEMA_VERSION)
        if int(schema_version or 0) != CALIBRATION_SCHEMA_VERSION:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID,
                f"version de format inconnue ({schema_version!r})",
            )
        signal = str(payload.get("signal") or "")
        if signal != SIGNAL_CORPUS_VECTOR_TOP10_MEAN:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID, f"signal inconnu ({signal!r})"
            )
        calibrator_type = str(payload.get("calibrator_type") or "")
        if calibrator_type != CALIBRATOR_LOGISTIC:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID, f"calibrateur inconnu ({calibrator_type!r})"
            )
        coefficients = payload.get("coefficients")
        if not isinstance(coefficients, dict):
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID, "coefficients absents"
            )
        intercept = coefficients.get("intercept")
        slope = coefficients.get("top10_mean")
        for label, value in (("intercept", intercept), ("top10_mean", slope)):
            if value is None:
                raise CorpusAdmissionCalibrationError(
                    ERROR_CALIBRATION_INVALID, f"coefficient {label} absent"
                )
            if not _is_finite(value):
                raise CorpusAdmissionCalibrationError(
                    ERROR_CALIBRATION_INVALID, f"coefficient {label} non fini ({value!r})"
                )
        return cls(
            corpus_id=str(corpus_id),
            signal=signal,
            calibrator_type=calibrator_type,
            embedding_provider=str(payload.get("embedding_provider") or "").strip().lower(),
            embedding_model=str(payload.get("embedding_model") or "").strip(),
            embedding_dimension=int(payload.get("embedding_dimension") or 0),
            intercept=float(intercept),
            coefficient=float(slope),
            version=int(payload.get("calibration_version") or 0),
            schema_version=CALIBRATION_SCHEMA_VERSION,
        )

    # -- usage -------------------------------------------------------------
    def ensure_matches(
        self, *, corpus_id: Any, embedding_provider: str | None, embedding_model: str | None,
        embedding_dimension: int,
    ) -> None:
        """Refuse une calibration qui ne décrit plus l'espace vectoriel interrogé."""
        if str(corpus_id) != self.corpus_id:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID,
                f"calibration liée au corpus {self.corpus_id} appliquée à {corpus_id}",
            )
        provider = (embedding_provider or "").strip().lower()
        model = (embedding_model or "").strip()
        if provider != self.embedding_provider:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID,
                f"provider d'embeddings différent (calibré {self.embedding_provider!r})",
            )
        if model != self.embedding_model:
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID,
                f"modèle d'embeddings différent (calibré {self.embedding_model!r})",
            )
        if int(embedding_dimension) != int(self.embedding_dimension):
            raise CorpusAdmissionCalibrationError(
                ERROR_CALIBRATION_INVALID,
                f"dimension différente (calibrée {self.embedding_dimension})",
            )

    def probability(self, signal_value: float) -> float:
        """``sigmoid(intercept + coefficient × signal_value)``, bornée à [0, 1]."""
        if not _is_finite(signal_value):
            raise CorpusAdmissionCalibrationError(
                ERROR_SIGNAL_UNAVAILABLE, f"valeur de signal non finie ({signal_value!r})"
            )
        z = self.intercept + self.coefficient * float(signal_value)
        # Bornage numérique : au-delà, `exp` déborde sans que la probabilité change.
        z = max(-60.0, min(60.0, z))
        return 1.0 / (1.0 + math.exp(-z))


# ---------------------------------------------------------------------------
# Décision
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdmissionDecision:
    """Décision d'admission d'UN corpus pour UNE question. Tout-ou-rien.

    ``admitted`` gouverne les DEUX legs du corpus : admis, le lexical et le vectoriel
    votent ; rejeté, aucun de ses candidats n'entre dans la fusion. Il n'y a pas de
    filtrage chunk par chunk : rien dans les mesures ne le soutient, et un filtre par
    score rendrait la sélection dépendante d'une échelle non comparable.
    """

    admitted: bool
    reason: str
    signal_name: str = SIGNAL_CORPUS_VECTOR_TOP10_MEAN
    signal_value: float | None = None
    probability: float | None = None
    threshold: float | None = None
    calibrator_type: str | None = None
    calibration_version: int | None = None
    error_code: str | None = None


def decide_admission(
    *,
    enabled: bool,
    threshold: float,
    vector_candidates: Sequence[Any] | None,
    vector_leg_healthy: bool,
    calibration: LogisticAdmissionCalibration | None,
    corpus_id: Any,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_dimension: int,
) -> AdmissionDecision:
    """Décide l'admission d'un corpus. Ne lit aucun réglage : tout est passé.

    Ordre des refus, du plus structurel au plus circonstanciel :

    1. porte désactivée -> admission (comportement historique, aucune décision prise) ;
    2. leg vectoriel en erreur -> REJET : le signal repose sur lui, et laisser le leg
       lexical entrer seul contournerait la porte par le leg le moins informatif
       (``corpus_fts`` est ANTI-corrélé à la pertinence) ;
    3. aucun candidat vectoriel -> REJET, signal inexistant ;
    4. calibration absente ou incohérente -> REJET explicite avec code d'erreur.
       Jamais de porte ouverte silencieuse : un corpus non calibré n'est pas un corpus
       réputé pertinent ;
    5. probabilité comparée au seuil. ``p >= seuil`` admet — l'égalité admet, choix
       explicite et testé.
    """
    if not enabled:
        return AdmissionDecision(admitted=True, reason=REASON_FEATURE_DISABLED)

    if not vector_leg_healthy:
        return AdmissionDecision(
            admitted=False, reason=REASON_VECTOR_LEG_ERROR,
            threshold=threshold, error_code=ERROR_SIGNAL_UNAVAILABLE,
        )

    try:
        signal_value = admission_signal_value(vector_candidates)
    except CorpusAdmissionCalibrationError as exc:
        return AdmissionDecision(
            admitted=False, reason=REASON_SIGNAL_UNAVAILABLE,
            threshold=threshold, error_code=exc.code,
        )
    if signal_value is None:
        return AdmissionDecision(
            admitted=False, reason=REASON_NO_VECTOR_CANDIDATES, threshold=threshold,
        )

    if calibration is None:
        return AdmissionDecision(
            admitted=False, reason=REASON_CALIBRATION_MISSING,
            signal_value=signal_value, threshold=threshold,
            error_code=ERROR_CALIBRATION_MISSING,
        )

    try:
        calibration.ensure_matches(
            corpus_id=corpus_id,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
        )
        probability = calibration.probability(signal_value)
    except CorpusAdmissionCalibrationError as exc:
        return AdmissionDecision(
            admitted=False, reason=REASON_CALIBRATION_INVALID,
            signal_value=signal_value, threshold=threshold,
            calibrator_type=calibration.calibrator_type,
            calibration_version=calibration.version,
            error_code=exc.code,
        )

    admitted = probability >= float(threshold)
    return AdmissionDecision(
        admitted=admitted,
        reason=(
            REASON_PROBABILITY_ABOVE_THRESHOLD if admitted
            else REASON_PROBABILITY_BELOW_THRESHOLD
        ),
        signal_value=signal_value,
        probability=probability,
        threshold=float(threshold),
        calibrator_type=calibration.calibrator_type,
        calibration_version=calibration.version,
    )

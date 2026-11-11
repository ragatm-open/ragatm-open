import uuid

from django.db import models

from .corpus import DomainCorpus


class DomainCorpusAdmissionCalibration(models.Model):
    """Calibration d'admission d'un corpus métier — versionnée et auditable.

    Une ligne = une calibration entraînée à une date, sur un jeu annoté identifié par
    son empreinte, pour un couple (corpus, provider, modèle, dimension) donné. Les
    anciennes lignes sont CONSERVÉES et désactivées : une décision de retrieval prise en
    production doit rester explicable après un recalibrage.

    Ce qui n'est JAMAIS stocké ici : questions, texte documentaire, chunks, prompts,
    réponses, secrets. Uniquement deux coefficients, une identité et des métriques.

    Pourquoi une table plutôt qu'un champ sur ``DomainCorpus``
    ---------------------------------------------------------
    Un champ écrasé à chaque recalibrage rendrait l'historique irrécupérable, et
    l'identité (modèle d'embeddings au moment de l'entraînement) indistinguable de
    l'identité courante — or c'est précisément leur DIVERGENCE qui doit invalider la
    calibration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    corpus = models.ForeignKey(
        DomainCorpus,
        on_delete=models.CASCADE,
        related_name="admission_calibrations",
    )

    #: Numéro de version, croissant PAR corpus. Persisté dans la trace de retrieval :
    #: c'est lui qui relie une décision observée à la calibration qui l'a produite.
    version = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False)

    #: Identité du signal et du calibrateur (cf. `domain/corpus_admission.py`).
    signal = models.CharField(max_length=64)
    calibrator_type = models.CharField(max_length=32)

    #: Espace vectoriel dans lequel la calibration a été mesurée. Une divergence avec
    #: l'état courant du corpus INVALIDE la calibration : l'échelle des similarités
    #: change avec le modèle, et une probabilité calculée sur l'ancienne échelle serait
    #: crédible et fausse.
    embedding_provider = models.CharField(max_length=32)
    embedding_model = models.CharField(max_length=128)
    embedding_dimension = models.PositiveIntegerField()

    #: ``{"intercept": float, "top10_mean": float}``
    coefficients = models.JSONField(default=dict)
    #: Volumétrie et empreinte du jeu annoté — jamais son contenu.
    training = models.JSONField(default=dict)
    #: Métriques de validation croisée (stratifiée, group, leave-one-category-out).
    validation = models.JSONField(default=dict)

    #: Empreinte du jeu d'annotations. Deux calibrations issues du même jeu sont
    #: comparables ; deux empreintes différentes interdisent de comparer leurs métriques.
    dataset_hash = models.CharField(max_length=64)
    created_by = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "domain_corpus_admission_calibration"
        ordering = ["corpus_id", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["corpus", "version"],
                name="uniq_corpus_admission_calibration_version",
            ),
            # Au plus UNE calibration active par corpus : la décision de retrieval ne
            # doit jamais avoir à choisir entre deux calibrations concurrentes.
            models.UniqueConstraint(
                fields=["corpus"],
                condition=models.Q(is_active=True),
                name="uniq_active_corpus_admission_calibration",
            ),
        ]
        indexes = [
            models.Index(fields=["corpus", "is_active"]),
        ]

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"calibration v{self.version} corpus={self.corpus_id} ({state})"

    def as_payload(self) -> dict:
        """Vue sérialisée consommée par le domaine (``LogisticAdmissionCalibration``)."""
        from apps.rag_core.domain.corpus_admission import CALIBRATION_SCHEMA_VERSION

        return {
            "version": CALIBRATION_SCHEMA_VERSION,
            "calibration_version": int(self.version),
            "signal": self.signal,
            "calibrator_type": self.calibrator_type,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dimension": int(self.embedding_dimension),
            "coefficients": dict(self.coefficients or {}),
        }

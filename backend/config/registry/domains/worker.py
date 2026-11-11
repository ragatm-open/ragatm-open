"""Domaine ``worker`` — Celery, ordonnancement périodique et réindexation."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="CELERY_BROKER_URL",
        domain="worker",
        value_type="url",
        default="redis://redis:6379/0",
        description="Broker Celery (Redis).",
        required_in=("staging", "prod"),
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_RESULT_BACKEND",
        domain="worker",
        value_type="url",
        default="redis://redis:6379/0",
        default_expr="= CELERY_BROKER_URL",
        description="Backend de résultats Celery. Défaut = le broker.",
        consumers=("config.celery",),
        default_is_dynamic=True,
    ),
    D(
        name="CELERY_TASK_ALWAYS_EAGER",
        domain="worker",
        value_type="bool",
        default=False,
        description=(
            "Exécution synchrone des tâches (tests, environnement sans broker). "
            "Jamais activée en production."
        ),
        environments=("dev", "test"),
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_TIMEZONE",
        domain="worker",
        value_type="str",
        default="Europe/Paris",
        description="Fuseau horaire de Celery (aligné sur TIME_ZONE).",
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_WORKER_PREFETCH_MULTIPLIER",
        domain="worker",
        value_type="int",
        default=1,
        minimum=1,
        maximum=64,
        description="Nombre de messages préchargés par slot de worker.",
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_TASK_SOFT_TIME_LIMIT",
        domain="worker",
        value_type="int",
        default=540,
        minimum=1,
        description=(
            "Limite douce globale (9 min) : lève `SoftTimeLimitExceeded`, capturée => "
            "Document FAILED. Doit rester strictement inférieure à la limite dure."
        ),
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_TASK_TIME_LIMIT",
        domain="worker",
        value_type="int",
        default=600,
        minimum=1,
        description="Limite dure globale (10 min) : tue le worker en dernier recours.",
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_BEAT_REAP_REINDEX_SECONDS",
        domain="worker",
        value_type="int",
        default=300,
        minimum=1,
        description="Période de reprise des items de réindexation au lease expiré.",
        consumers=("config.celery", "apps.rag_core.checks_operations"),
    ),
    D(
        name="CELERY_BEAT_VISUAL_V2_SWEEP_SECONDS",
        domain="worker",
        value_type="int",
        default=300,
        minimum=60,
        maximum=3600,
        description=(
            "Période du balayeur Vision V2 (file `maintenance`) : ré-émet les "
            "préparations et jobs dont l'envoi est perdu. Planifié seulement si "
            "VISUAL_V2_ORCHESTRATION_ENABLED=1 hors production ; ne rouvre jamais un état final."
        ),
        consumers=("config.settings.base", "apps.rag_core.tasks"),
    ),
    D(
        name="CELERY_BEAT_REAP_SUMMARIES_SECONDS",
        domain="worker",
        value_type="int",
        default=600,
        minimum=1,
        description="Période du reaper de résumés bloqués en PENDING.",
        consumers=("config.celery", "apps.rag_core.checks_operations"),
    ),
    D(
        name="CELERY_BEAT_AUDIT_PURGE_SECONDS",
        domain="worker",
        value_type="int",
        default=86400,
        minimum=1,
        description="Période de la purge des journaux d'audit expirés.",
        consumers=("config.celery", "apps.rag_core.checks_operations"),
    ),
    D(
        name="CELERY_BEAT_HEARTBEAT_SECONDS",
        domain="worker",
        value_type="int",
        default=60,
        minimum=1,
        description=(
            "Période du battement de cœur de Beat : c'est lui qui prouve que Beat "
            "ordonnance réellement (un processus vivant ne suffit pas)."
        ),
        consumers=("config.celery", "apps.rag_core.services.beat_heartbeat"),
    ),
    D(
        name="CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS",
        domain="worker",
        value_type="int",
        default=180,
        minimum=1,
        description=(
            "Âge au-delà duquel le heartbeat est périmé (trois cycles manqués) : tolère "
            "un redémarrage, détecte un arrêt réel en moins de 4 minutes."
        ),
        consumers=("apps.rag_core.services.beat_heartbeat",),
    ),
    D(
        name="CELERY_BEAT_MEDIA_STORAGE_AUDIT_SECONDS",
        domain="worker",
        value_type="int",
        default=86400,
        minimum=1,
        description="Période du contrôle de cohérence base ↔ fichiers.",
        consumers=("config.celery",),
    ),
    D(
        name="CELERY_BEAT_RAGSPACE_DELETE_MONITOR_SECONDS",
        domain="worker",
        value_type="int",
        default=3600,
        minimum=1,
        description="Période de la supervision des purges de RagSpace en échec.",
        consumers=("config.celery",),
    ),
    D(
        name="PERIODIC_TASK_LOCK_TTL_SECONDS",
        domain="worker",
        value_type="int",
        default=900,
        minimum=1,
        description=(
            "TTL des verrous de tâches périodiques. Doit dépasser la durée d'exécution "
            "la plus longue attendue, sinon deux exécutions se recouvrent."
        ),
        consumers=("apps.rag_core.tasks",),
    ),
    D(
        name="REINDEX_ITEM_LEASE_SECONDS",
        domain="worker",
        value_type="int",
        default=900,
        minimum=1,
        description=(
            "Durée du bail d'un item de réindexation (reprise après perte de worker). "
            "Sert aussi de SEUIL d'âge du reaper pour un document INGESTING sans item "
            "vivant : doit rester strictement supérieur à CELERY_TASK_TIME_LIMIT, "
            "REINDEX_ITEM_TIME_LIMIT et au timeout HTTP synchrone."
        ),
        consumers=("apps.rag_core.services.reindex_jobs",),
    ),
    D(
        name="REINDEX_ITEM_SOFT_TIME_LIMIT",
        domain="worker",
        value_type="int",
        default=600,
        minimum=1,
        description="Limite douce d'un item de réindexation.",
        consumers=("apps.rag_core.services.reindex_jobs",),
    ),
    D(
        name="REINDEX_ITEM_TIME_LIMIT",
        domain="worker",
        value_type="int",
        default=660,
        minimum=1,
        description="Limite dure d'un item de réindexation.",
        consumers=("apps.rag_core.services.reindex_jobs",),
    ),
    D(
        name="REINDEX_EAGER_MAX_SYNC_DOCUMENTS",
        domain="worker",
        value_type="int",
        default=25,
        minimum=1,
        description="Garde EAGER : nombre maximal de documents réindexés en synchrone.",
        consumers=("apps.rag_core.services.reindex_jobs",),
    ),
    D(
        name="REINDEX_MAX_RETRY_SELECTION",
        domain="worker",
        value_type="int",
        default=2000,
        minimum=1,
        description=(
            "Taille maximale d'une campagne de reprise des documents en erreur. "
            "Au-delà, la demande est refusée (selection_too_large) : une campagne "
            "dont personne ne peut suivre la fin n'aide personne."
        ),
        consumers=("apps.rag_core.services.reindex_jobs",),
    ),
    D(
        name="GUNICORN_WORKERS",
        domain="worker",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description=(
            "Nombre de workers Gunicorn. Multiplie la capacité tabulaire réelle "
            "(sémaphore par processus)."
        ),
        source="backend/apps/rag_core/tabular/capacity.py (déploiement)",
        consumers=("apps.rag_core.tabular.capacity", "deploy"),
    ),
)

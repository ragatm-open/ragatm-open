"""Domaine ``observability`` — audit persistant, rétention et supervision."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="ACTION_AUDIT_ENABLED",
        domain="observability",
        value_type="bool",
        default=True,
        description=(
            "Émission des `ActionAuditEvent` (append-only). Rollback = 0 : le modèle "
            "reste, aucun événement n'est plus écrit. Fail-open, jamais bloquant."
        ),
        consumers=("apps.audit.services",),
    ),
    D(
        name="ACTION_AUDIT_RETENTION_DAYS",
        domain="observability",
        value_type="int",
        default=365,
        minimum=1,
        maximum=3650,
        description="Rétention des événements d'action. Toute valeur invalide bloque le démarrage.",
        consumers=("apps.audit.retention", "apps.rag_core.checks_operations"),
    ),
    D(
        name="RAG_QUERY_LOG_RETENTION_DAYS",
        domain="observability",
        value_type="int",
        default=365,
        minimum=1,
        maximum=3650,
        description="Rétention des journaux de requêtes RAG.",
        consumers=("apps.audit.retention",),
    ),
    D(
        name="LLM_EGRESS_AUDIT_RETENTION_DAYS",
        domain="observability",
        value_type="int",
        default=365,
        minimum=90,
        maximum=3650,
        description=(
            "Rétention des audits d'egress LLM. Audit de SÉCURITÉ : plancher de 90 jours "
            "imposé par `apps.audit.retention`."
        ),
        consumers=("apps.audit.retention",),
    ),
    D(
        name="EGRESS_POLICY_CHANGE_RETENTION_DAYS",
        domain="observability",
        value_type="int",
        default=1095,
        minimum=90,
        maximum=3650,
        description=(
            "Rétention des changements de politique d'egress — le geste le plus sensible "
            "de la plateforme. 3 ans : « qui a autorisé quoi, et quand ». "
        ),
        consumers=("apps.audit.retention",),
    ),
    D(
        name="AUDIT_PURGE_ENABLED",
        domain="observability",
        value_type="bool",
        default=True,
        description=(
            "Interrupteur d'exploitation des purges d'audit. À 0, les événements "
            "continuent d'être écrits mais ne sont plus purgés."
        ),
        consumers=("apps.audit.tasks",),
    ),
    D(
        name="AUDIT_PURGE_BATCH_SIZE",
        domain="observability",
        value_type="int",
        default=1000,
        minimum=1,
        maximum=100000,
        description=(
            "Taille de lot de purge : une purge non bornée tiendrait une transaction "
            "ouverte plusieurs minutes et bloquerait les écritures."
        ),
        consumers=("apps.audit.tasks",),
    ),
    D(
        name="AUDIT_PURGE_MAX_ROWS_PER_RUN",
        domain="observability",
        value_type="int",
        default=50000,
        minimum=1,
        description="Nombre maximal de lignes purgées par exécution.",
        consumers=("apps.audit.tasks",),
    ),
    D(
        name="RAG_QUERY_LOG_STORE_QUESTION",
        domain="observability",
        value_type="bool",
        default=True,
        description=(
            "Stocke le texte de la question dans RagQueryLog. À 0 : hash + longueur "
            "(minimisation)."
        ),
        consumers=("apps.rag_core.services.query_log",),
    ),
    D(
        name="RAG_QUERY_LOG_STORE_ANSWER",
        domain="observability",
        value_type="bool",
        default=True,
        description="Stocke le texte de la réponse dans RagQueryLog. À 0 : hash + longueur.",
        consumers=("apps.rag_core.services.query_log",),
    ),
    D(
        name="RAG_MEDIA_STORAGE_AUDIT_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Contrôle de cohérence base ↔ fichiers (READ-ONLY : signale `missing_file` / "
            "`orphan_file`, ne supprime jamais). L'entrée de schedule existe toujours ; "
            "la tâche sort immédiatement tant que l'interrupteur vaut 0."
        ),
        consumers=("apps.rag_core.tasks",),
    ),
    D(
        name="RAG_RAGSPACE_DELETE_MONITORING_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Supervision des purges de RagSpace en échec (READ-ONLY : lit l'audit, ne "
            "l'écrit pas)."
        ),
        consumers=("apps.rag_core.tasks",),
    ),
    D(
        name="RAG_RAGSPACE_DELETE_MONITORING_WINDOW_SECONDS",
        domain="observability",
        value_type="int",
        default=86400,
        minimum=60,
        description=(
            "Fenêtre de rattrapage de la supervision. Filet quand le curseur Redis est "
            "absent (première exécution, Redis perdu)."
        ),
        consumers=("apps.rag_core.tasks",),
    ),
    # ------------------------------------------------------------------
    # Feedback utilisateur et incidents diagnostiques
    # cf. docs/03-backend/feedback-incidents.md
    # ------------------------------------------------------------------
    D(
        name="FEEDBACK_PSEUDONYM_SALT",
        domain="observability",
        value_type="str",
        default="",
        secret=True,
        description=(
            "Sel de pseudonymisation des auteurs de feedback. Vide = repli sur "
            "DJANGO_SECRET_KEY (jamais d'état non salé). Une rotation rend les "
            "anciens pseudonymes incomparables aux nouveaux."
        ),
        consumers=("apps.feedback.services.identity",),
        default_is_dynamic=True,
    ),
    D(
        name="FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG",
        domain="observability",
        value_type="bool",
        default=True,
        description=(
            "Écrit encore RagQueryLog.feedback_* en plus de la table `answer_feedback`. "
            "Période de transition : passer à 0 une fois les requêtes SQL historiques "
            "basculées."
        ),
        consumers=("api.v1.chat.views",),
    ),
    D(
        name="APP_GIT_SHA",
        domain="observability",
        value_type="str",
        default="",
        description=(
            "Révision de code déployée, estampillée sur chaque incident. Vide = repli "
            "`git rev-parse` (développement uniquement, mémorisé par processus)."
        ),
        consumers=("apps.feedback.services.context",),
        default_is_dynamic=True,
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Tampon Redis des candidats de retrieval (identifiants, rangs, scores, "
            "raisons d'exclusion — JAMAIS de texte). OFF par défaut : c'est une "
            "collecte supplémentaire, elle doit être un choix explicite."
        ),
        consumers=("apps.rag_core.services.query.orchestrator", "apps.feedback.diagnostics"),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TTL_SECONDS",
        domain="observability",
        value_type="int",
        default=172800,
        minimum=300,
        maximum=604800,
        description=(
            "Durée de vie du tampon (48 h par défaut). Au-delà, un clic ne peut plus "
            "produire qu'un snapshot `replay` ou `partial`."
        ),
        consumers=("apps.feedback.diagnostics.buffer",),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_BUFFER_TOP_K",
        domain="observability",
        value_type="int",
        default=20,
        minimum=1,
        maximum=100,
        description=(
            "Nombre de candidats conservés dans le tampon. 20 suffit à distinguer "
            "« absent du rappel » de « écarté par la sélection », qui est la question."
        ),
        consumers=("apps.feedback.diagnostics.capture",),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_SNAPSHOT_RETENTION_DAYS",
        domain="observability",
        value_type="int",
        default=30,
        minimum=1,
        maximum=90,
        description=(
            "Rétention des snapshots diagnostiques (contenu sensible). Le feedback, "
            "lui, est durable : purger l'un ne purge pas l'autre."
        ),
        consumers=("apps.feedback.retention",),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_MAX_SNAPSHOT_BYTES",
        domain="observability",
        value_type="int",
        default=262144,
        minimum=4096,
        maximum=4194304,
        description=(
            "Taille maximale d'un snapshot. Le dépassement TRONQUE explicitement "
            "(`truncated=true` + motif) : une troncature silencieuse ferait passer "
            "une capture partielle pour complète."
        ),
        consumers=("apps.feedback.diagnostics.snapshot",),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Capture du prompt COMPLET dans le snapshot (contenu documentaire injecté). "
            "OFF = `prompt_capture=hash_only` et pas de replay `prompt-only`."
        ),
        consumers=("apps.feedback.diagnostics.snapshot",),
    ),
    D(
        name="RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Replay de retrieval quand le tampon a expiré. Produit un snapshot marqué "
            "`capture_mode=replay` — jamais présenté comme la capture d'origine."
        ),
        consumers=("apps.feedback.tasks",),
    ),
    D(
        name="FEEDBACK_INCIDENT_EXPORT_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Ouvre l'action MANUELLE d'export d'incident. Il n'existe aucun chemin "
            "d'export automatique : ce drapeau n'en crée pas."
        ),
        consumers=("apps.feedback.export",),
    ),
    D(
        name="FEEDBACK_INCIDENT_EXPORT_TTL_DAYS",
        domain="observability",
        value_type="int",
        default=30,
        minimum=1,
        maximum=180,
        description="Durée de validité d'un paquet d'incident. Au-delà, l'import le refuse.",
        consumers=("apps.feedback.export.package",),
    ),
    D(
        name="FEEDBACK_INCIDENT_EXPORT_KEY",
        domain="observability",
        value_type="str",
        default="",
        secret=True,
        description=(
            "Clé Fernet (base64 urlsafe, 32 octets) de chiffrement et de signature des "
            "paquets. Vide = tout export contenant du contenu de production est REFUSÉ."
        ),
        consumers=("apps.feedback.export.crypto",),
        default_is_dynamic=True,
    ),
    D(
        name="FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT",
        domain="observability",
        value_type="str",
        default="dev",
        description=(
            "Environnement d'origine estampillé dans le manifeste. L'import le vérifie "
            "et refuse de réimporter un paquet dans son environnement d'origine."
        ),
        consumers=("apps.feedback.export.package",),
    ),
    D(
        name="FEEDBACK_INCIDENT_IMPORT_ENABLED",
        domain="observability",
        value_type="bool",
        default=False,
        description=(
            "Garde-fou d'environnement des commandes d'import et de replay. Ne doit "
            "JAMAIS être ouvert en production."
        ),
        consumers=("apps.feedback.management.commands",),
    ),
    D(
        name="RAGATM_VERSION",
        domain="observability",
        value_type="str",
        default="",
        description=(
            "Version applicative reportée par le battement de cœur du planificateur. "
            "Vide = non renseignée. Distincte d'APP_GIT_SHA, qui identifie le COMMIT "
            "construit et est injectée par le build."
        ),
        consumers=("apps.rag_core.services.beat_heartbeat",),
    ),
    D(
        name="BACKUP_MAX_AGE_SECONDS",
        domain="observability",
        value_type="int",
        default=172800,
        minimum=60,
        maximum=31536000,
        description=(
            "Âge au-delà duquel la dernière sauvegarde est signalée comme périmée "
            "par la sonde d'exploitation. 48 h par défaut."
        ),
        consumers=("apps.rag_core.services.ops_health",),
    ),
    D(
        name="OPS_COUNTERS_IO_TIMEOUT_SECONDS",
        domain="observability",
        value_type="float",
        default=0.25,
        minimum=0.01,
        maximum=10.0,
        description=(
            "Budget d'entrées-sorties des compteurs d'exploitation, distinct du délai du "
            "reranking. Mesuré : après inactivité, une écriture Redis coûte p95 ~13-45 ms "
            "et jusqu'à 740 ms en extrême (contention de `fsync` AOF). 0,25 s écrête cette "
            "queue sans perdre d'incrément en régime normal. Dépassé, la requête continue "
            "et le compteur est simplement perdu — l'observabilité ne coûte jamais une "
            "réponse."
        ),
        consumers=("config.settings.base", "apps.rag_core.services.ops_counters"),
        notes=(
            "S'applique à l'alias de cache `ops`, qui vise le MÊME service Redis que "
            "`default` : aucune instance nouvelle, seul le profil de panne diffère."
        ),
    ),
    D(
        name="RAG_OPS_METRICS_TOKEN",
        domain="observability",
        value_type="str",
        default="",
        secret=True,
        description=(
            "Jeton exigé par l'endpoint `/ops/metrics` (`Authorization: Bearer …`). "
            "Vide = aucune authentification : la protection repose alors sur le seul "
            "cloisonnement réseau — la route est servie HORS de `/api/`, que le proxy "
            "public réécrit vers le backend sans liste blanche. Renseigné, il ajoute "
            "une défense en profondeur ; il ne protège que `/ops/metrics`, pas les "
            "autres routes du port applicatif."
        ),
        consumers=("config.urls",),
        notes=(
            "Lu depuis `backend/config/urls.py`, hors des arbres `apps/` et `api/`. "
            "C'est ce qui l'avait rendu invisible au balayage des réglages non "
            "déclarés avant l'extension de sa portée."
        ),
    ),
)

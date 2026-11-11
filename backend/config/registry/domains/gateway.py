"""Domaine ``gateway`` — AI Gateway.

Politique IT : toute génération IA locale passe par la Gateway (<hôte-gateway>:8100) ;
RagATM n'appelle JAMAIS Ollama en direct. Les invariants sont vérifiés par les
system checks `llm_router.E001`..`E005`.
"""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="AI_GATEWAY_ENABLED",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Route la génération ET les embeddings via l'AI Gateway. À 0 "
            "dans un environnement officiel, le check `llm_router.E001` bloque le "
            "démarrage."
        ),
        # NON éditable par overlay (durcissement P0). Un override à `false` faisait
        # basculer génération ET embeddings vers un appel Ollama DIRECT au runtime —
        # exactement ce que `llm_router.E001`/`E003` interdisent, sans qu'aucun
        # system check ne puisse le voir (les checks lisent les settings).
        runtime_editable=False,
        max_scope=None,
        consumers=(
            "apps.llm_router.router",
            "apps.llm_router.embeddings",
            "apps.llm_router.gateway_client",
        ),
        notes=(
            "Se change par déploiement. La politique IA d'un environnement ne doit pas "
            "être pilotable à chaud tant que la doctrine de mode dégradé n'est pas tranchée."
        ),
    ),
    D(
        name="AI_GATEWAY_BASE_URL",
        domain="gateway",
        value_type="url",
        default="",
        secret=True,
        redact_mode="url",
        description=(
            "URL privée de la Gateway ; le client construit `{base}/v1/generate`. "
            "Jamais affichée en clair : seuls le schéma et le port sont rendus."
        ),
        # NON éditable par overlay (durcissement P0). Clé d'INFRASTRUCTURE : elle
        # route aussi les EMBEDDINGS (donc l'indexation, dans les workers Celery).
        # Une erreur coupe génération et indexation, et la valeur posée est
        # redactée à l'affichage : l'exploitant ne peut pas la relire pour la
        # corriger.
        runtime_editable=False,
        max_scope=None,
        required_in=(),
        consumers=("apps.llm_router.gateway_client", "apps.llm_router.embeddings"),
        notes=(
            "Obligatoire dès que AI_GATEWAY_ENABLED=1 ; une URL loopback est refusée "
            "en environnement officiel (`llm_router.E002`). Se change par déploiement."
        ),
    ),
    D(
        name="AI_GATEWAY_URL",
        domain="gateway",
        value_type="url",
        default="",
        secret=True,
        redact_mode="url",
        description="Ancien nom de l'URL de la Gateway (docker-compose historique).",
        deprecated=True,
        replacement="AI_GATEWAY_BASE_URL",
        consumers=("config.settings.base",),
        notes="Encore lue en repli par `base.py` ; à retirer des composes et de la doc.",
    ),
    D(
        name="AI_GATEWAY_TIMEOUT_SECONDS",
        domain="gateway",
        value_type="int",
        default=30,
        minimum=1,
        maximum=300,
        description=(
            "Timeout CLIENT des appels à l'AI Gateway (secondes). Doit être strictement "
            "positif."
        ),
        runtime_editable=True,
        max_scope="environment",
        restart_required=False,
        consumers=("apps.llm_router.gateway_client",),
        notes=(
            "⚠️ Le service Gateway porte une variable du MÊME NOM pour son propre appel "
            "à Ollama, et **le plus petit des deux gagne**. Constaté le 26/07/2026 : "
            "60 s côté serveur de production contre 180 s demandés par le client staging. "
            "Vérifier les deux côtés avant de conclure qu'un timeout est appliqué."
        ),
    ),
    D(
        name="AI_GATEWAY_VISION_MODEL",
        domain="gateway",
        value_type="str",
        default="",
        description=(
            "Profil LOGIQUE de modèle vision demandé à l'AI Gateway "
            "(`POST /v1/vision/analyze`, champ `model`). Vide = champ omis, le Gateway "
            "applique son profil par défaut. Jamais un nom de modèle concret."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.rag_core.services.ingest.visual",),
        notes=(
            "client vision présent SANS appelant de production. "
            "Valeur contrainte à un identifiant de profil (sans `:` ni `/`)."
        ),
    ),
    D(
        name="AI_GATEWAY_VISION_TIMEOUT_SECONDS",
        domain="gateway",
        value_type="int",
        default=120,
        minimum=1,
        maximum=300,
        description=(
            "Timeout CLIENT de lecture d'un appel vision à l'AI Gateway (secondes). "
            "Le délai de connexion reste plafonné à 10 s."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.llm_router.gateway.vision",),
        notes=(
            "Provisoire : la valeur définitive dépend de la qualification du modèle "
            "Doit rester sous la limite souple Celery (540 s)."
        ),
    ),
    D(
        name="AI_GATEWAY_VISION_V2_ALLOWED_FINGERPRINTS",
        domain="gateway",
        value_type="list",
        default=[],
        description=(
            "Allowlist des empreintes de pipeline vision V2 (`pipeline.fingerprint`, "
            "`vision-v1:<sha256>`) acceptées par le rail V2 des crops (porte K3). Vide = "
            "rail V2 INACTIF : aucun appel n'est émis. Seule source d'autorité : "
            "`/health/vision` ne fait que diagnostiquer, il n'approuve jamais."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.llm_router.gateway.vision_v2", "apps.rag_core.services.raster.vision"),
        notes=(
            "à renseigner en staging seulement (Gateway V2). La "
            "production Gateway ne sert que V1 : laisser vide. Plusieurs valeurs "
            "permettent une transition contrôlée entre deux pipelines qualifiés."
        ),
    ),
    D(
        name="STRUCTURED_VISUAL_TIMEOUT_SECONDS",
        domain="gateway",
        value_type="int",
        default=180,
        minimum=30,
        maximum=300,
        description=(
            "Timeout CLIENT de lecture d'un appel `POST /v1/vision/structured-extract` "
            "(secondes). Couvre le démarrage à froid du moteur (~15 s) et le timeout propre "
            "du Gateway (150 s). Le délai de connexion reste plafonné à 10 s."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.llm_router.gateway.structured_extract",),
    ),
    D(
        name="STRUCTURED_VISUAL_SCHEMA_VERSION",
        domain="gateway",
        value_type="enum",
        choices=("1", "2"),
        default="1",
        description=(
            "Version de schéma demandée à `POST /v1/vision/structured-extract` (contrat "
            "ai-gateway-visual-contract/22A.1). `2` ajoute les blocs `evidence`, `qualification`, `capacity` et "
            "`consistency`, exigés à la réponse. CHOIX EXPLICITE, jamais découvert par essais : ne poser `2` "
            "qu'après avoir vu `\"2\"` dans `schema_versions` de `GET /health/structured-extract` de CET "
            "environnement (production Gateway V1 : `1` seulement, `2` refusé en 400). Retour arrière = `1`."
        ),
        runtime_editable=False,
        max_scope="environment",
        restart_required=True,
        consumers=("apps.llm_router.gateway.structured_extract",),
    ),
    D(
        name="AI_GATEWAY_STREAMING_ENABLED",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Streaming natif de l'AI Gateway (`/v1/generate/stream`, NDJSON) pour le "
            "chat interactif SSE. À 0, le chat conserve le pseudo-stream historique "
            "(génération non-streaming puis émission en un seul événement). N'a "
            "d'effet que si `AI_GATEWAY_ENABLED=1`."
        ),
        consumers=(
            "apps.llm_router.router",
            "apps.llm_router.gateway_client",
            "apps.chat.services.chat_service",
        ),
        notes=(
            "Repli d'exploitation : repasser à 0 et redémarrer restaure exactement le "
            "chemin pseudo-stream. Le payload envoyé à `/v1/generate/stream` est "
            "strictement identique à celui de `/v1/generate` (même modèle, mêmes "
            "options dont `num_ctx`) : seul le mode de transport diffère."
        ),
    ),
    D(
        name="AI_GATEWAY_PROVIDER",
        domain="gateway",
        value_type="str",
        default="ollama",
        description=(
            "Provider PRIMAIRE demandé à la Gateway — c'est LUI qui sert le modèle local. "
            "Découplé de RAG_LLM_PROVIDER, qui ne concerne que le chemin direct de développement."
        ),
        consumers=("apps.llm_router.gateway_client", "apps.llm_router.router"),
        notes=(
            "Sans ce découplage, la valeur de RAG_LLM_PROVIDER (chemin direct de "
            "développement) serait demandée telle quelle à la Gateway."
        ),
    ),
    D(
        name="AI_GATEWAY_MODEL",
        domain="gateway",
        value_type="str",
        default="compact-generation-8",
        description=(
            "Modèle demandé à la Gateway EN DERNIER RECOURS, quand la requête n'en "
            "porte aucun. N'écrase JAMAIS le `llm_model` persisté d'un RagSpace."
        ),
        consumers=("apps.llm_router.gateway_client",),
        notes=(
            "Piège vérifié en production le 27/07/2026 : cette variable valait "
            "`qwen3:30b-instruct` et `rag_query_log.llm_model` portait pourtant "
            "`llama3.1:8b` — le RagSpace imposait son propre modèle. Pour piloter le "
            "modèle des nouveaux espaces, utiliser DEFAULT_RAGSPACE_CHAT_MODEL."
        ),
    ),
    D(
        name="AI_GATEWAY_FALLBACK_ENABLED",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Repli de génération si la Gateway échoue. RAGatm Open : AUCUN fournisseur de "
            "repli n'existe ; à 1, la seule valeur admise de AI_GATEWAY_FALLBACK_PROVIDER "
            "reste `none` et l'erreur Gateway typée remonte telle quelle."
        ),
        runtime_editable=True,
        max_scope="environment",
        restart_required=False,
        consumers=("apps.llm_router.router",),
        notes=(
            "Combinaison AI_GATEWAY_ENABLED=1 + FALLBACK=0 autorisée mais signalée en "
            "production : une panne Gateway devient une panne de génération."
        ),
    ),
    D(
        name="EXTERNAL_EMBEDDINGS_EGRESS_ENABLED",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Plafond PLATEFORME de la sortie d'embeddings vers un provider externe. "
            "À 0 (défaut), aucun espace ne peut envoyer ses documents ou ses requêtes "
            "à un provider externe, quelle que soit sa configuration."
        ),
        # NON éditable à chaud, délibérément. `AI_GATEWAY_FALLBACK_ENABLED` l'est, mais
        # il décrit un repli d'exploitation pendant une panne. Celui-ci ouvre la sortie
        # du FONDS DOCUMENTAIRE : en faire un interrupteur de console d'administration
        # ferait d'une décision de confidentialité un clic. Elle doit passer par la
        # configuration d'environnement, donc par une revue et une trace de déploiement.
        runtime_editable=False,
        max_scope="environment",
        restart_required=False,
        consumers=("apps.llm_router.egress",),
        notes=(
            "Découplé de AI_GATEWAY_FALLBACK_ENABLED, qui gouverne le repli de "
            "GÉNÉRATION quand la passerelle tombe. Les deux décrivaient jusqu'ici une "
            "seule variable pour deux décisions d'exploitation distinctes : rouvrir le "
            "repli de génération rouvrait aussi, sans le vouloir, la porte des "
            "embeddings. Défaut FERMÉ — le P0 corrigé par le lot "
            "EMBEDDINGS-EXTERNAL-EGRESS-GUARD ne doit jamais se réintroduire par "
            "l'ouverture d'un flag qui ne parle pas de lui."
        ),
    ),
    D(
        name="AI_GATEWAY_FALLBACK_PROVIDER",
        domain="gateway",
        value_type="enum",
        choices=("none",),
        default="none",
        description=(
            "Provider de repli quand la Gateway est indisponible. RAGatm Open : `none` "
            "uniquement — aucun fournisseur externe n'existe et Ollama direct est interdit "
            "(`llm_router.E004`)."
        ),
        consumers=("apps.llm_router.router", "apps.llm_router.checks"),
    ),
    D(
        name="AI_GATEWAY_REQUIRED_BASE_URL",
        domain="gateway",
        value_type="url",
        default="",
        description=(
            "URL de la Gateway rappelée dans les messages des system checks (indication "
            "seulement). Vide par défaut : chaque installation renseigne la sienne."
        ),
        consumers=("apps.llm_router.checks",),
    ),
    D(
        name="AI_GATEWAY_POOL_HOSTS",
        domain="gateway",
        value_type="list",
        default=[],
        description=(
            "Noms d'hôte reconnus comme un AI Gateway Pool. Une URL Gateway dont l'hôte y "
            "figure est routée `pool` ; toute autre URL http(s) est un accès `direct`. Vide "
            "en profil Compact (Gateway jointe directement)."
        ),
        consumers=("apps.llm_router.gateway.endpoints", "apps.llm_router.checks"),
    ),
    # ------------------------------------------------------------------
    # Embeddings GOUVERNÉS (lot 3) — service DISTINCT de la génération.
    # ------------------------------------------------------------------
    # Pourquoi des clés séparées de `AI_GATEWAY_BASE_URL`. Les deux services ont des
    # cycles de vie différents : le pilote Granite tourne sur la Gateway de STAGING
    # (:8200) pendant que la génération reste sur celle de son environnement. Réutiliser
    # `AI_GATEWAY_BASE_URL` aurait lié les deux — impossible de piloter les embeddings
    # sans déplacer aussi la génération. Le prix est une clé de plus ; le bénéfice est
    # que le pilote ne peut pas déborder sur un chemin qu'il n'est pas censé toucher.
    D(
        name="AI_GATEWAY_EMBEDDINGS_ENABLED",
        domain="gateway",
        value_type="bool",
        default=True,
        description=(
            "Autorise le provider d'embeddings `ai_gateway`. À 0, un RagSpace configuré "
            "sur ce provider ÉCHOUE explicitement — il ne retombe jamais en silence sur "
            "Ollama, ce qui produirait des vecteurs d'un autre pipeline."
        ),
        runtime_editable=False,
        max_scope=None,
        consumers=("apps.llm_router.embeddings_gateway",),
        notes=(
            "Interrupteur de ROLLBACK du lot 3. Le repasser à 0 coupe le chemin gouverné "
            "sans migration ni réindexation ; les RagSpaces legacy sont intouchés."
        ),
    ),
    D(
        name="AI_GATEWAY_EMBEDDINGS_BASE_URL",
        domain="gateway",
        value_type="url",
        default="",
        secret=True,
        redact_mode="url",
        description=(
            "URL privée de la Gateway d'EMBEDDINGS ; le client construit "
            "`{base}/v1/embeddings`. Distincte de `AI_GATEWAY_BASE_URL` (génération) : "
            "le pilote Granite vise :8200 (staging) alors que la génération reste sur "
            "l'instance de l'environnement."
        ),
        runtime_editable=False,
        max_scope=None,
        consumers=("apps.llm_router.embeddings_gateway",),
        notes="Obligatoire dès que AI_GATEWAY_EMBEDDINGS_ENABLED=1. Se change par déploiement.",
    ),
    D(
        name="AI_GATEWAY_EMBEDDINGS_MODEL",
        domain="gateway",
        value_type="str",
        default="legacy-768",
        description=(
            "Modèle LOGIQUE d'embeddings demandé à la Gateway (jamais le nom Ollama). "
            "N'est utilisé que si le RagSpace ne porte pas son propre `embedding_model` : "
            "la source de vérité reste l'espace."
        ),
        consumers=("apps.llm_router.embeddings_gateway",),
    ),
    D(
        name="AI_GATEWAY_BUSY_THRESHOLD_MS",
        domain="gateway",
        value_type="float",
        default=120.0,
        minimum=1.0,
        maximum=60000.0,
        description=(
            "Latence médiane d'une sonde d'embedding court au-delà de laquelle la "
            "Gateway est considérée comme DÉJÀ OCCUPÉE. Les opérations lourdes "
            "renoncent plutôt que d'aggraver."
        ),
        consumers=("apps.llm_router.gateway_load",),
        notes=(
            "Les deux instances de Gateway partagent le même calcul : quatre flux de "
            "banc en staging multiplient par 2,5 la latence de reranking de la "
            "PRODUCTION (mesuré le 22/08/2026). L'isolation réelle est hors de ce "
            "dépôt — l'hôte n'est pas joignable en SSH et n'expose ni métriques, ni "
            "file d'attente. Le seuil relève donc de la RETENUE, pas de l'isolation. "
            "Valeur choisie dans le vide entre deux populations mesurées le "
            "23/08/2026, sans aucun recouvrement : au repos 23,9 / 25,5 / 74,4 ms "
            "(min/médiane/max), sous charge 248,6 / 249,4 / 277,9 ms. Aucune valeur "
            "observée entre 74 et 249."
        ),
    ),
    D(
        name="AI_GATEWAY_EMBEDDINGS_TIMEOUT_SECONDS",
        domain="gateway",
        value_type="float",
        default=30.0,
        minimum=1.0,
        maximum=600.0,
        description=(
            "Délai maximal d'un appel `/v1/embeddings`. Trop court, l'indexation échoue "
            "sur les gros lots ; trop long, une Gateway muette bloque un worker Celery."
        ),
        consumers=("apps.llm_router.embeddings_gateway",),
    ),
    D(
        name="ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Échappatoire de TESTS : dégrade les checks Gateway bloquants en warnings. "
            "Ne doit JAMAIS être posée dans un déploiement réel."
        ),
        environments=("dev", "test"),
        consumers=("apps.llm_router.checks",),
    ),
    D(
        name="CAPABILITIES_GATEWAY_PROBES_ENABLED",
        domain="gateway",
        value_type="bool",
        default=False,
        description=(
            "Sondes AI Gateway de la console d'administration (`/admin-core`, écran « Capacités "
            "documentaires ») : GET /health/runtime, /v1/models, /health/vision, /health/asr. FERMÉES "
            "par défaut : leur caractère non inférentiel n'est "
            "pas attesté sur le code Gateway déployé. À 0 : aucun appel Gateway depuis la console, "
            "services affichés UNKNOWN (`probes_disabled`). À 1 seulement après attestation."
        ),
        runtime_editable=False, max_scope="environment", restart_required=True,
        consumers=("apps.rag_core.services.capabilities",),
    ),
)

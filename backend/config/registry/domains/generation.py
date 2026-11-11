"""Domaine ``generation`` — providers, presets de génération et routage par tâche."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="RAG_LLM_PROVIDER",
        domain="generation",
        value_type="str",
        default="ollama",
        description=(
            "Provider du chemin de génération DIRECT (sans Gateway). RAGatm Open : seule la "
            "valeur `ollama` existe. Gateway active (obligatoire en environnement officiel), "
            "elle n'a aucun effet ; Gateway éteinte en environnement officiel, "
            "`llm_router.E003` la refuse. Aucun fournisseur externe n'est reconnu "
            "(`llm_router.E009`)."
        ),
        consumers=("apps.llm_router.router", "apps.llm_router.checks"),
        notes=(
            "Conservée pour la compatibilité des réglages ; en environnement officiel la "
            "variable n'a aucun effet nominal puisque AI_GATEWAY_ENABLED=1 est obligatoire."
        ),
    ),
    D(
        name="DEFAULT_LLM_PROVIDER",
        domain="generation",
        value_type="str",
        default="ollama",
        description="Ancien nom du provider de génération par défaut.",
        deprecated=True,
        replacement="RAG_LLM_PROVIDER",
        consumers=("config.settings.base",),
        notes="Encore lue en repli par `base.py` quand RAG_LLM_PROVIDER est absente.",
    ),
    D(
        name="RAG_LLM_MODEL",
        domain="generation",
        value_type="str",
        default="compact-generation-8",
        description="Modèle de génération par défaut d'un RagSpace qui n'en définit pas.",
        consumers=("apps.llm_router.router",),
    ),
    D(
        name="OLLAMA_DEFAULT_MODEL",
        domain="generation",
        value_type="str",
        default="compact-generation-8",
        description="Ancien nom du modèle de génération par défaut.",
        deprecated=True,
        replacement="RAG_LLM_MODEL",
        consumers=("config.settings.base",),
        notes="Encore lue en repli par `base.py` quand RAG_LLM_MODEL est absente.",
    ),
    D(
        name="LLM_TIMEOUT_S",
        domain="generation",
        value_type="int",
        default=120,
        minimum=1,
        maximum=3600,
        description="Timeout générique des appels de génération.",
        consumers=("apps.llm_router.router",),
    ),
    D(
        name="DEFAULT_RAGSPACE_CHAT_PROVIDER",
        domain="generation",
        value_type="str",
        default="ollama",
        description=(
            "Provider de chat des RagSpaces NOUVELLEMENT créés : imposé à un "
            "non-superuser, prérempli (et modifiable) pour un superuser."
        ),
        consumers=(
            "apps.llm_router.model_defaults",
            "apps.llm_router.model_policy",
            "apps.rag_core.models.RagSpace",
        ),
    ),
    D(
        name="DEFAULT_RAGSPACE_CHAT_MODEL",
        domain="generation",
        value_type="str",
        default="compact-generation-8",
        description=(
            "Modèle de chat des RagSpaces NOUVELLEMENT créés — imposé à un "
            "non-superuser, prérempli pour un superuser. C'est LA variable qui "
            "pilote le modèle réellement servi, pas AI_GATEWAY_MODEL."
        ),
        consumers=(
            "apps.llm_router.model_defaults",
            "apps.llm_router.model_policy",
            "apps.rag_core.models.RagSpace",
        ),
        notes=(
            "Ne s'applique qu'à la CRÉATION : les RagSpaces existants conservent leur "
            "`llm_model` persisté, jamais réécrit. Préséance : modèle persisté du "
            "RagSpace > cette variable > AI_GATEWAY_MODEL (repli si le champ est vide). "
            "Défaut aligné sur la production (benchmark du 11/07/2026) : à qualité "
            "déterministe STRICTEMENT égale à mistral-small3.2:24b, qwen3:30b-instruct "
            "répond 3x plus vite en médiane (598 ms vs 1 924 ms) pour deux fois moins "
            "de tokens."
        ),
    ),
    # --- Presets par type de tâche ------------------------------------------
    D(
        name="RAG_FACTUAL_TEMPERATURE",
        domain="generation",
        value_type="float",
        default=0.1,
        minimum=0.0,
        maximum=2.0,
        description="Température du chat factuel (RAG).",
        consumers=("apps.llm_router.presets",),
    ),
    D(
        name="RAG_FACTUAL_SEED",
        domain="generation",
        value_type="int",
        default=None,
        default_expr="(vide) — optionnel",
        minimum=1,
        maximum=9223372036854775807,
        description=(
            "Graine de génération du chat factuel. Vide => aucune graine transmise, "
            "comportement historique. Posée => la réponse devient reproductible à "
            "prompt identique."
        ),
        consumers=("apps.llm_router.presets", "apps.chat.services.chat_service"),
        notes=(
            "Le minimum est 1, et ce n'est pas une borne de confort : mesuré sur "
            "l'upstream, `seed=0` et `seed=-1` sont ACCEPTÉS mais ne produisent "
            "aucun déterminisme — deux appels identiques rendent deux réponses "
            "différentes, exactement comme sans graine. Les refuser à la "
            "configuration vaut mieux que de laisser croire à une reproductibilité "
            "qui n'existe pas. Les valeurs 1, 7, 42, 2^31-1, 2^31 et 2^63-1 ont été "
            "vérifiées déterministes. "
            "Outil d'ÉVALUATION avant d'être un réglage d'usage courant : figer la "
            "réponse d'un utilisateur qui repose la même question est un choix "
            "produit distinct, que ce réglage rend possible sans le décider."
        ),
    ),
    D(
        name="RAG_FACTUAL_NUM_CTX",
        domain="generation",
        value_type="int",
        default=8192,
        minimum=512,
        maximum=1048576,
        description=(
            "Fenêtre de contexte du chat factuel. Couplée à RAG_MAX_PROMPT_CHARS : le "
            "prompt doit y tenir, moins les tokens de génération (`rag_core.W003`)."
        ),
        consumers=("apps.llm_router.presets", "apps.rag_core.checks_prompt_budget"),
    ),
    D(
        name="RAG_FACTUAL_NUM_PREDICT",
        domain="generation",
        value_type="int",
        default=2048,
        minimum=1,
        maximum=65536,
        description="Budget de tokens générés pour une réponse factuelle.",
        consumers=("apps.llm_router.presets", "apps.rag_core.checks_prompt_budget"),
        notes=(
            "Relevé de 768 à 2048 le 23/08/2026. À 768, le plafond tombait exactement "
            "sur le p90 des réponses réelles : mesuré sur 200 réponses, 6 % étaient "
            "coupées en production et 20 % en staging — au milieu d'une phrase, et sans "
            "que rien ne le dise (la ligne « Sources: » est ajoutée APRÈS la "
            "génération, donnant à une réponse tronquée l'apparence d'une réponse "
            "finie). 2048 couvre le p99 des deux environnements (1 594 et 1 026 "
            "tokens) et vaut déjà pour les synthèses. "
            "La fenêtre du modèle n'était pas la contrainte : `qwen3:30b-instruct` "
            "accepte 262 144 tokens, et RAG_FACTUAL_NUM_CTX en alloue 8 192. "
            "Une génération plus longue occupe le GPU plus longtemps, et la Gateway "
            "SÉRIALISE (cf. docs/07-operations/decision-reranker-latence.md) : relever "
            "encore ce plafond se paierait en latence sous charge."
        ),
    ),
    D(
        name="RAG_REPORT_TEMPERATURE",
        domain="generation",
        value_type="float",
        default=0.1,
        minimum=0.0,
        maximum=2.0,
        description="Température des synthèses et rapports.",
        consumers=("apps.llm_router.presets",),
    ),
    D(
        name="RAG_REPORT_NUM_CTX",
        domain="generation",
        value_type="int",
        default=8192,
        minimum=512,
        maximum=1048576,
        description="Fenêtre de contexte des synthèses et rapports.",
        consumers=("apps.llm_router.presets",),
    ),
    D(
        name="RAG_REPORT_NUM_PREDICT",
        domain="generation",
        value_type="int",
        default=2048,
        minimum=1,
        maximum=65536,
        description="Budget de tokens générés pour une synthèse ou un rapport.",
        consumers=("apps.llm_router.presets", "apps.rag_core.services.summary"),
    ),
    D(
        name="RAG_REWRITE_TEMPERATURE",
        domain="generation",
        value_type="float",
        default=0.0,
        minimum=0.0,
        maximum=2.0,
        description="Température de la réécriture de requête (déterminisme recherché).",
        consumers=("apps.llm_router.presets",),
    ),
    D(
        name="RAG_REWRITE_NUM_CTX",
        domain="generation",
        value_type="int",
        default=2048,
        minimum=512,
        maximum=1048576,
        description="Fenêtre de contexte de la réécriture de requête.",
        consumers=("apps.llm_router.presets",),
    ),
    D(
        name="RAG_REWRITE_NUM_PREDICT",
        domain="generation",
        value_type="int",
        default=128,
        minimum=1,
        maximum=65536,
        description="Budget de tokens générés par la réécriture de requête.",
        consumers=("apps.llm_router.presets",),
    ),
    # --- Suggestions de reformulation ---------------------------------------
    D(
        name="RAG_REPHRASING_SUGGESTIONS_ENABLED",
        domain="generation",
        value_type="bool",
        default=False,
        description=(
            "Propose 2-3 reformulations concrètes sur une VRAIE non-réponse. Génération "
            "non bloquante, sans repli cloud implicite."
        ),
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    D(
        name="RAG_REPHRASING_MAX",
        domain="generation",
        value_type="int",
        default=3,
        minimum=1,
        maximum=10,
        description="Nombre maximal de reformulations proposées.",
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    D(
        name="RAG_REPHRASING_TEMPERATURE",
        domain="generation",
        value_type="float",
        default=0.1,
        minimum=0.0,
        maximum=2.0,
        description="Température de génération des reformulations.",
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    D(
        name="RAG_REPHRASING_NUM_CTX",
        domain="generation",
        value_type="int",
        default=2048,
        minimum=512,
        maximum=1048576,
        description="Fenêtre de contexte de génération des reformulations.",
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    D(
        name="RAG_REPHRASING_NUM_PREDICT",
        domain="generation",
        value_type="int",
        default=256,
        minimum=1,
        maximum=65536,
        description="Budget de tokens générés pour les reformulations.",
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    D(
        name="RAG_REPHRASING_TIMEOUT_S",
        domain="generation",
        value_type="float",
        default=3.0,
        minimum=0.1,
        maximum=120.0,
        description=(
            "Budget COURT réellement appliqué jusqu'au client Gateway : une lenteur de "
            "génération ne prolonge jamais le flux au-delà."
        ),
        consumers=("apps.rag_core.services.rephrasing",),
    ),
    # --- Routage opt-in du modèle par tâche ---------------------------------
    D(
        name="RAG_TASK_ROUTING_ENABLED",
        domain="generation",
        value_type="bool",
        default=False,
        description=(
            "Routage EXPÉRIMENTAL du modèle par type de tâche. Override le nom de modèle "
            "uniquement (jamais le provider) et seulement pour les espaces locaux."
        ),
        consumers=("apps.llm_router.task_routing",),
    ),
    D(
        name="RAG_TASK_MODEL_FACTUAL",
        domain="generation",
        value_type="str",
        default="",
        description="Modèle dédié aux réponses factuelles. Vide => modèle du workspace.",
        consumers=("apps.llm_router.task_routing",),
    ),
    D(
        name="RAG_TASK_MODEL_REPORT",
        domain="generation",
        value_type="str",
        default="",
        description="Modèle dédié aux synthèses. Vide => modèle du workspace.",
        consumers=("apps.llm_router.task_routing",),
    ),
    D(
        name="RAG_TASK_MODEL_REWRITE",
        domain="generation",
        value_type="str",
        default="",
        description="Modèle dédié à la réécriture de requête. Vide => modèle du workspace.",
        consumers=("apps.llm_router.task_routing",),
    ),
    # --- Ollama (paramètres de génération transmis) -------------------------
    D(
        name="OLLAMA_BASE_URL",
        domain="generation",
        value_type="url",
        default="http://ollama:11434",
        description=(
            "URL du service Ollama. RagATM ne l'appelle JAMAIS en direct en "
            "environnement officiel : la Gateway sert les modèles locaux."
        ),
        consumers=("apps.llm_router.providers.ollama",),
    ),
    D(
        name="OLLAMA_NUM_CTX",
        domain="generation",
        value_type="int",
        default=4096,
        minimum=512,
        maximum=1048576,
        description="Fenêtre de contexte par défaut transmise à Ollama.",
        consumers=("apps.llm_router.providers.ollama",),
    ),
    D(
        name="OLLAMA_TEMPERATURE",
        domain="generation",
        value_type="float",
        default=0.2,
        minimum=0.0,
        maximum=2.0,
        description="Température par défaut transmise à Ollama.",
        consumers=("apps.llm_router.providers.ollama",),
    ),
    D(
        name="OLLAMA_TOP_P",
        domain="generation",
        value_type="float",
        default=0.9,
        minimum=0.0,
        maximum=1.0,
        description="Top-p par défaut transmis à Ollama.",
        consumers=("apps.llm_router.providers.ollama",),
    ),
    D(
        name="OLLAMA_REPEAT_PENALTY",
        domain="generation",
        value_type="float",
        default=1.1,
        minimum=0.0,
        maximum=5.0,
        description="Pénalité de répétition par défaut transmise à Ollama.",
        consumers=("apps.llm_router.providers.ollama",),
    ),
    D(
        name="DEFAULT_LLM_MODEL",
        domain="generation",
        value_type="str",
        default="",
        description=(
            "Modèle de génération de dernier recours, tous providers confondus. "
            "Vide => le routeur retombe sur le défaut du provider retenu."
        ),
        source="backend/apps/llm_router/router.py",
        consumers=("apps.llm_router.router",),
    ),
    D(
        name="DEFAULT_OLLAMA_MODEL",
        domain="generation",
        value_type="str",
        default="",
        description="Modèle Ollama par défaut. Vide => cascade `DEFAULT_LLM_MODEL`.",
        source="backend/apps/llm_router/router.py",
        consumers=("apps.llm_router.router",),
    ),
)

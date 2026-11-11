"""Domaine ``core`` — RagATM_Core et configuration dynamique."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="DJANGO_ENV",
        domain="core",
        value_type="enum",
        choices=("dev", "test", "staging", "prod"),
        default="dev",
        description=(
            "Environnement de déploiement. Détermine le module de settings attendu, "
            "la sévérité des system checks et la portée « environment » des overrides Core."
        ),
        required=True,
        required_in=("staging", "prod"),
        default_expr="dev (imposé par l'environnement de déploiement)",
        default_is_dynamic=True,
        consumers=("config.settings.base", "config.settings.prod", "apps.llm_router.checks"),
        notes=(
            "En staging/prod, `config.settings.base` refuse de démarrer : "
            "DJANGO_SETTINGS_MODULE doit valoir `config.settings.prod`."
        ),
    ),
    D(
        name="LOAD_DOTENV",
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Charge `.env` même hors dev (exécutions « prod-like » locales et outillage). "
            "En dev le fichier est chargé sans cette variable."
        ),
        environments=("dev", "test"),
        consumers=("config.settings.base",),
    ),
    D(
        name="CORE_CONFIG_EDIT_ENABLED",
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Autorise l'ÉCRITURE d'overrides de configuration Core depuis l'Admin Core "
            "(superuser). N'a aucun effet sur le runtime : les overrides restent "
            "diagnostic-only tant que CORE_CONFIG_RUNTIME_ENABLED vaut 0."
        ),
        consumers=("api.v1.admin_core_config",),
        notes="Jamais activé en production : notion DISTINCTE de CORE_CONFIG_RUNTIME_ENABLED.",
    ),
    D(
        name="CORE_CONFIG_RUNTIME_ENABLED",
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Branche l'overlay de configuration Core sur le RUNTIME : le résolveur lit "
            "les overrides en base avec cache et epoch d'invalidation. À 0, le runtime lit "
            "STRICTEMENT les settings."
        ),
        consumers=("apps.rag_core.core.runtime_config",),
        notes=(
            "Exige un cache PARTAGÉ (DJANGO_CACHE_URL) : sans lui, l'epoch d'invalidation "
            "diverge entre les process HTTP et les workers Celery (check `rag_core.E003`)."
        ),
    ),
    D(
        name="DJANGO_CACHE_URL",
        domain="core",
        value_type="url",
        default="",
        description=(
            "URL du cache Django partagé (Redis). Vide => LocMemCache par process, "
            "acceptable uniquement tant que l'overlay runtime Core est désactivé."
        ),
        required_in=(),
        consumers=("config.settings.base.CACHES", "apps.rag_core.core.runtime_config"),
        notes="Recommandé : `redis://redis:6379/2` (base Redis distincte du broker Celery).",
    ),
    # ------------------------------------------------------------------ #
    D(
        name="ACTIONS_ROUTER_ENABLED",
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Routeur d'actions central (`apps.actions`) : ChatView délègue la détection "
            "d'intention. Aucune action mutante n'est déclenchable par une phrase."
        ),
        consumers=("apps.chat.views", "apps.actions.registry"),
    ),
    D(
        name="ACTIONS_LLM_CLASSIFIER_ENABLED",
        domain="core",
        value_type="bool",
        default=False,
        description=(
            "Classification LLM structurée du routeur d'actions, invoquée uniquement sur "
            "formulation ambiguë. Local, sans repli externe ni retry."
        ),
        consumers=("apps.actions.classifier",),
    ),
    D(
        name="ACTIONS_LLM_CLASSIFIER_TIMEOUT_SECONDS",
        domain="core",
        value_type="float",
        default=5.0,
        minimum=0.1,
        maximum=60.0,
        description=(
            "Budget d'appel du classifieur d'actions. 5 s mesurés en staging : 3 s "
            "produisaient des timeouts parasites au cold-load de la Gateway."
        ),
        consumers=("apps.actions.classifier",),
    ),
    D(
        name="ACTIONS_LLM_CLASSIFIER_PROVIDER",
        domain="core",
        value_type="str",
        default="local",
        description="Provider du classifieur d'actions (local uniquement en pratique).",
        consumers=("apps.actions.classifier",),
    ),
    D(
        name="ACTIONS_LLM_CLASSIFIER_MODEL",
        domain="core",
        value_type="str",
        default="",
        description="Modèle du classifieur d'actions. Vide => modèle par défaut du provider.",
        consumers=("apps.actions.classifier",),
    ),
    D(
        name="ACTIONS_LLM_CLASSIFIER_ALLOWED_MODELS",
        domain="core",
        value_type="list",
        default=[],
        description=(
            "Allowlist de modèles du classifieur (administration). Vide = pas de "
            "restriction supplémentaire. Le modèle ne vient JAMAIS de la requête utilisateur."
        ),
        consumers=("apps.actions.classifier",),
    ),
)

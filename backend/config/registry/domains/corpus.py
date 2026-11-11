"""Domaine ``corpus`` — analyses globales de corpus et recherche globale."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="CORPUS_ANALYTICS_MAX_DOCUMENTS",
        domain="corpus",
        value_type="int",
        default=2000,
        minimum=1,
        description="Limite de pré-vol : nombre de documents d'une analyse globale.",
        consumers=("apps.rag_core.services.analysis",),
    ),
    D(
        name="CORPUS_ANALYTICS_MAX_CHUNKS",
        domain="corpus",
        value_type="int",
        default=100000,
        minimum=1,
        description="Limite de pré-vol : nombre d'extraits d'une analyse globale.",
        consumers=("apps.rag_core.services.analysis",),
    ),
    D(
        name="CORPUS_ANALYTICS_MAX_ESTIMATED_TOKENS",
        domain="corpus",
        value_type="int",
        default=5000000,
        minimum=1,
        description="Limite de pré-vol : tokens estimés d'une analyse globale.",
        consumers=("apps.rag_core.services.analysis",),
    ),
    D(
        name="CORPUS_ANALYTICS_MAX_CONCURRENT_PER_WORKSPACE",
        domain="corpus",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description="Analyses globales simultanées autorisées par workspace.",
        consumers=("apps.rag_core.services.analysis",),
    ),
    D(
        name="CORPUS_ANALYTICS_RESULT_TTL_DAYS",
        domain="corpus",
        value_type="int",
        default=30,
        minimum=1,
        maximum=3650,
        description="Durée de conservation des résultats d'analyse globale.",
        consumers=("apps.rag_core.services.analysis",),
    ),
    D(
        name="CORPUS_ANALYTICS_TASK_SOFT_TIME_LIMIT",
        domain="corpus",
        value_type="int",
        default=3300,
        minimum=1,
        description=(
            "Limite douce PROPRE à la tâche d'analyse : une analyse longue ne doit pas "
            "hériter des 540 s globaux dimensionnés pour l'ingestion."
        ),
        consumers=("apps.rag_core.tasks",),
    ),
    D(
        name="CORPUS_ANALYTICS_TASK_TIME_LIMIT",
        domain="corpus",
        value_type="int",
        default=3600,
        minimum=1,
        description="Limite dure de la tâche d'analyse globale.",
        consumers=("apps.rag_core.tasks",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_CANDIDATE_CHUNKS",
        domain="corpus",
        value_type="int",
        default=2000,
        minimum=1,
        description=(
            "Au-delà de ce nombre de correspondances, la recherche est REFUSÉE plutôt "
            "que tronquée : un échantillon ne doit pas passer pour un parcours du corpus."
        ),
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_BATCHES",
        domain="corpus",
        value_type="int",
        default=20,
        minimum=1,
        description=(
            "Plafond RÉEL du coût : c'est le nombre de lots qui détermine le nombre "
            "d'appels LLM. Le planificateur resserre la requête plutôt que de tronquer."
        ),
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_ESTIMATED_INPUT_TOKENS",
        domain="corpus",
        value_type="int",
        default=300000,
        minimum=1,
        description="Plafond de tokens d'entrée estimés pour une recherche globale.",
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_RETRIES",
        domain="corpus",
        value_type="int",
        default=2,
        minimum=0,
        maximum=10,
        description=(
            "Retry TRANSITOIRE uniquement (timeout, 429, 5xx). Jamais sur un JSON "
            "invalide ou une source inventée : rejouer n'y changerait rien."
        ),
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_BATCH_CHUNKS",
        domain="corpus",
        value_type="int",
        default=20,
        minimum=1,
        description="Nombre d'extraits par lot soumis au modèle.",
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_BATCH_TOKENS",
        domain="corpus",
        value_type="int",
        default=6000,
        minimum=1,
        description="Budget de tokens par lot.",
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_OUTPUT_ITEMS",
        domain="corpus",
        value_type="int",
        default=10,
        minimum=1,
        description="Nombre maximal d'éléments retournés par une recherche globale.",
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_MAX_CONCURRENT_PER_WORKSPACE",
        domain="corpus",
        value_type="int",
        default=2,
        minimum=1,
        maximum=64,
        description="Recherches globales simultanées autorisées par workspace.",
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK",
        domain="corpus",
        value_type="bool",
        default=False,
        description=(
            "Repli externe COUPÉ par défaut : une analyse globale enverrait tout le "
            "corpus candidat chez un tiers. L'activer exige un consentement explicite."
        ),
        consumers=("apps.rag_core.services.global_search",),
    ),
    D(
        name="DOMAIN_CORPUS_SUMMARY_ENABLED",
        domain="corpus",
        value_type="bool",
        default=True,
        description=(
            "Résumé d'un document (ou d'une page) de corpus métier depuis le panneau "
            "de sources. ACTIVÉ par défaut ; désactivable explicitement par "
            "DOMAIN_CORPUS_SUMMARY_ENABLED=0. Source de vérité BACKEND — l'API "
            "refuse (404) quand le flag est à 0, même si l'interface propose l'action."
        ),
        consumers=("apps.corpus.services.summary", "api.v1.corpus_documents.views"),
    ),
    D(
        name="CORPUS_GLOBAL_SEARCH_RETRY_BACKOFF_S",
        domain="corpus",
        value_type="list",
        default=[2, 5],
        description=(
            "Délais (secondes) entre tentatives d'une analyse globale de corpus. "
            "La longueur de la liste FIXE le nombre de reprises : une liste vide "
            "signifie « aucune reprise », pas « reprises immédiates »."
        ),
        consumers=("apps.rag_core.analysis.global_search",),
    ),
)

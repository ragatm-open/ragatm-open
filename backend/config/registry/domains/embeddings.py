"""Domaine ``embeddings`` — profil de référence et indexation vectorielle."""
from __future__ import annotations

from config.registry.definition import ConfigDefinition as D

DEFINITIONS: tuple[D, ...] = (
    D(
        name="RAG_EMBEDDING_DIM",
        domain="embeddings",
        value_type="int",
        default=768,
        minimum=768,
        maximum=768,
        description=(
            "Dimension des embeddings. FIGÉE à 768 : elle doit correspondre à la colonne "
            "pgvector `rag_chunk.embedding`, elle-même figée par migration."
        ),
        runtime_editable=False,
        max_scope=None,
        consumers=("apps.rag_core.services.indexing", "apps.rag_core.apps.check_embedding_dim"),
        notes=(
            "Exclue DUREMENT de l'overlay runtime Core. La changer exige une migration "
            "`AlterField`, un ré-embedding complet et la reconstruction des index."
        ),
    ),
    D(
        name="EMBEDDINGS_PROVIDER",
        domain="embeddings",
        value_type="str",
        default="ai_gateway",
        description=(
            "Provider d'embeddings global. RAGatm Open : `ai_gateway` (pipeline gouverné de "
            "l'AI Gateway) est le SEUL provider réel ; `stub` (tests) est refusé en production."
        ),
        required_in=("staging", "prod"),
        consumers=("apps.llm_router.embeddings", "config.settings.prod"),
    ),
    D(
        name="EMBEDDINGS_MODEL",
        domain="embeddings",
        value_type="str",
        default="default",
        description=(
            "Modèle d'embeddings. `default` = sentinelle « laisser le provider décider » "
            "(pour ai_gateway : AI_GATEWAY_EMBEDDINGS_MODEL)."
        ),
        consumers=("apps.llm_router.embeddings",),
    ),
    D(
        name="RAG_EMBED_BATCH_SIZE",
        domain="embeddings",
        value_type="int",
        default=64,
        minimum=1,
        maximum=64,
        description=(
            "Taille des lots d'embedding. L'AI Gateway refuse les lots au-delà de 64 "
            "textes : un document de plus de 64 chunks échouerait ENTIÈREMENT."
        ),
        consumers=("apps.rag_core.services.indexing",),
    ),
    D(
        name="DEFAULT_RAGSPACE_EMBEDDING_PROVIDER",
        domain="embeddings",
        value_type="str",
        default="ai_gateway",
        description=(
            "Provider d'embeddings imposé par le backend aux RagSpaces créés par un "
            "non-superuser."
        ),
        consumers=("apps.llm_router.model_policy",),
    ),
    D(
        name="DEFAULT_RAGSPACE_EMBEDDING_MODEL",
        domain="embeddings",
        value_type="str",
        default="legacy-768",
        description=(
            "Profil LOGIQUE d'embeddings (registre de l'AI Gateway) imposé aux RagSpaces créés "
            "par un non-superuser."
        ),
        consumers=("apps.llm_router.model_policy",),
    ),
    D(
        name="RAG_BENCH_REAL_EMBEDDINGS",
        domain="embeddings",
        value_type="bool",
        default=False,
        description=(
            "Opt-in des tests marqués `embeddings` : exige un vrai provider "
            "d'embeddings (appel réseau)."
        ),
        environments=("dev", "test"),
        source="tests (marqueur pytest `embeddings`)",
        consumers=("tests",),
    ),
    D(
        name="EMBEDDINGS_TIMEOUT_S",
        domain="embeddings",
        value_type="float",
        default=30.0,
        minimum=1.0,
        maximum=600.0,
        description=(
            "Délai maximal d'un appel d'embeddings. Trop court, l'ingestion échoue "
            "sur les lots volumineux ; trop long, un provider muet bloque un worker."
        ),
        source="backend/apps/llm_router/embeddings.py",
        consumers=("apps.llm_router.embeddings",),
    ),
    D(
        name="EMBEDDINGS_EXPECTED_PROVIDER",
        domain="embeddings",
        value_type="str",
        default="ai_gateway",
        description=(
            "Provider d'embeddings ATTENDU, comparé à la configuration réelle par "
            "le system check `rag_core.E00x`. Ce n'est pas un réglage de service : "
            "c'est l'assertion contre laquelle on vérifie que le service n'a pas dérivé."
        ),
        consumers=("apps.rag_core.checks_embeddings",),
    ),
    D(
        name="EMBEDDINGS_EXPECTED_MODEL",
        domain="embeddings",
        value_type="str",
        default="legacy-768",
        description="Modèle d'embeddings ATTENDU, comparé à la configuration réelle.",
        consumers=("apps.rag_core.checks_embeddings",),
    ),
)

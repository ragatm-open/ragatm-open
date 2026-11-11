from django.apps import AppConfig
from django.core.checks import Error, register


class RagCoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rag_core"

    def ready(self) -> None:
        # Enregistre les system checks rag_core.
        from apps.rag_core.checks_embeddings import (
            check_embeddings_profile,
            check_governed_embeddings_model,
        )

        from apps.rag_core.checks_rerank import (
            check_rerank_paths_exclusive,
            check_rerank_provider,
            check_reranker_gateway_address,
        )

        from apps.rag_core.checks_tabular_capacity import check_tabular_capacity

        from apps.rag_core.checks_admission import check_admission_policy
        from apps.rag_core.checks_analytical_budgets import check_analytical_budgets
        from apps.rag_core.checks_prompt_budget import check_prompt_budget
        from apps.rag_core.checks_structural_noise import check_structural_noise_penalty
        from apps.rag_core.checks_fts_stored_vector import (
            check_fts_stored_vector_config,
            check_fts_stored_vector_schema,
        )

        register(check_embedding_dim)
        # Pénalité de bruit structurel : un facteur hors bornes déformerait le
        # classement sans lever d'erreur.
        register(check_structural_noise_penalty)
        # Budget de prompt : il tronque le contexte en silence quand il déborde la
        # fenêtre du modèle.
        register(check_prompt_budget)
        # Budgets analytiques (explicatif / comparatif) : un réglage inerte doit se voir
        # au démarrage, pas se deviner en production.
        register(check_analytical_budgets)
        # Politique d'admission (lot 4) : une configuration incohérente ne plante
        # pas, elle DÉCIDE AUTREMENT — silencieusement. Refus au démarrage.
        register(check_admission_policy)
        # Provider de reranking : refuse une valeur inconnue quand le reranking est
        # activé (sinon le stub tournerait à la place du cross-encoder, en silence).
        register(check_rerank_provider)
        # Exclusion mutuelle des deux chemins de reranking : activés ensemble, le
        # classement de la Gateway est réécrit par le reranker historique — une
        # qualification mesurerait la composition des deux en croyant en mesurer un.
        register(check_rerank_paths_exclusive)
        # Reranking Gateway activé sans adresse : chaque requête échouerait et
        # replierait sur l'ordre RRF — une baseline servie sous l'étiquette
        # « rerankée ». L'adresse n'étant pas dans l'identité gelée d'une
        # campagne (topologie), la garde doit vivre ici.
        register(check_reranker_gateway_address)
        # Garde de démarrage de la capacité tabulaire : bloquante en production.
        register(check_tabular_capacity)
        # Vecteur lexical stocké : la config FTS est figée dans le schéma par la
        # colonne générée. Une divergence ne planterait pas — elle classerait
        # avec la mauvaise langue, en silence. Refus au démarrage.
        register(check_fts_stored_vector_config)
        register(check_fts_stored_vector_schema)
        register(check_runtime_config_cache)
        # Profil d'embeddings (source de vérité ollama/nomic-embed-text/768/hybride) :
        # Warning en dev, Error bloquante en staging/prod.
        register(check_embeddings_profile)
        register(check_governed_embeddings_model)

        # --- Exploitation (Lot 4-E) ---------------------------------------
        # Une configuration d'ordonnancement ou de rétention incohérente doit
        # BLOQUER le démarrage : un service qui tourne sans rien ordonnancer, ou
        # qui purge des audits de sécurité trop tôt, est pire qu'un service arrêté.
        from apps.rag_core.checks_operations import (
            check_audit_models_have_retention,
            check_audit_retention,
            check_beat_schedule,
        )

        register(check_audit_retention)
        register(check_beat_schedule)
        register(check_audit_models_have_retention)

        # --- Extraction structurée (expérimentale) --------------------------
        # Refusée en production et inerte hors allowlist par le code : ce refus est
        # silencieux. Avertissement au démarrage, jamais bloquant.
        from apps.rag_core.checks_structured_visual import check_structured_visual_scope

        register(check_structured_visual_scope)

        # --- Configuration (registre canonique) ---------------------------
        # `config/` n'est pas une application Django : ses checks sont
        # enregistrés ici, depuis l'app toujours installée. Ils NE dupliquent
        # aucune garde existante (cf. docstring de `config/checks.py`).
        from config.checks import register_all as register_config_checks

        register_config_checks()


def check_embedding_dim(app_configs, **kwargs):
    """Échoue si `RAG_EMBEDDING_DIM` diverge de la dimension figée de la colonne
    pgvector `rag_chunk.embedding`.

    La colonne est figée par migration ; un setting divergent casse l'ingestion
    de façon silencieuse (docs FAILED) et expose à une migration destructrice.
    """
    from django.conf import settings

    from apps.rag_core.models.chunk import Chunk

    errors = []
    column_dim = Chunk._meta.get_field("embedding").dimensions
    setting_dim = getattr(settings, "RAG_EMBEDDING_DIM", column_dim)

    try:
        setting_dim = int(setting_dim)
    except (TypeError, ValueError):
        errors.append(
            Error(
                f"RAG_EMBEDDING_DIM n'est pas un entier valide ({setting_dim!r}).",
                id="rag_core.E002",
            )
        )
        return errors

    if setting_dim != column_dim:
        errors.append(
            Error(
                "RAG_EMBEDDING_DIM ne correspond pas à la dimension de la colonne "
                "pgvector rag_chunk.embedding "
                f"(setting={setting_dim}, colonne={column_dim}). "
                "La colonne est figée par migration. Pour changer la dimension : "
                "créer une migration AlterField, ré-encoder tous les documents et "
                "reconstruire les index vectoriels.",
                id="rag_core.E001",
            )
        )
    return errors


def check_runtime_config_cache(app_configs, **kwargs):
    """Échoue si le runtime overlay RagATM_Core est activé sans cache partagé.

    ``CORE_CONFIG_RUNTIME_ENABLED=True`` sans ``DJANGO_CACHE_URL`` => Django retombe
    sur LocMemCache (par-process) : l'epoch d'invalidation n'est pas cohérent entre
    les process HTTP et Celery. On bloque cette combinaison au démarrage.

    NB : l'id idéal "rag_core.E001" demandé est DÉJÀ pris par le check de dimension
    d'embeddings (tout comme E002) ; on utilise donc "rag_core.E003" pour éviter
    toute collision d'identifiant.
    """
    from django.conf import settings

    errors = []
    runtime_on = bool(getattr(settings, "CORE_CONFIG_RUNTIME_ENABLED", False))
    cache_url = (getattr(settings, "DJANGO_CACHE_URL", "") or "").strip()

    if runtime_on and not cache_url:
        errors.append(
            Error(
                "CORE_CONFIG_RUNTIME_ENABLED is enabled but DJANGO_CACHE_URL is not configured.",
                hint=(
                    "Set DJANGO_CACHE_URL=redis://redis:6379/2 or disable "
                    "CORE_CONFIG_RUNTIME_ENABLED."
                ),
                id="rag_core.E003",
            )
        )
    return errors

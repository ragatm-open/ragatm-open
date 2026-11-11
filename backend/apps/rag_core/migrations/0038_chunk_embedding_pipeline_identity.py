# backend/apps/rag_core/migrations/0038_chunk_embedding_pipeline_identity.py
import apps.llm_router.model_defaults
from django.db import migrations, models


class Migration(migrations.Migration):
    """Identité du pipeline d'embeddings, par chunk (lot 3).

    ## Ce que cette migration fait

    1. `rag_chunk.pipeline_id`     — identité VERBATIM rendue par l'AI Gateway.
    2. `rag_chunk.pipeline_family` — clé de comparabilité, obtenue en DÉCOUPANT (1).
    3. `RagSpace.embedding_provider` — ajoute le choix `ai_gateway`.
    4. Index composite `(rag_space_id, pipeline_family)` — support du filtre de
       recherche gouvernée.

    ## Ce que cette migration NE fait PAS

    **Aucun backfill.** Les chunks existants restent à `pipeline_id = NULL`, et c'est
    le point le plus important de cette migration. Étiqueter rétroactivement un chunk
    historique reviendrait à affirmer quels poids l'ont produit alors que rien ne le
    prouve — une identité fausse est plus dangereuse qu'une identité absente, parce
    que le filtre de comparabilité, lui, lui ferait confiance.

    **Aucun vecteur n'est réécrit.** La colonne `embedding` n'est pas touchée, la
    dimension reste 768, et les index HNSW/ivfflat existants ne sont pas reconstruits.

    ## Coût et verrous

    - Les deux `AddField` sont NULLABLE et sans défaut : sur PostgreSQL 11+, c'est une
      opération de catalogue, sans réécriture de table ni verrou long, quelle que soit
      la taille de `rag_chunk`.
    - `AlterField` sur `embedding_provider` ne change que des `choices` Django : aucun
      DDL n'est émis.
    - Les index, eux, verrouilleraient la table en écriture. Ils sont donc créés
      `CONCURRENTLY`, comme les index 0014 et 0023 de cette même table (d'où
      `atomic = False`). `IF NOT EXISTS` rend la migration rejouable : une création
      concurrente interrompue laisse un index INVALIDE, qu'il faut alors supprimer à
      la main avant de rejouer.

    ## Rollback

    `migrate rag_core 0037` supprime les deux colonnes et les deux index. Aucun
    embedding n'est perdu : seule l'identité de pipeline disparaît. Les espaces
    gouvernés doivent alors repasser en provider historique (cf. runbook §Rollback).
    """

    # Requis par `CREATE INDEX CONCURRENTLY` : PostgreSQL refuse de l'exécuter dans un
    # bloc transactionnel.
    atomic = False

    dependencies = [
        ("rag_core", "0037_reindex_retry_strategies"),
    ]

    operations = [
        migrations.AddField(
            model_name="chunk",
            name="pipeline_id",
            # `db_index=False` ICI : l'index est créé plus bas en CONCURRENTLY, et
            # l'état Django est réaligné par le `SeparateDatabaseAndState`.
            field=models.CharField(blank=True, db_index=False, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="chunk",
            name="pipeline_family",
            field=models.CharField(blank=True, max_length=191, null=True),
        ),
        migrations.AlterField(
            model_name="ragspace",
            name="embedding_provider",
            field=models.CharField(
                choices=[
                    ("openai", "OpenAI"),
                    ("ollama", "Ollama"),
                    ("ai_gateway", "AI Gateway"),
                ],
                default=apps.llm_router.model_defaults.default_ragspace_embedding_provider,
                max_length=32,
            ),
        ),
        # Index créés hors ORM pour obtenir CONCURRENTLY. `state_operations` tient
        # l'état Django au courant : sans lui, `makemigrations` proposerait de recréer
        # ces mêmes index à chaque exécution.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS rag_chunk_space_pipeline_idx
                        ON rag_chunk (rag_space_id, pipeline_family);
                    """,
                    reverse_sql="""
                        DROP INDEX CONCURRENTLY IF EXISTS rag_chunk_space_pipeline_idx;
                    """,
                ),
                migrations.RunSQL(
                    sql="""
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS rag_chunk_pipeline_id_idx
                        ON rag_chunk (pipeline_id);
                    """,
                    reverse_sql="""
                        DROP INDEX CONCURRENTLY IF EXISTS rag_chunk_pipeline_id_idx;
                    """,
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="chunk",
                    index=models.Index(
                        fields=["rag_space", "pipeline_family"],
                        name="rag_chunk_space_pipeline_idx",
                    ),
                ),
                migrations.AlterField(
                    model_name="chunk",
                    name="pipeline_id",
                    field=models.CharField(
                        blank=True, db_index=True, max_length=255, null=True
                    ),
                ),
            ],
        ),
    ]

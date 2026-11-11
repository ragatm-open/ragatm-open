# backend/apps/rag_core/migrations/0014_chunk_indexes_fts_hnsw.py
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        # ⚠️ Adapte si ton dernier fichier n'est pas 0013_...
        ("rag_core", "0013_rename_ragbench_idx_rag_core_ra_rag_spa_b59267_idx_and_more"),
    ]

    operations = [
        # --- Ensure pgvector extension exists (safe no-op if already installed)
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql=migrations.RunSQL.noop,
        ),

        # --- FTS GIN index for hybrid search
        # Supports:
        #   to_tsvector('simple', c.text) @@ to_tsquery('simple', ...)
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS rag_chunk_text_fts_gin_idx
                ON rag_chunk
                USING GIN (to_tsvector('simple', text));
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS rag_chunk_text_fts_gin_idx;
            """,
        ),

        # --- Vector index (HNSW) for cosine distance (<=>)
        # Your query uses: ORDER BY c.embedding <=> %(qv)s::vector
        # So we want: vector_cosine_ops
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS rag_chunk_embedding_hnsw_cosine_idx
                ON rag_chunk
                USING HNSW (embedding vector_cosine_ops);
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS rag_chunk_embedding_hnsw_cosine_idx;
            """,
        ),
    ]

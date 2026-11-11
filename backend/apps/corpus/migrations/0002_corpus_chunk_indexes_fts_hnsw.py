# backend/apps/corpus/migrations/0002_corpus_chunk_indexes_fts_hnsw.py
from django.db import migrations


class Migration(migrations.Migration):
    """Index FTS (GIN) + vectoriel (HNSW) sur ``domain_corpus_chunk``.

    Strictement parallèle aux index de ``rag_chunk`` (rag_core 0014/0023) afin que
    le retrieval bi-source (FTS + vector) du corpus métier ait les mêmes garanties
    de performance que le privé. Aucune table privée n'est touchée. ``CONCURRENTLY``
    impose ``atomic = False`` et ``IF NOT EXISTS`` garde la migration idempotente.
    """

    atomic = False

    dependencies = [
        ("corpus", "0001_initial"),
    ]

    operations = [
        # pgvector requis pour la colonne embedding (no-op si déjà présent).
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # FTS GIN — config 'simple' (défaut, robuste sigles/entités).
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS domain_corpus_chunk_text_fts_gin_idx
                ON domain_corpus_chunk
                USING GIN (to_tsvector('simple', text));
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS domain_corpus_chunk_text_fts_gin_idx;
            """,
        ),
        # FTS GIN — config 'french' parallèle (A/B de RAG_FTS_CONFIG, cf. rag_core 0023).
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS domain_corpus_chunk_text_fts_french_gin_idx
                ON domain_corpus_chunk
                USING GIN (to_tsvector('french', text));
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS domain_corpus_chunk_text_fts_french_gin_idx;
            """,
        ),
        # Vectoriel HNSW (cosine) — aligné sur l'opérateur <=> utilisé au retrieval.
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS domain_corpus_chunk_embedding_hnsw_cosine_idx
                ON domain_corpus_chunk
                USING HNSW (embedding vector_cosine_ops);
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS domain_corpus_chunk_embedding_hnsw_cosine_idx;
            """,
        ),
    ]

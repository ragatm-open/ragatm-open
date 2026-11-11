# backend/apps/rag_core/migrations/0023_chunk_fts_french_index.py
from django.db import migrations


class Migration(migrations.Migration):
    """Index GIN FTS 'french' parallèle (B3).

    Permet d'A/B-tester RAG_FTS_CONFIG='french' sans casser la prod : l'index
    'simple' (migration 0014) reste en place et reste le défaut. Sans cet index,
    une requête en config 'french' ferait un seq scan (l'index 'simple' ne matche
    pas l'expression `to_tsvector('french', text)`).
    """

    atomic = False

    dependencies = [
        ("rag_core", "0022_normalize_document_status_uppercase"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS rag_chunk_text_fts_french_gin_idx
                ON rag_chunk
                USING GIN (to_tsvector('french', text));
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS rag_chunk_text_fts_french_gin_idx;
            """,
        ),
    ]

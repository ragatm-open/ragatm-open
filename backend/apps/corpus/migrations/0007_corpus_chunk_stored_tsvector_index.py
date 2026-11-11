# backend/apps/corpus/migrations/0007_corpus_chunk_stored_tsvector_index.py
from django.db import migrations


class Migration(migrations.Migration):
    """Index GIN sur la colonne stockée (M2/2).

    Séparé de la migration de colonne pour deux raisons :
      - CONCURRENTLY interdit la transaction (`atomic = False`) alors que la
        création de colonne doit rester transactionnelle pour être annulable ;
      - la colonne existe déjà quand l'index se construit, donc l'index se bâtit
        sans bloquer les écritures.

    Les index d'expression historiques (`domain_corpus_chunk_text_fts_french_gin_idx`,
    migration 0002) sont CONSERVÉS : ils servent le chemin flag OFF, qui
    reste le défaut et la cible du rollback.
    """

    atomic = False

    dependencies = [
        ("corpus", "0006_corpus_chunk_stored_tsvector_column"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS domain_corpus_chunk_text_tsv_french_gin
                ON domain_corpus_chunk USING GIN (text_tsv_french);
            """,
            reverse_sql="""
                DROP INDEX CONCURRENTLY IF EXISTS domain_corpus_chunk_text_tsv_french_gin;
            """,
        ),
    ]

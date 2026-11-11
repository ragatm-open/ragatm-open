# backend/apps/rag_core/migrations/0039_chunk_stored_tsvector_column.py
from django.db import migrations


class Migration(migrations.Migration):
    """Colonne générée stockée portant `to_tsvector('french', text)` (M1/2).

    Pourquoi : la requête FTS recalculait le vecteur lexical 3× par ligne
    candidate (projection du rang, condition `@@`, filtre `min_rank`). Sur un
    corpus réel, 91 % du temps SQL était de la tokenisation CPU pure, sans
    aucune lecture disque. La colonne stockée matérialise ce calcul une fois à
    l'écriture.

    `GENERATED ALWAYS ... STORED` garantit que la colonne ne peut pas diverger
    du texte : PostgreSQL la recalcule à chaque INSERT/UPDATE et REFUSE toute
    écriture directe. Il n'y a donc ni trigger à maintenir, ni resynchronisation.

    ⚠️ La config `'french'` est FIGÉE dans le schéma. Changer `RAG_FTS_CONFIG`
    sans refaire cette migration rendrait la colonne fausse : l'invariant est
    verrouillé au démarrage par les checks `rag_core.E120/E121/E122`.

    Verrou : ADD COLUMN d'une colonne générée réécrit la table (ACCESS EXCLUSIVE
    pendant toute la durée). Voir le plan de bascule production pour la fenêtre.

    `max_parallel_maintenance_workers = 0` : les workers parallèles allouent de
    la mémoire partagée via /dev/shm, dimensionné à 64 Mo par défaut sous Docker
    alors que `maintenance_work_mem` vaut 512 Mo. Sans ce garde-fou la migration
    échoue sur « could not resize shared memory segment ». Le SET est LOCAL,
    donc limité à la transaction de migration : aucun réglage global modifié.
    """

    atomic = True

    dependencies = [
        ("rag_core", "0038_chunk_embedding_pipeline_identity"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                SET LOCAL max_parallel_maintenance_workers = 0;
                ALTER TABLE rag_chunk
                  ADD COLUMN IF NOT EXISTS text_tsv_french tsvector
                  GENERATED ALWAYS AS (to_tsvector('french'::regconfig, text)) STORED;
            """,
            reverse_sql="""
                ALTER TABLE rag_chunk DROP COLUMN IF EXISTS text_tsv_french;
            """,
        ),
    ]

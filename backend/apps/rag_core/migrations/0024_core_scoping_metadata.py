# PR 1 RagATM_Core — métadonnées de scoping (additif, non destructif).
#
# Ajoute des champs de scoping pour la trajectoire RagATM_Core :
#   - RagSpace : project (def "ragatm"), environment (def "prod")
#   - Document : collection (def "legacy"), source_app (def "ragatm"),
#                confidentiality (def "internal"), document_status (def "active"),
#                version (def "")
#
# Compatibilité arrière :
#   - 100 % AddField avec `default` : PostgreSQL applique le défaut à toutes les
#     lignes EXISTANTES. Aucun backfill séparé n'est requis ; les RagSpace et
#     Document déjà en base reçoivent automatiquement les valeurs de compatibilité.
#   - `document_status` (métier) est volontairement DISTINCT de `Document.status`
#     (technique d'ingestion) : aucun champ existant n'est renommé ni réutilisé.
#   - Aucun index, contrainte, choix ou comportement n'est ajouté ici : les champs
#     sont inertes (ni l'ingestion, ni le retrieval, ni l'API ne les lisent).
#
# Rollback : `migrate rag_core 0023_chunk_fts_french_index` (reverse = DROP COLUMN
# des 7 colonnes ; aucune perte de donnée applicative existante).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rag_core', '0023_chunk_fts_french_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='collection',
            field=models.CharField(default='legacy', max_length=128),
        ),
        migrations.AddField(
            model_name='document',
            name='confidentiality',
            field=models.CharField(default='internal', max_length=32),
        ),
        migrations.AddField(
            model_name='document',
            name='document_status',
            field=models.CharField(default='active', max_length=32),
        ),
        migrations.AddField(
            model_name='document',
            name='source_app',
            field=models.CharField(default='ragatm', max_length=64),
        ),
        migrations.AddField(
            model_name='document',
            name='version',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='ragspace',
            name='environment',
            field=models.CharField(default='prod', max_length=32),
        ),
        migrations.AddField(
            model_name='ragspace',
            name='project',
            field=models.CharField(default='ragatm', max_length=64),
        ),
    ]

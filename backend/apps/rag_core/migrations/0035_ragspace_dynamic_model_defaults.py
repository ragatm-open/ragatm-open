"""Défauts de champ modèle du RagSpace : littéraux -> callables lisant les settings.

MIGRATION D'ÉTAT UNIQUEMENT. `manage.py sqlmigrate rag_core 0035` rend quatre
`(no-op)` : Django n'applique pas les `default=` au niveau base (seul `db_default`
le ferait). Aucun `UPDATE`, aucun `ALTER COLUMN`, aucune ligne existante touchée
— les RagSpaces déjà créés conservent exactement leur `llm_model`, y compris
`llama3.1:8b` lorsque c'était un choix délibéré. Aucune réindexation n'est
déclenchée : les champs d'embeddings ne changent pas de valeur, seulement de
valeur PAR DÉFAUT pour les créations futures.

Les callables référencées ici doivent rester importables sous
`apps.llm_router.model_defaults` : les renommer casserait la relecture de cette
migration.
"""


import apps.llm_router.model_defaults
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rag_core', '0034_ragspace_created_by_ragspace_owner_scope_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ragspace',
            name='embedding_model',
            field=models.CharField(blank=True, default=apps.llm_router.model_defaults.default_ragspace_embedding_model, max_length=128),
        ),
        migrations.AlterField(
            model_name='ragspace',
            name='embedding_provider',
            field=models.CharField(choices=[('openai', 'OpenAI'), ('ollama', 'Ollama')], default=apps.llm_router.model_defaults.default_ragspace_embedding_provider, max_length=32),
        ),
        migrations.AlterField(
            model_name='ragspace',
            name='llm_model',
            field=models.CharField(blank=True, default=apps.llm_router.model_defaults.default_ragspace_chat_model, max_length=128),
        ),
        migrations.AlterField(
            model_name='ragspace',
            name='llm_provider',
            field=models.CharField(choices=[('openai', 'OpenAI'), ('ollama', 'Ollama')], default=apps.llm_router.model_defaults.default_ragspace_chat_provider, max_length=32),
        ),
    ]

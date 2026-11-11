"""RAGatm Open — providers de chat et d'embeddings des RagSpaces.

L'édition Open ne connaît qu'un moteur de chat (`ollama` = modèle local servi par l'AI
Gateway) et qu'un pipeline d'embeddings (`ai_gateway`, identité gouvernée). Les anciens
choix (`openai`, `ollama` pour les embeddings) disparaissent des `choices` du modèle ; les
défauts restent résolus par les réglages (`DEFAULT_RAGSPACE_*`). Migration de schéma sans
transformation de données : une base vierge n'a aucune ligne à convertir.
"""
from django.db import migrations, models

import apps.llm_router.model_defaults


class Migration(migrations.Migration):

    dependencies = [
        ("rag_core", "0047_audio_transcript"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ragspace",
            name="llm_provider",
            field=models.CharField(
                choices=[("ollama", "Modèle local (AI Gateway)")],
                default=apps.llm_router.model_defaults.default_ragspace_chat_provider,
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="ragspace",
            name="embedding_provider",
            field=models.CharField(
                choices=[("ai_gateway", "AI Gateway (pipeline gouverné)")],
                default=apps.llm_router.model_defaults.default_ragspace_embedding_provider,
                max_length=32,
            ),
        ),
    ]

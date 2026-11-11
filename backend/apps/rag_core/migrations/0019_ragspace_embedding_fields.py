# Étape 1 — Chat/Embeddings configurables PAR RagSpace.
#
# - Ajoute `embedding_provider` / `embedding_model` (défaut OpenAI).
# - Passe le défaut de `llm_provider` (chat) à "openai".
#
# Migration des espaces EXISTANTS :
# - les deux nouveaux champs embeddings reçoivent le défaut OpenAI
#   (text-embedding-3-small) via AddField : conforme à l'exigence "OpenAI par
#   défaut pour les embeddings", y compris pour les anciens espaces ;
# - le provider de CHAT existant est volontairement PRÉSERVÉ (on ne réécrit pas
#   une valeur déjà stockée) afin de ne pas casser des configurations Ollama en
#   place. Seul le défaut des nouveaux espaces devient OpenAI.
#
# NB sécurité : si OPENAI_API_KEY est absente côté serveur, l'indexation d'un
# espace configuré OpenAI échoue de façon EXPLICITE (EmbeddingsError) — jamais de
# repli silencieux vers Ollama.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rag_core", "0018_document_meta"),
    ]

    operations = [
        migrations.AddField(
            model_name="ragspace",
            name="embedding_provider",
            field=models.CharField(
                choices=[("openai", "OpenAI"), ("ollama", "Ollama")],
                default="openai",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="ragspace",
            name="embedding_model",
            field=models.CharField(
                blank=True,
                default="text-embedding-3-small",
                max_length=128,
            ),
        ),
        migrations.AlterField(
            model_name="ragspace",
            name="llm_provider",
            field=models.CharField(
                choices=[("openai", "OpenAI"), ("ollama", "Ollama")],
                default="openai",
                max_length=32,
            ),
        ),
    ]

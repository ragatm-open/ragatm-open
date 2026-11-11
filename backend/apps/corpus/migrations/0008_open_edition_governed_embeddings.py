"""RAGatm Open — le corpus métier n'encode que par le pipeline gouverné de l'AI Gateway.

Défaut `ai_gateway` / profil logique `legacy-768` ; l'ancien défaut (fournisseur externe)
n'existe plus dans l'édition Open. Schéma seulement : aucune donnée à convertir sur une
installation vierge.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("corpus", "0007_corpus_chunk_stored_tsvector_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="domaincorpus",
            name="embedding_provider",
            field=models.CharField(
                choices=[("ai_gateway", "AI Gateway (pipeline gouverné)")],
                default="ai_gateway",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="domaincorpus",
            name="embedding_model",
            field=models.CharField(blank=True, default="legacy-768", max_length=128),
        ),
    ]

# Étape 1 — audit des changements d'embeddings par RagSpace.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0008_llmsettingschangelog"),
    ]

    operations = [
        migrations.AddField(
            model_name="llmsettingschangelog",
            name="old_embedding_provider",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="llmsettingschangelog",
            name="old_embedding_model",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="llmsettingschangelog",
            name="new_embedding_provider",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="llmsettingschangelog",
            name="new_embedding_model",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
    ]

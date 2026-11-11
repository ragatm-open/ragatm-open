# backend/apps/rag_core/migrations/0012_ragbenchmarkquestion.py
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rag_core", "0011_unique_doc_space_sha256_not_failed"),  # ajuste si besoin
    ]

    operations = [
        migrations.CreateModel(
            name="RagBenchmarkQuestion",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("rag_space_id", models.UUIDField(db_index=True)),
                ("question", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("expected_keywords", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="ragbenchmarkquestion",
            index=models.Index(fields=["rag_space_id", "is_active", "created_at"], name="ragbench_idx"),
        ),
    ]

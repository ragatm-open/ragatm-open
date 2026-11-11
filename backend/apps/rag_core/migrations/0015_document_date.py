# backend/apps/rag_core/migrations/0015_document_date.py
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rag_core", "0014_chunk_indexes_fts_hnsw"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="document_date",
            field=models.DateField(null=True, blank=True),
        ),
        migrations.AddIndex(
            model_name="document",
            index=models.Index(fields=["rag_space", "document_date"], name="rag_doc_space_date_idx"),
        ),
    ]

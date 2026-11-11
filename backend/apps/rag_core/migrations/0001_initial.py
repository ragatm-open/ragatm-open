from django.db import migrations, models
import django.db.models.deletion
import uuid
from pgvector.django import VectorField

class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        # Sécurise l'extension (même si tu l'actives via initdb)
        migrations.RunSQL("CREATE EXTENSION IF NOT EXISTS vector;", reverse_sql=migrations.RunSQL.noop),

        migrations.CreateModel(
            name="RagSpace",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("name", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "rag_space"},
        ),

        migrations.CreateModel(
            name="Document",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("filename", models.CharField(max_length=512)),
                ("content_type", models.CharField(max_length=128, blank=True, default="")),
                ("size_bytes", models.BigIntegerField(default=0)),
                ("sha256", models.CharField(max_length=64, blank=True, default="")),
                ("status", models.CharField(max_length=16, default="pending")),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("rag_space", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="documents", to="rag_core.ragspace")),
            ],
            options={"db_table": "rag_document"},
        ),

        migrations.CreateModel(
            name="Chunk",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("chunk_index", models.IntegerField(default=0)),
                ("text", models.TextField()),
                ("embedding", VectorField(dimensions=768, null=True, blank=True)),
                ("page_number", models.IntegerField(null=True, blank=True)),
                ("meta", models.JSONField(default=dict, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chunks", to="rag_core.document")),
                ("rag_space", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chunks", to="rag_core.ragspace")),
            ],
            options={"db_table": "rag_chunk"},
        ),

        migrations.AddIndex(
            model_name="document",
            index=models.Index(fields=["rag_space", "created_at"], name="doc_space_created_idx"),
        ),
        migrations.AddIndex(
            model_name="document",
            index=models.Index(fields=["rag_space", "status"], name="doc_space_status_idx"),
        ),
        migrations.AddIndex(
            model_name="chunk",
            index=models.Index(fields=["rag_space", "document"], name="chunk_space_doc_idx"),
        ),
        migrations.AddIndex(
            model_name="chunk",
            index=models.Index(fields=["rag_space", "chunk_index"], name="chunk_space_idx_idx"),
        ),

        # Index vector (cosine) : utile dès qu'on aura de vrais embeddings
        migrations.RunSQL(
            """
            CREATE INDEX IF NOT EXISTS rag_chunk_embedding_ivfflat
            ON rag_chunk
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 50);
            """,
            reverse_sql="DROP INDEX IF EXISTS rag_chunk_embedding_ivfflat;",
        ),

        # Index texte (hybrid search) : tsvector simple (FR plus tard si besoin)
        migrations.RunSQL(
            """
            CREATE INDEX IF NOT EXISTS rag_chunk_text_tsv_gin
            ON rag_chunk
            USING GIN (to_tsvector('simple', text));
            """,
            reverse_sql="DROP INDEX IF EXISTS rag_chunk_text_tsv_gin;",
        ),
    ]

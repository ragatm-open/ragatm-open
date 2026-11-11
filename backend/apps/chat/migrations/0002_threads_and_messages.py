from __future__ import annotations

import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0001_initial"),
        ("rag_core", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChatThread",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("title", models.CharField(max_length=160, null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("rag_space", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chat_threads", to="rag_core.ragspace")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chat_threads", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "chat_thread",
            },
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("role", models.CharField(max_length=16, choices=[("system", "system"), ("user", "user"), ("assistant", "assistant")])),
                ("content", models.TextField()),
                ("sources", models.JSONField(null=True, blank=True)),
                ("request_id", models.CharField(max_length=64, null=True, blank=True)),
                ("rag_query_log_id", models.UUIDField(null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("thread", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="chat.chatthread")),
            ],
            options={
                "db_table": "chat_message",
            },
        ),
        migrations.AddIndex(
            model_name="chatthread",
            index=models.Index(fields=["user", "rag_space", "-updated_at"], name="chatthread_user_rs_upd_idx"),
        ),
        migrations.AddIndex(
            model_name="chatthread",
            index=models.Index(fields=["rag_space", "-updated_at"], name="chatthread_rs_upd_idx"),
        ),
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(fields=["thread", "created_at"], name="chatmsg_thread_created_idx"),
        ),
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(fields=["thread", "role"], name="chatmsg_thread_role_idx"),
        ),
    ]

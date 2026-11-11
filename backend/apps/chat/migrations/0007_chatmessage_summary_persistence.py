# backend/apps/chat/migrations/0007_chatmessage_summary_persistence.py
"""Persistance des résumés documentaires dans le fil de discussion.

Additive et rétrocompatible :
- tous les champs ajoutés ont une valeur par défaut neutre ("" ou NULL), donc
  aucun backfill n'est nécessaire et les messages historiques restent lisibles ;
- ``updated_at`` est initialisé à ``created_at`` pour les lignes existantes,
  plutôt qu'à la date de migration : un message ancien n'a pas été « modifié »
  au moment du déploiement, et le prétendre fausserait toute observabilité ;
- la contrainte d'unicité est PARTIELLE et restreinte à kind='document_summary'.
  Ces lignes n'existent qu'à partir de cette migration : la contrainte ne peut
  donc pas échouer sur des doublons préexistants. Aucun préflight n'est requis.

Rollback : ``migrate chat 0006`` supprime les colonnes. Les résumés déjà écrits
perdent alors leur rattachement au fil (le contenu textuel du message, lui,
disparaît avec la ligne — cf. docs/03-backend/chat-generation.md).
"""
from django.db import migrations, models


def _init_updated_at(apps, schema_editor):
    """Aligne updated_at sur created_at pour les messages préexistants."""
    ChatMessage = apps.get_model("chat", "ChatMessage")
    ChatMessage.objects.update(updated_at=models.F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0006_chatmessage_analysis_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="kind",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="status",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="document_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="summary_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.RunPython(_init_updated_at, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(
                fields=["summary_id", "status"],
                name="chat_messag_summary_0f9fc2_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="chatmessage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "document_summary"), models.Q(("request_id", ""), _negated=True)),
                fields=("thread", "request_id", "role"),
                name="uniq_chat_message_summary_request",
            ),
        ),
    ]

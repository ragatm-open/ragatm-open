# backend/apps/audit/migrations/0007_ragquerylog_feedback_fields.py
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0006_alter_ragquerylog_policy_and_more"),  # ajuste si ton dernier numéro diffère
    ]

    operations = [
        migrations.AddField(
            model_name="ragquerylog",
            name="feedback_score",
            field=models.SmallIntegerField(blank=True, null=True, help_text="1 = positive, -1 = negative"),
        ),
        migrations.AddField(
            model_name="ragquerylog",
            name="feedback_reason",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                help_text="missing_sources | incorrect | incomplete | irrelevant | other",
            ),
        ),
        migrations.AddField(
            model_name="ragquerylog",
            name="feedback_comment",
            field=models.TextField(blank=True, default=""),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0004_ragquerylog_llm_completion_tokens_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ragquerylog",
            name="request_id",
            field=models.CharField(
                max_length=64,
                blank=True,
                default="",
                db_index=True,
            ),
        ),
        migrations.AddField(
            model_name="ragquerylog",
            name="policy",
            field=models.CharField(
                max_length=64,
                blank=True,
                default="",
                db_index=True,
            ),
        ),
    ]

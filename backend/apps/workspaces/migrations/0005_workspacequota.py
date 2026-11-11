"""Quotas documentaires par workspace : table WorkspaceQuota + backfill.

Backfill : chaque workspace existant reçoit un quota aux DÉFAUTS DE PLATEFORME
(settings/env/code) de ce lot. Aucun workspace actif ne reste sans quota
exploitable ; pas de NULL ambigu (pas de mode « illimité »).

Réversible : le reverse de CreateModel supprime la table (donc les lignes) ; le
reverse du backfill est un no-op.
"""
from __future__ import annotations

import django.db.models.deletion
from django.conf import settings as dj_settings
from django.db import migrations, models


def _default_count() -> int:
    try:
        return int(getattr(dj_settings, "WORKSPACE_DEFAULT_MAX_DOCUMENT_COUNT", 1000)) or 1000
    except Exception:
        return 1000


def _default_bytes() -> int:
    try:
        v = int(getattr(dj_settings, "WORKSPACE_DEFAULT_MAX_DOCUMENT_BYTES", 10 * 1024 ** 3))
        return v or (10 * 1024 ** 3)
    except Exception:
        return 10 * 1024 ** 3


def backfill_quotas(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    WorkspaceQuota = apps.get_model("workspaces", "WorkspaceQuota")
    count = _default_count()
    byts = _default_bytes()
    missing = list(
        Workspace.objects.filter(quota__isnull=True).values_list("id", flat=True)
    )
    WorkspaceQuota.objects.bulk_create(
        [
            WorkspaceQuota(
                workspace_id=ws_id, max_document_count=count, max_document_bytes=byts
            )
            for ws_id in missing
        ],
        batch_size=500,
    )


def unbackfill_quotas(apps, schema_editor):
    # Le reverse de CreateModel supprime la table ; rien à faire ici.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0004_workspace_is_active"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkspaceQuota",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("max_document_count", models.PositiveIntegerField()),
                ("max_document_bytes", models.BigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "workspace",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="quota",
                        to="workspaces.workspace",
                    ),
                ),
            ],
            options={
                "db_table": "workspace_quota",
            },
        ),
        migrations.AddConstraint(
            model_name="workspacequota",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_document_count__gt", 0)),
                name="workspace_quota_count_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="workspacequota",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_document_bytes__gt", 0)),
                name="workspace_quota_bytes_positive",
            ),
        ),
        migrations.RunPython(backfill_quotas, unbackfill_quotas),
    ]

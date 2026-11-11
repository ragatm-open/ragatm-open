from django.db import migrations

def backfill_ragspace_workspace(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    RagSpace = apps.get_model("rag_core", "RagSpace")

    # workspace "Default" (id stable via get_or_create)
    ws, _ = Workspace.objects.get_or_create(name="Default")

    RagSpace.objects.filter(workspace__isnull=True).update(workspace=ws)

class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "__first__"),
        ("rag_core", "0003_ragspace_workspace"),  # adapte au nom réel
    ]

    operations = [
        migrations.RunPython(backfill_ragspace_workspace, migrations.RunPython.noop),
    ]

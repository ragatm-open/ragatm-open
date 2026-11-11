from django.db import migrations

# Anciens statuts lowercase (bases déployées avant le passage uppercase de 0017)
# -> statuts canoniques uppercase actuels.
STATUS_MAP = {
    "pending": "PENDING",
    "ingesting": "INGESTING",
    "ready": "READY",
    "failed": "FAILED",
}


def uppercase_status(apps, schema_editor):
    """Normalise les statuts de Document en uppercase.

    Idempotente : les lignes déjà uppercase ne sont pas touchées (le filtre
    `__iexact` + `exclude(status=upper)` ne sélectionne que ce qui diffère).
    L'UPDATE ne peut pas violer la contrainte unique (rag_space, sha256) car il
    ne change que la valeur de statut, pas le couple dédupliqué.
    """
    Document = apps.get_model("rag_core", "Document")
    for lower, upper in STATUS_MAP.items():
        Document.objects.filter(status__iexact=lower).exclude(status=upper).update(status=upper)


class Migration(migrations.Migration):

    dependencies = [
        ("rag_core", "0021_documentsummary_error_message_documentsummary_status_and_more"),
    ]

    operations = [
        # Reverse = noop : on ne réintroduit jamais de lowercase.
        migrations.RunPython(uppercase_status, migrations.RunPython.noop),
    ]

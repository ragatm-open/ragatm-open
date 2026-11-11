"""Backfill CV-3B : une ``DocumentVersion`` par ``Document`` existant aux données autoritaires.

Source UNIQUE : colonnes déjà persistées de ``rag_document`` —
``sha256`` → ``content_sha256``, ``size_bytes``, ``storage_file`` (nom logique dans le
stockage Django, jamais un chemin machine) → ``storage_key``, ``created_at``.
Aucun fichier média n'est lu, aucun hash recalculé, aucun fichier déplacé.

Politique explicite (« version différée », décidée pour CV-3B) : un document sans
``sha256`` valide ou sans fichier stocké — quel que soit son statut, FAILED compris —
n'obtient PAS de version ici. Rien n'est fabriqué ; les exclus sont comptés dans le
journal ``rag_core.migrations``. Leur version naîtra à la prochaine (ré)indexation, qui
réécrit ``sha256`` depuis les octets stockés (``ensure_document_version``).

Aucune ``Page`` n'est créée : ni le nombre de pages ni la géométrie PDF ne sont
persistés en base, et les déduire des chunks serait faux (page sans chunk, page
visuelle). Les pages naissent à la prochaine extraction (PAGE_BACKFILL_DEFERRED).

Inverse : supprime les versions (et, par cascade, leurs pages) AVANT le retrait des
tables par 0042 ; ne touche jamais ``Document``, les fichiers ni les chunks.
"""
import logging
import re

from django.db import migrations

logger = logging.getLogger("rag_core.migrations")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BATCH = 1000


def backfill_document_versions(apps, schema_editor):
    Document = apps.get_model("rag_core", "Document")
    DocumentVersion = apps.get_model("rag_core", "DocumentVersion")

    already = set(DocumentVersion.objects.values_list("document_id", flat=True))
    inspected = created = 0
    skipped = {"sha256_missing": 0, "storage_missing": 0, "already_versioned": 0}
    batch = []
    rows = (Document.objects.order_by("created_at", "id")
            .values_list("id", "sha256", "size_bytes", "storage_file", "created_at")
            .iterator(chunk_size=_BATCH))
    for doc_id, sha, size_bytes, storage_name, created_at in rows:
        inspected += 1
        if doc_id in already:
            skipped["already_versioned"] += 1
            continue
        if not _SHA256_RE.match(sha or ""):
            skipped["sha256_missing"] += 1
            continue
        if not storage_name:
            skipped["storage_missing"] += 1
            continue
        batch.append(DocumentVersion(document_id=doc_id, content_sha256=sha,
                                     size_bytes=size_bytes, storage_key=storage_name,
                                     created_at=created_at))
        if len(batch) >= _BATCH:
            DocumentVersion.objects.bulk_create(batch)
            created += len(batch)
            batch = []
    if batch:
        DocumentVersion.objects.bulk_create(batch)
        created += len(batch)
    logger.info("rag_core 0043 document versions: inspected=%d created=%d skipped=%s media_files_read=0",
                inspected, created, skipped)


def remove_document_versions(apps, schema_editor):
    DocumentVersion = apps.get_model("rag_core", "DocumentVersion")
    DocumentVersion.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("rag_core", "0042_document_version_and_page"),
    ]

    operations = [
        migrations.RunPython(backfill_document_versions, remove_document_versions),
    ]

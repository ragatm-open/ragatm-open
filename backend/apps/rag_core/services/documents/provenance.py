"""Provenance documentaire (CV-3B) : version immuable des octets et pages physiques.

Deux helpers IDEMPOTENTS, appelés par l'ingestion commune (`extract_and_index_bytes`) :

- ``ensure_document_version(document_id)`` : la version des octets ACTUELS du document,
  dérivée UNIQUEMENT de données déjà persistées (``sha256``, ``size_bytes``,
  ``storage_file.name``) — exactement la règle du backfill (migration 0043). Aucune
  lecture de fichier, aucun recalcul de hash.
- ``ensure_pdf_pages(version, geometry)`` : matérialise les ``Page`` d'une version à
  partir de la géométrie lue par l'extraction PDF. Même géométrie → no-op ; géométrie
  différente pour les MÊMES octets → ``ProvenanceConflict`` (jamais de mise à jour).

Politique « version différée » (CV-3B) : un document sans empreinte (``sha256`` vide) ou
sans fichier stocké n'a pas de version — rien n'est fabriqué. Sa version naîtra à la
prochaine (ré)indexation, qui réécrit déjà ``sha256`` depuis les octets stockés ; un
document sans fichier n'a jamais de version (aucun octet à versionner).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional

from django.db import IntegrityError, transaction

from apps.rag_core.models import Document, DocumentVersion, Page, RagSpace
from apps.rag_core.services.ragspace_mutation import lock_ragspace_for_mutation

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProvenanceConflict(Exception):
    """Une ligne immuable existe déjà avec des valeurs différentes pour la même identité."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _authoritative_fields(sha256: str, size_bytes, storage_name) -> Optional[dict]:
    """Champs de version si TOUS sont autoritaires en base, sinon ``None``."""
    sha = sha256 or ""
    key = storage_name or ""
    if not _SHA256_RE.match(sha) or not key or size_bytes is None or size_bytes < 0:
        return None
    return {"content_sha256": sha, "size_bytes": int(size_bytes), "storage_key": str(key)}


def _lock_document(document_id) -> Optional[dict]:
    """Verrous dans l'ordre CANONIQUE du domaine : RagSpace, puis Document.

    La suppression d'un espace tient le verrou RagSpace de bout en bout : une écriture
    de provenance concurrente attend donc qu'elle se termine (et ne trouve plus le
    document) ou la précède (et sa ligne est emportée par la cascade). Sans ce verrou,
    une version insérée entre l'inventaire de la cascade et la suppression du document
    ferait échouer la suppression sur la clé étrangère.
    """
    space_id = Document.objects.filter(id=document_id).values_list("rag_space_id", flat=True).first()
    if space_id is None:
        return None
    try:
        lock_ragspace_for_mutation(space_id)
    except RagSpace.DoesNotExist:
        return None
    return (Document.objects.select_for_update()
            .filter(id=document_id)
            .values("sha256", "size_bytes", "storage_file")
            .first())


def ensure_document_version(document_id) -> Optional[DocumentVersion]:
    """Version des octets actuels du document (créée si absente), ou ``None``.

    ``None`` : document disparu, ou données autoritaires absentes (politique différée).
    Lève ``ProvenanceConflict`` si une version de même empreinte existe avec une autre
    clé de stockage ou une autre taille — un invariant brisé, jamais réécrit.
    """
    with transaction.atomic():
        row = _lock_document(document_id)
        if row is None:
            return None
        fields = _authoritative_fields(row["sha256"], row["size_bytes"], row["storage_file"])
        if fields is None:
            return None
        existing = DocumentVersion.objects.filter(
            document_id=document_id, content_sha256=fields["content_sha256"]).first()
        if existing is None:
            try:
                with transaction.atomic():
                    return DocumentVersion.objects.create(document_id=document_id, **fields)
            except IntegrityError:
                existing = DocumentVersion.objects.filter(
                    document_id=document_id, content_sha256=fields["content_sha256"]).first()
                if existing is None:
                    raise
        if (existing.storage_key, existing.size_bytes) != (fields["storage_key"], fields["size_bytes"]):
            raise ProvenanceConflict("document_version_conflict",
                                     "same content_sha256 with a different storage_key/size_bytes")
        return existing


def _page_identity(page_number, mediabox, cropbox, rotate, page_label) -> tuple:
    return (int(page_number), tuple(mediabox), tuple(cropbox), int(rotate), page_label)


def ensure_pdf_pages(version: DocumentVersion, geometry: Iterable) -> List[Page]:
    """Pages de ``version`` conformes à ``geometry`` (``[PdfPageGeometry]``), idempotent."""
    wanted = [
        Page(document_version=version, page_number=g.page_number, page_label=g.page_label,
             mediabox=list(g.mediabox), cropbox=list(g.cropbox), rotate=g.rotate)
        for g in geometry
    ]
    if [p.page_number for p in wanted] != list(range(1, len(wanted) + 1)):
        raise ProvenanceConflict("page_numbering_invalid", "pages must be numbered 1..N without gaps")
    for p in wanted:  # bulk_create ne passe pas par save() : validation explicite
        p.full_clean(validate_unique=False, validate_constraints=False)

    def _identities(pages) -> list:
        return [_page_identity(p.page_number, p.mediabox, p.cropbox, p.rotate, p.page_label)
                for p in pages]

    with transaction.atomic():
        if _lock_document(version.document_id) is None or not DocumentVersion.objects.filter(
                id=version.id).exists():
            raise ProvenanceConflict("document_version_missing", "version or document deleted")
        existing = list(Page.objects.filter(document_version=version).order_by("page_number"))
        if not existing:
            return Page.objects.bulk_create(wanted)
        if _identities(existing) != _identities(wanted):
            raise ProvenanceConflict("page_geometry_conflict",
                                     "stored pages differ from the geometry of the same bytes")
        return existing

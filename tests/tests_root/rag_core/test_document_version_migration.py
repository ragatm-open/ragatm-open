"""Backfill ``rag_core/0043`` (CV-3B) exercé RÉELLEMENT : 0041 → tête → 0042 → tête.

Vérifie : une version par document aux données autoritaires, exclusions explicites
(politique « version différée »), lignes ``Document`` inchangées, aucune ``Page``
fabriquée, inverse propre. Aucun fichier média n'existe dans ce test : la migration
n'en a pas besoin (elle n'en lit aucun).

Isolé : restaure la TÊTE de l'app avant de rendre la main, même en cas d'échec.
"""
import hashlib

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True)]

APP = "rag_core"
BEFORE = "0041_document_ingest_started_at"
SCHEMA = "0042_document_version_and_page"
BACKFILL = "0043_backfill_document_versions"


def _head() -> str:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    targets = [name for app, name in executor.loader.graph.leaf_nodes() if app == APP]
    assert targets
    return targets[0]


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([(APP, target)])
    executor.loader.build_graph()
    return executor.loader.project_state([(APP, target)]).apps


@pytest.fixture
def restore_head():
    yield
    _migrate(_head())


def _snapshot(Document):
    return sorted(Document.objects.values_list("id", "sha256", "size_bytes", "storage_file", "status",
                                               "filename", "meta"))


def test_backfill_creates_exactly_one_version_per_authoritative_document(restore_head):
    _migrate(BEFORE)
    # Schéma de `workspace`, `rag_space` et `rag_document` identique en 0041 et en tête :
    # les modèles réels écrivent des lignes « d'avant » fidèles (un état historique
    # partiel ignorerait les colonnes récentes des autres apps).
    from apps.rag_core.models import Document, RagSpace
    from apps.workspaces.models import Workspace

    space = RagSpace.objects.create(workspace=Workspace.objects.create(name="W"), name="S")

    def doc(name, body, status="READY", sha=None, storage="default"):
        return Document.objects.create(
            rag_space=space, filename=name, status=status, size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest() if sha is None else sha,
            storage_file=f"rag_docs/{space.id}/{name}" if storage == "default" else storage)

    ready = doc("a.pdf", b"A")
    failed = doc("b.pdf", b"B", status="FAILED")                 # FAILED avec octets : versionné
    pending = doc("c.pdf", b"C", status="PENDING")
    no_sha = doc("d.pdf", b"D", sha="")                          # différé : aucune empreinte
    no_file = doc("e.pdf", b"E", status="FAILED", storage=None)  # aucun octet à versionner
    before = _snapshot(Document)

    new = _migrate(_head())
    Document = new.get_model("rag_core", "Document")
    DocumentVersion = new.get_model("rag_core", "DocumentVersion")
    Page = new.get_model("rag_core", "Page")

    versions = {v.document_id: v for v in DocumentVersion.objects.all()}
    assert set(versions) == {ready.id, failed.id, pending.id}
    for d in (ready, failed, pending):
        v = versions[d.id]
        assert (v.content_sha256, v.size_bytes, v.storage_key) == (d.sha256, d.size_bytes, d.storage_file.name)
        assert v.created_at == Document.objects.get(id=d.id).created_at
    assert no_sha.id not in versions and no_file.id not in versions
    assert Page.objects.count() == 0                              # PAGE_BACKFILL_DEFERRED
    assert _snapshot(Document) == before                          # documents intacts

    # Inverse : les versions disparaissent, les documents restent.
    back = _migrate(SCHEMA)
    assert back.get_model("rag_core", "DocumentVersion").objects.count() == 0
    assert _snapshot(back.get_model("rag_core", "Document")) == before

    # Rejeu par la machinerie standard : même résultat, pas de doublon.
    again = _migrate(_head())
    assert again.get_model("rag_core", "DocumentVersion").objects.count() == 3

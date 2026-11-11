"""Migration ``rag_core/0045`` (CV-3E1) exercée RÉELLEMENT : 0044 → 0045 → 0044 → 0045.

La migration est purement additive (une table technique, ses contraintes) : aucun
backfill, aucune lecture média, aucun crop fabriqué, aucune tâche. Les lignes 0044
(versions, pages, rasters, crops) traversent l'aller-retour intactes.

Isolé : restaure la TÊTE de l'app avant de rendre la main, même en cas d'échec.
"""
import hashlib

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True)]

APP = "rag_core"
BEFORE = "0044_page_raster_and_crop"
TARGET = "0045_visual_crop_job"
TABLE = "rag_visual_crop_job"


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


@pytest.fixture
def restore_head():
    yield
    _migrate(_head())


def _tables():
    with connection.cursor() as cur:
        return set(connection.introspection.table_names(cur))


def _snapshot():
    with connection.cursor() as cur:
        out = {}
        for table, cols in (("rag_document_version", "id, content_sha256"),
                            ("rag_document_page", "id, page_number"),
                            ("rag_page_raster", "id, pixels_sha256"),
                            ("rag_crop", "id, crop_pixels_sha256, crop_sha256, contract_version")):
            cur.execute(f"SELECT {cols} FROM {table} ORDER BY id")
            out[table] = cur.fetchall()
        return out


def _constraints():
    with connection.cursor() as cur:
        return set(connection.introspection.get_constraints(cur, TABLE))


def test_0045_is_additive_and_reversible_without_touching_0044_rows(restore_head):
    _migrate(TARGET)
    from apps.rag_core.models import (Crop, Document, DocumentVersion, Page, PageRaster, RagSpace,
                                      VisualCropJob)
    from apps.workspaces.models import Workspace

    # Lignes 0044 réelles (aucun fichier : la migration n'en lit aucun).
    space = RagSpace.objects.create(workspace=Workspace.objects.create(name="W"), name="S")
    doc = Document.objects.create(rag_space=space, filename="a.pdf", status="READY", size_bytes=1,
                                  sha256=hashlib.sha256(b"A").hexdigest(), storage_file="rag_docs/x/a.pdf")
    version = DocumentVersion.objects.create(document=doc, content_sha256=doc.sha256, size_bytes=1,
                                             storage_key="rag_docs/x/a.pdf")
    page = Page.objects.create(document_version=version, page_number=1, mediabox=[0, 0, 595, 842],
                               cropbox=[0, 0, 595, 842], rotate=0)
    raster = PageRaster.objects.create(page=page, rasterizer_id="ragatm-pdfium/1", rasterizer_params={"dpi": 150},
                                       params_sha256="a" * 64, width=100, height=100,
                                       pixels_sha256="b" * 64, implementation={"t": 1})
    crop = Crop.objects.create(page_raster=raster, contract_version="ragatm-crop/2", x_min=0, y_min=0,
                               x_max=10, y_max=10, width=10, height=10, origin={"reason": "t"},
                               origin_reason="t", crop_pixels_sha256="c" * 64, crop_sha256="d" * 64,
                               encoding_id="png-rgb8-zl6/1", storage_key="rag_docs/x/c.png")
    VisualCropJob.objects.create(crop=crop, task="describe", context_sha256="e" * 64,
                                 expected_pipeline_fingerprint="fp", analysis_generation=1)
    before = _snapshot()
    assert TABLE in _tables()
    names = _constraints()
    assert {"uniq_rag_visual_crop_job_key", "rag_vcj_state_valid", "rag_vcj_generation_gte_1",
            "rag_vcj_model_attempts_bounded", "rag_vcj_capacity_deferrals_bounded",
            "rag_vcj_activations_bounded", "rag_vcj_lease_iff_running",
            "rag_vcj_result_iff_succeeded", "rag_vcj_error_code_on_failure"} <= names

    _migrate(BEFORE)                                     # 0045 → 0044
    assert TABLE not in _tables()
    assert _snapshot() == before                         # aucune perte des données 0044

    _migrate(TARGET)                                     # 0044 → 0045
    assert TABLE in _tables() and _constraints() == names
    assert _snapshot() == before
    with connection.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TABLE}")
        assert cur.fetchone()[0] == 0                    # aucun backfill


def test_0045_operations_are_schema_only():
    from importlib import import_module

    from django.db import migrations

    mod = import_module(f"apps.rag_core.migrations.{TARGET}")
    ops = mod.Migration.operations
    assert [type(op) for op in ops] == [migrations.CreateModel]
    assert ops[0].name == "VisualCropJob" and ops[0].options["db_table"] == TABLE
    assert mod.Migration.dependencies == [(APP, BEFORE)]

"""Migration ``rag_core/0046`` (CV-3E4A) exercée RÉELLEMENT : 0045 → 0046 → 0045 → 0046.

Strictement additive : deux tables (politique de périmètre, préparations durables) et
un index PARTIEL sur les jobs ouverts. Aucune ligne de politique ni de préparation
fabriquée (absence de politique = FERMÉ), jobs existants intacts (clé, état, résultat).

Isolé : restaure la TÊTE de l'app avant de rendre la main, même en cas d'échec.
"""
import hashlib

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True)]

APP = "rag_core"
BEFORE = "0045_visual_crop_job"
TARGET = "0046_visual_scope_policy_and_prepare_run"


def _head() -> str:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    return [name for app, name in executor.loader.graph.leaf_nodes() if app == APP][0]


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


def _jobs():
    with connection.cursor() as cur:
        cur.execute("SELECT id, crop_id, task, schema_version, context_sha256, expected_pipeline_fingerprint, "
                    "analysis_generation, state, model_attempts, capacity_deferrals, activations, "
                    "result_raw::text FROM rag_visual_crop_job ORDER BY id")
        return cur.fetchall()


def _index_def(name):
    with connection.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", [name])
        row = cur.fetchone()
        return row[0] if row else None


def test_0046_is_additive_reversible_and_preserves_existing_jobs(restore_head):
    _migrate(TARGET)
    from apps.rag_core.models import (Crop, Document, DocumentVersion, Page, PageRaster, RagSpace,
                                      VisualCropJob)
    from apps.workspaces.models import Workspace

    space = RagSpace.objects.create(workspace=Workspace.objects.create(name="W"), name="S")
    doc = Document.objects.create(rag_space=space, filename="a.pdf", status="READY", size_bytes=1,
                                  sha256=hashlib.sha256(b"A").hexdigest(), storage_file="rag_docs/x/a.pdf")
    version = DocumentVersion.objects.create(document=doc, content_sha256=doc.sha256, size_bytes=1,
                                             storage_key="rag_docs/x/a.pdf")
    page = Page.objects.create(document_version=version, page_number=1, mediabox=[0, 0, 595, 842],
                               cropbox=[0, 0, 595, 842], rotate=0)
    raster = PageRaster.objects.create(page=page, rasterizer_id="ragatm-pdfium/1", rasterizer_params={"dpi": 200},
                                       params_sha256="a" * 64, width=100, height=100, pixels_sha256="b" * 64,
                                       implementation={"t": 1})
    for i in range(3):                                   # 13 jobs historiques : ici 3 représentatifs
        crop = Crop.objects.create(page_raster=raster, contract_version="ragatm-crop/2", x_min=i, y_min=0,
                                   x_max=i + 10, y_max=10, width=10, height=10, origin={"reason": "t"},
                                   origin_reason="t", crop_pixels_sha256="c" * 64, crop_sha256="d" * 64,
                                   encoding_id="png-rgb8-zl6/1", storage_key=f"rag_docs/x/c{i}.png")
        VisualCropJob.objects.create(crop=crop, task="describe", context_sha256="e" * 64,
                                     expected_pipeline_fingerprint="fp", analysis_generation=1, state="succeeded",
                                     model_attempts=1, activations=1, result_raw={"schema_version": "2", "i": i})
    before = _jobs()
    assert {"rag_visual_scope_policy", "rag_visual_prepare_run"} <= _tables()
    open_idx = _index_def("rag_vcj_open_idx")
    assert open_idx and "WHERE" in open_idx and "pending" in open_idx and "succeeded" not in open_idx
    assert _index_def("rag_vpr_state_updated_idx")

    _migrate(BEFORE)                                     # 0046 → 0045
    assert not ({"rag_visual_scope_policy", "rag_visual_prepare_run"} & _tables())
    assert _index_def("rag_vcj_open_idx") is None
    assert _jobs() == before

    _migrate(TARGET)                                     # 0045 → 0046
    assert _jobs() == before                             # clé, état, compteurs, résultat intacts
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM rag_visual_scope_policy")
        assert cur.fetchone()[0] == 0                    # aucune politique créée : tout est FERMÉ
        cur.execute("SELECT count(*) FROM rag_visual_prepare_run")
        assert cur.fetchone()[0] == 0


def test_0046_operations_are_schema_only():
    from importlib import import_module

    from django.db import migrations

    mod = import_module(f"apps.rag_core.migrations.{TARGET}")
    kinds = {type(op) for op in mod.Migration.operations}
    assert kinds <= {migrations.CreateModel, migrations.AddField, migrations.AddIndex,
                     migrations.AddConstraint}
    assert (APP, BEFORE) in mod.Migration.dependencies

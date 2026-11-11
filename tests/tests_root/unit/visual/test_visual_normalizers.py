"""normaliseurs déterministes (goldens neutres)."""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re

import pytest

from apps.rag_core.services.ingest.extraction import pdf_tables as P
from apps.rag_core.services.ingest.visual import contracts as C
from apps.rag_core.services.ingest.visual import normalizers as N

from . import _vision_mocks as M

HASH = hashlib.sha256(M.IMAGE_BYTES).hexdigest()


def _asset(**kw):
    params = dict(content=M.IMAGE_BYTES, mime_type="image/png", width=640, height=480,
                  source_kind="pdf", content_hash=HASH, ordinal=2, source_page=3,
                  source_bbox=(100, 200, 500, 400))
    params.update(kw)
    return C.VisualAsset(**params)


def _artifact(kind, mutate=None, **asset_kw):
    body = M.success(kind)
    if mutate:
        mutate(body)
    return C.build_artifact(_asset(**asset_kw), body)


def _text(kind, **kw):
    out = N.normalize_artifact(_artifact(kind), **kw)
    assert len(out.units) == 1
    return out.units[0].text


# --------------------------------------------------------------------------- #
# Photo
# --------------------------------------------------------------------------- #
class TestPhoto:
    def test_golden(self):
        assert _text("photo") == "\n".join([
            "Visual type: photograph",
            "Summary: photo summary",
            f"Description: A blue circle beside a red square {M.PAYLOAD_SENTINEL}",
            "Setting: indoor table",
            "Objects:",
            "- circle (count: 1; attributes: blue)",
            "- square (attributes: red, matte) (approximate)",
            "Observable relations:",
            "- circle left of square",
            "Visible text:",
            "- LOT 42",
            "- ACME (ambiguous)",
        ])

    def test_deterministic(self):
        assert _text("photo") == _text("photo")

    @pytest.mark.parametrize("field", ["caption", "alt_text"])
    def test_summary_equal_to_context_is_omitted(self, field):
        ctx = C.VisualContext(**{field: "  PHOTO   Summary "})
        assert "Summary:" not in _text("photo", context=ctx)

    def test_unrelated_context_does_not_drop_summary(self):
        assert "Summary: photo summary" in _text("photo", context=C.VisualContext(caption="other"))

    def test_context_is_never_serialized_as_evidence(self):
        ctx = C.VisualContext(caption=M.CONTEXT_SENTINEL, nearby_text=M.CONTEXT_SENTINEL)
        out = N.normalize_artifact(_artifact("photo"), context=ctx)
        assert M.CONTEXT_SENTINEL not in out.units[0].text
        assert M.CONTEXT_SENTINEL not in json.dumps(out.units[0].meta)

    def test_known_visible_text_deduplicated_exactly(self):
        out = N.normalize_artifact(_artifact("photo"), known_texts=["lot  42", "ACM"])
        text = out.units[0].text
        assert "- LOT 42" not in text
        assert "- ACME (ambiguous)" in text
        assert out.units[0].meta["visual"]["visible_text_deduplicated"] == 1

    def test_meta(self):
        meta = N.normalize_artifact(_artifact("photo")).units[0].meta["visual"]
        assert meta["kind"] == "photo"
        assert meta["asset_hash"] == HASH
        assert meta["region_index"] is None
        assert meta["schema_version"] == "1"
        assert meta["model"] == {"logical": "vision-large", "concrete": "concrete-model",
                                 "digest": "sha256:abc"}
        assert meta["pipeline"]["pipeline_id"] == "vision-pipeline-1"
        assert meta["warnings"] == [] and meta["requires_visual_check"] is False


# --------------------------------------------------------------------------- #
# Diagramme
# --------------------------------------------------------------------------- #
class TestDiagram:
    def test_golden(self):
        assert _text("diagram") == "\n".join([
            "Visual type: diagram",
            "Diagram type: process",
            "Nodes:",
            "- Intake [box]",
            "- Review [box]",
            "- Archive [cylinder] (approximate)",
            "- Notify",
            "Edges:",
            "- Intake -> Review; relation: feeds; label: form",
            "- Review <-> Archive",
            "- Review -- Notify (undirected)",
            "- between Archive and Notify (direction unknown) (ambiguous)",
            "Groups:",
            "- Back office: members Review, Archive",
            "Annotations:",
            "- draft flow",
        ])

    def test_unknown_direction_has_no_arrow(self):
        line = [ln for ln in _text("diagram").splitlines() if "direction unknown" in ln][0]
        for arrow in ("->", "<-", "<->"):
            assert arrow not in line

    def test_duplicate_labels_disambiguated_by_id(self):
        def mutate(body):
            body["payload"]["nodes"][1]["label"] = "Intake"
        text = N.normalize_artifact(_artifact("diagram", mutate)).units[0].text
        assert "- Intake [n1] -> Intake [n2]" in text

    def test_large_diagram_split_into_neighborhoods(self):
        def mutate(body):
            n = 400
            body["payload"]["nodes"] = [{"id": f"n{i}", "label": f"Node {i}", "type": None,
                                         "certainty": "exact"} for i in range(n)]
            body["payload"]["edges"] = [{"source_id": f"n{i}", "target_id": f"n{i + 1}",
                                         "direction": "unknown" if i % 2 else "forward",
                                         "relation": None, "label": None, "certainty": "exact"}
                                        for i in range(n - 1)]
            body["payload"]["groups"] = [{"id": "g", "label": "All", "members": ["n0", "n1"]}]
        units = N.normalize_artifact(_artifact("diagram", mutate)).units
        assert len(units) > 1
        joined = "\n".join(u.text for u in units)
        assert "- Node 0 -> Node 1" in joined
        assert "- between Node 1 and Node 2 (direction unknown)" in joined
        assert "- All: members Node 0, Node 1" in joined
        assert all(len(u.text) <= N.VISUAL_UNIT_MAX_CHARS for u in units)
        assert {u.meta["visual"]["unit"] for u in units} >= {"diagram_neighborhood", "diagram_annotations"}


# --------------------------------------------------------------------------- #
# Graphique
# --------------------------------------------------------------------------- #
class TestChart:
    def test_golden(self):
        assert _text("chart") == "\n".join([
            "Visual type: chart",
            "Chart type: bar",
            "Axes:",
            "- Quarter (horizontal); scale: categorical",
            "- Amount (vertical) [EUR]; scale: linear; range ≈ 0 to 20 (approximate)",
            "Series: 2024",
            "  - Q1 = 10",
            "  - Q2 ≈ 14.5 (approximate, read from visual)",
            "  - Q3: unreadable",
            "Generated trend summary: Amounts rise from Q1 to Q2.",
        ])

    def test_approximate_never_equal(self):
        line = [ln for ln in _text("chart").splitlines() if "Q2" in ln][0]
        assert "=" not in line and "≈" in line and "approximate" in line

    def test_unreadable_has_no_number(self):
        line = [ln for ln in _text("chart").splitlines() if "Q3" in ln][0]
        assert not any(ch.isdigit() for ch in line.replace("Q3", ""))

    def test_generated_summary_flag(self):
        unit = N.normalize_artifact(_artifact("chart")).units[0]
        assert unit.meta["visual"]["generated_summary"] is True

        def mutate(body):
            body["payload"]["trend_summary"] = None
        unit = N.normalize_artifact(_artifact("chart", mutate)).units[0]
        assert unit.meta["visual"]["generated_summary"] is False
        assert "Generated trend summary" not in unit.text

    def test_summary_never_replaces_points(self):
        assert "  - Q1 = 10" in _text("chart")

    def test_large_chart_split_by_series(self):
        def mutate(body):
            points = [{"label": f"p{j}", "x": j, "value": j, "value_text": None,
                       "precision": "approximate_visual"} for j in range(120)]
            body["payload"]["series"] = [{"id": f"s{i}", "label": f"S{i}", "axis_ids": None,
                                          "points": copy.deepcopy(points)} for i in range(4)]
        units = N.normalize_artifact(_artifact("chart", mutate)).units
        assert [u.meta["visual"]["unit"] for u in units] == ["chart_overview"] + ["chart_series"] * 4
        assert units[0].meta["visual"]["generated_summary"] is True
        assert all("Generated trend summary" not in u.text for u in units[1:])


# --------------------------------------------------------------------------- #
# Dessin technique
# --------------------------------------------------------------------------- #
class TestTechnical:
    def test_golden(self):
        assert _text("technical_drawing") == "\n".join([
            "Visual type: technical drawing",
            "Drawing type: schematic",
            "Elements:",
            "- P1 [pump]",
            "- T1 [tank]",
            "- e3 [valve] (ambiguous)",
            "Explicit dimensions:",
            "- DN 50 (unit: mm; normalized value: 50; between: P1, T1)",
            "- 1 2?0 (unit: mm) (ambiguous)",
            "Symbols:",
            "- arrow: flow direction",
            "Zones:",
            "- Zone A: members P1, T1",
            "Relations:",
            "- P1 feeds T1",
            "- e3 connected_to T1; label: bypass (ambiguous)",
            "Legend:",
            "- solid line = pipe",
            "Annotations:",
            "- check before commissioning",
        ])

    def test_ambiguous_dimension_value_not_asserted(self):
        line = [ln for ln in _text("technical_drawing").splitlines() if "2?0" in ln][0]
        assert "120" not in line and "normalized value" not in line

    def test_verbatim_value_text_kept_when_value_null(self):
        def mutate(body):
            body["payload"]["explicit_dimensions"][0]["value"] = None
        text = N.normalize_artifact(_artifact("technical_drawing", mutate)).units[0].text
        assert "- DN 50 (unit: mm; between: P1, T1)" in text


# --------------------------------------------------------------------------- #
# Mixte
# --------------------------------------------------------------------------- #
class TestMixed:
    def test_children_and_routing(self):
        out = N.normalize_artifact(_artifact("mixed"))
        assert [u.meta["visual"]["unit"] for u in out.units] == ["chart", "text_region"]
        assert len(out.tables) == 1
        assert out.omitted_regions == 1

    def test_child_provenance_and_mapped_bboxes(self):
        out = N.normalize_artifact(_artifact("mixed"))
        chart, text = (u.meta["visual"] for u in out.units)
        assert (chart["region_index"], chart["region_bbox_norm"], chart["region_bbox_source"]) == (
            0, [0.0, 0.0, 0.5, 1.0], [100, 200, 300, 400])
        assert (text["region_index"], text["region_bbox_source"]) == (2, [300, 200, 500, 240])
        assert chart["asset_hash"] == HASH and chart["source_page"] == 3

    def test_table_region_goes_through_table_adapter(self):
        table = N.normalize_artifact(_artifact("mixed")).tables[0]
        assert isinstance(table.table, P.LogicalTable)
        assert table.table.schema == ["2024 > H1", "2024 > H2", "Total"]
        assert table.requires_visual_check is True

    def test_no_parent_unit(self):
        out = N.normalize_artifact(_artifact("mixed"))
        assert all(u.meta["visual"]["kind"] != "mixed" for u in out.units)
        assert all("mixed summary" not in u.text for u in out.units)

    def test_text_region_dedup_against_known_text(self):
        out = N.normalize_artifact(_artifact("mixed"), known_texts=["quarterly OVERVIEW"])
        assert [u.meta["visual"]["unit"] for u in out.units] == ["chart"]
        assert out.omitted_regions == 2


# --------------------------------------------------------------------------- #
# Non-preuves et confidentialité
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["unknown", "decorative"])
def test_unknown_and_decorative_produce_nothing(kind):
    out = N.normalize_artifact(_artifact(kind))
    assert (out.units, out.tables) == ([], [])


def test_unusable_payload_produces_nothing():
    def mutate(body):
        body["payload"]["nodes"] = []
        body["payload"]["edges"] = []
        body["payload"]["groups"] = []
    assert N.normalize_artifact(_artifact("diagram", mutate)).units == []


@pytest.mark.parametrize("kind", ["photo", "diagram", "chart", "technical_drawing", "mixed", "table"])
def test_no_image_bytes_or_base64_in_metadata(kind):
    out = N.normalize_artifact(_artifact(kind, source_locator={"type": "page", "path": "f.pdf"}))
    metas = [u.meta for u in out.units]
    for t in out.tables:
        metas += [P.fragment_to_meta(t.table, r, f) for r in t.table.rows for f in r.fragments]
    blob = json.dumps(metas, ensure_ascii=False)
    assert M.IMAGE_B64 not in blob
    assert "IMAGE-SENTINEL" not in blob
    assert M.PAYLOAD_SENTINEL not in blob


# --------------------------------------------------------------------------- #
# Gardes statiques
# --------------------------------------------------------------------------- #
BACKEND = pathlib.Path(__file__).resolve().parents[4] / "backend"
VISUAL_SOURCES = sorted((BACKEND / "apps/rag_core/services/ingest/visual").glob("*.py")) + [
    BACKEND / "apps/llm_router/gateway/vision.py",
    BACKEND / "apps/llm_router/gateway/vision_contract.py",
]


def test_sources_exist():
    assert len(VISUAL_SOURCES) >= 6 and all(p.exists() for p in VISUAL_SOURCES)


@pytest.mark.parametrize("path", VISUAL_SOURCES, ids=lambda p: p.name)
def test_no_direct_model_server_path(path):
    src = path.read_text(encoding="utf-8")
    for forbidden in ("11434", "/api/generate", "/api/chat", "OLLAMA_BASE_URL", "import ollama",
                      "llm_router.router", "llm_router.embeddings"):
        assert forbidden not in src, forbidden

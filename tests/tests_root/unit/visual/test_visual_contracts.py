"""contrats en mémoire : visuel, requête V1, identifiants, bbox,
artefact, utilisabilité, dédoublonnage. Aucun réseau."""
from __future__ import annotations

import copy
import hashlib
import json
import re

import pytest

from apps.llm_router.gateway import vision
from apps.rag_core.services.ingest.visual import contracts as C

from . import _vision_mocks as M

LOCATOR = {"type": "page", "path": "LOCATOR-PATH-SENTINEL.pdf", "identifier": "LOCATOR-ID-SENTINEL"}
DOC_ID = "DOCUMENT-UUID-SENTINEL-0001"
HASH = hashlib.sha256(M.IMAGE_BYTES).hexdigest()


def _asset(**kw):
    params = dict(content=M.IMAGE_BYTES, mime_type="image/png", width=640, height=480,
                  source_kind="pdf", content_hash=HASH, ordinal=0, source_locator=LOCATOR,
                  source_page=3, source_bbox=(100, 200, 500, 400),
                  context=C.VisualContext(caption=M.CONTEXT_SENTINEL), origin="rendered",
                  render_dpi=144)
    params.update(kw)
    return C.VisualAsset(**params)


# --------------------------------------------------------------------------- #
# VisualAsset
# --------------------------------------------------------------------------- #
class TestVisualAsset:
    def test_valid_asset(self):
        asset = _asset()
        assert asset.task == "auto"
        assert asset.source_bbox == (100.0, 200.0, 500.0, 400.0)

    @pytest.mark.parametrize("override", [
        {"content": b""},
        {"mime_type": "image/gif"},
        {"width": 0},
        {"height": -1},
        {"width": True},
        {"source_kind": "xlsx"},
        {"content_hash": "ABC"},
        {"content_hash": HASH.upper()},
        {"ordinal": -1},
        {"source_page": 0},
        {"source_bbox": (10, 10, 5, 20)},
        {"source_bbox": (1, 2, 3)},
        {"origin": "scanned"},
        {"render_dpi": 0},
        {"task": "ocr"},
        {"occurrences": (C.VisualOccurrence(source_page=0),)},
    ])
    def test_invariants_rejected(self, override):
        with pytest.raises(C.VisualAssetError):
            _asset(**override)

    def test_repr_never_carries_bytes_context_or_locator(self):
        text = repr(_asset())
        assert "IMAGE-SENTINEL" not in text
        assert M.CONTEXT_SENTINEL not in text
        assert "LOCATOR-ID-SENTINEL" not in text


# --------------------------------------------------------------------------- #
# Requête V1
# --------------------------------------------------------------------------- #
class TestRequest:
    def test_request_golden(self):
        asset = _asset()
        payload = C.build_vision_request(asset, document_opaque_id=DOC_ID).payload
        assert payload == {
            "schema_version": "1",
            "task": "auto",
            "locale": "fr",
            "request_id": C.request_id_for(DOC_ID, HASH, "auto", asset.context),
            "image": {"data": M.IMAGE_B64, "mime_type": "image/png"},
            "context": {"caption": M.CONTEXT_SENTINEL, "alt_text": None, "nearby_text": None},
        }

    def test_no_provenance_in_serialized_request(self):
        serialized = json.dumps(C.build_vision_request(_asset(), document_opaque_id=DOC_ID).payload)
        for leak in ("LOCATOR-PATH-SENTINEL", "LOCATOR-ID-SENTINEL", DOC_ID, HASH, "source_",
                     "render_dpi", "origin", "ordinal", "occurrences", "rag_space", "workspace",
                     "filename"):
            assert leak not in serialized, leak

    def test_request_passes_client_shape_guard(self):
        from apps.llm_router.gateway import vision_contract

        vision_contract.validate_request_shape(
            C.build_vision_request(_asset(), document_opaque_id=DOC_ID).payload)

    def test_explicit_task_and_locale(self):
        payload = C.build_vision_request(_asset(task="chart"), document_opaque_id=DOC_ID,
                                         locale="en").payload
        assert (payload["task"], payload["locale"]) == ("chart", "en")

    def test_invalid_locale_rejected(self):
        with pytest.raises(C.VisualAssetError):
            C.build_vision_request(_asset(), document_opaque_id=DOC_ID, locale="de")

    def test_logical_model_sent_when_configured(self):
        payload = C.build_vision_request(_asset(), document_opaque_id=DOC_ID,
                                         model="vision-large").payload
        assert payload["model"] == "vision-large"

    @pytest.mark.parametrize("model", [None, "", "   "])
    def test_empty_model_is_omitted(self, model):
        assert "model" not in C.build_vision_request(_asset(), document_opaque_id=DOC_ID,
                                                     model=model).payload

    @pytest.mark.parametrize("model", ["mistral-small3.2:24b", "org/model", "a b"])
    def test_concrete_model_name_refused(self, model):
        with pytest.raises(C.VisualAssetError):
            C.build_vision_request(_asset(), document_opaque_id=DOC_ID, model=model)

    def test_model_from_settings(self, settings):
        settings.AI_GATEWAY_VISION_MODEL = ""
        assert C.vision_model_from_settings() is None
        settings.AI_GATEWAY_VISION_MODEL = "vision-large"
        assert C.vision_model_from_settings() == "vision-large"


class TestRequestId:
    def test_format_and_determinism(self):
        ctx = C.VisualContext(caption="a")
        rid = C.request_id_for(DOC_ID, HASH, "auto", ctx)
        assert re.fullmatch(r"rgv1-[0-9a-f]{32}", rid)
        assert rid == C.request_id_for(DOC_ID, HASH, "auto", C.VisualContext(caption="a"))
        assert len(rid) <= 128
        assert DOC_ID not in rid

    @pytest.mark.parametrize("change", ["doc", "hash", "task", "context"])
    def test_each_input_changes_id(self, change):
        base = dict(document_opaque_id=DOC_ID, content_hash=HASH, task="auto",
                    context=C.VisualContext(caption="a"))
        other = dict(base)
        other.update({"doc": {"document_opaque_id": "other"}, "hash": {"content_hash": "0" * 64},
                      "task": {"task": "table"},
                      "context": {"context": C.VisualContext(caption="b")}}[change])
        assert C.request_id_for(**base) != C.request_id_for(**other)

    def test_empty_document_id_refused(self):
        with pytest.raises(C.VisualAssetError):
            C.request_id_for("", HASH, "auto", C.VisualContext())


class TestContext:
    def test_fields_stay_separate(self):
        ctx = C.VisualContext(caption="cap", alt_text="alt", nearby_text="near")
        assert ctx.as_request() == {"caption": "cap", "alt_text": "alt", "nearby_text": "near"}

    @pytest.mark.parametrize("value", [None, "", "   \n"])
    def test_absent_is_null_not_empty(self, value):
        assert C.VisualContext(caption=value).as_request()["caption"] is None

    def test_bounded_at_word_boundary(self):
        text = "word " * 150
        out = C.bound_context_text(text)
        assert len(out) <= C.CONTEXT_MAX_CHARS
        assert out.endswith("word")
        assert not out.endswith(" ")

    def test_long_token_hard_cut(self):
        assert C.bound_context_text("x" * 700) == "x" * C.CONTEXT_MAX_CHARS

    def test_exact_boundary_kept(self):
        text = "a" * C.CONTEXT_MAX_CHARS + " tail"
        assert C.bound_context_text(text) == "a" * C.CONTEXT_MAX_CHARS

    def test_bounding_is_deterministic(self):
        text = "alpha beta " * 80
        assert C.bound_context_text(text) == C.bound_context_text(text)


class TestAnalysisKey:
    BASE = dict(content_hash=HASH, task="auto", model="vision-large", locale="fr",
                context=C.VisualContext(caption="a"))

    def test_deterministic(self):
        assert C.analysis_key(**self.BASE) == C.analysis_key(**dict(self.BASE))
        assert re.fullmatch(r"[0-9a-f]{64}", C.analysis_key(**self.BASE))

    @pytest.mark.parametrize("override", [
        {"context": C.VisualContext(caption="b")},
        {"task": "table"},
        {"model": "vision-small"},
        {"model": None},
        {"locale": "en"},
    ])
    def test_differs(self, override):
        assert C.analysis_key(**self.BASE) != C.analysis_key(**{**self.BASE, **override})

    def test_context_beyond_bound_does_not_change_key(self):
        long_a = "word " * 150 + "tail-a"
        long_b = "word " * 150 + "tail-b"
        a = C.analysis_key(**{**self.BASE, "context": C.VisualContext(caption=long_a)})
        b = C.analysis_key(**{**self.BASE, "context": C.VisualContext(caption=long_b)})
        assert a == b


# --------------------------------------------------------------------------- #
# Bbox
# --------------------------------------------------------------------------- #
class TestBbox:
    def test_required_mapping(self):
        assert C.map_bbox((100, 200, 500, 400), [0.5, 0.2, 1.0, 0.8]) == (300, 240, 500, 360)

    def test_full_image(self):
        assert C.map_bbox((10, 20, 30, 40), [0, 0, 1, 1]) == (10, 20, 30, 40)

    @pytest.mark.parametrize("norm", [[0, 0, 1.2, 1], [0.5, 0, 0.4, 1], [0, 0, 1]])
    def test_invalid_normalized_bbox(self, norm):
        with pytest.raises(C.VisualAssetError):
            C.map_bbox((0, 0, 10, 10), norm)


# --------------------------------------------------------------------------- #
# Artefact
# --------------------------------------------------------------------------- #
class TestPptxSource:
    """`pptx` est une nature de source, jamais une page."""

    PPTX_LOCATOR = {"type": "pptx", "slide_number": 4, "shape_order": 2, "rel_id": "rId3",
                    "part": "ppt/media/image1.png", "grouped": False}

    def _pptx_asset(self, **kw):
        params = dict(source_kind="pptx", source_page=None, source_bbox=None,
                      source_locator=dict(self.PPTX_LOCATOR), origin="embedded",
                      render_dpi=None, task="describe")
        params.update(kw)
        return _asset(**params)

    def test_pptx_source_kind_accepted(self):
        asset = self._pptx_asset()
        assert asset.source_kind == "pptx" and asset.source_page is None

    def test_slide_number_is_projected_as_citation_not_page(self):
        meta = C.artifact_meta(C.build_artifact(self._pptx_asset(), M.success("photo")), "photo")
        assert meta["slide_number"] == 4
        assert meta["visual"]["source_page"] is None
        assert meta["locator"]["slide_number"] == 4
        assert "page_number" not in meta

    def test_request_carries_no_provenance(self):
        body = json.dumps(C.build_vision_request(self._pptx_asset(), document_opaque_id=DOC_ID).payload)
        for fuite in ("pptx", "slide_number", "rId3", "ppt/media"):
            assert fuite not in body

    @pytest.mark.parametrize("value", [0, -1, True, "4", None, 4.0])
    def test_invalid_slide_number_never_projected(self, value):
        loc = dict(self.PPTX_LOCATOR, slide_number=value)
        meta = C.artifact_meta(C.build_artifact(self._pptx_asset(source_locator=loc),
                                                M.success("photo")), "photo")
        assert "slide_number" not in meta

    def test_other_sources_do_not_gain_slide_number(self):
        meta = C.artifact_meta(C.build_artifact(_asset(), M.success("photo")), "photo")
        assert "slide_number" not in meta


class TestArtifact:
    def test_provenance_comes_from_asset_and_identity_is_retained(self):
        data = M.success("photo")
        art = C.build_artifact(_asset(), data)
        assert (art.source_kind, art.source_page, art.source_bbox) == ("pdf", 3, (100, 200, 500, 400))
        assert art.source_locator == LOCATOR
        assert art.asset_hash == HASH
        assert art.model == data["model"]
        assert art.pipeline == data["pipeline"]
        assert art.image == data["image"]
        assert (art.schema_version, art.task, art.visual_kind) == ("1", "auto", "photo")
        assert art.usable is True

    def test_gateway_response_cannot_overwrite_provenance(self):
        """Même si un corps (non validé) portait des champs de provenance, seul le
        visuel source fait foi."""
        data = M.success("photo")
        data.update({"source_page": 99, "source_bbox": [0, 0, 1, 1], "source_locator": {"x": 1}})
        art = C.build_artifact(_asset(), data)
        assert (art.source_page, art.source_bbox, art.source_locator) == (3, (100, 200, 500, 400),
                                                                          LOCATOR)

    def test_repr_has_no_payload_or_summary(self):
        text = repr(C.build_artifact(_asset(), M.success("photo")))
        assert M.PAYLOAD_SENTINEL not in text

    def test_mixed_children_carry_region_provenance(self):
        parent = C.build_artifact(_asset(), M.success("mixed"))
        children = C.region_artifacts(parent)
        assert [c.region_index for c in children] == [0, 1, 2, 3]
        assert children[1].region_bbox_norm == (0.5, 0.2, 1.0, 0.8)
        assert children[1].region_bbox_source == (300, 240, 500, 360)
        assert all(c.asset_hash == HASH and c.source_page == 3 for c in children)
        assert [c.usable for c in children] == [True, True, True, False]

    def test_mixed_children_without_source_bbox(self):
        parent = C.build_artifact(_asset(source_bbox=None), M.success("mixed"))
        assert all(c.region_bbox_source is None for c in C.region_artifacts(parent))

    def test_meta_is_bounded(self):
        art = C.build_artifact(_asset(), M.success("photo"))
        blob = json.dumps(C.artifact_meta(art, "photo"))
        for forbidden in (M.IMAGE_B64, "IMAGE-SENTINEL", M.CONTEXT_SENTINEL, M.PAYLOAD_SENTINEL):
            assert forbidden not in blob


# --------------------------------------------------------------------------- #
# Utilisabilité
# --------------------------------------------------------------------------- #
def _payload(kind):
    return copy.deepcopy(M.PAYLOADS[kind])


class TestUsable:
    @pytest.mark.parametrize("kind", ["photo", "table", "diagram", "chart", "technical_drawing",
                                      "mixed"])
    def test_catalogue_kinds_usable(self, kind):
        assert C.is_usable(kind, _payload(kind)) is True

    @pytest.mark.parametrize("kind", ["unknown", "decorative"])
    def test_never_usable(self, kind):
        assert C.is_usable(kind, None) is False
        assert C.is_usable(kind, {"description": "x"}) is False

    def test_photo_blank_description(self):
        p = _payload("photo")
        p["description"] = "   "
        assert C.is_usable("photo", p) is False

    def test_table_without_filled_data(self):
        p = _payload("table")
        for c in p["cells"]:
            if c["role"] == "data":
                c.update(state="unreadable", text=None, certainty="unreadable")
        assert C.is_usable("table", p) is False

    def test_one_unreadable_cell_does_not_invalidate_table(self):
        assert C.is_usable("table", _payload("table")) is True

    def test_diagram_without_nodes(self):
        p = {"diagram_type": None, "nodes": [], "edges": [], "groups": [], "annotations": []}
        assert C.is_usable("diagram", p) is False

    def test_chart_rules(self):
        p = _payload("chart")
        p["axes"] = []
        for s in p["series"]:
            s["axis_ids"] = None
        assert C.is_usable("chart", p) is True  # points lisibles
        for pt in p["series"][0]["points"]:
            pt.update(precision="unreadable", value=None, value_text=None)
        assert C.is_usable("chart", p) is False

    def test_technical_dimension_only(self):
        p = _payload("technical_drawing")
        p.update(elements=[], zones=[], relations=[])
        for d in p["explicit_dimensions"]:
            d["between"] = None
        assert C.is_usable("technical_drawing", p) is True
        p["explicit_dimensions"] = []
        assert C.is_usable("technical_drawing", p) is False

    def test_mixed_with_only_non_evidence_regions(self):
        p = {"regions": [
            {"bbox": [0, 0, 1, 0.5], "visual_kind": "decorative", "payload": None, "warnings": [],
             "requires_visual_check": False},
            {"bbox": [0, 0.5, 1, 1], "visual_kind": "unknown", "payload": None, "warnings": [],
             "requires_visual_check": True},
        ]}
        assert C.is_usable("mixed", p) is False


# --------------------------------------------------------------------------- #
# Dédoublonnage
# --------------------------------------------------------------------------- #
class TestDedup:
    def test_nfkc_casefold_whitespace(self):
        assert C.normalize_for_dedup("  ＬＯＴ\t42  ") == C.normalize_for_dedup("lot 42")
        assert C.normalize_for_dedup("Straße") == C.normalize_for_dedup("STRASSE")

    def test_exact_only_no_fuzzy(self):
        assert C.normalize_for_dedup("lot 42") != C.normalize_for_dedup("lot 42.")

    def test_known_text_set_skips_empty(self):
        assert C.known_text_set(["", "  ", "A"]) == frozenset({"a"})


# --------------------------------------------------------------------------- #
# Bout en bout, transport simulé (§101)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return copy.deepcopy(self._body)


@pytest.mark.parametrize("kind", ["photo", "table", "diagram", "chart", "technical_drawing",
                                  "mixed", "unknown", "decorative"])
def test_request_transport_validation_artifact(monkeypatch, kind):
    asset = _asset()
    request = C.build_vision_request(asset, document_opaque_id=DOC_ID)
    body = M.success(kind)
    body["request_id"] = request.request_id
    sent = []

    def fake_post(url, **kwargs):
        sent.append(kwargs["json"])
        return _Resp(body)

    monkeypatch.setattr("apps.llm_router.gateway.vision.requests.post", fake_post)
    client = vision.VisionGatewayClient(base_url="http://gateway.test", timeout_s=5, enabled=True)
    art = C.build_artifact(asset, client.analyze(request).data)
    assert sent == [request.payload]
    assert art.visual_kind == kind
    assert art.usable is (kind not in ("unknown", "decorative"))

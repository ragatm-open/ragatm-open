"""catalogue de mocks du contrat AI Gateway Vision V1.

Chaque réponse reproduit l'enveloppe FERMÉE du schéma V1 (aucun champ en plus, aucun
en moins). Les cas de dérive partent d'une réponse légale et y introduisent UNE seule
anomalie, pour que l'échec observé désigne sa cause.

Contenu neutre : aucune donnée métier, aucun document réel.

Trois formes ne sont pas détaillées par le texte du contrat gelé transmis à RAGatm :
les entrées de `legend` d'un dessin technique, la charge d'une région mixte `text`,
et l'énumération `orientation` d'un axe. Les mocks les représentent sous une forme
minimale et le validateur ne les contrôle qu'au niveau conteneur ; l'écart est à
réconcilier contre les fixtures dorées du Gateway lors de la qualification.
"""
from __future__ import annotations

import base64
import copy

REQUEST_ID = "rgv1-0123456789abcdef0123456789abcdef"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nVISUAL-1A-IMAGE-SENTINEL"
IMAGE_B64 = base64.b64encode(IMAGE_BYTES).decode("ascii")
CONTEXT_SENTINEL = "CONTEXT-SENTINEL-7Q2"
PAYLOAD_SENTINEL = "PAYLOAD-SENTINEL-9Z4"


def request_payload(**overrides) -> dict:
    """Requête V1 de référence — celle que l'adaptateur doit produire."""
    payload = {
        "schema_version": "1",
        "task": "auto",
        "locale": "fr",
        "request_id": REQUEST_ID,
        "image": {"data": IMAGE_B64, "mime_type": "image/png"},
        "context": {"caption": CONTEXT_SENTINEL, "alt_text": None, "nearby_text": None},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Charges utiles légales
# --------------------------------------------------------------------------- #
PHOTO = {
    "description": f"A blue circle beside a red square {PAYLOAD_SENTINEL}",
    "setting": "indoor table",
    "objects": [
        {"label": "circle", "count": 1, "attributes": ["blue"], "certainty": "exact"},
        {"label": "square", "count": None, "attributes": ["red", "matte"], "certainty": "approximate"},
    ],
    "observable_relations": [
        {"subject": "circle", "relation": "left of", "object": "square", "certainty": "exact"},
    ],
    "visible_text": [
        {"text": "LOT 42", "certainty": "exact"},
        {"text": "ACME", "certainty": "ambiguous"},
    ],
}

# Grille dorée : en-tête sur deux niveaux, coin, en-têtes de ligne, en-tête fusionné,
# valeur exacte, vide, illisible, approximative, ambiguë, donnée fusionnée.
TABLE = {
    "n_rows": 5,
    "n_cols": 4,
    "caption": None,
    "notes": [],
    "cells": [
        {"row": 0, "col": 0, "row_span": 2, "col_span": 1, "role": "corner", "state": "filled",
         "text": "Region", "certainty": "exact"},
        {"row": 0, "col": 1, "row_span": 1, "col_span": 2, "role": "column_header", "state": "filled",
         "text": "2024", "certainty": "exact"},
        {"row": 0, "col": 3, "row_span": 2, "col_span": 1, "role": "column_header", "state": "filled",
         "text": "Total", "certainty": "exact"},
        {"row": 1, "col": 1, "row_span": 1, "col_span": 1, "role": "column_header", "state": "filled",
         "text": "H1", "certainty": "exact"},
        {"row": 1, "col": 2, "row_span": 1, "col_span": 1, "role": "column_header", "state": "filled",
         "text": "H2", "certainty": "exact"},
        {"row": 2, "col": 0, "row_span": 1, "col_span": 1, "role": "row_header", "state": "filled",
         "text": "North", "certainty": "exact"},
        {"row": 2, "col": 1, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "120", "certainty": "exact"},
        {"row": 2, "col": 2, "row_span": 1, "col_span": 1, "role": "data", "state": "blank",
         "text": None, "certainty": "exact"},
        {"row": 2, "col": 3, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "120", "certainty": "exact"},
        {"row": 3, "col": 0, "row_span": 1, "col_span": 1, "role": "row_header", "state": "filled",
         "text": "South", "certainty": "exact"},
        {"row": 3, "col": 1, "row_span": 1, "col_span": 1, "role": "data", "state": "unreadable",
         "text": None, "certainty": "unreadable"},
        {"row": 3, "col": 2, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "95", "certainty": "approximate"},
        {"row": 3, "col": 3, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "210", "certainty": "exact"},
        {"row": 4, "col": 0, "row_span": 1, "col_span": 1, "role": "row_header", "state": "filled",
         "text": "West", "certainty": "exact"},
        {"row": 4, "col": 1, "row_span": 1, "col_span": 2, "role": "data", "state": "filled",
         "text": "80", "certainty": "ambiguous"},
        {"row": 4, "col": 3, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "80", "certainty": "exact"},
    ],
}

DIAGRAM = {
    "diagram_type": "process",
    "nodes": [
        {"id": "n1", "label": "Intake", "type": "box", "certainty": "exact"},
        {"id": "n2", "label": "Review", "type": "box", "certainty": "exact"},
        {"id": "n3", "label": "Archive", "type": "cylinder", "certainty": "approximate"},
        {"id": "n4", "label": "Notify", "type": None, "certainty": "exact"},
    ],
    "edges": [
        {"source_id": "n1", "target_id": "n2", "direction": "forward", "relation": "feeds",
         "label": "form", "certainty": "exact"},
        {"source_id": "n2", "target_id": "n3", "direction": "bidirectional", "relation": None,
         "label": None, "certainty": "exact"},
        {"source_id": "n2", "target_id": "n4", "direction": "undirected", "relation": None,
         "label": None, "certainty": "exact"},
        {"source_id": "n3", "target_id": "n4", "direction": "unknown", "relation": None,
         "label": None, "certainty": "ambiguous"},
    ],
    "groups": [{"id": "g1", "label": "Back office", "members": ["n2", "n3"]}],
    "annotations": [{"text": "draft flow", "certainty": "exact"}],
}

CHART = {
    "chart_type": "bar",
    "axes": [
        {"id": "x", "orientation": "horizontal", "label": "Quarter", "unit": None,
         "scale": "categorical", "range": None, "certainty": "exact"},
        {"id": "y", "orientation": "vertical", "label": "Amount", "unit": "EUR", "scale": "linear",
         "range": {"min": 0, "max": 20, "certainty": "approximate"}, "certainty": "exact"},
    ],
    "series": [
        {"id": "s1", "label": "2024", "axis_ids": ["x", "y"], "points": [
            {"label": "Q1", "x": "Q1", "value": 10, "value_text": "10", "precision": "exact_labeled"},
            {"label": "Q2", "x": "Q2", "value": 14.5, "value_text": None,
             "precision": "approximate_visual"},
            {"label": "Q3", "x": "Q3", "value": None, "value_text": None, "precision": "unreadable"},
        ]},
    ],
    "trend_summary": "Amounts rise from Q1 to Q2.",
}

TECHNICAL = {
    "drawing_type": "schematic",
    "elements": [
        {"id": "e1", "kind": "pump", "label": "P1", "certainty": "exact"},
        {"id": "e2", "kind": "tank", "label": "T1", "certainty": "exact"},
        {"id": "e3", "kind": "valve", "label": None, "certainty": "ambiguous"},
    ],
    "explicit_dimensions": [
        {"value_text": "DN 50", "unit": "mm", "value": 50, "between": ["e1", "e2"],
         "certainty": "exact"},
        {"value_text": "1 2?0", "unit": "mm", "value": 120, "between": None,
         "certainty": "ambiguous"},
    ],
    "symbols": [{"label": "arrow", "meaning": "flow direction", "certainty": "exact"}],
    "zones": [{"id": "z1", "label": "Zone A", "members": ["e1", "e2"]}],
    "relations": [
        {"source_id": "e1", "target_id": "e2", "relation": "feeds", "label": None,
         "certainty": "exact"},
        {"source_id": "e3", "target_id": "e2", "relation": "connected_to", "label": "bypass",
         "certainty": "ambiguous"},
    ],
    "legend": [{"text": "solid line = pipe", "certainty": "exact"}],
    "annotations": [{"text": "check before commissioning", "certainty": "exact"}],
}

MIXED = {
    "regions": [
        {"bbox": [0.0, 0.0, 0.5, 1.0], "visual_kind": "chart", "payload": CHART, "warnings": [],
         "requires_visual_check": False},
        {"bbox": [0.5, 0.2, 1.0, 0.8], "visual_kind": "table", "payload": TABLE,
         "warnings": ["unreadable_region"], "requires_visual_check": True},
        {"bbox": [0.5, 0.0, 1.0, 0.2], "visual_kind": "text",
         "payload": {"text": "Quarterly overview", "certainty": "exact"}, "warnings": [],
         "requires_visual_check": False},
        {"bbox": [0.5, 0.8, 1.0, 1.0], "visual_kind": "decorative", "payload": None, "warnings": [],
         "requires_visual_check": False},
    ],
}

PAYLOADS = {
    "photo": PHOTO, "table": TABLE, "diagram": DIAGRAM, "chart": CHART,
    "technical_drawing": TECHNICAL, "mixed": MIXED, "unknown": None, "decorative": None,
}


def success(kind: str, *, task: str = "auto") -> dict:
    """Réponse V1 légale et complète pour `kind`."""
    warnings, check = [], False
    if kind == "unknown":
        warnings, check = ["insufficient_visual_evidence"], True
    return {
        "status": "ok",
        "provider": "ai_gateway",
        "upstream_provider": "ollama",
        "schema_version": "1",
        "task": task,
        "visual_kind": kind,
        "request_id": REQUEST_ID,
        "summary": None if kind in ("unknown", "decorative") else f"{kind} summary",
        "warnings": warnings,
        "requires_visual_check": check,
        "payload": copy.deepcopy(PAYLOADS[kind]),
        "model": {"logical": "vision-large", "concrete": "concrete-model", "digest": "sha256:abc"},
        "pipeline": {"prompt_profile": "auto", "prompt_profile_version": "1",
                     "pipeline_id": "vision-pipeline-1"},
        "image": {"width": 1000, "height": 500, "pixels": 500000, "bytes": 2048, "format": "png"},
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "latency_ms": 1200,
        "truncated": False,
    }


def error(code: str, *, retryable: bool, message: str = "diagnostic message",
          provider: str = "ai_gateway") -> dict:
    return {"detail": {"status": "error", "provider": provider, "code": code,
                       "message": message, "retryable": retryable, "request_id": REQUEST_ID}}


# --------------------------------------------------------------------------- #
# Dérives : UNE anomalie par cas
# --------------------------------------------------------------------------- #
def _with(kind, mutate):
    def build():
        body = success(kind)
        mutate(body)
        return body
    return build


def _set(key, value):
    return lambda body: body.__setitem__(key, value)


def _drop_last_cell(body):
    body["payload"]["cells"].pop()


def _overlap(body):
    body["payload"]["cells"].append(
        {"row": 2, "col": 1, "row_span": 1, "col_span": 1, "role": "data", "state": "filled",
         "text": "dup", "certainty": "exact"})


def _dangling_edge(body):
    body["payload"]["edges"][0]["target_id"] = "n99"


def _number_on_unreadable(body):
    body["payload"]["series"][0]["points"][2]["value"] = 3


def _photo_missing_description(body):
    del body["payload"]["description"]


def _nested_mixed(body):
    body["payload"]["regions"][0]["visual_kind"] = "mixed"


def _single_region(body):
    body["payload"]["regions"] = body["payload"]["regions"][:1]


def _bbox_out_of_range(body):
    body["payload"]["regions"][0]["bbox"] = [0.0, 0.0, 1.2, 1.0]


def _extra_payload_key(body):
    body["payload"]["confidence"] = 0.9


def _blank_with_text(body):
    body["payload"]["cells"][7]["text"] = "not blank"


DRIFT_CASES = {
    "schema_version_2": _with("photo", _set("schema_version", "2")),
    "extra_top_level_field": _with("photo", _set("confidence", 0.93)),
    "missing_top_level_field": _with("photo", lambda b: b.pop("usage")),
    "truncated_success": _with("photo", _set("truncated", True)),
    "status_not_ok": _with("photo", _set("status", "error")),
    "provider_not_gateway": _with("photo", _set("provider", "ollama")),
    "unknown_visual_kind": _with("photo", _set("visual_kind", "screenshot")),
    "unknown_warning": _with("photo", _set("warnings", ["blurry"])),
    "model_identity_incomplete": _with("photo", lambda b: b["model"].pop("digest")),
    "pipeline_identity_incomplete": _with("photo", lambda b: b["pipeline"].pop("pipeline_id")),
    "unknown_with_payload": _with("unknown", _set("payload", {"description": "x"})),
    "unknown_without_check": _with("unknown", _set("requires_visual_check", False)),
    "decorative_with_payload": _with("decorative", _set("payload", {"description": "x"})),
    "photo_missing_description": _with("photo", _photo_missing_description),
    "payload_extra_key": _with("photo", _extra_payload_key),
    "table_grid_hole": _with("table", _drop_last_cell),
    "table_grid_overlap": _with("table", _overlap),
    "table_blank_with_text": _with("table", _blank_with_text),
    "diagram_dangling_edge": _with("diagram", _dangling_edge),
    "chart_number_on_unreadable_point": _with("chart", _number_on_unreadable),
    "mixed_single_region": _with("mixed", _single_region),
    "mixed_nested": _with("mixed", _nested_mixed),
    "mixed_bbox_out_of_range": _with("mixed", _bbox_out_of_range),
    "payload_not_object": _with("photo", _set("payload", ["not", "an", "object"])),
}

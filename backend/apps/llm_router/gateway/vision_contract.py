"""Contrat AI Gateway Vision V1 côté consommateur.

Le contrat serveur est GELÉ (AI-GATEWAY-VISION-CONTRACT-0) : ce module ne le
redessine pas, il le VÉRIFIE. La réponse V1 est fermée — un champ en plus est une
dérive, pas une extension à ignorer.

Contrôles appliqués :

* enveloppe commune exacte (clés, constantes, énumérations, identités modèle/pipeline) ;
* charge utile par ``visual_kind`` (clés exactes, énumérations, types) ;
* invariants référentiels : grille de tableau couvrante sans trou ni chevauchement,
  arêtes et groupes pointant des nœuds existants, axes et éléments référencés existants ;
* sémantique canonique : cellule vide/illisible/remplie, point illisible sans nombre,
  ``unknown``/``decorative`` sans charge utile.

Écart à réconcilier lors de la qualification : le texte gelé ne détaille ni les entrées
``legend``/``annotations`` d'un dessin technique, ni la charge d'une région mixte
``text``, ni l'énumération ``orientation`` d'un axe. Ces formes sont contrôlées au
niveau conteneur seulement (liste, objet ou null, chaîne ou null).
"""
from __future__ import annotations

from typing import Any, Iterable

SCHEMA_VERSION = "1"

TASKS = frozenset({"auto", "describe", "table", "diagram", "chart", "technical_drawing"})
LOCALES = frozenset({"fr", "en"})
MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

VISUAL_KINDS = frozenset({"photo", "table", "diagram", "chart", "technical_drawing", "mixed",
                          "decorative", "unknown"})
REGION_KINDS = frozenset({"photo", "table", "diagram", "chart", "technical_drawing", "text",
                          "decorative", "unknown"})
CERTAINTIES = frozenset({"exact", "approximate", "ambiguous", "unreadable"})
WARNINGS = frozenset({"low_resolution", "unreadable_region", "ambiguous_structure",
                      "incomplete_axes", "insufficient_visual_evidence"})

TABLE_ROLES = frozenset({"column_header", "row_header", "data", "corner", "note", "unknown"})
TABLE_STATES = frozenset({"filled", "blank", "unreadable"})
EDGE_DIRECTIONS = frozenset({"forward", "bidirectional", "undirected", "unknown"})
CHART_TYPES = frozenset({"bar", "line", "pie", "scatter", "area", "stacked_bar", "other",
                         "unknown"})
AXIS_SCALES = frozenset({"linear", "log", "categorical", "temporal", "unknown"})
POINT_PRECISIONS = frozenset({"exact_labeled", "approximate_visual", "unreadable"})
TECHNICAL_RELATIONS = frozenset({"connected_to", "inside", "adjacent_to", "above", "below",
                                 "left_of", "right_of", "feeds", "enters", "exits", "other"})

REQUEST_KEYS = frozenset({"schema_version", "task", "model", "locale", "request_id", "image",
                          "context"})
REQUEST_ID_MAX_CHARS = 128

RESPONSE_KEYS = frozenset({"status", "provider", "upstream_provider", "schema_version", "task",
                           "visual_kind", "request_id", "summary", "warnings",
                           "requires_visual_check", "payload", "model", "pipeline", "image",
                           "usage", "latency_ms", "truncated"})
MODEL_KEYS = ("logical", "concrete", "digest")
PIPELINE_KEYS = ("prompt_profile", "prompt_profile_version", "pipeline_id")
IMAGE_KEYS = ("width", "height", "pixels", "bytes", "format")
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class ContractViolation(Exception):
    """Violation interne ; ``reason`` = chemin contractuel, jamais une valeur."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def _fail(path: str, what: str) -> None:
    raise ContractViolation(f"{path}: {what}")


def _obj(value: Any, path: str, keys: Iterable[str]) -> dict:
    if not isinstance(value, dict):
        _fail(path, "object expected")
    if set(value) != set(keys):
        _fail(path, "fields differ from closed V1 schema")
    return value


def _str(value: Any, path: str, *, nullable: bool = False, non_empty: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        _fail(path, "string expected")
    if non_empty and not value.strip():
        _fail(path, "non-empty string expected")


def _int(value: Any, path: str, *, minimum: int | None = None, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "integer expected")
    if minimum is not None and value < minimum:
        _fail(path, f"integer >= {minimum} expected")


def _num(value: Any, path: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "number expected")


def _bool(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "boolean expected")


def _enum(value: Any, path: str, allowed: frozenset) -> None:
    if not isinstance(value, str) or value not in allowed:
        _fail(path, "value outside V1 enumeration")


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail(path, "array expected")
    return value


def _str_list(value: Any, path: str) -> list:
    for i, item in enumerate(_list(value, path)):
        _str(item, f"{path}[{i}]")
    return value


def _ids(items: list, path: str) -> set:
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        _fail(path, "duplicate id")
    return set(ids)


def _refs(values: Iterable[str], known: set, path: str) -> None:
    if any(v not in known for v in values):
        _fail(path, "reference to unknown id")


# --------------------------------------------------------------------------- #
# Charges utiles
# --------------------------------------------------------------------------- #
def _photo(p: Any, path: str) -> None:
    _obj(p, path, ("description", "setting", "objects", "observable_relations", "visible_text"))
    _str(p["description"], f"{path}.description")
    _str(p["setting"], f"{path}.setting", nullable=True)
    for i, o in enumerate(_list(p["objects"], f"{path}.objects")):
        q = f"{path}.objects[{i}]"
        _obj(o, q, ("label", "count", "attributes", "certainty"))
        _str(o["label"], f"{q}.label")
        _int(o["count"], f"{q}.count", minimum=0, nullable=True)
        _str_list(o["attributes"], f"{q}.attributes")
        _enum(o["certainty"], f"{q}.certainty", CERTAINTIES)
    for i, r in enumerate(_list(p["observable_relations"], f"{path}.observable_relations")):
        q = f"{path}.observable_relations[{i}]"
        _obj(r, q, ("subject", "relation", "object", "certainty"))
        for key in ("subject", "relation", "object"):
            _str(r[key], f"{q}.{key}")
        _enum(r["certainty"], f"{q}.certainty", CERTAINTIES)
    _text_items(p["visible_text"], f"{path}.visible_text")


def _text_items(value: Any, path: str) -> None:
    for i, t in enumerate(_list(value, path)):
        _obj(t, f"{path}[{i}]", ("text", "certainty"))
        _str(t["text"], f"{path}[{i}].text")
        _enum(t["certainty"], f"{path}[{i}].certainty", CERTAINTIES)


def expand_table_grid(p: dict, path: str = "payload") -> dict:
    """Coordonnée -> index de la cellule ancre qui la couvre.

    Exige la couverture EXACTE : chaque coordonnée couverte par une et une seule cellule.
    """
    n_rows, n_cols = p["n_rows"], p["n_cols"]
    grid: dict = {}
    for idx, c in enumerate(p["cells"]):
        if c["row"] + c["row_span"] > n_rows or c["col"] + c["col_span"] > n_cols:
            _fail(f"{path}.cells[{idx}]", "span outside grid")
        for r in range(c["row"], c["row"] + c["row_span"]):
            for k in range(c["col"], c["col"] + c["col_span"]):
                if (r, k) in grid:
                    _fail(f"{path}.cells[{idx}]", "grid overlap")
                grid[(r, k)] = idx
    if len(grid) != n_rows * n_cols:
        _fail(f"{path}.cells", "grid hole")
    return grid


def _table(p: Any, path: str) -> None:
    _obj(p, path, ("n_rows", "n_cols", "caption", "notes", "cells"))
    _int(p["n_rows"], f"{path}.n_rows", minimum=1)
    _int(p["n_cols"], f"{path}.n_cols", minimum=1)
    _str(p["caption"], f"{path}.caption", nullable=True)
    _str_list(p["notes"], f"{path}.notes")
    for i, c in enumerate(_list(p["cells"], f"{path}.cells")):
        q = f"{path}.cells[{i}]"
        _obj(c, q, ("row", "col", "row_span", "col_span", "role", "state", "text", "certainty"))
        _int(c["row"], f"{q}.row", minimum=0)
        _int(c["col"], f"{q}.col", minimum=0)
        _int(c["row_span"], f"{q}.row_span", minimum=1)
        _int(c["col_span"], f"{q}.col_span", minimum=1)
        _enum(c["role"], f"{q}.role", TABLE_ROLES)
        _enum(c["state"], f"{q}.state", TABLE_STATES)
        _str(c["text"], f"{q}.text", nullable=True)
        _enum(c["certainty"], f"{q}.certainty", CERTAINTIES)
        if c["state"] == "blank" and (c["text"] is not None or c["certainty"] != "exact"):
            _fail(q, "blank cell must have text=null and certainty=exact")
        if c["state"] == "unreadable" and (c["text"] is not None or c["certainty"] != "unreadable"):
            _fail(q, "unreadable cell must have text=null and certainty=unreadable")
        if c["state"] == "filled" and (c["text"] is None or c["certainty"] == "unreadable"):
            _fail(q, "filled cell must have text and a readable certainty")
    expand_table_grid(p, path)


def _diagram(p: Any, path: str) -> None:
    _obj(p, path, ("diagram_type", "nodes", "edges", "groups", "annotations"))
    _str(p["diagram_type"], f"{path}.diagram_type", nullable=True)
    nodes = _list(p["nodes"], f"{path}.nodes")
    for i, n in enumerate(nodes):
        q = f"{path}.nodes[{i}]"
        _obj(n, q, ("id", "label", "type", "certainty"))
        _str(n["id"], f"{q}.id", non_empty=True)
        _str(n["label"], f"{q}.label", nullable=True)
        _str(n["type"], f"{q}.type", nullable=True)
        _enum(n["certainty"], f"{q}.certainty", CERTAINTIES)
    known = _ids(nodes, f"{path}.nodes")
    for i, e in enumerate(_list(p["edges"], f"{path}.edges")):
        q = f"{path}.edges[{i}]"
        _obj(e, q, ("source_id", "target_id", "direction", "relation", "label", "certainty"))
        _str(e["source_id"], f"{q}.source_id")
        _str(e["target_id"], f"{q}.target_id")
        _refs((e["source_id"], e["target_id"]), known, q)
        _enum(e["direction"], f"{q}.direction", EDGE_DIRECTIONS)
        _str(e["relation"], f"{q}.relation", nullable=True)
        _str(e["label"], f"{q}.label", nullable=True)
        _enum(e["certainty"], f"{q}.certainty", CERTAINTIES)
    _groups(p["groups"], f"{path}.groups", known)
    _text_items(p["annotations"], f"{path}.annotations")


def _groups(value: Any, path: str, known: set) -> None:
    for i, g in enumerate(_list(value, path)):
        q = f"{path}[{i}]"
        _obj(g, q, ("id", "label", "members"))
        _str(g["id"], f"{q}.id", non_empty=True)
        _str(g["label"], f"{q}.label", nullable=True)
        _refs(_str_list(g["members"], f"{q}.members"), known, f"{q}.members")


def _chart(p: Any, path: str) -> None:
    _obj(p, path, ("chart_type", "axes", "series", "trend_summary"))
    _enum(p["chart_type"], f"{path}.chart_type", CHART_TYPES)
    axes = _list(p["axes"], f"{path}.axes")
    for i, a in enumerate(axes):
        q = f"{path}.axes[{i}]"
        _obj(a, q, ("id", "orientation", "label", "unit", "scale", "range", "certainty"))
        _str(a["id"], f"{q}.id", non_empty=True)
        _str(a["orientation"], f"{q}.orientation", nullable=True)
        _str(a["label"], f"{q}.label", nullable=True)
        _str(a["unit"], f"{q}.unit", nullable=True)
        _enum(a["scale"], f"{q}.scale", AXIS_SCALES)
        if a["range"] is not None:
            _obj(a["range"], f"{q}.range", ("min", "max", "certainty"))
            _num(a["range"]["min"], f"{q}.range.min", nullable=True)
            _num(a["range"]["max"], f"{q}.range.max", nullable=True)
            _enum(a["range"]["certainty"], f"{q}.range.certainty", CERTAINTIES)
        _enum(a["certainty"], f"{q}.certainty", CERTAINTIES)
    axis_ids = _ids(axes, f"{path}.axes")
    series = _list(p["series"], f"{path}.series")
    for i, s in enumerate(series):
        q = f"{path}.series[{i}]"
        _obj(s, q, ("id", "label", "axis_ids", "points"))
        _str(s["id"], f"{q}.id", non_empty=True)
        _str(s["label"], f"{q}.label", nullable=True)
        if s["axis_ids"] is not None:
            _refs(_str_list(s["axis_ids"], f"{q}.axis_ids"), axis_ids, f"{q}.axis_ids")
        for j, pt in enumerate(_list(s["points"], f"{q}.points")):
            r = f"{q}.points[{j}]"
            _obj(pt, r, ("label", "x", "value", "value_text", "precision"))
            _str(pt["label"], f"{r}.label", nullable=True)
            if pt["x"] is not None and (isinstance(pt["x"], bool)
                                        or not isinstance(pt["x"], (int, float, str))):
                _fail(f"{r}.x", "number, string or null expected")
            _num(pt["value"], f"{r}.value", nullable=True)
            _str(pt["value_text"], f"{r}.value_text", nullable=True)
            _enum(pt["precision"], f"{r}.precision", POINT_PRECISIONS)
            if pt["precision"] == "unreadable" and (pt["value"] is not None
                                                    or pt["value_text"] is not None):
                _fail(r, "unreadable point must carry no value")
    _ids(series, f"{path}.series")
    _str(p["trend_summary"], f"{path}.trend_summary", nullable=True)


def _technical(p: Any, path: str) -> None:
    _obj(p, path, ("drawing_type", "elements", "explicit_dimensions", "symbols", "zones",
                   "relations", "legend", "annotations"))
    _str(p["drawing_type"], f"{path}.drawing_type", nullable=True)
    elements = _list(p["elements"], f"{path}.elements")
    for i, e in enumerate(elements):
        q = f"{path}.elements[{i}]"
        _obj(e, q, ("id", "kind", "label", "certainty"))
        _str(e["id"], f"{q}.id", non_empty=True)
        _str(e["kind"], f"{q}.kind", nullable=True)
        _str(e["label"], f"{q}.label", nullable=True)
        _enum(e["certainty"], f"{q}.certainty", CERTAINTIES)
    known = _ids(elements, f"{path}.elements")
    for i, d in enumerate(_list(p["explicit_dimensions"], f"{path}.explicit_dimensions")):
        q = f"{path}.explicit_dimensions[{i}]"
        _obj(d, q, ("value_text", "unit", "value", "between", "certainty"))
        _str(d["value_text"], f"{q}.value_text")
        _str(d["unit"], f"{q}.unit", nullable=True)
        _num(d["value"], f"{q}.value", nullable=True)
        if d["between"] is not None:
            _refs(_str_list(d["between"], f"{q}.between"), known, f"{q}.between")
        _enum(d["certainty"], f"{q}.certainty", CERTAINTIES)
    for i, s in enumerate(_list(p["symbols"], f"{path}.symbols")):
        q = f"{path}.symbols[{i}]"
        _obj(s, q, ("label", "meaning", "certainty"))
        _str(s["label"], f"{q}.label", nullable=True)
        _str(s["meaning"], f"{q}.meaning", nullable=True)
        _enum(s["certainty"], f"{q}.certainty", CERTAINTIES)
    _groups(p["zones"], f"{path}.zones", known)
    for i, r in enumerate(_list(p["relations"], f"{path}.relations")):
        q = f"{path}.relations[{i}]"
        _obj(r, q, ("source_id", "target_id", "relation", "label", "certainty"))
        _str(r["source_id"], f"{q}.source_id")
        _str(r["target_id"], f"{q}.target_id")
        _refs((r["source_id"], r["target_id"]), known, q)
        _enum(r["relation"], f"{q}.relation", TECHNICAL_RELATIONS)
        _str(r["label"], f"{q}.label", nullable=True)
        _enum(r["certainty"], f"{q}.certainty", CERTAINTIES)
    # Formes non détaillées par le texte gelé : conteneur seulement (écart 1E).
    _list(p["legend"], f"{path}.legend")
    _list(p["annotations"], f"{path}.annotations")


def _mixed(p: Any, path: str) -> None:
    _obj(p, path, ("regions",))
    regions = _list(p["regions"], f"{path}.regions")
    if len(regions) < 2:
        _fail(f"{path}.regions", "at least 2 regions expected")
    for i, reg in enumerate(regions):
        q = f"{path}.regions[{i}]"
        _obj(reg, q, ("bbox", "visual_kind", "payload", "warnings", "requires_visual_check"))
        validate_norm_bbox(reg["bbox"], f"{q}.bbox")
        _enum(reg["visual_kind"], f"{q}.visual_kind", REGION_KINDS)
        _warnings(reg["warnings"], f"{q}.warnings")
        _bool(reg["requires_visual_check"], f"{q}.requires_visual_check")
        kind = reg["visual_kind"]
        if kind in ("decorative", "unknown"):
            if reg["payload"] is not None:
                _fail(f"{q}.payload", "null expected")
        elif kind == "text":
            if reg["payload"] is not None and not isinstance(reg["payload"], dict):
                _fail(f"{q}.payload", "object or null expected")
        else:
            _PAYLOAD_VALIDATORS[kind](reg["payload"], f"{q}.payload")


_PAYLOAD_VALIDATORS = {
    "photo": _photo,
    "table": _table,
    "diagram": _diagram,
    "chart": _chart,
    "technical_drawing": _technical,
    "mixed": _mixed,
}


def validate_norm_bbox(value: Any, path: str = "bbox") -> tuple:
    """``[x_min, y_min, x_max, y_max]`` normalisé dans [0,1], origine haut-gauche, ordonné."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail(path, "4 numbers expected")
    for i, v in enumerate(value):
        _num(v, f"{path}[{i}]")
        if not 0.0 <= v <= 1.0:
            _fail(f"{path}[{i}]", "value outside [0,1]")
    if not (value[0] < value[2] and value[1] < value[3]):
        _fail(path, "min must be lower than max")
    return tuple(float(v) for v in value)


def _warnings(value: Any, path: str) -> None:
    for i, w in enumerate(_list(value, path)):
        _enum(w, f"{path}[{i}]", WARNINGS)


def _identity(value: Any, path: str, keys: tuple) -> None:
    _obj(value, path, keys)
    for key in keys:
        _str(value[key], f"{path}.{key}", non_empty=True)


# --------------------------------------------------------------------------- #
# Entrées publiques
# --------------------------------------------------------------------------- #
def validate_response(data: Any, *, expected_request_id: str | None,
                      expected_task: str | None = None) -> dict:
    """Valide une réponse de succès V1. Lève ``ContractViolation`` sinon.

    ``expected_task`` : tâche DEMANDÉE. Une réponse servie sous une autre tâche que
    celle demandée est une dérive de contrat — la normalisation qui suivrait
    interpréterait une charge utile produite par un autre profil.
    """
    _obj(data, "response", RESPONSE_KEYS)
    if data["status"] != "ok":
        _fail("status", "'ok' expected")
    if data["provider"] != "ai_gateway":
        _fail("provider", "'ai_gateway' expected")
    _str(data["upstream_provider"], "upstream_provider", non_empty=True)
    if data["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", "unsupported schema version")
    _enum(data["task"], "task", TASKS)
    if expected_task is not None and data["task"] != expected_task:
        _fail("task", "does not match the requested task")
    _enum(data["visual_kind"], "visual_kind", VISUAL_KINDS)
    _str(data["request_id"], "request_id", nullable=True)
    if expected_request_id is not None and data["request_id"] != expected_request_id:
        _fail("request_id", "does not match request")
    _str(data["summary"], "summary", nullable=True)
    _warnings(data["warnings"], "warnings")
    _bool(data["requires_visual_check"], "requires_visual_check")
    _identity(data["model"], "model", MODEL_KEYS)
    _identity(data["pipeline"], "pipeline", PIPELINE_KEYS)
    _obj(data["image"], "image", IMAGE_KEYS)
    for key in ("width", "height", "pixels", "bytes"):
        _int(data["image"][key], f"image.{key}", minimum=1)
    _str(data["image"]["format"], "image.format", non_empty=True)
    _obj(data["usage"], "usage", USAGE_KEYS)
    for key in USAGE_KEYS:
        _int(data["usage"][key], f"usage.{key}", minimum=0, nullable=True)
    _num(data["latency_ms"], "latency_ms")
    if data["latency_ms"] < 0:
        _fail("latency_ms", "non-negative number expected")
    if data["truncated"] is not False:
        _fail("truncated", "successful V1 response is never truncated")

    kind = data["visual_kind"]
    if kind == "unknown":
        if data["payload"] is not None:
            _fail("payload", "null expected for unknown")
        if "insufficient_visual_evidence" not in data["warnings"]:
            _fail("warnings", "unknown requires insufficient_visual_evidence")
        if data["requires_visual_check"] is not True:
            _fail("requires_visual_check", "unknown requires visual check")
    elif kind == "decorative":
        if data["payload"] is not None:
            _fail("payload", "null expected for decorative")
        if data["warnings"]:
            _fail("warnings", "decorative carries no warning")
        if data["requires_visual_check"] is not False:
            _fail("requires_visual_check", "decorative requires no visual check")
    else:
        _PAYLOAD_VALIDATORS[kind](data["payload"], "payload")
    return data


def validate_request_shape(payload: Any) -> None:
    """Garde locale : seules les clés V1 partent vers le Gateway (aucune provenance)."""
    if not isinstance(payload, dict) or not set(payload) <= REQUEST_KEYS:
        _fail("request", "fields outside V1 request schema")
    for key in ("schema_version", "task", "locale", "image", "context"):
        if key not in payload:
            _fail(f"request.{key}", "required")
    if payload["schema_version"] != SCHEMA_VERSION:
        _fail("request.schema_version", "unsupported schema version")
    _enum(payload["task"], "request.task", TASKS)
    _enum(payload["locale"], "request.locale", LOCALES)
    if "model" in payload:
        _str(payload["model"], "request.model", non_empty=True)
    if "request_id" in payload:
        _str(payload["request_id"], "request.request_id", non_empty=True)
        if len(payload["request_id"]) > REQUEST_ID_MAX_CHARS:
            _fail("request.request_id", "too long")
    _obj(payload["image"], "request.image", ("data", "mime_type"))
    _str(payload["image"]["data"], "request.image.data", non_empty=True)
    _enum(payload["image"]["mime_type"], "request.image.mime_type", MIME_TYPES)
    _obj(payload["context"], "request.context", ("caption", "alt_text", "nearby_text"))
    for key in ("caption", "alt_text", "nearby_text"):
        _str(payload["context"][key], f"request.context.{key}", nullable=True)

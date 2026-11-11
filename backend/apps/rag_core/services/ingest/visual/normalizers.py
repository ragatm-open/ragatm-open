"""Sérialisation déterministe des artefacts visuels.

Principes communs :

* le statut de certitude n'apparaît que s'il n'est pas ``exact`` ;
* aucune affirmation plus forte que la réponse : direction inconnue sans flèche,
  valeur approximative jamais présentée comme égale, point illisible sans nombre,
  dimension ambiguë sans valeur normalisée ;
* aucune géométrie inférée : ce module sérialise, il ne reconstruit rien ;
* le contexte du document n'est jamais resérialisé comme preuve ; un résumé Gateway
  identique (normalisation exacte) à la légende ou au texte alternatif est omis ;
* texte visible déjà connu (natif/OCR, fourni par l'appelant) omis à l'identique.

Sortie : ``StructuredUnit`` en mémoire et ``VisualTable`` — rien n'est indexé ici.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from apps.llm_router.gateway import vision_contract as V1
from apps.rag_core.services.ingest.extraction.base import StructuredUnit

from .contracts import (
    VisualArtifact,
    VisualContext,
    artifact_meta,
    known_text_set,
    normalize_for_dedup,
    region_artifacts,
)
from .table_adapter import VisualTable, adapt_table_payload

#: Au-delà, un diagramme ou un graphique est découpé en unités cohérentes
#: (voisinage de nœud, série). Seuil interne ; l'indexation redécoupe dans l'unité.
VISUAL_UNIT_MAX_CHARS = 4000


@dataclass
class VisualNormalization:
    units: List[StructuredUnit] = field(default_factory=list)
    tables: List[VisualTable] = field(default_factory=list)
    #: régions mixtes sans preuve exploitable (décoratives, inconnues, vides).
    omitted_regions: int = 0


def _suffix(certainty: Optional[str]) -> str:
    return "" if certainty in (None, "exact") else f" ({certainty})"


def _num(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _names(items: list) -> dict:
    """id -> nom affiché ; l'id est ajouté quand un libellé est absent ou répété."""
    labels = [i["label"] for i in items]
    out = {}
    for item in items:
        label = item["label"]
        if not label:
            out[item["id"]] = item["id"]
        elif labels.count(label) > 1:
            out[item["id"]] = f"{label} [{item['id']}]"
        else:
            out[item["id"]] = label
    return out


def _unit(lines: List[str], artifact: VisualArtifact, unit: str, **extra) -> StructuredUnit:
    meta = artifact_meta(artifact, unit)
    meta["visual"].update(extra)
    return StructuredUnit(text="\n".join(lines).strip(), meta=meta)


def _section(title: str, lines: List[str]) -> List[str]:
    return [f"{title}:", *lines] if lines else []


# --------------------------------------------------------------------------- #
# Photo
# --------------------------------------------------------------------------- #
def normalize_photo(artifact: VisualArtifact, *, context: Optional[VisualContext] = None,
                    known_texts: frozenset = frozenset()) -> List[StructuredUnit]:
    if not artifact.usable:
        return []
    p = artifact.payload
    context = context or VisualContext()
    context_norms = {normalize_for_dedup(context.caption), normalize_for_dedup(context.alt_text)}
    context_norms.discard("")
    lines = ["Visual type: photograph"]
    summary_norm = normalize_for_dedup(artifact.summary)
    if summary_norm and summary_norm not in context_norms \
            and summary_norm != normalize_for_dedup(p["description"]):
        lines.append(f"Summary: {artifact.summary.strip()}")
    lines.append(f"Description: {p['description'].strip()}")
    if p["setting"]:
        lines.append(f"Setting: {p['setting']}")

    objects = []
    for o in p["objects"]:
        details = []
        if o["count"] is not None:
            details.append(f"count: {o['count']}")
        if o["attributes"]:
            details.append(f"attributes: {', '.join(o['attributes'])}")
        text = f"- {o['label']}" + (f" ({'; '.join(details)})" if details else "")
        objects.append(text + _suffix(o["certainty"]))
    lines += _section("Objects", objects)

    lines += _section("Observable relations", [
        f"- {r['subject']} {r['relation']} {r['object']}{_suffix(r['certainty'])}"
        for r in p["observable_relations"]])

    visible, dropped = [], 0
    for t in p["visible_text"]:
        if normalize_for_dedup(t["text"]) in known_texts:
            dropped += 1
            continue
        visible.append(f"- {t['text']}{_suffix(t['certainty'])}")
    lines += _section("Visible text", visible)
    return [_unit(lines, artifact, "photo", visible_text_deduplicated=dropped)]


# --------------------------------------------------------------------------- #
# Diagramme
# --------------------------------------------------------------------------- #
def _edge_line(e: dict, names: dict) -> str:
    s, t = names[e["source_id"]], names[e["target_id"]]
    direction = e["direction"]
    if direction == "forward":
        head = f"- {s} -> {t}"
    elif direction == "bidirectional":
        head = f"- {s} <-> {t}"
    elif direction == "undirected":
        head = f"- {s} -- {t} (undirected)"
    else:
        # Aucune flèche : l'existence du lien est observée, pas son sens.
        head = f"- between {s} and {t} (direction unknown)"
    details = []
    if e["relation"]:
        details.append(f"relation: {e['relation']}")
    if e["label"]:
        details.append(f"label: {e['label']}")
    return head + (f"; {'; '.join(details)}" if details else "") + _suffix(e["certainty"])


def normalize_diagram(artifact: VisualArtifact) -> List[StructuredUnit]:
    if not artifact.usable:
        return []
    p = artifact.payload
    names = _names(p["nodes"])
    header = ["Visual type: diagram"]
    if p["diagram_type"]:
        header.append(f"Diagram type: {p['diagram_type']}")

    def node_line(n):
        return f"- {names[n['id']]}" + (f" [{n['type']}]" if n["type"] else "") + _suffix(n["certainty"])

    def group_line(g):
        return f"- {g['label'] or g['id']}: members {', '.join(names[m] for m in g['members'])}"

    annotations = [f"- {a['text']}{_suffix(a['certainty'])}" for a in p["annotations"]]
    whole = (header + _section("Nodes", [node_line(n) for n in p["nodes"]])
             + _section("Edges", [_edge_line(e, names) for e in p["edges"]])
             + _section("Groups", [group_line(g) for g in p["groups"]])
             + _section("Annotations", annotations))
    if len("\n".join(whole)) <= VISUAL_UNIT_MAX_CHARS:
        return [_unit(whole, artifact, "diagram")]

    units = []
    for n in p["nodes"]:
        edges = [e for e in p["edges"] if n["id"] in (e["source_id"], e["target_id"])]
        groups = [g for g in p["groups"] if n["id"] in g["members"]]
        lines = (header + _section("Node", [node_line(n)])
                 + _section("Edges", [_edge_line(e, names) for e in edges])
                 + _section("Groups", [group_line(g) for g in groups]))
        units.append(_unit(lines, artifact, "diagram_neighborhood", node_id=n["id"]))
    if annotations:
        units.append(_unit(header + _section("Annotations", annotations), artifact,
                           "diagram_annotations"))
    return units


# --------------------------------------------------------------------------- #
# Graphique
# --------------------------------------------------------------------------- #
def _axis_line(a: dict) -> str:
    text = f"- {a['label'] or a['id']}"
    if a["orientation"]:
        text += f" ({a['orientation']})"
    if a["unit"]:
        text += f" [{a['unit']}]"
    text += f"; scale: {a['scale']}"
    rng = a["range"]
    if rng is not None:
        lo = _num(rng["min"]) if rng["min"] is not None else "unknown"
        hi = _num(rng["max"]) if rng["max"] is not None else "unknown"
        if rng["certainty"] == "exact":
            text += f"; range: {lo} to {hi}"
        elif rng["certainty"] == "unreadable":
            text += "; range: unreadable"
        else:
            text += f"; range ≈ {lo} to {hi} ({rng['certainty']})"
    return text + _suffix(a["certainty"])


def _point_line(pt: dict, index: int) -> str:
    name = pt["label"] if pt["label"] is not None else (
        _num(pt["x"]) if pt["x"] is not None else f"point {index + 1}")
    if pt["label"] is not None and pt["x"] is not None and _num(pt["x"]) != pt["label"]:
        name = f"{pt['label']} (x: {_num(pt['x'])})"
    precision = pt["precision"]
    if precision == "unreadable":
        return f"  - {name}: unreadable"
    if precision == "exact_labeled":
        shown = pt["value_text"] if pt["value_text"] is not None else (
            _num(pt["value"]) if pt["value"] is not None else None)
        return f"  - {name} = {shown}" if shown is not None else f"  - {name}: value not given"
    shown = _num(pt["value"]) if pt["value"] is not None else pt["value_text"]
    if shown is None:
        return f"  - {name}: approximate, value not given"
    return f"  - {name} ≈ {shown} (approximate, read from visual)"


def normalize_chart(artifact: VisualArtifact) -> List[StructuredUnit]:
    if not artifact.usable:
        return []
    p = artifact.payload
    header = ["Visual type: chart", f"Chart type: {p['chart_type']}"]
    axes = _section("Axes", [_axis_line(a) for a in p["axes"]])
    series_blocks = []
    for s in p["series"]:
        series_blocks.append([f"Series: {s['label'] or s['id']}",
                              *[_point_line(pt, j) for j, pt in enumerate(s["points"])]])
    trend = ([f"Generated trend summary: {p['trend_summary']}"] if p["trend_summary"] else [])

    whole = header + axes + [line for block in series_blocks for line in block] + trend
    if len("\n".join(whole)) <= VISUAL_UNIT_MAX_CHARS:
        return [_unit(whole, artifact, "chart", generated_summary=bool(trend))]
    units = [_unit(header + axes + trend, artifact, "chart_overview",
                   generated_summary=bool(trend))]
    for s, block in zip(p["series"], series_blocks):
        units.append(_unit(header + axes + block, artifact, "chart_series",
                           series_id=s["id"], generated_summary=False))
    return units


# --------------------------------------------------------------------------- #
# Dessin technique
# --------------------------------------------------------------------------- #
def _free_items(items: list) -> List[str]:
    """``legend``/``annotations`` : forme non détaillée par le contrat gelé (écart 1E)."""
    out = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(f"- {item.strip()}")
        elif isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
            certainty = item.get("certainty")
            out.append(f"- {item['text'].strip()}"
                       + (_suffix(certainty) if certainty in V1.CERTAINTIES else ""))
    return out


def normalize_technical_drawing(artifact: VisualArtifact) -> List[StructuredUnit]:
    if not artifact.usable:
        return []
    p = artifact.payload
    names = _names(p["elements"])
    lines = ["Visual type: technical drawing"]
    if p["drawing_type"]:
        lines.append(f"Drawing type: {p['drawing_type']}")
    lines += _section("Elements", [
        f"- {names[e['id']]}" + (f" [{e['kind']}]" if e["kind"] else "") + _suffix(e["certainty"])
        for e in p["elements"]])

    dims = []
    for d in p["explicit_dimensions"]:
        details = []
        if d["unit"]:
            details.append(f"unit: {d['unit']}")
        # Valeur normalisée seulement si la lecture est exacte ; value_text fait foi.
        if d["value"] is not None and d["certainty"] == "exact":
            details.append(f"normalized value: {_num(d['value'])}")
        if d["between"]:
            details.append(f"between: {', '.join(names[i] for i in d['between'])}")
        dims.append(f"- {d['value_text']}" + (f" ({'; '.join(details)})" if details else "")
                    + _suffix(d["certainty"]))
    lines += _section("Explicit dimensions", dims)

    lines += _section("Symbols", [
        f"- {s['label'] or '[unlabeled symbol]'}" + (f": {s['meaning']}" if s["meaning"] else "")
        + _suffix(s["certainty"]) for s in p["symbols"]])
    lines += _section("Zones", [
        f"- {z['label'] or z['id']}: members {', '.join(names[m] for m in z['members'])}"
        for z in p["zones"]])
    lines += _section("Relations", [
        f"- {names[r['source_id']]} {r['relation']} {names[r['target_id']]}"
        + (f"; label: {r['label']}" if r["label"] else "") + _suffix(r["certainty"])
        for r in p["relations"]])
    lines += _section("Legend", _free_items(p["legend"]))
    lines += _section("Annotations", _free_items(p["annotations"]))
    return [_unit(lines, artifact, "technical_drawing")]


# --------------------------------------------------------------------------- #
# Région texte (mixte)
# --------------------------------------------------------------------------- #
def normalize_text_region(artifact: VisualArtifact, *,
                          known_texts: frozenset = frozenset()) -> List[StructuredUnit]:
    if not artifact.usable:
        return []
    payload = artifact.payload
    kept, dropped = [], 0
    for line in payload["text"].splitlines():
        norm = normalize_for_dedup(line)
        if not norm:
            continue
        if norm in known_texts:
            dropped += 1
            continue
        kept.append(line.strip())
    if not kept:
        return []
    certainty = payload.get("certainty")
    lines = [f"Visual type: text region{_suffix(certainty) if certainty in V1.CERTAINTIES else ''}",
             *kept]
    return [_unit(lines, artifact, "text_region", lines_deduplicated=dropped)]


# --------------------------------------------------------------------------- #
# Aiguillage
# --------------------------------------------------------------------------- #
def normalize_table(artifact: VisualArtifact) -> Optional[VisualTable]:
    if not artifact.usable:
        return None
    table = adapt_table_payload(
        artifact.payload,
        source_digest=artifact.asset_hash,
        source_page=artifact.source_page,
        occurrence=artifact.region_index or 0,
        requires_visual_check=artifact.requires_visual_check,
    )
    table.visual_meta = artifact_meta(artifact, "table")
    return table


def _normalize_one(artifact: VisualArtifact, out: VisualNormalization, *,
                   context: Optional[VisualContext], known: frozenset) -> bool:
    kind = artifact.visual_kind
    if kind == "table":
        table = normalize_table(artifact)
        if table is None:
            return False
        out.tables.append(table)
        return True
    if kind == "photo":
        units = normalize_photo(artifact, context=context, known_texts=known)
    elif kind == "diagram":
        units = normalize_diagram(artifact)
    elif kind == "chart":
        units = normalize_chart(artifact)
    elif kind == "technical_drawing":
        units = normalize_technical_drawing(artifact)
    elif kind == "text":
        units = normalize_text_region(artifact, known_texts=known)
    else:
        units = []  # decorative / unknown : jamais une preuve
    out.units.extend(units)
    return bool(units)


def normalize_artifact(artifact: VisualArtifact, *, context: Optional[VisualContext] = None,
                       known_texts: Iterable[str] = ()) -> VisualNormalization:
    """Point d'entrée : artefact validé -> unités et tableaux en mémoire.

    ``mixed`` : une sortie par région exploitable, AUCUNE unité parent (le résumé du
    parent dupliquerait la preuve des régions).
    """
    known = known_text_set(known_texts)
    out = VisualNormalization()
    if artifact.visual_kind == "mixed":
        for child in region_artifacts(artifact):
            if not _normalize_one(child, out, context=context, known=known):
                out.omitted_regions += 1
        return out
    _normalize_one(artifact, out, context=context, known=known)
    return out

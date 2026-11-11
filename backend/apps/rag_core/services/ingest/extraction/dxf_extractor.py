# backend/apps/rag_core/services/ingest/extraction/dxf_extractor.py
"""
Extracteur DXF documentaire : octets DXF → ezdxf (lecture STRICTE,
en mémoire) → sémantique CAO accessible → `StructuredUnit[]` → `ExtractionResult.units`.

Le support est DOCUMENTAIRE, pas graphique : aucun rendu, aucune géométrie, aucun viewer,
aucune conversion DWG, aucun sous-processus. Les entités géométriques (LINE, POLYLINE,
ARC, CIRCLE, HATCH…) ne produisent jamais de texte — elles sont seulement COMPTÉES. Les
coordonnées brutes ne sont jamais indexées.

Lecture : `ezdxf.read` (ASCII, encodage par `$ACADVER`/`$DWGCODEPAGE`) ou
`Drawing.load(binary_tags_loader)` (DXF binaire). JAMAIS `ezdxf.recover` — il répare
silencieusement un fichier endommagé (2 correctifs observés sur un fichier tronqué) ;
un DXF invalide est refusé `dxf_malformed`. Aucun `doc.audit()` : il MUTE le document.

Limite structurelle `DOCUMENT_DXF_MAX_ENTITIES` (entités des layouts + définitions de
blocs), vérifiée dès le chargement, AVANT toute extraction sémantique
(`ExtractionComplexityExceeded`, `dxf_complexity_limit`).

Unités (déterministes, ordre des layouts par onglets puis ordre document) :
- résumé du dessin (version, unités, layouts, calques, comptes d'entités, blocs, XREF
  déclarées) — émis dès qu'il y a au moins une entité ;
- annotations (TEXT, MTEXT en texte plein, DIMENSION, MULTILEADER) regroupées par
  layout + calque, dans l'ordre du dessin ; calques éteints/gelés conservés et signalés ;
- une unité par instance de bloc (INSERT) portant des ATTRIB utiles (`TAG: valeur`) ;
- une unité par définition de bloc nommé portant du texte (ATTDEF listés par tag,
  jamais de valeur : la valeur vit sur l'instance).

XREF : détectées, décrites (nom, chemin déclaré), stockées — JAMAIS ouvertes, suivies,
résolues ni parsées. `meta.locator = {type: "dxf", path, identifier?}` avec
`identifier = handle:<HANDLE>` pour les instances de bloc (handles DXF stables).
"""

from __future__ import annotations

import io
import logging
import time
from collections import Counter
from importlib.util import find_spec
from typing import Optional

from . import config
from .base import (
    CorruptedFile,
    ExtractionComplexityExceeded,
    ExtractionContext,
    ExtractionResult,
    RejectedFile,
    StructuredUnit,
    build_locator,
)
from .dxf_signature import DXF_MIME, is_binary_dxf

logger = logging.getLogger(__name__)

FORMAT = "dxf"
PARSER = "ezdxf"
COMPLEXITY_CODE = "dxf_complexity_limit"
MALFORMED_CODE = "dxf_malformed"
VERSION_CODE = "dxf_unsupported_version"
DEPENDENCY_CODE = "dxf_dependency_unavailable"

MAX_LAYOUTS_RECORDED = 64
MAX_LAYERS_RECORDED = 200
MAX_LAYER_STATES_RECORDED = 50
MAX_BLOCKS_RECORDED = 200
MAX_XREFS_RECORDED = 64
MAX_ENTITY_TYPES_RECORDED = 64
MAX_SUMMARY_ITEMS = 50
#: Un seul MTEXT peut être gigantesque : borne par entité (le chunker commun découpe
#: ensuite), avec avertissement.
MAX_TEXT_CHARS_PER_ENTITY = 100_000
#: Chemin XREF conservé tel que déclaré, borné (jamais résolu).
MAX_XREF_PATH_CHARS = 512

COMPLEXITY_MESSAGE = (
    "Le dessin DXF dépasse le nombre maximal d'entités autorisé et n'a pas été indexé."
)
MALFORMED_MESSAGE = "Le fichier DXF est invalide ou endommagé et n'a pas pu être analysé."
VERSION_MESSAGE = "La version de ce fichier DXF n'est pas prise en charge."

_ANNOTATION_TYPES = frozenset({"TEXT", "MTEXT", "DIMENSION", "MULTILEADER", "MLEADER"})


def parser_available() -> bool:
    """ezdxf est-il déployé ? Résolu par `find_spec` (aucun import au chargement)."""
    try:
        return find_spec("ezdxf") is not None
    except Exception:  # pragma: no cover - sys.path cassé
        return False


def _clean(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) and value else ""


def _fmt_number(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


# ---------------------------------------------------------------------------
# Chargement STRICT
# ---------------------------------------------------------------------------
def _text_stream(text: str) -> io.StringIO:
    """Flux texte à fins de ligne universelles (`\r\n` et `\r` lus comme `\n`).

    ezdxf lit ligne à ligne et ne retire que le `\n` : sur un DXF AutoCAD/Windows
    (CRLF) le `\r` restait dans chaque valeur (`AC1032\r`), la version n'était pas
    reconnue et le dessin était lu comme un R12 vide — 0 unité sans erreur.
    `newline=None` reproduit `ezdxf.readfile` en mémoire.
    """
    return io.StringIO(text, newline=None)


def _decode_ascii(data: bytes) -> str:
    """Décodage selon l'en-tête : UTF-8 dès R2007 (AC1021), sinon `$DWGCODEPAGE`.
    Erreurs remplacées (U+FFFD) — jamais de substituts invalides vers la base."""
    encoding = "utf-8"
    try:
        from ezdxf.lldxf.validator import dxf_info

        info = dxf_info(_text_stream(data[:16384].decode("latin-1", errors="replace")))
        if info.version < "AC1021" and info.encoding:
            encoding = info.encoding
    except Exception:
        encoding = "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _load_document(data: bytes):
    """(Drawing, is_binary) — lecture stricte, en mémoire, sans réparation."""
    import ezdxf

    if is_binary_dxf(data):
        from ezdxf.document import Drawing
        from ezdxf.lldxf.tagger import binary_tags_loader

        return Drawing.load(binary_tags_loader(data)), True
    return ezdxf.read(_text_stream(_decode_ascii(data))), False


# ---------------------------------------------------------------------------
# Sémantique
# ---------------------------------------------------------------------------
def _units_info(doc) -> tuple[int, Optional[str], Optional[str]]:
    """(code $INSUNITS, nom lisible, abréviation) — rien d'inventé si indéfini."""
    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        code = 0
    if code <= 0:
        return code, None, None
    name = abbrev = None
    try:
        from ezdxf import units as U

        name = U.unit_name(code) or None
        abbrev = U.decode(code) or None
    except Exception:
        pass
    return code, name, abbrev


def _entity_text(e, warnings: list) -> str:
    t = e.dxftype()
    try:
        if t == "TEXT":
            raw = e.plain_text() if hasattr(e, "plain_text") else e.dxf.get("text", "")
        elif t == "MTEXT":
            raw = e.plain_text()
        elif t in ("MULTILEADER", "MLEADER"):
            raw = e.get_mtext_content() if hasattr(e, "get_mtext_content") else ""
        else:
            return ""
    except Exception:
        return ""
    text = _clean(raw)
    if len(text) > MAX_TEXT_CHARS_PER_ENTITY:
        text = text[:MAX_TEXT_CHARS_PER_ENTITY]
        if "dxf_text_truncated" not in warnings:
            warnings.append("dxf_text_truncated")
    return text


def _dimension_line(e, abbrev: Optional[str]) -> str:
    text = _clean(e.dxf.get("text", "") or "")
    measurement = None
    try:
        m = e.dxf.get("actual_measurement", None)
        measurement = float(m) if m is not None else None
    except Exception:
        measurement = None
    # Le code 42 est FACULTATIF : GNU LibreDWG y écrit la sentinelle -1 quand la mesure
    # n'est pas stockée (95 % des cotes des plans DWG réels observés). Une mesure négative n'existe pas :
    # elle vaut « absente », et la mesure géométrique ci-dessous prend le relais.
    if measurement is not None and measurement < 0:
        measurement = None
    if measurement is None:
        try:
            measurement = float(e.get_measurement())
        except Exception:
            measurement = None
    number = None
    if measurement is not None:
        number = _fmt_number(measurement)
        try:
            linear = (int(e.dimtype) & 7) in (0, 1)
        except Exception:
            linear = False
        if linear and abbrev:
            number = f"{number} {abbrev}"
    if text in ("", "<>"):
        value = number
    elif "<>" in text:
        value = _clean(text.replace("<>", number or ""))
    else:
        value = text
    return f"Dimension: {value}" if value else ""


def _bounded(names: list, limit: int) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", … (+{len(names) - limit})"


def _extract(doc, *, is_binary: bool, max_entities: int) -> tuple[list, dict, list]:
    import ezdxf

    warnings: list[str] = []

    def _warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    layout_names = list(doc.layouts.names_in_taborder())
    layouts = [doc.layouts.get(name) for name in layout_names]
    blocks = [b for b in doc.blocks if not b.is_any_layout]
    entity_count = sum(len(lay) for lay in layouts) + sum(len(b) for b in blocks)
    if entity_count > max_entities:
        raise ExtractionComplexityExceeded(
            COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
            meta={"dxf_entity_count": entity_count, "dxf_max_entities": max_entities},
        )

    # Calques et leur état (jamais un motif d'exclusion).
    layer_names: list[str] = []
    layer_state: dict[str, str] = {}
    for layer in doc.layers:
        name = str(layer.dxf.name)
        layer_names.append(name)
        try:
            if layer.is_off():
                layer_state[name] = "off"
            elif layer.is_frozen():
                layer_state[name] = "frozen"
        except Exception:
            pass
    layers_off = [n for n, s in layer_state.items() if s == "off"]
    layers_frozen = [n for n, s in layer_state.items() if s == "frozen"]

    unit_code, unit_name, unit_abbrev = _units_info(doc)
    # Entités que ezdxf ne modélise pas (`DXFTagStorage`, ex. SECTIONOBJECT) : DXF valide mais
    # sans sémantique documentaire — comptées et observées, jamais lues (ni `layer`, ni tags
    # bruts), jamais un motif de rejet. Un type CONNU reste fail-closed.
    unknown_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    block_usage: Counter = Counter()
    units: list[StructuredUnit] = []
    annotation_lines = 0
    block_unit_count = 0

    # Un seul passage par layout : groupes (calque -> lignes) + unités de blocs.
    for lay in layouts:
        lname = str(lay.name)
        groups: dict[str, list[str]] = {}
        block_units: list[StructuredUnit] = []
        for e in lay:
            t = e.dxftype()
            entity_counts[t] += 1
            if not e.dxf.is_supported("layer"):
                unknown_counts[t] += 1
                continue
            layer = str(e.dxf.get("layer", "0") or "0")
            if t == "INSERT":
                bname = str(e.dxf.get("name", "") or "")
                block_usage[bname] += 1
                attrs = []
                try:
                    for a in e.attribs:
                        tag = _clean(str(a.dxf.get("tag", "") or ""))
                        val = _clean(str(a.dxf.get("text", "") or ""))
                        if tag and val:
                            attrs.append((tag, val))
                except Exception:
                    attrs = []
                if not attrs:
                    continue
                lines = ["DXF block", f"Layout: {lname}", f"Layer: {layer}", f"Block: {bname}"]
                lines += [f"{tag}: {val}" for tag, val in attrs]
                block_units.append(StructuredUnit(
                    text="\n".join(lines),
                    meta={
                        "locator": build_locator(
                            type=FORMAT, path=f"Layout:{lname}/Layer:{layer}/Block:{bname}",
                            identifier=f"handle:{e.dxf.handle}" if e.dxf.get("handle") else None,
                        ),
                        "structured_format": FORMAT,
                    },
                ))
                block_unit_count += 1
                continue
            if t not in _ANNOTATION_TYPES:
                continue
            line = _dimension_line(e, unit_abbrev) if t == "DIMENSION" else _entity_text(e, warnings)
            if line:
                groups.setdefault(layer, []).append(line)
        for layer, lines in groups.items():
            head = ["DXF annotations", f"Layout: {lname}", f"Layer: {layer}"]
            if layer in layer_state:
                head.append(f"Layer state: {layer_state[layer]}")
            annotation_lines += len(lines)
            units.append(StructuredUnit(
                text="\n".join(head) + "\n\n" + "\n".join(lines),
                meta={"locator": build_locator(type=FORMAT, path=f"Layout:{lname}/Layer:{layer}"),
                      "structured_format": FORMAT},
            ))
        units.extend(block_units)

    # Définitions de blocs : XREF décrites (jamais ouvertes), texte des blocs nommés.
    xrefs: list[dict] = []
    for b in blocks:
        bname = str(b.name)
        try:
            if b.block.is_xref:
                path = str(b.block.dxf.get("xref_path", "") or "")[:MAX_XREF_PATH_CHARS]
                xrefs.append({"name": bname, "path": path})
                continue
        except Exception:
            pass
        texts: list[str] = []
        attdefs: list[str] = []
        anonymous = bname.startswith("*")
        for e in b:
            t = e.dxftype()
            entity_counts[t] += 1
            if not e.dxf.is_supported("layer"):
                unknown_counts[t] += 1
                continue
            if anonymous:
                continue
            if t == "ATTDEF":
                tag = _clean(str(e.dxf.get("tag", "") or ""))
                if tag:
                    attdefs.append(tag)
            elif t in ("TEXT", "MTEXT"):
                txt = _entity_text(e, warnings)
                if txt:
                    texts.append(txt)
        if anonymous or not texts:
            continue
        head = ["DXF block definition", f"Block: {bname}"]
        if attdefs:
            head.append("Attributes: " + ", ".join(attdefs))
        units.append(StructuredUnit(
            text="\n".join(head) + "\n\n" + "\n".join(texts),
            meta={"locator": build_locator(type=FORMAT, path=f"Block:{bname}"), "structured_format": FORMAT},
        ))

    # Résumé du dessin, en tête — dès qu'il y a une entité.
    block_names = sorted(block_usage)
    type_lines = [f"{t}: {n}" for t, n in sorted(entity_counts.items())]
    if entity_count > 0:
        lines = ["DXF drawing", f"Version: {doc.dxfversion} ({doc.acad_release})"]
        if unit_name:
            lines.append(f"Units: {unit_name}")
        lines.append("Layouts: " + _bounded(layout_names, MAX_SUMMARY_ITEMS))
        if layer_names:
            lines.append("Layers: " + _bounded(layer_names, MAX_SUMMARY_ITEMS))
        lines.append("Entities: " + _bounded(type_lines, MAX_SUMMARY_ITEMS))
        if block_names:
            lines.append("Blocks: " + _bounded(block_names, MAX_SUMMARY_ITEMS))
        for x in xrefs[:MAX_SUMMARY_ITEMS]:
            lines.append(f"Declared XREF: {x['name']} -> {x['path']}")
        if len(xrefs) > MAX_SUMMARY_ITEMS:
            lines.append(f"Declared XREF: … (+{len(xrefs) - MAX_SUMMARY_ITEMS})")
        units.insert(0, StructuredUnit(
            text="\n".join(lines),
            meta={"locator": build_locator(type=FORMAT, path="Drawing"), "structured_format": FORMAT},
        ))

    # Métadonnées bornées.
    if len(layout_names) > MAX_LAYOUTS_RECORDED:
        _warn("dxf_layouts_truncated")
    if len(layer_names) > MAX_LAYERS_RECORDED:
        _warn("dxf_layers_truncated")
    if len(block_names) > MAX_BLOCKS_RECORDED:
        _warn("dxf_blocks_truncated")
    if len(xrefs) > MAX_XREFS_RECORDED:
        _warn("dxf_xrefs_truncated")
    if len(entity_counts) > MAX_ENTITY_TYPES_RECORDED:
        _warn("dxf_entity_types_truncated")
    if unknown_counts:
        _warn("dxf_unknown_entities")
    top_types = sorted(entity_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ENTITY_TYPES_RECORDED]
    top_unknown = sorted(unknown_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ENTITY_TYPES_RECORDED]
    metadata = {
        "format": FORMAT,
        "parser": PARSER,
        "parser_version": ezdxf.__version__,
        "dxf_version": str(doc.dxfversion),
        "dxf_release": str(doc.acad_release),
        "dxf_binary": bool(is_binary),
        "units": unit_code,
        "units_name": unit_name,
        "layouts": layout_names[:MAX_LAYOUTS_RECORDED],
        "layouts_count": len(layout_names),
        "layers": layer_names[:MAX_LAYERS_RECORDED],
        "layers_count": len(layer_names),
        "layers_off": layers_off[:MAX_LAYER_STATES_RECORDED],
        "layers_frozen": layers_frozen[:MAX_LAYER_STATES_RECORDED],
        "entity_count": entity_count,
        "entity_counts": dict(sorted(top_types)),
        "unknown_entity_count": sum(unknown_counts.values()),
        "unknown_entity_counts": dict(sorted(top_unknown)),
        "blocks": block_names[:MAX_BLOCKS_RECORDED],
        "blocks_count": len(block_names),
        "xrefs_declared": xrefs[:MAX_XREFS_RECORDED],
        "xrefs_count": len(xrefs),
        "semantic_unit_count": len(units),
        "annotation_count": annotation_lines,
        "block_unit_count": block_unit_count,
    }
    return units, metadata, warnings


# ---------------------------------------------------------------------------
# Extracteur
# ---------------------------------------------------------------------------
class DxfExtractor:
    """Octets DXF → `ExtractionResult(units=…)` ; `text` reste vide (rail `units` seul)."""

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        data = data or b""
        if not parser_available():
            raise RejectedFile(
                "L'ingestion DXF est activée mais indisponible sur cet environnement "
                "(ezdxf absent). Contactez un administrateur.",
                code=DEPENDENCY_CODE,
            )
        from ezdxf.lldxf.const import DXFError, DXFStructureError, DXFVersionError

        t0 = time.monotonic()
        try:
            doc, is_binary = _load_document(data)
        except DXFVersionError:
            raise CorruptedFile(VERSION_MESSAGE, code=VERSION_CODE)
        except (DXFStructureError, DXFError):
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        except ExtractionComplexityExceeded:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError,
                UnicodeDecodeError, RecursionError, OverflowError, MemoryError):
            # Erreur fatale du parseur sur un fichier hostile : contrôlée, jamais un 500.
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)

        max_entities = max(1, int(config.dxf_max_entities()))
        try:
            units, metadata, warnings = _extract(doc, is_binary=is_binary, max_entities=max_entities)
        except ExtractionComplexityExceeded:
            raise
        except (DXFError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        finally:
            del doc

        result = ExtractionResult(text="", units=units, metadata=metadata)
        for w in warnings:
            result.add_warning(w)
        logger.info(
            "dxf extracted: version=%s entities=%d units=%d layouts=%d layers=%d xrefs=%d "
            "duration_ms=%d warnings=%d",
            metadata["dxf_version"], metadata["entity_count"], len(units),
            metadata["layouts_count"], metadata["layers_count"], metadata["xrefs_count"],
            int((time.monotonic() - t0) * 1000), len(warnings),
        )
        return result.finalize(fmt=FORMAT, mime=DXF_MIME, method=PARSER)

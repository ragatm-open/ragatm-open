# backend/apps/rag_core/services/ingest/extraction/xml_extractor.py
"""
Extracteur XML générique : octets XML → analyse SÉCURISÉE en flux
→ unités sémantiques (`StructuredUnit`) → `ExtractionResult.units`.

Sécurité (non optionnelle) : defusedxml avec `forbid_dtd`, `forbid_entities`,
`forbid_external` — tout DOCTYPE, toute entité (interne, externe, paramètre) et toute
référence externe sont refusés (`unsafe_xml`) ; aucun accès réseau ni fichier ; une
pré-vérification par octets (`xml_security.is_unsafe_xml`) refuse avant même le parseur.
Limites STRUCTURELLES (`DOCUMENT_XML_MAX_DEPTH`, racine = 1 ; `DOCUMENT_XML_MAX_NODES`,
éléments) appliquées PENDANT la lecture en flux (`iterparse`) : dépassement =
`ExtractionComplexityExceeded` (`xml_complexity_limit`), arrêt immédiat, rien d'indexé.

Sémantique (générique, sans schéma métier) :
- `path` déterministe par nom LOCAL (`/Racine/Enfant[2]`), indice `[n]` seulement entre
  frères de même nom ;
- **records répétés** : sous un parent, des enfants de même nom local (≥ 2) forment des
  records ; chacun devient une unité, à sa frontière la plus EXTERNE (aucune unité
  séparée pour les répétitions imbriquées : leur texte est déjà dans le record) ;
- le contenu hors records est regroupé par branche directe sous la racine (attributs et
  texte propres de la racine dans une unité `/Racine`) ; sans aucun record, un petit
  document (≤ 64 éléments) tient dans UNE unité racine ;
- texte canonique autoporteur : `XML path: <chemin>` puis, en ordre document, des
  lignes `chemin/relatif: valeur`, `chemin/relatif/@attribut: valeur`, `text: valeur`
  pour le texte propre de l'unité ; contenu mixte rendu dans l'ordre textuel ;
  commentaires et instructions de traitement jamais indexés ; CDATA = texte ;
  espaces normalisés, valeurs jamais converties ni traduites ;
- `meta.locator = {type: "element", path, identifier?}` (`id` / `xml:id`).

Interdit ici : toute écriture base, embeddings, retrieval, statut de document.
"""

from __future__ import annotations

import io
import logging
import time
from collections import Counter
from importlib.util import find_spec
from typing import Iterator, Optional

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
from .xml_security import UNSAFE_XML_CODE, UNSAFE_XML_MESSAGE, is_unsafe_xml
from .xml_signature import XML_MIME

logger = logging.getLogger(__name__)

FORMAT = "xml"
PARSER = "defusedxml"
COMPLEXITY_CODE = "xml_complexity_limit"
MALFORMED_CODE = "xml_malformed"
DEPENDENCY_CODE = "xml_dependency_unavailable"

XML_ID_ATTR = "{http://www.w3.org/XML/1998/namespace}id"
#: Sans record répété, un document d'au plus N éléments tient dans UNE unité racine ;
#: au-delà, une unité par branche directe sous la racine (déterministe, testé).
SMALL_DOCUMENT_MAX_ELEMENTS = 64
#: Bornes des métadonnées de namespaces (jamais répétées dans le texte canonique).
MAX_NAMESPACES_RECORDED = 64
MAX_NAMESPACE_URI_CHARS = 200
MAX_IDENTIFIER_CHARS = 200

COMPLEXITY_MESSAGE = (
    "Le document XML dépasse les limites de complexité autorisées (profondeur ou nombre "
    "d'éléments) et n'a pas été indexé."
)
MALFORMED_MESSAGE = "Le fichier XML est mal formé et n'a pas pu être analysé."


def parser_available() -> bool:
    """defusedxml est-il déployé ? Résolu par `find_spec` (aucun import au chargement)."""
    try:
        return find_spec("defusedxml") is not None
    except Exception:  # pragma: no cover - sys.path cassé
        return False


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _namespace_of(tag) -> Optional[str]:
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return None


def _norm(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) and value else ""


# ---------------------------------------------------------------------------
# Analyse en flux, bornée
# ---------------------------------------------------------------------------
def _parse(data: bytes, *, max_depth: int, max_nodes: int):
    from xml.etree.ElementTree import ParseError

    import defusedxml
    from defusedxml import DefusedXmlException
    from defusedxml import ElementTree as DET

    root = None
    depth = 0
    max_seen = 0
    nodes = 0
    namespaces: dict[str, str] = {}
    warnings: list[str] = []

    def _warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    try:
        events = DET.iterparse(
            io.BytesIO(data), events=("start-ns", "start", "end"),
            forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
        for event, payload in events:
            if event == "start":
                nodes += 1
                depth += 1
                if depth > max_depth:
                    raise ExtractionComplexityExceeded(
                        COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
                        meta={"xml_limit": "depth", "xml_max_depth": max_depth},
                    )
                if nodes > max_nodes:
                    raise ExtractionComplexityExceeded(
                        COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
                        meta={"xml_limit": "nodes", "xml_max_nodes": max_nodes},
                    )
                if depth > max_seen:
                    max_seen = depth
                if root is None:
                    root = payload
            elif event == "end":
                depth -= 1
            else:  # start-ns
                prefix, uri = payload
                prefix = prefix or ""
                if prefix in namespaces:
                    continue
                if len(namespaces) >= MAX_NAMESPACES_RECORDED:
                    _warn("xml_namespaces_truncated")
                    continue
                if len(uri) > MAX_NAMESPACE_URI_CHARS:
                    _warn("xml_namespace_uri_truncated")
                    uri = uri[:MAX_NAMESPACE_URI_CHARS]
                namespaces[prefix] = uri
    except ExtractionComplexityExceeded:
        raise
    except DefusedXmlException:
        # DTD / entités / référence externe : fermé, message générique, sans détail.
        raise RejectedFile(UNSAFE_XML_MESSAGE, code=UNSAFE_XML_CODE)
    except ParseError as e:
        pos = getattr(e, "position", None)
        where = f" (ligne {pos[0]}, colonne {pos[1]})" if isinstance(pos, tuple) and len(pos) == 2 else ""
        raise CorruptedFile(MALFORMED_MESSAGE + where, code=MALFORMED_CODE)
    except (UnicodeDecodeError, ValueError, OverflowError, RecursionError):
        raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)

    if root is None:
        raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
    return root, {"node_count": nodes, "max_depth": max_seen}, namespaces, warnings, defusedxml.__version__


# ---------------------------------------------------------------------------
# Unités sémantiques
# ---------------------------------------------------------------------------
def _children_with_paths(elem, path: str) -> Iterator[tuple]:
    """(enfant, chemin) en ordre document ; `[n]` seulement entre frères homonymes."""
    children = list(elem)
    counts = Counter(_local(c.tag) for c in children)
    seen: Counter = Counter()
    for c in children:
        name = _local(c.tag)
        seen[name] += 1
        suffix = f"[{seen[name]}]" if counts[name] > 1 else ""
        yield c, f"{path}/{name}{suffix}"


def _mark_records(root) -> tuple[set, int, int]:
    """Records = enfants homonymes (≥ 2) hors record ; frontière la plus externe."""
    record_ids: set = set()
    groups = 0
    total = 0
    stack = [(root, False)]
    while stack:
        elem, inside = stack.pop()
        total += 1
        children = list(elem)
        if not children:
            continue
        counts = Counter(_local(c.tag) for c in children)
        if not inside:
            groups += sum(1 for n in counts.values() if n > 1)
        for c in reversed(children):
            is_rec = (not inside) and counts[_local(c.tag)] > 1
            if is_rec:
                record_ids.add(id(c))
            stack.append((c, inside or is_rec))
    return record_ids, groups, total


def _emit(elem, rel: str, lines: list, record_ids: set, skip_records: bool) -> None:
    prefix = rel + "/" if rel else ""
    for k, v in elem.attrib.items():
        nv = _norm(v)
        if nv:
            lines.append(f"{prefix}@{_local(k)}: {nv}")
    children = list(elem)
    label = rel if rel else "text"
    if children and any(_norm(c.tail) for c in children):
        # Contenu mixte : ordre textuel préservé, contexte structurel = le chemin.
        txt = _norm("".join(elem.itertext()))
        if txt:
            lines.append(f"{label}: {txt}")
        return
    own = _norm(elem.text)
    if own:
        lines.append(f"{label}: {own}")
    for c, cpath in _children_with_paths(elem, prefix.rstrip("/")):
        if skip_records and id(c) in record_ids:
            continue
        _emit(c, cpath.lstrip("/") if not rel else cpath, lines, record_ids, skip_records)


def _identifier(elem) -> Optional[str]:
    for key in ("id", XML_ID_ATTR):
        v = _norm(elem.attrib.get(key))
        if v:
            return v[:MAX_IDENTIFIER_CHARS]
    return None


def _make_unit(elem, path: str, record_ids: set, *, whole: bool) -> Optional[StructuredUnit]:
    lines: list[str] = []
    _emit(elem, "", lines, record_ids, skip_records=not whole)
    if not lines:
        return None
    locator = build_locator(type="element", path=path, identifier=_identifier(elem))
    return StructuredUnit(
        text="\n".join([f"XML path: {path}", *lines]),
        meta={"locator": locator, "structured_format": FORMAT},
    )


def _nested_records(elem, path: str, record_ids: set) -> Iterator[tuple]:
    """Records (frontière externe) sous une branche résiduelle, en ordre document."""
    for c, cpath in _children_with_paths(elem, path):
        if id(c) in record_ids:
            yield c, cpath
        else:
            yield from _nested_records(c, cpath, record_ids)


def _build_units(root) -> tuple[list, int]:
    record_ids, groups, total = _mark_records(root)
    root_path = "/" + _local(root.tag)
    if not record_ids and total <= SMALL_DOCUMENT_MAX_ELEMENTS:
        unit = _make_unit(root, root_path, record_ids, whole=True)
        return ([unit] if unit else []), groups

    units: list = []
    # Attributs et texte propres de la racine (jamais ses enfants).
    own: list[str] = []
    for k, v in root.attrib.items():
        nv = _norm(v)
        if nv:
            own.append(f"@{_local(k)}: {nv}")
    root_text = _norm(root.text)
    if root_text:
        own.append(f"text: {root_text}")
    if own:
        units.append(StructuredUnit(
            text="\n".join([f"XML path: {root_path}", *own]),
            meta={"locator": build_locator(type="element", path=root_path, identifier=_identifier(root)),
                  "structured_format": FORMAT},
        ))
    for child, cpath in _children_with_paths(root, root_path):
        if id(child) in record_ids:
            u = _make_unit(child, cpath, record_ids, whole=True)
            if u:
                units.append(u)
            continue
        u = _make_unit(child, cpath, record_ids, whole=False)
        if u:
            units.append(u)
        for rec, rpath in _nested_records(child, cpath, record_ids):
            ru = _make_unit(rec, rpath, record_ids, whole=True)
            if ru:
                units.append(ru)
    return units, groups


# ---------------------------------------------------------------------------
# Extracteur
# ---------------------------------------------------------------------------
class XmlExtractor:
    """Octets XML → `ExtractionResult(units=…)` ; `text` reste vide (rail `units` seul)."""

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        data = data or b""
        if not parser_available():
            raise RejectedFile(
                "L'ingestion XML est activée mais indisponible sur cet environnement "
                "(defusedxml absent). Contactez un administrateur.",
                code=DEPENDENCY_CODE,
            )
        # Défense en profondeur : refus par octets AVANT tout parseur (même code que
        # les XML Office). defusedxml reste la garantie effective.
        if is_unsafe_xml(data):
            raise RejectedFile(UNSAFE_XML_MESSAGE, code=UNSAFE_XML_CODE)

        max_depth = max(1, int(config.xml_max_depth()))
        max_nodes = max(1, int(config.xml_max_nodes()))
        t0 = time.monotonic()
        root, stats, namespaces, warnings, parser_version = _parse(
            data, max_depth=max_depth, max_nodes=max_nodes,
        )
        units, record_groups = _build_units(root)
        root_tag = _local(root.tag)
        root_ns = _namespace_of(root.tag)
        root.clear()
        del root

        result = ExtractionResult(
            text="",
            units=units,
            metadata={
                "format": FORMAT,
                "parser": PARSER,
                "parser_version": parser_version,
                "root_tag": root_tag,
                "root_namespace": (root_ns[:MAX_NAMESPACE_URI_CHARS] if root_ns else None),
                "namespaces": namespaces,
                "node_count": stats["node_count"],
                "max_depth": stats["max_depth"],
                "record_groups": record_groups,
                "unit_count": len(units),
            },
        )
        for w in warnings:
            result.add_warning(w)
        logger.info(
            "xml extracted: nodes=%d units=%d depth=%d records=%d duration_ms=%d warnings=%d",
            stats["node_count"], len(units), stats["max_depth"], record_groups,
            int((time.monotonic() - t0) * 1000), len(warnings),
        )
        return result.finalize(fmt=FORMAT, mime=XML_MIME, method=PARSER)

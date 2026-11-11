# backend/apps/rag_core/services/ingest/extraction/ifc_extractor.py
"""
Extracteur IFC sémantique : octets IFC-SPF → pré-scan borné →
IfcOpenShell (import PARESSEUX, en mémoire) → hiérarchie spatiale, éléments, propriétés,
quantités, matériaux, systèmes, classifications → `StructuredUnit[]`.

RÈGLES ABSOLUES : aucune géométrie (ni le module géométrique d'IfcOpenShell, ni la
génération de formes, ni itérateur, ni noyau CAO), aucun rendu, aucune coordonnée
indexée, aucun fichier sur disque, aucun processus externe, aucune résolution de
référence externe (documents, classifications, bibliothèques : décrites, jamais ouvertes
ni téléchargées). Preuve statique : `test_ifc_extractor.py`.

Pipeline :
1. en-tête STEP obligatoire (`ISO-10303-21;`) sinon `ifc_malformed` — avant tout import ;
2. pré-scan structurel BORNÉ des déclarations `#n=` en début de ligne (jamais le parseur
   sémantique, jamais un juge de validité) : au-delà de `DOCUMENT_IFC_MAX_ENTITIES`
   → `ifc_complexity_limit` SANS charger IfcOpenShell ;
3. `ifcopenshell.file.from_string` ; schéma non supporté → `ifc_unsupported_schema` ;
   structure invalide / exception native Python → `ifc_malformed` (jamais de réparation) ;
4. compte définitif des entités du modèle (borné, arrêt anticipé) ;
5. traversée sémantique BORNÉE (relations explicitement retenues : agrégation,
   containment, type, matériau, classification, système ; ensembles de visite et
   profondeur maximale contre les cycles).

Stratégie d'unités HYBRIDE (jamais « une entité = une unité ») :
- résumé du modèle ; une unité par site / bâtiment / niveau / espace (propriétés et
  comptes des éléments contenus) ; une unité par ÉLÉMENT RICHE (description, tag,
  propriétés, quantités, matériaux, classifications, systèmes, ou nom + type) ; les
  éléments pauvres sont GROUPÉS par conteneur + classe + type (compte, échantillon de
  noms) ; une unité par TYPE référencé (propriétés du type portées une seule fois,
  l'instance ne cite que le nom du type).
- `meta.locator = {type: "ifc", path: "Projet / Site / Bâtiment / Niveau / Espace /
  Classe / Nom", identifier: "GlobalId:<GlobalId>"}` ; jamais de numéro de page.

Interdit ici : base de données, chunks, embeddings, retrieval.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from importlib.util import find_spec
from typing import Any, Optional

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
from .ifc_signature import IFC_MIME, has_step_header

logger = logging.getLogger(__name__)

FORMAT = "ifc"
PARSER = "ifcopenshell"
COMPLEXITY_CODE = "ifc_complexity_limit"
MALFORMED_CODE = "ifc_malformed"
SCHEMA_CODE = "ifc_unsupported_schema"
DEPENDENCY_CODE = "ifc_dependency_unavailable"

SUPPORTED_SCHEMA_PREFIXES = ("IFC2X3", "IFC4")
MAX_VALUE_CHARS = 2000
MAX_LINES_PER_UNIT = 500
MAX_PATH_DEPTH = 16
MAX_LIST_ITEMS = 32
MAX_GROUP_SAMPLE = 10
MAX_CLASSES_IN_SUMMARY = 20
MAX_CLASS_COUNTS_RECORDED = 64
MAX_EXTERNAL_REFS_RECORDED = 32
MAX_EXTERNAL_REF_CHARS = 200

COMPLEXITY_MESSAGE = (
    "La maquette IFC dépasse le nombre maximal d'entités autorisé et n'a pas été indexée."
)
MALFORMED_MESSAGE = "Le fichier IFC est invalide ou endommagé et n'a pas pu être analysé."
SCHEMA_MESSAGE = "Le schéma de cette maquette IFC n'est pas pris en charge."

_STATEMENT_RE = re.compile(rb"(?m)^[ \t]*#\d+[ \t]*=")
_HEADER_TOKENS = (b"HEADER;", b"FILE_DESCRIPTION", b"FILE_NAME", b"FILE_SCHEMA", b"ENDSEC;", b"DATA;")
_END_MARKER = b"END-ISO-10303-21;"
_SPATIAL_KIND = {
    "IfcSite": "site", "IfcBuilding": "building", "IfcBuildingStorey": "storey", "IfcSpace": "space",
}
_QUANTITY_UNIT_KIND = {
    "IfcQuantityLength": "LENGTHUNIT", "IfcQuantityArea": "AREAUNIT", "IfcQuantityVolume": "VOLUMEUNIT",
    "IfcQuantityWeight": "MASSUNIT", "IfcQuantityTime": "TIMEUNIT",
}
_MEANINGLESS_PREDEFINED = {None, "", "NOTDEFINED", "USERDEFINED"}


def parser_available() -> bool:
    """IfcOpenShell est-il déployé ? Résolu par `find_spec` — AUCUN import au chargement
    (l'import coûte ~115 Mo RSS ; seuls les workers qui traitent un IFC le paient)."""
    try:
        return find_spec("ifcopenshell") is not None
    except Exception:  # pragma: no cover - sys.path cassé
        return False


# ---------------------------------------------------------------------------
# Pré-scan et chargement
# ---------------------------------------------------------------------------
def _prescan_statement_count(data: bytes, cap: int) -> int:
    """Nombre de déclarations STEP `#n=` en DÉBUT de ligne, arrêt dès `cap + 1`.

    Structurel et borné : une chaîne ou un commentaire contenant `#n=` en milieu de
    ligne n'est pas compté ; un commentaire multi-lignes portant `#n=` en début de
    ligne serait sur-compté — dans le sens FERMÉ (refus), jamais dans l'autre. Le
    compte définitif vient du modèle chargé.
    """
    n = 0
    for _ in _STATEMENT_RE.finditer(data):
        n += 1
        if n > cap:
            break
    return n


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _structurally_complete(data: bytes) -> bool:
    """En-tête STEP complet (HEADER/FILE_DESCRIPTION/FILE_NAME/FILE_SCHEMA/DATA) et
    marqueur de fin présent : un fichier tronqué ou sans en-tête est refusé AVANT tout
    import — c'est aussi ce qui évite le chemin d'erreur bruyant du parseur natif."""
    head = data[:16384]
    if not all(tok in head for tok in _HEADER_TOKENS):
        return False
    return _END_MARKER in data[-4096:]


class _ContainedFinalizers:
    """Contention du chemin d'erreur d'IfcOpenShell (§29, reproduit avec
    0.8.5) : la finalisation d'un wrapper partiellement construit lève ``KeyError`` dans
    ``__del__`` ; Python l'imprime sur stderr via ``sys.unraisablehook`` — bruit de
    worker sans conséquence fonctionnelle. Le crochet est remplacé LE TEMPS DE
    L'EXTRACTION IFC uniquement, et seulement pour les objets IfcOpenShell — tout autre
    événement est délégué au crochet précédent. Un ``gc.collect()`` à la sortie force
    les finaliseurs à s'exécuter dans la fenêtre. Aucune vraie erreur du parseur n'est
    masquée : elles lèvent normalement et sont traduites par l'appelant."""

    def __enter__(self):
        import sys

        self._sys = sys
        self._previous = sys.unraisablehook

        def _hook(unraisable):
            obj = getattr(unraisable, "object", None)
            if "ifcopenshell" in (repr(obj) + repr(type(obj))):
                logger.debug("ifcopenshell finalizer noise contained: %r", getattr(unraisable, "exc_value", None))
                return
            self._previous(unraisable)

        sys.unraisablehook = _hook
        return self

    def __exit__(self, *exc):
        import gc

        try:
            gc.collect()
        finally:
            self._sys.unraisablehook = self._previous
        return False


def _load_model(text: str) -> tuple:
    """Import RÉEL d'IfcOpenShell (paresseux) et chargement en mémoire.

    Renvoie ``(modèle, journal du parseur)``. Le journal global d'IfcOpenShell est vidé
    AVANT le chargement (il est partagé par le processus et consommé à la lecture),
    puis relu : toute entrée ``[Error]`` signifie un modèle structurellement invalide
    que le parseur a « réparé » en silence — refusé par l'appelant (échec fermé).
    """
    import ifcopenshell

    ifcopenshell.get_log()  # vide le journal partagé
    model = ifcopenshell.file.from_string(text)
    return model, str(ifcopenshell.get_log() or "")


# ---------------------------------------------------------------------------
# Helpers de valeur / texte
# ---------------------------------------------------------------------------
def _fmt_number(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


class _Budget:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)


def _fmt_value(v: Any, budget: _Budget) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _fmt_number(v)
    if isinstance(v, str):
        s = " ".join(v.split())
        if not s:
            return None
        if len(s) > MAX_VALUE_CHARS:
            budget.warn("ifc_value_truncated")
            s = s[:MAX_VALUE_CHARS] + "…"
        return s
    if isinstance(v, (list, tuple)):
        items = [x for x in (_fmt_value(i, budget) for i in v[:MAX_LIST_ITEMS]) if x]
        if len(v) > MAX_LIST_ITEMS:
            budget.warn("ifc_list_truncated")
        return ", ".join(items) or None
    if isinstance(v, dict):
        items = [f"{k}={x}" for k, x in ((str(k), _fmt_value(x, budget)) for k, x in list(v.items())[:MAX_LIST_ITEMS]) if x]
        return ", ".join(items) or None
    name = getattr(v, "Name", None)
    if isinstance(name, str) and name.strip():
        return " ".join(name.split())
    return None


def _s(v: Any) -> str:
    return " ".join(v.split()) if isinstance(v, str) else ""


def _label(e) -> str:
    """Segment de chemin lisible : LongName, sinon Name, sinon classe."""
    return _s(getattr(e, "LongName", None)) or _s(getattr(e, "Name", None)) or e.is_a()


def _gid(e) -> Optional[str]:
    g = getattr(e, "GlobalId", None)
    return g if isinstance(g, str) and g else None


# ---------------------------------------------------------------------------
# Traversée sémantique
# ---------------------------------------------------------------------------
class _Model:
    """Vues bornées et mémoïsées sur le modèle (un seul passage par famille)."""

    def __init__(self, f, budget: _Budget):
        import ifcopenshell.util.classification as UC
        import ifcopenshell.util.element as UE
        import ifcopenshell.util.system as US
        import ifcopenshell.util.unit as UU

        self.f, self.budget, self.UE, self.UC, self.US, self.UU = f, budget, UE, UC, US, UU
        self._unit_symbols: dict[str, Optional[str]] = {}
        projects = f.by_type("IfcProject")
        self.project = projects[0] if projects else None
        self.project_name = (_s(getattr(self.project, "Name", None)) if self.project else "") or "Project"
        # Chemins spatiaux (agrégation), calculés une fois, protégés contre les cycles.
        self._path_cache: dict[int, str] = {}

    # -- unités de mesure -------------------------------------------------
    def unit_symbol(self, kind: str) -> Optional[str]:
        if kind not in self._unit_symbols:
            sym = None
            try:
                u = self.UU.get_project_unit(self.f, kind)
                sym = self.UU.get_unit_symbol(u) if u else None
            except Exception:
                sym = None
            self._unit_symbols[kind] = sym or None
        return self._unit_symbols[kind]

    # -- hiérarchie ---------------------------------------------------------
    def parent(self, e):
        try:
            p = self.UE.get_aggregate(e)
        except Exception:
            p = None
        if p is None and not e.is_a("IfcSpatialStructureElement"):
            try:
                p = self.UE.get_container(e)
            except Exception:
                p = None
        return p

    def spatial_path(self, e) -> str:
        """« Projet / Site / Bâtiment / Niveau / Espace » jusqu'à `e` inclus."""
        key = e.id()
        if key in self._path_cache:
            return self._path_cache[key]
        labels: list[str] = []
        seen: set[int] = set()
        cur = e
        depth = 0
        while cur is not None and depth < MAX_PATH_DEPTH and cur.id() not in seen:
            seen.add(cur.id())
            if cur.is_a("IfcProject"):
                break
            labels.append(_label(cur))
            cur = self.parent(cur)
            depth += 1
        path = " / ".join([self.project_name] + list(reversed(labels)))
        self._path_cache[key] = path
        return path

    def children(self, e) -> list:
        """Enfants directs : parties agrégées + éléments contenus, ordre déterministe."""
        out = []
        for getter in ("get_parts", "get_contained"):
            fn = getattr(self.UE, getter, None)
            if fn is None:
                continue
            try:
                out.extend(fn(e) or [])
            except Exception:
                continue
        uniq = {c.id(): c for c in out}
        return sorted(uniq.values(), key=lambda c: (c.is_a(), _label(c), _gid(c) or "", c.id()))

    # -- attributs sémantiques ---------------------------------------------
    def psets(self, e, *, qtos: bool) -> dict:
        """Propriétés PROPRES à l'entité (`should_inherit=False`) : celles du type sont
        portées une seule fois par l'unité du type (§56, dédoublonnage) — et cela évite
        une résolution du type par ensemble de propriétés (coût mesuré : ×2)."""
        try:
            raw = (self.UE.get_psets(e, qtos_only=True, should_inherit=False) if qtos
                   else self.UE.get_psets(e, psets_only=True, should_inherit=False))
        except TypeError:  # signature plus ancienne
            try:
                raw = self.UE.get_psets(e, qtos_only=True) if qtos else self.UE.get_psets(e, psets_only=True)
            except Exception:
                return {}
        except Exception:
            return {}
        return raw or {}

    def quantity_lines(self, e) -> list[str]:
        lines = []
        for qname, props in self.psets(e, qtos=True).items():
            units_by_prop: dict[str, Optional[str]] = {}
            qto_id = props.get("id")
            if isinstance(qto_id, int):
                try:
                    qto = self.f.by_id(qto_id)
                    for q in getattr(qto, "Quantities", None) or []:
                        sym = None
                        own = getattr(q, "Unit", None)
                        if own is not None:
                            try:
                                sym = self.UU.get_unit_symbol(own)
                            except Exception:
                                sym = None
                        if not sym:
                            kind = _QUANTITY_UNIT_KIND.get(q.is_a())
                            sym = self.unit_symbol(kind) if kind else None
                        units_by_prop[_s(getattr(q, "Name", None))] = sym
                except Exception:
                    pass
            for pname, val in props.items():
                if pname == "id":
                    continue
                txt = _fmt_value(val, self.budget)
                if txt is None:
                    continue
                sym = units_by_prop.get(pname)
                lines.append(f"{qname}.{pname}: {txt}{' ' + sym if sym else ''}")
        return lines

    def property_lines(self, e) -> list[str]:
        lines = []
        for pname_set, props in self.psets(e, qtos=False).items():
            for pname, val in props.items():
                if pname == "id":
                    continue
                txt = _fmt_value(val, self.budget)
                if txt is not None:
                    lines.append(f"{pname_set}.{pname}: {txt}")
        return lines

    def materials(self, e) -> list[str]:
        try:
            m = self.UE.get_material(e)
        except Exception:
            return []
        return self._material_names(m)[:MAX_LIST_ITEMS]

    def _material_names(self, m, depth: int = 0) -> list[str]:
        if m is None or depth > 3:
            return []
        names: list[str] = []
        if m.is_a("IfcMaterial"):
            n = _s(getattr(m, "Name", None))
            return [n] if n else []
        for attr in ("ForLayerSet", "ForProfileSet"):
            inner = getattr(m, attr, None)
            if inner is not None:
                return self._material_names(inner, depth + 1)
        for attr in ("MaterialLayers", "MaterialConstituents", "MaterialProfiles", "Materials"):
            items = getattr(m, attr, None)
            if items:
                for it in list(items)[:MAX_LIST_ITEMS]:
                    mat = getattr(it, "Material", None) if not it.is_a("IfcMaterial") else it
                    names.extend(self._material_names(mat, depth + 1))
                break
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def classifications(self, e) -> list[str]:
        out = []
        try:
            refs = self.UC.get_references(e) or []
        except Exception:
            refs = []
        for r in list(refs)[:MAX_LIST_ITEMS]:
            system = ""
            try:
                c = self.UC.get_classification(r)
                system = _s(getattr(c, "Name", None)) if c is not None else ""
            except Exception:
                system = ""
            ident = _s(getattr(r, "Identification", None)) or _s(getattr(r, "ItemReference", None))
            name = _s(getattr(r, "Name", None))
            parts = [p for p in (system, ident, name) if p]
            if parts:
                out.append(" ".join(parts))
        return out

    def systems(self, e) -> list[str]:
        try:
            return [n for n in (_s(getattr(s, "Name", None)) for s in (self.US.get_element_systems(e) or [])) if n][:MAX_LIST_ITEMS]
        except Exception:
            return []

    def type_of(self, e):
        try:
            return self.UE.get_type(e)
        except Exception:
            return None


def _identity_lines(e, budget: _Budget) -> list[str]:
    lines = []
    for attr in ("Name", "LongName", "Description", "ObjectType", "Tag"):
        raw = getattr(e, attr, None)  # un seul accès natif par attribut (coût mesuré)
        v = _fmt_value(raw, budget) if isinstance(raw, str) else None
        if v:
            lines.append(f"{attr}: {v}")
    pt = getattr(e, "PredefinedType", None)
    if isinstance(pt, str) and pt not in _MEANINGLESS_PREDEFINED:
        lines.append(f"PredefinedType: {pt}")
    return lines


def _bounded_lines(lines: list[str], budget: _Budget) -> list[str]:
    if len(lines) > MAX_LINES_PER_UNIT:
        budget.warn("ifc_properties_truncated")
        return lines[:MAX_LINES_PER_UNIT]
    return lines


def _unit(kind_line: str, head: list[str], body: list[str], *, path: str, gid: Optional[str], budget: _Budget) -> StructuredUnit:
    body = _bounded_lines(body, budget)
    text = "\n".join([kind_line, *head] + ([""] + body if body else []))
    return StructuredUnit(
        text=text,
        meta={"locator": build_locator(type=FORMAT, path=path, identifier=f"GlobalId:{gid}" if gid else None),
              "structured_format": FORMAT},
    )


def _extract(f, budget: _Budget, *, max_entities: int) -> tuple[list, dict]:
    import ifcopenshell

    # Compte définitif, borné (arrêt anticipé).
    entity_count = 0
    for _ in f:
        entity_count += 1
        if entity_count > max_entities:
            raise ExtractionComplexityExceeded(
                COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
                meta={"ifc_entity_count": entity_count, "ifc_max_entities": max_entities},
            )

    M = _Model(f, budget)
    products = f.by_type("IfcProduct")
    spatial_all = [p for p in products if p.is_a() in _SPATIAL_KIND]
    counts = Counter(p.is_a() for p in spatial_all)
    elements = [p for p in products if p.is_a("IfcElement")]
    element_class_counts = Counter(e.is_a() for e in elements)

    units: list[StructuredUnit] = []
    stats = {"rich": 0, "grouped": 0, "spatial": 0, "types": 0}

    # -- éléments : conteneur, richesse, type ----------------------------------
    by_container: dict[Optional[int], list] = {}
    type_instances: Counter = Counter()
    type_objects: dict[int, Any] = {}
    for e in elements:
        try:
            c = M.UE.get_container(e)
        except Exception:
            c = None
        by_container.setdefault(c.id() if c is not None else None, []).append(e)

    def element_lines(e) -> tuple[list[str], bool, str]:
        """(lignes, riche ?, nom du type)."""
        ident = _identity_lines(e, budget)
        t = M.type_of(e)
        tname = _s(getattr(t, "Name", None)) if t is not None else ""
        if t is not None:
            type_instances[t.id()] += 1
            type_objects[t.id()] = t
        props = M.property_lines(e)
        qtos = M.quantity_lines(e)
        mats = M.materials(e)
        cls = M.classifications(e)
        systems = M.systems(e)
        has_name = bool(_s(getattr(e, "Name", None)))
        strong = bool(props or qtos or mats or cls or systems) or any(
            ln.startswith(("Description:", "Tag:")) for ln in ident)
        rich = strong or (has_name and (bool(tname) or any(ln.startswith(("ObjectType:", "PredefinedType:")) for ln in ident)))
        lines = list(ident)
        if tname:
            lines.append(f"Type: {tname}")
        lines += props + qtos
        lines += [f"Material: {m}" for m in mats]
        lines += [f"Classification: {c}" for c in cls]
        lines += [f"System: {s}" for s in systems]
        return lines, rich, tname

    def emit_elements(container, container_path: str) -> None:
        elems = by_container.get(container.id() if container is not None else None, [])
        if not elems:
            return
        elems = sorted(elems, key=lambda x: (x.is_a(), _s(getattr(x, "Name", None)), _gid(x) or "", x.id()))
        groups: dict[tuple[str, str], list] = {}
        for e in elems:
            lines, rich, tname = element_lines(e)
            if rich:
                gid = _gid(e)
                name = _s(getattr(e, "Name", None)) or gid or e.is_a()
                head = [f"Path: {container_path}", f"Class: {e.is_a()}"]
                if gid:
                    head.append(f"GlobalId: {gid}")
                units.append(_unit("IFC element", head, lines, path=f"{container_path} / {e.is_a()} / {name}",
                                   gid=gid, budget=budget))
                stats["rich"] += 1
            else:
                groups.setdefault((e.is_a(), tname), []).append(e)
        for (cls_name, tname), members in groups.items():
            head = [f"Path: {container_path}", f"Class: {cls_name}"]
            if tname:
                head.append(f"Type: {tname}")
            head.append(f"Count: {len(members)}")
            names = [n for n in (_s(getattr(m, "Name", None)) for m in members) if n]
            body: list[str] = []
            if names:
                sample = names[:MAX_GROUP_SAMPLE]
                more = f", … (+{len(names) - MAX_GROUP_SAMPLE})" if len(names) > MAX_GROUP_SAMPLE else ""
                body.append("Names: " + ", ".join(sample) + more)
            path = f"{container_path} / {cls_name}" + (f" / {tname}" if tname else "")
            units.append(_unit("IFC element group", head, body, path=path, gid=None, budget=budget))
            stats["grouped"] += 1

    # -- hiérarchie spatiale (DFS depuis le projet, ordre déterministe) ----------
    visited: set[int] = set()

    def spatial_unit(sp) -> None:
        kind = _SPATIAL_KIND.get(sp.is_a())
        if kind is None:
            return
        path = M.spatial_path(sp)
        parent_path = path.rsplit(" / ", 1)[0] if " / " in path else M.project_name
        head = [f"Path: {parent_path}"]
        body = _identity_lines(sp, budget) + M.property_lines(sp) + M.quantity_lines(sp)
        body += [f"Classification: {c}" for c in M.classifications(sp)]
        kids = M.children(sp)
        kid_counts = Counter(k.is_a() for k in kids)
        spaces = sum(1 for k in kids if k.is_a("IfcSpace"))
        if kind in ("building", "storey") and spaces:
            body.append(f"Spaces: {spaces}")
        for cls_name, n in sorted(kid_counts.items()):
            if cls_name in _SPATIAL_KIND:
                continue
            body.append(f"{cls_name}: {n}")
        units.append(_unit(f"IFC {kind}", head, body, path=path, gid=_gid(sp), budget=budget))
        stats["spatial"] += 1
        emit_elements(sp, path)

    def dfs(node, depth: int) -> None:
        if depth > MAX_PATH_DEPTH or node.id() in visited:
            return
        visited.add(node.id())
        if node.is_a() in _SPATIAL_KIND:
            spatial_unit(node)
        for child in M.children(node):
            if child.is_a("IfcSpatialStructureElement") or child.is_a() in _SPATIAL_KIND:
                dfs(child, depth + 1)

    if M.project is not None:
        dfs(M.project, 0)
    # Conteneurs spatiaux hors du projet (modèle incomplet) : parcourus quand même.
    for sp in sorted(spatial_all, key=lambda x: (x.is_a(), _label(x), _gid(x) or "", x.id())):
        if sp.id() not in visited:
            dfs(sp, 0)
    # Éléments non contenus : chemin de repli déterministe.
    emit_elements(None, f"{M.project_name} / Uncontained")

    # -- types référencés ---------------------------------------------------------
    for tid, n in sorted(type_instances.items(), key=lambda kv: (type_objects[kv[0]].is_a(), _label(type_objects[kv[0]]), kv[0])):
        t = type_objects[tid]
        tname = _s(getattr(t, "Name", None)) or _gid(t) or t.is_a()
        head = [f"Class: {t.is_a()}", f"Name: {tname}", f"Instances: {n}"]
        body = [ln for ln in _identity_lines(t, budget) if not ln.startswith("Name:")]
        body += M.property_lines(t) + [f"Material: {m}" for m in M.materials(t)]
        body += [f"Classification: {c}" for c in M.classifications(t)]
        units.append(_unit("IFC type", head, body, path=f"Types / {t.is_a()} / {tname}", gid=_gid(t), budget=budget))
        stats["types"] += 1

    # -- références externes : décrites, jamais résolues -------------------------
    ext_refs: list[str] = []
    ext_count = 0
    for cls_name in ("IfcExternalReference", "IfcClassification"):
        try:
            for r in f.by_type(cls_name):
                loc = getattr(r, "Location", None)
                if isinstance(loc, str) and loc.strip():
                    ext_count += 1
                    if len(ext_refs) < MAX_EXTERNAL_REFS_RECORDED:
                        ext_refs.append(loc.strip()[:MAX_EXTERNAL_REF_CHARS])
        except Exception:
            continue

    # -- résumé du modèle, en tête ---------------------------------------------
    if M.project is not None or products:
        head = [f"Schema: {f.schema}", f"Project: {M.project_name}"]
        sites = [s for s in spatial_all if s.is_a("IfcSite")]
        if len(sites) == 1:
            head.append(f"Site: {_label(sites[0])}")
        head += [f"Sites: {counts['IfcSite']}", f"Buildings: {counts['IfcBuilding']}",
                 f"Storeys: {counts['IfcBuildingStorey']}", f"Spaces: {counts['IfcSpace']}",
                 f"Products: {len(products)}"]
        body = ["Main element classes:"] + [
            f"{c}: {n}" for c, n in sorted(element_class_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CLASSES_IN_SUMMARY]
        ] if element_class_counts else []
        units.insert(0, _unit("IFC model", head, body, path="Model", gid=_gid(M.project) if M.project else None, budget=budget))

    if len(element_class_counts) > MAX_CLASS_COUNTS_RECORDED:
        budget.warn("ifc_class_counts_truncated")
    metadata = {
        "format": FORMAT,
        "parser": PARSER,
        "parser_version": str(ifcopenshell.version),
        "schema": str(f.schema),
        "schema_identifier": str(getattr(f, "schema_identifier", f.schema)),
        "project_name": M.project_name if M.project is not None else None,
        "entity_count": entity_count,
        "product_count": len(products),
        "element_count": len(elements),
        "sites_count": counts["IfcSite"],
        "buildings_count": counts["IfcBuilding"],
        "storeys_count": counts["IfcBuildingStorey"],
        "spaces_count": counts["IfcSpace"],
        "element_class_counts": dict(sorted(element_class_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CLASS_COUNTS_RECORDED]),
        "rich_element_units": stats["rich"],
        "grouped_element_units": stats["grouped"],
        "spatial_summary_units": stats["spatial"],
        "type_units": stats["types"],
        "semantic_unit_count": len(units),
        "external_references_count": ext_count,
        "external_references_sample": ext_refs,
    }
    return units, metadata


# ---------------------------------------------------------------------------
# Extracteur
# ---------------------------------------------------------------------------
class IfcExtractor:
    """Octets IFC → `ExtractionResult(units=…)` ; `text` reste vide (rail `units` seul)."""

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        data = data or b""
        if not parser_available():
            raise RejectedFile(
                "L'ingestion IFC est activée mais indisponible sur cet environnement "
                "(IfcOpenShell absent). Contactez un administrateur.",
                code=DEPENDENCY_CODE,
            )
        if not has_step_header(data) or not _structurally_complete(data):
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)

        max_entities = max(1, int(config.ifc_max_entities()))
        prescan = _prescan_statement_count(data, max_entities)
        if prescan > max_entities:
            raise ExtractionComplexityExceeded(
                COMPLEXITY_MESSAGE, code=COMPLEXITY_CODE,
                meta={"ifc_prescan_count": prescan, "ifc_max_entities": max_entities},
            )

        t0 = time.monotonic()
        budget = _Budget()
        with _ContainedFinalizers():
            return self._extract_contained(data, budget=budget, max_entities=max_entities, prescan=prescan, t0=t0)

    def _extract_contained(self, data: bytes, *, budget: _Budget, max_entities: int, prescan: int, t0: float) -> ExtractionResult:
        try:
            f, parser_log = _load_model(_decode(data))
        except ExtractionComplexityExceeded:
            raise
        except Exception as e:  # traduction FERMÉE de toute erreur du parseur natif
            if type(e).__name__ == "SchemaError" or "schema" in str(e).lower():
                raise CorruptedFile(SCHEMA_MESSAGE, code=SCHEMA_CODE)
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        if "[Error]" in parser_log:
            # Le parseur a rencontré une structure invalide et l'a ignorée : jamais de
            # réparation silencieuse, le modèle est refusé. Le journal reste côté serveur.
            logger.warning("ifc parser reported errors (rejected): %s", parser_log[:500].replace("\n", " | "))
            del f
            raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        if "[Warning]" in parser_log:
            budget.warn("ifc_parser_warnings")
        try:
            schema = str(getattr(f, "schema", "") or "").upper()
            if not schema.startswith(SUPPORTED_SCHEMA_PREFIXES):
                raise CorruptedFile(SCHEMA_MESSAGE, code=SCHEMA_CODE)
            try:
                units, metadata = _extract(f, budget, max_entities=max_entities)
            except (ExtractionComplexityExceeded, CorruptedFile):
                raise
            except Exception:
                logger.warning("ifc semantic extraction failed (controlled)", exc_info=True)
                raise CorruptedFile(MALFORMED_MESSAGE, code=MALFORMED_CODE)
        finally:
            del f

        metadata["prescan_count"] = prescan
        result = ExtractionResult(text="", units=units, metadata=metadata)
        for w in budget.warnings:
            result.add_warning(w)
        logger.info(
            "ifc extracted: schema=%s entities=%d products=%d units=%d rich=%d grouped=%d "
            "spatial=%d types=%d ext_refs=%d duration_ms=%d warnings=%d",
            metadata["schema"], metadata["entity_count"], metadata["product_count"], len(units),
            metadata["rich_element_units"], metadata["grouped_element_units"],
            metadata["spatial_summary_units"], metadata["type_units"], metadata["external_references_count"],
            int((time.monotonic() - t0) * 1000), len(budget.warnings),
        )
        return result.finalize(fmt=FORMAT, mime=IFC_MIME, method=PARSER)

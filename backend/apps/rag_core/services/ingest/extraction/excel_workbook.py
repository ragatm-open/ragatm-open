# backend/apps/rag_core/services/ingest/extraction/excel_workbook.py
from __future__ import annotations

"""
Pré-passe STDLIB sur un classeur `.xlsx` — sécurité + structure.

Deux raisons d'exister, toutes deux établies par le spike E1
(`docs/reports/SPIKE_E1_OPENPYXL_RAGATM.md`) :

1. **Sécurité.** Les gardes du lot E0 (`zip_security`, `xml_security`) doivent
   s'appliquer AVANT qu'openpyxl ne touche l'archive. Et la taille de
   `xl/sharedStrings.xml` — qui pilote À ELLE SEULE la mémoire d'openpyxl
   (RSS ≈ 45 Mo + 3,5 × taille) — est DÉCLARÉE dans le répertoire central du ZIP :
   on la lit sans décompresser un octet, donc on peut refuser avant tout parsing.

2. **Structure.** Le mode `read_only` d'openpyxl ne donne NI cellules fusionnées,
   NI tables déclarées, NI commentaires, NI graphiques (mesuré : `AttributeError`).
   Ces informations vivent dans des parties XML séparées, qu'on lit ici.

C'est aussi ici qu'on **scanne les formules** : `data_only=True` renvoie `None` pour
une formule sans valeur en cache — EXACTEMENT comme pour une cellule vide. Sans ce
scan, on indexerait des colonnes vides là où l'utilisateur voit des nombres. Le scan
XML en flux coûte +19 % du temps de lecture ; une seconde ouverture openpyxl en
coûterait +108 % (mesuré). D'où ce module.

Rien n'est jamais extrait sur disque. Aucune macro n'est lue. Aucun lien suivi.
"""

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from . import config
from .base import CorruptedFile, ExtractionLimitExceeded, RejectedFile
from .xml_security import reject_unsafe_xml
from .zip_security import guard_zip_archive, read_bounded_entry

logger = logging.getLogger(__name__)

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

SHARED_STRINGS_PART = "xl/sharedStrings.xml"
WORKBOOK_PART = "xl/workbook.xml"
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"

# Bornes de lecture des petites parties structurelles (anti zip-bomb ciblée).
_MAX_WORKBOOK_XML = 8 * 1024 * 1024
_MAX_TABLE_XML = 1 * 1024 * 1024
_MAX_SHEET_HEAD = 64 * 1024          # suffit très largement pour <dimension>

# Le scan des feuilles lit le XML par blocs : jamais tout le fichier en mémoire.
_SCAN_CHUNK = 1 << 20                # 1 Mio

# Bornes des listes remontées en métadonnées (anti-explosion de meta).
MAX_UNCACHED_CELLS_REPORTED = 20
MAX_MERGED_RANGES_REPORTED = 50
#: Les fusions sont COLLECTÉES bien au-delà de ce qui est rapporté : la reconstruction
#: des en-têtes multi-lignes (P1.1) en a besoin. Un tableau financier en porte des
#: dizaines. Borne large mais dure — un classeur pathologique ne doit pas faire exploser
#: la mémoire.
MAX_MERGED_RANGES_COLLECTED = 5_000
MAX_DEFINED_NAMES_REPORTED = 50

# --------------------------------------------------------------------------- #
# Codes d'avertissement STABLES des dégradations structurelles (M2).
# Stables = exposables en API, testables, journalisables. Une perte d'information
# structurelle doit porter un code ; un `return {}` muet est un mensonge par omission.
# --------------------------------------------------------------------------- #
WARN_WORKBOOK_RELS_UNREADABLE = "workbook_relationships_unreadable"
WARN_MERGED_RANGES_TRUNCATED = "excel_merged_ranges_truncated"
WARN_MERGED_RANGE_INVALID = "excel_merged_range_invalid"
WARN_SHEET_UNREADABLE = "excel_sheet_unreadable"


@dataclass
class SheetInfo:
    """Une feuille, vue depuis la pré-passe (avant tout openpyxl)."""

    name: str
    index: int                       # 1-based, ordre du classeur
    state: str = "visible"           # visible | hidden | veryHidden
    part: str = ""                   # xl/worksheets/sheetN.xml
    declared_dimension: Optional[str] = None   # ⚠️ NON FIABLE (cf. E1 §10)
    declared_cells: int = 0          # cellules impliquées par la dimension déclarée
    declared_cols: int = 0           # largeur déclarée (colonnes) — sert à repérer un fantôme
    merged_ranges: List[str] = field(default_factory=list)
    formula_count: int = 0
    formula_cached_count: int = 0
    uncached_formula_cells: List[str] = field(default_factory=list)

    @property
    def hidden(self) -> bool:
        return self.state in ("hidden", "veryHidden")


@dataclass
class WorkbookInventory:
    """Tout ce que la pré-passe sait du classeur, avant qu'openpyxl ne l'ouvre."""

    sheets: List[SheetInfo] = field(default_factory=list)
    defined_names: List[str] = field(default_factory=list)
    tables: List[dict] = field(default_factory=list)      # {name, ref, sheet_part}
    shared_strings_bytes: int = 0
    uncompressed_bytes: int = 0
    charts: int = 0
    images: int = 0
    comments: int = 0
    external_links: int = 0
    ole_objects: int = 0
    warnings: List[str] = field(default_factory=list)

    # -- dégradations structurelles (M2) ----------------------------------
    #: Relations illisibles : aucun scan XML n'a pu être fait (formules, fusions,
    #: dimensions). L'extraction reste possible mais partielle.
    relationships_unreadable: bool = False
    #: Cause humainement lisible de la première dégradation rencontrée.
    degraded_cause: str = ""
    #: Budget de fusions dépassé sur au moins une feuille.
    merged_ranges_truncated: bool = False
    merged_truncated_sheets: List[str] = field(default_factory=list)
    #: Plages fusionnées non parsables, écartées de la détection d'en-tête.
    invalid_merged_ranges: List[str] = field(default_factory=list)

    @property
    def structurally_degraded(self) -> bool:
        """Vrai dès qu'une information structurelle a été perdue."""
        return bool(self.relationships_unreadable
                    or self.merged_ranges_truncated
                    or self.invalid_merged_ranges)

    # -- agrégats formules (document) ------------------------------------
    @property
    def formula_count(self) -> int:
        return sum(s.formula_count for s in self.sheets)

    @property
    def formula_cached_count(self) -> int:
        return sum(s.formula_cached_count for s in self.sheets)

    @property
    def formula_uncached_count(self) -> int:
        return self.formula_count - self.formula_cached_count

    @property
    def uncached_formula_cells(self) -> List[str]:
        out: List[str] = []
        for s in self.sheets:
            for ref in s.uncached_formula_cells:
                out.append(f"{s.name}!{ref}")
                if len(out) >= MAX_UNCACHED_CELLS_REPORTED:
                    return out
        return out

    @property
    def merged_ranges(self) -> List[str]:
        """Échantillon BORNÉ, pour les métadonnées. La détection d'en-tête, elle, lit
        `SheetInfo.merged_ranges` en entier."""
        out: List[str] = []
        for s in self.sheets:
            for r in s.merged_ranges:
                out.append(f"{s.name}!{r}")
                if len(out) >= MAX_MERGED_RANGES_REPORTED:
                    return out
        return out

    def add_warning(self, code: str) -> None:
        if code and code not in self.warnings:
            self.warnings.append(code)

    def mark_relationships_unreadable(self, cause: str) -> None:
        """Relations du classeur illisibles : connaissance structurelle perdue.

        Positionne un avertissement stable ET conserve la cause. L'extraction peut se
        poursuivre (openpyxl lit souvent le fichier quand même), mais elle est
        **dégradée** et doit être signalée comme telle, jamais annoncée complète.
        """
        self.relationships_unreadable = True
        if not self.degraded_cause:
            self.degraded_cause = cause
        self.add_warning(WARN_WORKBOOK_RELS_UNREADABLE)

    def mark_merged_ranges_truncated(self, sheet_name: str) -> None:
        """Budget de fusions dépassé : des fusions ont été écartées."""
        self.merged_ranges_truncated = True
        if sheet_name and sheet_name not in self.merged_truncated_sheets:
            self.merged_truncated_sheets.append(sheet_name)
        self.add_warning(WARN_MERGED_RANGES_TRUNCATED)

    def mark_invalid_merged_range(self, sheet_name: str, ref: str) -> None:
        """Plage fusionnée non parsable : écartée de la reconstruction d'en-tête."""
        self.invalid_merged_ranges.append(f"{sheet_name}!{ref}"[:120])
        self.add_warning(WARN_MERGED_RANGE_INVALID)


# ---------------------------------------------------------------------
# Helpers de plage
# ---------------------------------------------------------------------
_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")


def col_index(letters: str) -> int:
    """`A` -> 1, `Z` -> 26, `AA` -> 27."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letter(idx: int) -> str:
    """1 -> `A`, 27 -> `AA`."""
    out = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def parse_range(ref: str) -> Optional[tuple]:
    """`A1:F28` -> (1, 1, 6, 28) = (col_min, row_min, col_max, row_max). None si illisible."""
    if not ref:
        return None
    parts = ref.replace("$", "").upper().split(":")
    try:
        m1 = _REF_RE.match(parts[0])
        if not m1:
            return None
        c1, r1 = col_index(m1.group(1)), int(m1.group(2))
        if len(parts) == 1:
            return (c1, r1, c1, r1)
        m2 = _REF_RE.match(parts[1])
        if not m2:
            return None
        c2, r2 = col_index(m2.group(1)), int(m2.group(2))
        return (min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------
# Scan d'une feuille : dimension déclarée + fusions + formules
# ---------------------------------------------------------------------
_DIMENSION_RE = re.compile(rb'<dimension\s+ref="([^"]+)"')
_MERGE_RE = re.compile(rb'<mergeCell\s+ref="([^"]+)"')
# Une cellule complète : `<c .../>` ou `<c ...>…</c>`.
_CELL_RE = re.compile(rb"<c\b[^>]*?/>|<c\b[^>]*?>.*?</c>", re.DOTALL)
_CELL_REF_RE = re.compile(rb'\br="([A-Z]+\d+)"')
_HAS_FORMULA_RE = re.compile(rb"<f[\s/>]")
_HAS_VALUE_RE = re.compile(rb"<v>")


def _scan_sheet(zf: zipfile.ZipFile, sheet: SheetInfo,
                inv: Optional["WorkbookInventory"] = None) -> None:
    """Scanne le XML d'une feuille EN FLUX : dimension, fusions, formules.

    Ne matérialise jamais tout le XML (les feuilles peuvent peser des dizaines de Mo
    décompressées). Le tampon est purgé au fur et à mesure des `<c>` complets.
    """
    try:
        raw = zf.open(sheet.part, "r")
    except KeyError:
        raise CorruptedFile(
            f"Classeur invalide : feuille « {sheet.name} » introuvable dans l'archive.",
            code="excel_corrupted",
        )
    except Exception as e:
        # `from e` : sans lui, un MemoryError ou un bug de lecture ZIP devient un
        # « classeur corrompu » indébogable. La cause reste attachée.
        raise CorruptedFile(
            "Classeur Excel illisible ou corrompu.", code="excel_corrupted") from e

    head_checked = False
    buf = b""
    try:
        with raw:
            while True:
                chunk = raw.read(_SCAN_CHUNK)
                if not chunk:
                    break
                buf += chunk

                if not head_checked:
                    # La garde XML s'applique AU DÉBUT du flux : une déclaration
                    # DOCTYPE/ENTITY ne peut apparaître qu'avant l'élément racine.
                    reject_unsafe_xml(buf[:_MAX_SHEET_HEAD], part_name=sheet.part)
                    m = _DIMENSION_RE.search(buf[:_MAX_SHEET_HEAD])
                    if m:
                        sheet.declared_dimension = m.group(1).decode("ascii", "replace")
                        box = parse_range(sheet.declared_dimension)
                        if box:
                            c1, r1, c2, r2 = box
                            sheet.declared_cols = max(0, (c2 - c1 + 1))
                            sheet.declared_cells = sheet.declared_cols * max(0, (r2 - r1 + 1))
                    head_checked = True

                for m in _MERGE_RE.finditer(buf):
                    if len(sheet.merged_ranges) < MAX_MERGED_RANGES_COLLECTED:
                        sheet.merged_ranges.append(m.group(1).decode("ascii", "replace"))
                    elif inv is not None:
                        # Plafond atteint : des fusions sont écartées. Le dire —
                        # un en-tête fusionné perdu dégrade la détection d'en-tête.
                        inv.mark_merged_ranges_truncated(sheet.name)

                last = 0
                for m in _CELL_RE.finditer(buf):
                    cell = m.group(0)
                    last = m.end()
                    if not _HAS_FORMULA_RE.search(cell):
                        continue
                    sheet.formula_count += 1
                    if _HAS_VALUE_RE.search(cell):
                        sheet.formula_cached_count += 1
                    elif len(sheet.uncached_formula_cells) < MAX_UNCACHED_CELLS_REPORTED:
                        ref = _CELL_REF_RE.search(cell)
                        if ref:
                            sheet.uncached_formula_cells.append(
                                ref.group(1).decode("ascii", "replace")
                            )

                # On conserve la queue non consommée (une cellule peut être à cheval
                # sur deux blocs). Bornée : une cellule ne fait jamais 1 Mio.
                buf = buf[last:] if last else buf[-_SCAN_CHUNK:]
    except RejectedFile:
        raise
    except zipfile.BadZipFile as e:
        raise CorruptedFile(
            "Classeur Excel illisible ou corrompu.", code="excel_corrupted") from e


# ---------------------------------------------------------------------
# Parties structurelles
# ---------------------------------------------------------------------
def _resolve_part(target: str) -> str:
    """Cible d'une relation -> chemin d'entrée ZIP.

    Les cibles de `xl/_rels/workbook.xml.rels` sont RELATIVES à `xl/`
    (`worksheets/sheet1.xml`) mais peuvent aussi être ABSOLUES dans le paquet
    (`/xl/worksheets/sheet1.xml` — c'est ce qu'écrit openpyxl). Les deux formes sont
    valides ; il faut les ramener au même chemin d'archive.
    """
    if not target:
        return ""
    if target.startswith("/"):
        return target.lstrip("/")           # /xl/worksheets/… -> xl/worksheets/…
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"                   # worksheets/… -> xl/worksheets/…


def _parse_workbook(zf: zipfile.ZipFile, inv: WorkbookInventory) -> Dict[str, str]:
    """Feuilles (nom, ordre, visibilité) + plages nommées. Renvoie {rId: target}."""
    xml = read_bounded_entry(zf, WORKBOOK_PART, max_bytes=_MAX_WORKBOOK_XML)
    if not xml:
        raise CorruptedFile(
            "Classeur Excel invalide : xl/workbook.xml manquant ou illisible.",
            code="excel_missing_workbook_xml",
        )
    reject_unsafe_xml(xml, part_name=WORKBOOK_PART)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise CorruptedFile("XML de classeur malformé.", code="excel_xml_parse_error")

    rels = _parse_workbook_rels(zf, inv)

    sheets_el = root.find(f"{_NS_MAIN}sheets")
    if sheets_el is not None:
        for i, el in enumerate(sheets_el.findall(f"{_NS_MAIN}sheet"), start=1):
            rid = el.get(f"{_NS_REL}id") or ""
            part = _resolve_part(rels.get(rid, ""))
            inv.sheets.append(SheetInfo(
                name=el.get("name") or f"Feuille{i}",
                index=i,
                state=(el.get("state") or "visible"),
                part=part,
            ))

    names_el = root.find(f"{_NS_MAIN}definedNames")
    if names_el is not None:
        for el in names_el.findall(f"{_NS_MAIN}definedName"):
            n = el.get("name")
            if n and len(inv.defined_names) < MAX_DEFINED_NAMES_REPORTED:
                inv.defined_names.append(n)

    return rels


def _parse_workbook_rels(zf: zipfile.ZipFile, inv: "WorkbookInventory") -> Dict[str, str]:
    """Table `rId -> partie`. Un échec ici est STRUCTUREL, jamais silencieux.

    Sans relations, aucune `SheetInfo.part` n'est résolue : le scan XML est sauté pour
    TOUT le classeur — ni formule, ni fusion, ni dimension déclarée. openpyxl peut
    malgré tout ouvrir le fichier et produire des chunks : l'extraction paraîtrait
    réussie tout en ayant perdu la connaissance structurelle. D'où le marquage
    explicite (`relationships_unreadable`) plutôt qu'un `return {}` muet.
    """
    xml = read_bounded_entry(zf, WORKBOOK_RELS_PART, max_bytes=_MAX_WORKBOOK_XML)
    if not xml:
        inv.mark_relationships_unreadable(
            "partie absente ou au-delà de la borne de lecture")
        return {}
    reject_unsafe_xml(xml, part_name=WORKBOOK_RELS_PART)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        inv.mark_relationships_unreadable(f"XML malformé : {e}")
        return {}
    out: Dict[str, str] = {}
    for el in root.findall(f"{_NS_PKG_REL}Relationship"):
        rid, target = el.get("Id"), el.get("Target")
        if rid and target:
            out[rid] = target
    return out


def _parse_tables(zf: zipfile.ZipFile, inv: WorkbookInventory) -> None:
    """Tables Excel DÉCLARÉES (`xl/tables/*.xml`). Inaccessibles en `read_only` (E1)."""
    for name in zf.namelist():
        if not (name.startswith("xl/tables/") and name.endswith(".xml")):
            continue
        xml = read_bounded_entry(zf, name, max_bytes=_MAX_TABLE_XML)
        if not xml:
            continue
        reject_unsafe_xml(xml, part_name=name)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        ref = root.get("ref")
        if ref:
            inv.tables.append({
                "name": root.get("displayName") or root.get("name") or "",
                "ref": ref,
                "header_row_count": int(root.get("headerRowCount") or 1),
            })


def _inventory_non_tabular(zf: zipfile.ZipFile, inv: WorkbookInventory) -> None:
    """Contenu non tabulaire : DÉTECTÉ par le seul `namelist()`, jamais parsé.

    Coût nul, risque nul — et l'utilisateur comprend pourquoi les chiffres d'un
    graphique ne sont pas répondables.
    """
    for n in zf.namelist():
        if n.startswith("xl/charts/"):
            inv.charts += 1
        elif n.startswith("xl/media/"):
            inv.images += 1
        elif n.startswith("xl/comments") or "/comments" in n:
            inv.comments += 1
        elif n.startswith("xl/externalLinks/") and n.endswith(".xml"):
            inv.external_links += 1
        elif n.startswith("xl/embeddings/"):
            inv.ole_objects += 1

    if inv.charts or inv.images:
        inv.add_warning("excel_visual_content_ignored")
    if inv.comments:
        inv.add_warning("excel_comments_ignored")
    if inv.external_links:
        # JAMAIS suivi, JAMAIS résolu : openpyxl est ouvert avec keep_links=False.
        inv.add_warning("excel_external_links_detected")
    if inv.ole_objects:
        inv.add_warning("excel_ole_objects_detected")


# ---------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------
def inspect_workbook(data: bytes) -> WorkbookInventory:
    """Inspecte un `.xlsx` AVANT tout appel à openpyxl.

    Ordre volontaire — chaque étape est moins chère que la suivante :
      1. ouverture ZIP (répertoire central seulement) ;
      2. garde zip-bomb (E0) — ne décompresse rien ;
      3. **borne `sharedStrings`** — décide de la mémoire, sans décompresser ;
      4. structure (workbook, tables, inventaire) ;
      5. scan des feuilles en flux (formules, fusions, dimension déclarée).

    Lève `RejectedFile` / `ExtractionLimitExceeded` / `CorruptedFile`.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise CorruptedFile(
            "Classeur Excel illisible ou corrompu.", code="excel_corrupted") from e

    inv = WorkbookInventory()

    with zf:
        # 2) Garde ZIP mutualisée (lot E0) : entrées, décompressé cumulé, ratio.
        guard_zip_archive(
            zf,
            max_entries=config.docx_max_zip_entries(),
            max_uncompressed_bytes=config.excel_max_uncompressed_bytes(),
            max_compression_ratio=config.docx_max_compression_ratio(),
            code_prefix="excel",
            label="Classeur Excel",
        )

        infos = zf.infolist()
        inv.uncompressed_bytes = sum(max(0, i.file_size) for i in infos)
        for i in infos:
            if i.filename == SHARED_STRINGS_PART:
                inv.shared_strings_bytes = max(0, i.file_size)
                break

        # 3) LA borne mémoire (E1). Taille DÉCLARÉE dans le répertoire central :
        #    aucun octet n'est décompressé pour l'obtenir.
        limit = config.excel_max_shared_strings_bytes()
        if inv.shared_strings_bytes > limit:
            raise ExtractionLimitExceeded(
                f"Classeur Excel : table de chaînes trop volumineuse "
                f"({inv.shared_strings_bytes // (1024 * 1024)} Mo > "
                f"{limit // (1024 * 1024)} Mo). Ce classeur demanderait trop de mémoire. "
                "Réduisez le nombre de valeurs textuelles distinctes, ou ajustez "
                "DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB.",
                code="excel_shared_strings_too_large",
            )

        # 4) Structure.
        _parse_workbook(zf, inv)
        if not inv.sheets:
            raise CorruptedFile(
                "Classeur Excel invalide : aucune feuille déclarée.",
                code="excel_corrupted",
            )
        if len(inv.sheets) > config.excel_max_sheets():
            raise ExtractionLimitExceeded(
                f"Classeur Excel : trop de feuilles ({len(inv.sheets)} > "
                f"{config.excel_max_sheets()}).",
                code="excel_too_many_sheets",
            )
        _parse_tables(zf, inv)
        _inventory_non_tabular(zf, inv)

        # 5) Scan des feuilles à indexer (les masquées sont ignorées par défaut :
        #    inutile de payer leur scan).
        for sheet in inv.sheets:
            if not config.excel_sheet_is_indexable(sheet.state):
                continue
            if not sheet.part:
                continue
            _scan_sheet(zf, sheet, inv)

    # Refus PRÉCOCE sur la dimension déclarée : elle ne fait pas autorité (E1 §10),
    # mais si elle annonce déjà l'infaisable, autant refuser sans rien parser.
    #
    # 🔴 Faux positif corrigé. Un classeur de reporting déclare couramment une dimension
    # FANTÔME (`A1:XFD671` : toute la largeur d'Excel) alors qu'il ne contient qu'une
    # poignée de cellules réelles — mesuré : 11 M déclarées, 5 733 réelles. La balise
    # `<dimension>` étant celle-là même qu'on a appris à ne PAS croire (`reset_dimensions`),
    # on ne refuse pas sur elle quand la largeur est aberrante : on défère au comptage des
    # cellules RÉELLES pendant l'extraction en flux, qui, lui, ne ment pas et s'arrête net
    # au seuil. Les gardes ZIP (taille décompressée, sharedStrings) protègent déjà la mémoire.
    max_cols = config.excel_max_declared_cols()
    reliable = [
        s for s in inv.sheets
        if config.excel_sheet_is_indexable(s.state) and 0 < s.declared_cols <= max_cols
    ]
    phantom = [
        s for s in inv.sheets
        if config.excel_sheet_is_indexable(s.state) and s.declared_cols > max_cols
    ]
    if phantom:
        # Non bloquant : le classeur n'est PAS refusé là-dessus, il sera lu en flux.
        inv.add_warning("excel_phantom_dimension_ignored")

    declared_total = sum(s.declared_cells for s in reliable)
    if declared_total > config.excel_max_cells():
        raise ExtractionLimitExceeded(
            f"Classeur Excel trop volumineux : {declared_total} cellules déclarées "
            f"(limite : {config.excel_max_cells()}). Ajustez DOCUMENT_EXCEL_MAX_CELLS "
            "si nécessaire.",
            code="excel_too_many_cells",
        )

    if inv.merged_ranges:
        inv.add_warning("excel_merged_cells_detected")
    if inv.formula_uncached_count:
        inv.add_warning("excel_formulas_not_cached")

    return inv

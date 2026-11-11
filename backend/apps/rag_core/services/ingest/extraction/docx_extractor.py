# backend/apps/rag_core/services/ingest/extraction/docx_extractor.py
from __future__ import annotations

"""
Extracteur DOCX (stdlib `zipfile` + `xml.etree`).

- DOCX traité comme une archive ZIP NON FIABLE ;
- protections anti zip-bomb : nb d'entrées, taille décompressée totale, ratio
  (garde MUTUALISÉE dans `zip_security`, partagée avec les futurs formats Office) ;
- durcissement XML : toute déclaration DOCTYPE/ENTITY est refusée AVANT parsing
  (`xml_security`) — ElementTree expand les entités INTERNES, et la garde zip-bomb
  ne voit pas passer une bombe à entités (minuscule une fois décompressée) ;
- .docm (macro-enabled) est refusé en amont (validation) ; ici on n'exécute
  JAMAIS de macro / contenu actif (on ne lit que le XML de texte) ;
- extraction : paragraphes, titres (styles Heading/Titre), listes (numPr),
  tableaux (linéarisés "cellule | cellule").
"""

import io
import posixpath
import zipfile
from typing import List, Optional
from xml.etree import ElementTree as ET

from . import config, mime as M
from .base import (
    Block,
    BlockType,
    CorruptedFile,
    ExtractionContext,
    ExtractionLimitExceeded,
    ExtractionResult,
    RejectedFile,
    join_blocks_text,
)
from .xml_security import reject_deeply_nested_xml, reject_unsafe_xml
from .zip_security import guard_zip_archive, read_entry_strict

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_DOCUMENT_PART = "word/document.xml"

#: Version de la logique d'extraction DOCX.
#:
#: Elle voyage dans `Document.meta.extraction` et sert de garde à l'aperçu : les
#: chunks ne portent que des OFFSETS dans le texte extrait, donc toute évolution
#: changeant `ExtractionResult.text` déplace silencieusement ces offsets. Un
#: aperçu qui lirait des offsets produits par une autre version surlignerait le
#: mauvais passage — d'où le refus explicite plutôt que l'approximation.
#:
#: À incrémenter pour TOUT changement du texte produit (lot 2 : `outlineLvl`,
#: `numbering.xml`, sauts de page, `w:sdt`, tableaux imbriqués…).
EXTRACTOR_VERSION = "docx-v1"


def _q(tag: str) -> str:
    return f"{_W}{tag}"


def _guard_zip_bomb(zf: zipfile.ZipFile) -> None:
    """Garde anti zip-bomb DOCX — délègue à la garde Office partagée.

    Seuils, codes d'erreur et messages sont STRICTEMENT ceux d'avant la mutualisation
    (`docx_too_many_entries`, `docx_uncompressed_too_large`, `docx_compression_ratio`).
    """
    guard_zip_archive(
        zf,
        max_entries=config.docx_max_zip_entries(),
        max_uncompressed_bytes=config.docx_max_uncompressed_bytes(),
        max_compression_ratio=config.docx_max_compression_ratio(),
        code_prefix="docx",
        label="DOCX",
    )


def _para_text(p: ET.Element) -> str:
    parts: List[str] = []
    for t in p.iter():
        tag = t.tag
        if tag == _q("t") and t.text:
            parts.append(t.text)
        elif tag == _q("tab"):
            parts.append("\t")
        elif tag in (_q("br"), _q("cr")):
            parts.append("\n")
    return "".join(parts).strip()


def _para_style(p: ET.Element) -> Optional[str]:
    ppr = p.find(_q("pPr"))
    if ppr is None:
        return None
    style = ppr.find(_q("pStyle"))
    if style is not None:
        return style.get(_q("val"))
    return None


def _is_list_para(p: ET.Element) -> bool:
    ppr = p.find(_q("pPr"))
    return ppr is not None and ppr.find(_q("numPr")) is not None


def _heading_level(style: Optional[str]) -> Optional[int]:
    if not style:
        return None
    s = style.lower()
    if s.startswith("heading") or s.startswith("titre") or s.startswith("title"):
        for ch in s:
            if ch.isdigit():
                return int(ch)
        return 1
    return None


def _table_text(tbl: ET.Element) -> List[str]:
    rows: List[str] = []
    for tr in tbl.findall(_q("tr")):
        cells: List[str] = []
        for tc in tr.findall(_q("tc")):
            cell_parts = [_para_text(p) for p in tc.findall(_q("p"))]
            cells.append(" ".join(x for x in cell_parts if x).strip())
        line = " | ".join(cells).strip()
        if line:
            rows.append(line)
    return rows


_RELS_PART = "word/_rels/document.xml.rels"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _native_charts(zf: zipfile.ZipFile, root: ET.Element):
    """``({r:id: ChartData}, stats)`` des graphiques natifs référencés par le corps. Une partie illisible, dangereuse
    ou absente est COMPTÉE et ignorée : le reste du document est extrait normalement."""
    from .office_chart import C_NS, CHART_REL_TYPE, R_NS, parse_chart_xml

    rids = [el.get(f"{{{R_NS}}}id") for el in root.iter(f"{{{C_NS}}}chart") if el.get(f"{{{R_NS}}}id")]
    stats = {"seen": len(rids), "extracted": 0, "failed": 0, "truncated": 0}
    if not rids:
        return {}, stats
    targets = {}
    try:
        rels = read_entry_strict(zf, _RELS_PART, max_bytes=config.docx_max_uncompressed_bytes(),
                                 code_prefix="docx", label="DOCX")
        reject_unsafe_xml(rels, part_name=_RELS_PART)
        for rel in ET.fromstring(rels).iter(f"{{{_PKG_REL_NS}}}Relationship"):
            if rel.get("Type") == CHART_REL_TYPE and rel.get("TargetMode") != "External":
                targets[rel.get("Id")] = posixpath.normpath(posixpath.join("word", rel.get("Target") or ""))
    except Exception:
        targets = {}
    charts = {}
    for rid in dict.fromkeys(rids):
        part = targets.get(rid)
        chart = None
        if part and not part.startswith(".."):
            try:
                chart = parse_chart_xml(read_entry_strict(zf, part, max_bytes=config.docx_max_uncompressed_bytes(),
                                                          code_prefix="docx", label="DOCX"), part_name=part)
            except Exception:
                chart = None
        if chart is None:
            stats["failed"] += 1
            continue
        charts[rid] = chart
        stats["extracted"] += 1
        stats["truncated"] += int(chart.truncated)
    return charts, stats


def _para_charts(p: ET.Element, charts: dict) -> list:
    if not charts:
        return []
    from .office_chart import C_NS, R_NS

    return [charts[r] for r in (el.get(f"{{{R_NS}}}id") for el in p.iter(f"{{{C_NS}}}chart")) if r in charts]


class DocxExtractor:
    name = "docx"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.DOCX

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        res = ExtractionResult()
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            raise CorruptedFile("Fichier DOCX illisible ou corrompu.", code="docx_corrupted")

        with zf:
            _guard_zip_bomb(zf)

            try:
                # Lecture BORNÉE pendant la décompression. `guard_zip_archive`
                # ci-dessus ne peut vérifier que les tailles DÉCLARÉES par
                # l'archive ; une entrée qui sous-déclare sa taille passerait la
                # garde puis se décompresserait sans limite.
                xml_bytes = read_entry_strict(
                    zf,
                    _DOCUMENT_PART,
                    max_bytes=config.docx_max_uncompressed_bytes(),
                    code_prefix="docx",
                    label="DOCX",
                )
            except KeyError:
                raise CorruptedFile(
                    "Fichier DOCX invalide : word/document.xml manquant.",
                    code="docx_missing_document_xml",
                )
            except (RejectedFile, ExtractionLimitExceeded):
                raise
            except Exception:
                raise CorruptedFile("Fichier DOCX illisible ou corrompu.", code="docx_corrupted")

            # Durcissement XML : AVANT tout parsing. Une bombe à entités n'explose
            # qu'à l'intérieur du parseur — après, il est trop tard.
            reject_unsafe_xml(xml_bytes, part_name=_DOCUMENT_PART)
            # Même logique pour l'imbrication : quelques kilo-octets suffisent à
            # épuiser la pile du parseur, et aucune garde de TAILLE ne le voit.
            reject_deeply_nested_xml(xml_bytes, part_name=_DOCUMENT_PART)

            try:
                root = ET.fromstring(xml_bytes)
            except ET.ParseError:
                raise CorruptedFile("XML DOCX malformé.", code="docx_xml_parse_error")

            # --- Visuels INTÉGRÉS ----------------------
            # ICI, et pas ailleurs : l'archive est encore ouverte et l'arbre déjà
            # parsé. Aucune réouverture du paquet, aucun second parsing, aucune
            # seconde extraction de texte ou de tableaux.
            #
            # La nature de source est déclarée MÊME porte fermée : c'est elle qui arme
            # la garde « éteindre la fonctionnalité ne retire jamais en silence une
            # évidence visuelle publiée » côté réindexation.
            res.visual_source_kind = "docx"
            if "docx" in (ctx.visual_sources or ()):
                from .docx_visual import discover_docx_visual_assets

                decouverte = discover_docx_visual_assets(zf, root)
                res.visual_assets.extend(decouverte.assets)
                if decouverte.stats.get("drawings_seen") or decouverte.stats.get("admitted"):
                    res.metadata["docx_visual"] = dict(decouverte.stats)

            # --- Graphiques NATIFS ----------
            # Lus pendant que l'archive est ouverte ; rendus plus bas, à leur place dans le flux.
            charts, chart_stats = ({}, None)
            if config.office_chart_data_enabled():
                charts, chart_stats = _native_charts(zf, root)

        if chart_stats is not None:
            res.metadata["docx_native_charts"] = chart_stats
            if chart_stats["failed"]:
                res.add_warning("docx_native_chart_unreadable")
            if chart_stats["truncated"]:
                res.add_warning("docx_native_chart_truncated")

        body = root.find(_q("body"))
        if body is None:
            return res.finalize(fmt="docx", mime=M.DOCX, method="docx")

        blocks: List[Block] = []
        n_titles = 0
        n_tables = 0
        list_buf: List[str] = []

        def flush_list():
            if list_buf:
                blocks.append(Block(type=BlockType.LIST, text="\n".join(list_buf)))
                list_buf.clear()

        for child in list(body):
            tag = child.tag
            if tag == _q("p"):
                txt = _para_text(child)
                for chart in _para_charts(child, charts):
                    from .office_chart import render_chart

                    flush_list()
                    blocks.append(Block(type=BlockType.TABLE, text=render_chart(chart)))
                if not txt:
                    continue
                level = _heading_level(_para_style(child))
                if level is not None:
                    flush_list()
                    blocks.append(Block(type=BlockType.TITLE, text=txt, level=level))
                    n_titles += 1
                elif _is_list_para(child):
                    list_buf.append(f"- {txt}")
                else:
                    flush_list()
                    blocks.append(Block(type=BlockType.PARAGRAPH, text=txt))
            elif tag == _q("tbl"):
                flush_list()
                rows = _table_text(child)
                if rows:
                    blocks.append(Block(type=BlockType.TABLE, text="\n".join(rows)))
                    n_tables += 1

        flush_list()

        res.blocks = blocks
        res.text = join_blocks_text(blocks)
        res.metadata["sections"] = n_titles
        res.metadata["tables"] = n_tables
        # Voyage jusqu'à `Document.meta.extraction` : c'est la garde qui permettra
        # à l'aperçu de refuser des offsets produits par une autre version.
        res.metadata["extractor_version"] = EXTRACTOR_VERSION
        return res.finalize(fmt="docx", mime=M.DOCX, method="docx")

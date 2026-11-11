# backend/apps/rag_core/services/ingest/extraction/pptx_extractor.py
from __future__ import annotations

"""
Extracteur PowerPoint `.pptx` — python-pptx, structuré PAR DIAPOSITIVE.

Architecture, calquée sur l'extracteur Excel (lot E2) :

- **pré-passe stdlib** (`zipfile` + `xml.etree`) qui applique TOUTES les gardes avant que
  python-pptx ne touche l'archive : anti zip-bomb (garde Office mutualisée), durcissement
  XML (DOCTYPE/ENTITY), nombre de diapositives. python-pptx charge la présentation
  entière en mémoire : le laisser ouvrir une archive piégée reviendrait à poser la garde
  après le dégât ;
- **traversée** des formes via python-pptx, groupes parcourus récursivement, bornée par
  des compteurs (formes, cellules, caractères) ;
- **rendu** délégué à `pptx_render` (module pur, sans python-pptx) ;
- **sortie** dans `ExtractionResult.slides` : une unité logique par diapositive.

Ce qui n'est JAMAIS fait ici :
- exécuter une macro ou du contenu actif (`.pptm` est refusé en amont, par CONTENU) ;
- suivre une relation externe (`ppt/slides/_rels/*` pointant vers http:// ou un chemin
  local) — on ne lit que les parties embarquées de l'archive ;
- convertir en PDF, lancer LibreOffice, ou tout autre processus externe ;
- OCRiser les images de diapositives (hors périmètre du lot) ;
- inventer du texte pour une diapositive qui n'en a pas.
"""

import io
import re
import zipfile
from typing import List, Optional

from . import config
from . import mime as M
from .base import (
    Block,
    BlockType,
    CorruptedFile,
    ExtractionContext,
    ExtractionLimitExceeded,
    ExtractionResult,
    RejectedFile,
    UnsupportedFormat,
)
from .pptx_render import ShapeText, TextRun, render_notes, render_slide
from .xml_security import reject_deeply_nested_xml, reject_unsafe_xml
from .zip_security import guard_zip_archive

EXTRACTOR_VERSION = "1"
CHUNKING_VERSION = "1"

_PRESENTATION_PART = "ppt/presentation.xml"

# Bornes de lecture de la pré-passe : on ne décompresse une part XML que si sa taille
# DÉCLARÉE est plausible. Une diapositive dépasse rarement quelques dizaines de Ko.
_MAX_XML_PART_BYTES = 8 * 1024 * 1024


def _guard_zip_bomb(zf: zipfile.ZipFile) -> None:
    """Garde anti zip-bomb PPTX — délègue à la garde Office partagée (DOCX, Excel)."""
    guard_zip_archive(
        zf,
        max_entries=config.pptx_max_zip_entries(),
        max_uncompressed_bytes=config.pptx_max_uncompressed_bytes(),
        max_compression_ratio=config.pptx_max_compression_ratio(),
        code_prefix="pptx",
        label="PPTX",
    )


def _slide_part_names(names: List[str]) -> List[str]:
    """Parties de diapositives présentes dans l'archive, sans les rels ni les layouts."""
    return [
        n
        for n in names
        if n.startswith("ppt/slides/")
        and n.endswith(".xml")
        and "/_rels/" not in n
    ]


def _inspect(data: bytes) -> dict:
    """Pré-passe stdlib : gardes + inventaire, AVANT tout appel à python-pptx.

    Renvoie un petit inventaire ; lève `CorruptedFile` / `ExtractionLimitExceeded` /
    `RejectedFile` selon le cas. Aucun XML n'est parsé ici au-delà du contrôle de
    sûreté : on cherche des octets, on ne construit pas d'arbre.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise CorruptedFile(
            "Présentation illisible : l'archive est corrompue ou incomplète.",
            code="invalid_pptx_archive",
        ) from e

    with zf:
        # 1) Anti zip-bomb — ne décompresse rien (répertoire central seulement).
        _guard_zip_bomb(zf)

        names = zf.namelist()

        # 2) Partie canonique. `detect_mime` l'exige déjà pour classer en PPTX ; on
        #    re-vérifie ici car l'extracteur doit être sûr appelé directement (tests,
        #    réingestion) et non seulement derrière la validation.
        if _PRESENTATION_PART not in names:
            raise CorruptedFile(
                "Présentation invalide : la partie `ppt/presentation.xml` est absente.",
                code="invalid_pptx_archive",
            )

        # 3) Chiffrement. Un fichier Office protégé par mot de passe est un conteneur
        #    OLE2 et n'arrive pas ici (il est détecté LEGACY_OFFICE). Le cas résiduel
        #    est une archive OOXML dont les parties sont chiffrées (DRM/IRM) : elle
        #    porte `EncryptedPackage` ou `ppt/encryption`. On le nomme explicitement
        #    plutôt que de laisser python-pptx échouer sur un « XML illisible ».
        if any("EncryptedPackage" in n or n.startswith("ppt/encryption") for n in names):
            raise RejectedFile(
                "Présentation protégée ou chiffrée : impossible d'en extraire le contenu. "
                "Retirez la protection puis réimportez le fichier.",
                code="encrypted_pptx",
            )

        slide_parts = _slide_part_names(names)
        max_slides = config.pptx_max_slides()
        if len(slide_parts) > max_slides:
            raise ExtractionLimitExceeded(
                f"Présentation trop volumineuse : {len(slide_parts)} diapositives "
                f"(max {max_slides}).",
                code="pptx_limit_exceeded",
            )

        # 4) Durcissement XML : DOCTYPE/ENTITY refusés AVANT tout parsing. python-pptx
        #    s'appuie sur lxml, dont le parseur par défaut expand les entités internes.
        #    La garde zip-bomb ne voit pas passer une bombe à entités : minuscule
        #    compressée, minuscule décompressée, explosive une fois parsée.
        for part in [_PRESENTATION_PART] + sorted(slide_parts):
            try:
                info = zf.getinfo(part)
            except KeyError:
                continue
            if info.file_size > _MAX_XML_PART_BYTES:
                raise ExtractionLimitExceeded(
                    f"Présentation : partie XML anormalement volumineuse ({part}).",
                    code="pptx_limit_exceeded",
                )
            try:
                raw = zf.read(part)
            except Exception as e:
                raise CorruptedFile(
                    "Présentation illisible : une partie de l'archive est corrompue.",
                    code="invalid_pptx_archive",
                ) from e
            reject_unsafe_xml(raw, part_name=part)

    return {"slide_parts": len(slide_parts)}


def _load_presentation(data: bytes):
    """Ouvre la présentation avec python-pptx, après la pré-passe."""
    try:
        from pptx import Presentation  # import LOCAL : cf. `extract`
    except ImportError as e:  # pragma: no cover - dépendance cœur, absente = env cassé
        raise UnsupportedFormat(
            "L'extraction des présentations PowerPoint est indisponible sur cet "
            "environnement (bibliothèque manquante).",
            code="pptx_library_missing",
        ) from e

    try:
        return Presentation(io.BytesIO(data))
    except Exception as e:
        raise CorruptedFile(
            "Présentation illisible : l'archive est corrompue ou n'est pas une "
            "présentation PowerPoint valide.",
            code="invalid_pptx_archive",
        ) from e


class _Budget:
    """Compteurs transverses à la présentation (bornes anti-abus).

    Portés par un objet plutôt que par des variables locales : la traversée des groupes
    est récursive, et un budget passé par référence évite de faire remonter des totaux
    à chaque niveau.
    """

    def __init__(self) -> None:
        self.chars = 0
        self.table_cells = 0
        self.shapes_truncated = False
        self.chars_truncated = False
        self.table_cells_truncated = False
        self.group_depth_reached = False
        self.unreadable_slides: List[int] = []
        self.image_only_slides: List[int] = []
        self.empty_slides: List[int] = []
        # graphiques natifs (cache de données du fichier).
        self.charts_seen = 0
        self.charts_extracted = 0
        self.charts_failed = 0
        self.charts_truncated = 0


def _para_runs(text_frame, budget: _Budget, *, bulleted: bool = False) -> List[TextRun]:
    """Paragraphes d'une zone de texte, avec puces et niveaux.

    python-pptx n'expose pas de booléen « ceci est une puce » : le format vit dans les
    propriétés du paragraphe (`buChar`/`buAutoNum`/`buNone`) et, le plus souvent, est
    HÉRITÉ du layout sans être écrit dans la diapositive — le lire dans le XML de la
    diapositive donnerait donc surtout des faux négatifs.

    On décide par le TYPE DE FORME, qui est fiable et n'exige aucune devinette :
    - un placeholder de corps/contenu est une liste à puces par défaut dans PowerPoint
      -> `bulleted=True`, tous ses paragraphes sont des puces ;
    - une zone de texte libre est de la prose -> rendue telle quelle.

    `level` porte l'indentation dans les deux cas ; un niveau > 0 implique une puce même
    dans une zone libre (on n'indente pas de la prose sans raison).
    """
    runs: List[TextRun] = []
    max_chars = config.pptx_max_chars()

    for para in text_frame.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        if budget.chars + len(text) > max_chars:
            budget.chars_truncated = True
            break
        budget.chars += len(text)
        level = int(getattr(para, "level", 0) or 0)
        runs.append(TextRun(text=text, level=level, is_bullet=bulleted or level > 0))

    return runs


def _is_bulleted_body(shape) -> bool:
    """La forme est-elle un placeholder de CORPS (liste à puces par défaut) ?

    Titre et sous-titre sont exclus : ce ne sont pas des listes. Tout autre placeholder
    porteur de texte (BODY, OBJECT, les deux moitiés d'une mise en page « Deux contenus »…)
    est bulleté par convention PowerPoint.
    """
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER

        if not shape.is_placeholder:
            return False
        ph_type = shape.placeholder_format.type
        return ph_type not in (
            PP_PLACEHOLDER.TITLE,
            PP_PLACEHOLDER.CENTER_TITLE,
            PP_PLACEHOLDER.SUBTITLE,
        )
    except Exception:
        return False


def _table_rows(shape, budget: _Budget) -> Optional[List[List[str]]]:
    """Cellules d'un tableau, bornées par le budget global.

    Une cellule fusionnée est répétée par python-pptx sur toute son étendue ; on la
    laisse telle quelle : dédupliquer supposerait de deviner l'intention, et une valeur
    répétée est moins trompeuse qu'un trou. `render_table` normalise la largeur.
    """
    max_cells = config.pptx_max_table_cells()
    max_chars = config.pptx_max_chars()
    rows: List[List[str]] = []

    try:
        table = shape.table
    except Exception:
        return None

    for row in table.rows:
        cells: List[str] = []
        for cell in row.cells:
            if budget.table_cells >= max_cells:
                budget.table_cells_truncated = True
                return rows or None
            budget.table_cells += 1
            try:
                value = (cell.text or "").strip()
            except Exception:
                value = ""
            if budget.chars + len(value) > max_chars:
                budget.chars_truncated = True
                value = ""
            else:
                budget.chars += len(value)
            cells.append(value)
        rows.append(cells)

    return rows or None


def _placeholder_kind(shape) -> Optional[str]:
    """« title » / « subtitle » si la forme est le placeholder correspondant.

    On passe par l'énumération NOMMÉE de python-pptx, jamais par des valeurs en dur :
    `PP_PLACEHOLDER` ne reprend pas les entiers du XML OOXML (TITLE y vaut 1, pas 13),
    et une constante recopiée à la main donnerait un extracteur qui ne détecte aucun
    titre — sans lever la moindre erreur.

    Un titre peut être porté par TITLE (mise en page « Titre et contenu ») ou par
    CENTER_TITLE (mise en page « Diapositive de titre ») : les deux comptent.
    """
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER

        if not shape.is_placeholder:
            return None
        ph_type = shape.placeholder_format.type
    except Exception:
        return None

    try:
        if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            return "title"
        if ph_type == PP_PLACEHOLDER.SUBTITLE:
            return "subtitle"
    except Exception:
        return None
    return None


# Un `descr` qui n'est qu'un nom de fichier (« image.png », « Capture d'écran 2024-01-05
# à 10.32.11.png ») ou un nom de forme par défaut (« Picture 3 », « Image 12 ») n'est PAS
# une description : c'est ce que PowerPoint — et python-pptx — y écrivent tout seuls quand
# l'auteur n'a rien saisi. L'indexer polluerait le retrieval avec du bruit et ferait passer
# une diapositive image-seule pour une diapositive décrite.
_ALT_TEXT_FILENAME_RE = re.compile(
    r"^[\w\-. '()\[\]]+\.(png|jpe?g|gif|bmp|tiff?|webp|svg|emf|wmf)$", re.IGNORECASE
)
_ALT_TEXT_DEFAULT_NAME_RE = re.compile(
    r"^(picture|image|graphique|chart|diagram|object|objet|espace réservé|"
    r"content placeholder|placeholder|zone de texte|text box|rectangle|group|groupe)"
    r"\s*\d*$",
    re.IGNORECASE,
)


def _is_meaningful_alt_text(descr: str, shape_name: str) -> bool:
    """Le texte alternatif apprend-il quelque chose à un lecteur ?

    Fonction séparée et sans dépendance à python-pptx : c'est la règle qu'on veut pouvoir
    tester directement, indépendamment de la façon dont on lit l'attribut XML.
    """
    descr = (descr or "").strip()
    if not descr:
        return False
    if descr == (shape_name or "").strip():
        return False
    if _ALT_TEXT_FILENAME_RE.match(descr):
        return False
    if _ALT_TEXT_DEFAULT_NAME_RE.match(descr):
        return False
    return True


def _alt_text(shape) -> str:
    """Texte alternatif d'une forme (accessibilité), tel que SAISI par l'auteur.

    python-pptx ne l'expose pas : il vit dans l'attribut `descr` de `<p:cNvPr>`. Seuls
    les `descr` réellement informatifs sont retenus (cf. `_is_meaningful_alt_text`).
    """
    try:
        descr = shape._element._nvXxPr.cNvPr.get("descr") or ""
    except Exception:
        return ""
    if not _is_meaningful_alt_text(descr, getattr(shape, "name", "") or ""):
        return ""
    return descr.strip()


def _native_chart(shape, budget: "_Budget"):
    """Données du graphique natif d'une forme, ou ``None`` (compté comme échec)."""
    from .office_chart import parse_chart_xml

    budget.charts_seen += 1
    try:
        chart = parse_chart_xml(shape.chart_part.blob, part_name=str(shape.chart_part.partname))
    except Exception:  # XML dangereux ou malformé, partie absente : le graphique est ignoré, pas le fichier
        chart = None
    if chart is None:
        budget.charts_failed += 1
        return None
    budget.charts_extracted += 1
    budget.charts_truncated += int(chart.truncated)
    return chart


def _collect_shapes(
    shapes,
    budget: _Budget,
    *,
    depth: int = 0,
    counter: Optional[List[int]] = None,
    pictures: Optional[list] = None,
) -> List[ShapeText]:
    """Traverse les formes d'une diapositive, groupes inclus (récursif).

    `counter` est une liste à un élément : un compteur d'ordre PARTAGÉ entre les niveaux
    de récursion, pour que le rang d'apparition XML reste globalement croissant et
    départage le tri de façon totale (cf. `pptx_render.sort_key`).

    `pictures` : si fourni, reçoit les images rencontrées
    `(rang, groupée, forme, texte_alternatif)` — pour la découverte visuelle. C'est la
    MÊME traversée : aucune seconde passe. Absent (porte fermée), rien ne change.
    """
    if counter is None:
        counter = [0]

    out: List[ShapeText] = []
    max_shapes = config.pptx_max_shapes_per_slide()
    max_depth = config.pptx_group_max_depth()
    include_alt = config.pptx_include_alt_text()

    for shape in shapes:
        if counter[0] >= max_shapes:
            budget.shapes_truncated = True
            break
        counter[0] += 1
        order = counter[0]

        try:
            top = int(getattr(shape, "top", 0) or 0)
            left = int(getattr(shape, "left", 0) or 0)
        except Exception:
            top, left = 0, 0

        # 1) Groupe -> récursion bornée. Le discriminant est la présence d'un conteneur
        #    `.shapes` : seule une forme groupée en porte un. On ne teste PAS
        #    `bool(shape_type)`, qui serait faux pour un type d'énumération valant 0.
        if hasattr(shape, "shapes"):
            if depth >= max_depth:
                budget.group_depth_reached = True
                continue
            try:
                nested = _collect_shapes(
                    shape.shapes, budget, depth=depth + 1, counter=counter,
                    pictures=pictures,
                )
            except Exception:
                nested = []
            # Une forme groupée porte ses coordonnées propres ; on garde celles de
            # l'enfant, qui sont absolues dans python-pptx.
            out.extend(nested)
            continue

        # 2 bis) Graphique NATIF : catégories et valeurs du cache enregistré dans le fichier (jamais une lecture
        #    d'image). Échec de lecture = compté, la forme retombe sur son texte alternatif (étape 5).
        if getattr(shape, "has_chart", False) and config.office_chart_data_enabled():
            chart = _native_chart(shape, budget)
            if chart is not None:
                from .office_chart import chart_caption, chart_rows

                out.append(ShapeText(kind="chart", table=chart_rows(chart), alt_text=chart_caption(chart),
                                     top=top, left=left, order=order))
                continue

        # 2) Tableau.
        if getattr(shape, "has_table", False):
            rows = _table_rows(shape, budget)
            if rows:
                out.append(ShapeText(kind="table", table=rows, top=top, left=left, order=order))
            continue

        # 3) Zone de texte / placeholder.
        if getattr(shape, "has_text_frame", False):
            try:
                runs = _para_runs(
                    shape.text_frame, budget, bulleted=_is_bulleted_body(shape)
                )
            except Exception:
                runs = []
            if runs:
                kind = _placeholder_kind(shape) or "body"
                out.append(ShapeText(kind=kind, runs=runs, top=top, left=left, order=order))
                continue

        # 4) Image intégrée candidate au rail visuel, relevée seulement si l'appelant le
        #    demande. N'altère pas la sortie native : l'étape 5 s'applique à l'identique.
        if pictures is not None and _is_picture(shape):
            pictures.append((order, depth > 0, shape, _alt_text(shape) if include_alt else ""))

        # 5) Ni texte ni tableau -> texte alternatif si l'auteur en a saisi un.
        if include_alt:
            alt = _alt_text(shape)
            if alt:
                out.append(
                    ShapeText(kind="alt_text", alt_text=alt, top=top, left=left, order=order)
                )

    return out


def _notes_runs(slide, budget: _Budget) -> List[TextRun]:
    """Notes du présentateur d'une diapositive, si la partie existe."""
    try:
        if not slide.has_notes_slide:
            return []
        frame = slide.notes_slide.notes_text_frame
        if frame is None:
            return []
        return _para_runs(frame, budget)
    except Exception:
        return []


class PptxExtractor:
    name = "pptx"

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool:
        return detected_mime == M.PPTX

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult:
        # Défense en profondeur : la porte de `validation` n'est pas la seule. Un appel
        # direct à l'extracteur (réingestion, test) doit respecter le même flag.
        if not config.pptx_enabled():
            raise RejectedFile(
                "L'extraction des présentations PowerPoint est désactivée sur cet "
                "environnement (DOCUMENT_PPTX_ENABLED=0).",
                code="pptx_disabled",
            )

        res = ExtractionResult()
        budget = _Budget()

        # Nature de source visuelle déclarée MÊME porte fermée, comme pour le DOCX : c'est
        # elle qui arme la garde « éteindre la fonctionnalité ne retire jamais en silence
        # une évidence visuelle publiée » côté réindexation. Transitoire, hors métadonnées.
        res.visual_source_kind = "pptx"
        # Images intégrées : relevées pendant la traversée
        # existante, seulement si la source visuelle est active.
        pictures: Optional[list] = [] if "pptx" in (ctx.visual_sources or ()) else None

        # 1) Pré-passe stdlib : toutes les gardes AVANT python-pptx.
        inv = _inspect(data)

        # 2) Ouverture.
        prs = _load_presentation(data)

        include_notes = config.pptx_include_notes()
        blocks: List[Block] = []
        n_tables = 0

        # 3) Traversée. `prs.slides` suit l'ordre de `ppt/_rels/presentation.xml.rels`,
        #    c'est-à-dire l'ordre RÉEL de la présentation — pas l'ordre alphabétique de
        #    `namelist()`, qui trierait `slide10.xml` avant `slide2.xml`.
        for index, slide in enumerate(prs.slides, start=1):
            slide_pictures: Optional[list] = [] if pictures is not None else None
            try:
                shapes = _collect_shapes(slide.shapes, budget, pictures=slide_pictures)
            except Exception:
                budget.unreadable_slides.append(index)
                continue

            title_shapes = [s for s in shapes if s.kind == "title"]
            title: Optional[str] = None
            if title_shapes:
                # Le titre est rendu séparément, en tête : on retire sa forme du corps
                # pour ne pas l'écrire deux fois.
                first = title_shapes[0]
                lines = [r.text.strip() for r in first.runs if r.text.strip()]
                title = " ".join(lines).strip() or None
                shapes = [s for s in shapes if s is not first]

            if slide_pictures:
                from .pptx_visual import PptxPicture

                pictures.extend(
                    PptxPicture(slide_number=index, shape_order=order, grouped=grouped,
                                shape=shape, alt_text=alt, slide_title=title)
                    for order, grouped, shape, alt in slide_pictures
                )

            body_shapes = [s for s in shapes if s.kind != "alt_text"]
            has_text = bool(title) or any(
                (s.kind in ("table", "chart") and s.table) or s.runs for s in body_shapes
            )
            has_images = any(s.kind == "alt_text" for s in shapes) or _slide_has_picture(slide)
            n_tables += sum(1 for s in shapes if s.kind == "table")

            text = render_slide(slide_number=index, title=title, shapes=shapes)

            if not has_text:
                # Diapositive sans texte exploitable. On ne fabrique RIEN : on la
                # signale, et on ne l'indexe que si son texte alternatif porte
                # réellement quelque chose.
                if has_images:
                    budget.image_only_slides.append(index)
                else:
                    budget.empty_slides.append(index)

            if not (text or "").strip() or text.strip() == f"[Diapositive {index}]":
                continue

            res.slides.append(
                {
                    "text": text,
                    "meta": {
                        "kind": "pptx_slide",
                        "slide_number": index,
                        "slide_title": title,
                        "has_images": has_images,
                        "has_text": has_text,
                        "requires_visual_check": bool(has_images and not has_text),
                    },
                }
            )
            # Clé posée seulement si un graphique natif est rendu : drapeau éteint, sortie identique à l'octet.
            n_charts = sum(1 for s in shapes if s.kind == "chart")
            if n_charts:
                res.slides[-1]["meta"]["native_charts"] = n_charts
            blocks.append(
                Block(
                    type=BlockType.SECTION,
                    text=text,
                    meta={"slide_number": index, "slide_title": title},
                )
            )

            if include_notes:
                notes_text = render_notes(
                    slide_number=index, runs=_notes_runs(slide, budget)
                )
                if notes_text.strip():
                    res.slides.append(
                        {
                            "text": notes_text,
                            "meta": {
                                "kind": "pptx_notes",
                                "slide_number": index,
                                "slide_title": title,
                                "requires_visual_check": False,
                            },
                        }
                    )

        res.blocks = blocks

        if pictures is not None:
            # Aucune réouverture : les octets sont lus sur les parties déjà chargées.
            from .pptx_visual import discover_pptx_visual_assets

            decouverte = discover_pptx_visual_assets(pictures)
            res.visual_assets.extend(decouverte.assets)
            if decouverte.stats.get("pictures_seen") or decouverte.stats.get("admitted"):
                res.metadata["pptx_visual"] = dict(decouverte.stats)

        return self._finalize(res, budget, inv, n_tables)

    def _finalize(
        self, res: ExtractionResult, budget: _Budget, inv: dict, n_tables: int
    ) -> ExtractionResult:
        slide_units = [s for s in res.slides if s["meta"]["kind"] == "pptx_slide"]
        notes_units = [s for s in res.slides if s["meta"]["kind"] == "pptx_notes"]

        # `text` sert au FTS et à la garde « document vide » de l'indexation. Il est
        # reconstruit depuis les unités pour rester cohérent avec ce qui sera indexé.
        res.text = "\n\n".join(s["text"] for s in res.slides).strip()

        # -- warnings : chaque perte de contenu est NOMMÉE, jamais silencieuse ------
        if budget.image_only_slides:
            res.add_warning("pptx_image_only_slides")
        if budget.empty_slides:
            res.add_warning("pptx_empty_slides")
        if budget.unreadable_slides:
            res.add_warning("pptx_unreadable_slides")
        if budget.shapes_truncated:
            res.add_warning("pptx_max_shapes_reached")
        if budget.chars_truncated:
            res.add_warning("pptx_max_chars_reached")
        if budget.table_cells_truncated:
            res.add_warning("pptx_max_table_cells_reached")
        if budget.group_depth_reached:
            res.add_warning("pptx_group_depth_reached")
        if budget.charts_failed:
            res.add_warning("pptx_native_chart_unreadable")
        if budget.charts_truncated:
            res.add_warning("pptx_native_chart_truncated")

        md = res.metadata
        md["type"] = "pptx"
        md["pptx"] = {
            "slide_count": int(inv.get("slide_parts") or 0),
            "indexed_slides": len(slide_units),
            "notes_units": len(notes_units),
            "tables": n_tables,
            "extracted_chars": budget.chars,
            "table_cells": budget.table_cells,
            "image_only_slides": list(budget.image_only_slides)[:50],
            "empty_slides": list(budget.empty_slides)[:50],
            "unreadable_slides": list(budget.unreadable_slides)[:50],
            "notes_included": config.pptx_include_notes(),
            "alt_text_included": config.pptx_include_alt_text(),
        }
        if config.office_chart_data_enabled():
            md["pptx"]["native_charts"] = {"seen": budget.charts_seen, "extracted": budget.charts_extracted,
                                           "failed": budget.charts_failed, "truncated": budget.charts_truncated}
        md["slides"] = int(inv.get("slide_parts") or 0)
        md["tables"] = n_tables
        md["extractor"] = "pptx"
        md["extractor_version"] = EXTRACTOR_VERSION
        md["chunking_version"] = CHUNKING_VERSION

        res.partial = bool(
            budget.shapes_truncated
            or budget.chars_truncated
            or budget.table_cells_truncated
            or budget.unreadable_slides
        )

        return res.finalize(fmt="pptx", mime=M.PPTX, method="pptx:python-pptx")


def _is_picture(shape) -> bool:
    """Forme image (y compris placeholder image), hors vidéo et hors forme remplie."""
    try:
        from pptx.shapes.picture import Picture

        return isinstance(shape, Picture)
    except Exception:
        return False


def _slide_has_picture(slide) -> bool:
    """Une diapositive porte-t-elle au moins une image ?

    Sert uniquement à distinguer « diapositive image seule » de « diapositive vide » dans
    les avertissements : les deux ne se corrigent pas de la même façon côté utilisateur.
    """
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                return True
    except Exception:
        return False
    return False

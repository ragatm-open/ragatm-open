"""Fabriques PPTX DÉTERMINISTES avec médias, lors de la qualification.

Partagées par les tests unitaires (découverte) et d'intégration (ingestion). Tout
cas hostile est fabriqué à partir d'une présentation VALIDE produite par python-pptx,
puis réécrit à l'endroit exact que le test vise.
"""
from __future__ import annotations

import io
import re
import zipfile

from pptx import Presentation
from pptx.util import Emu

_LAYOUT_TITLE_CONTENT = 1
_LAYOUT_TITLE_ONLY = 5
_LAYOUT_BLANK = 6

LINKED_TARGET = "http://example.invalid/linked-image.png"


def png(w: int, h: int, couleur: tuple, forme: str = "ellipse") -> bytes:
    """PNG déterministe (contenu différent selon couleur/forme)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(img)
    box = [w // 8, h // 8, w - w // 8, h - h // 8]
    (d.ellipse if forme == "ellipse" else d.rectangle)(box, fill=couleur)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def raster(fmt: str, w: int = 200, h: int = 160, couleur=(30, 120, 200)) -> bytes:
    """Même image, encodée dans un autre format raster (GIF, BMP, TIFF, WEBP, JPEG)."""
    from PIL import Image

    img = Image.open(io.BytesIO(png(w, h, couleur)))
    if fmt == "GIF":
        img = img.convert("P")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


PHOTO = png(200, 160, (230, 120, 30))
AUTRE = png(180, 150, (40, 150, 70), forme="rect")
TROISIEME = png(190, 170, (150, 40, 150))
QUATRIEME = png(210, 150, (20, 160, 160), forme="rect")
CINQUIEME = png(170, 190, (200, 200, 20))
ICONE = png(32, 32, (20, 20, 20), forme="rect")
LOGO = png(160, 140, (10, 90, 180), forme="rect")


def build_pptx_with_media(slides, *, notes=None) -> bytes:
    """Présentation nominale via python-pptx.

    Chaque diapositive : ``title`` (str|None), ``bullets`` (liste de str), ``table``
    (liste de lignes), ``pictures`` (liste de dicts ``{"data", "alt", "grouped"}``).
    ``notes`` : ``{index_1_based: texte}``.
    """
    prs = Presentation()
    notes = notes or {}
    for i, spec in enumerate(slides, start=1):
        has_title = spec.get("title") is not None
        has_bullets = bool(spec.get("bullets"))
        if has_title and has_bullets:
            layout = prs.slide_layouts[_LAYOUT_TITLE_CONTENT]
        elif has_title:
            layout = prs.slide_layouts[_LAYOUT_TITLE_ONLY]
        else:
            layout = prs.slide_layouts[_LAYOUT_BLANK]
        slide = prs.slides.add_slide(layout)
        if has_title and slide.shapes.title is not None:
            slide.shapes.title.text = spec["title"]
        if has_bullets:
            body = next(ph for ph in slide.placeholders if ph.placeholder_format.idx != 0)
            tf = body.text_frame
            tf.clear()
            for j, text in enumerate(spec["bullets"]):
                para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                para.text = text
        if spec.get("table"):
            rows = spec["table"]
            n_cols = max(len(r) for r in rows)
            shape = slide.shapes.add_table(len(rows), n_cols, Emu(500_000), Emu(4_000_000),
                                           Emu(5_000_000), Emu(1_000_000))
            for r, row in enumerate(rows):
                for c in range(n_cols):
                    shape.table.cell(r, c).text = row[c] if c < len(row) else ""
        for k, pic in enumerate(spec.get("pictures") or ()):
            container = slide.shapes.add_group_shape().shapes if pic.get("grouped") else slide.shapes
            shape = container.add_picture(io.BytesIO(pic["data"]), Emu(500_000 + k * 1_200_000),
                                          Emu(1_500_000), Emu(1_000_000), Emu(1_000_000))
            if pic.get("alt"):
                shape._element._nvXxPr.cNvPr.set("descr", pic["alt"])
        if i in notes:
            slide.notes_slide.notes_text_frame.text = notes[i]
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _rewrite(data: bytes, transform) -> bytes:
    """Réécrit l'archive : ``transform(entries: dict[name, bytes]) -> dict``."""
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        entries = {info.filename: src.read(info.filename) for info in src.infolist()}
    entries = transform(dict(entries))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name, payload in entries.items():
            out.writestr(name, payload)
    return buf.getvalue()


def media_parts(data: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sorted(n for n in zf.namelist() if n.startswith("ppt/media/"))


def link_first_picture(data: bytes, *, slide: int = 1) -> bytes:
    """Transforme la première image de la diapositive en image LIÉE (``r:link`` externe).

    La relation interne d'origine reste dans le paquet : seule la référence du ``blip``
    change. Une découverte qui suivrait la liste des relations au lieu du ``blip`` la
    reprendrait — ce que le test doit précisément interdire.
    """
    slide_part = f"ppt/slides/slide{slide}.xml"
    rels_part = f"ppt/slides/_rels/slide{slide}.xml.rels"

    def _t(entries):
        xml = entries[slide_part].decode("utf-8")
        xml, n = re.subn(r'r:embed="[^"]+"', 'r:link="rIdLinked1"', xml, count=1)
        assert n == 1, "aucune image à lier"
        entries[slide_part] = xml.encode("utf-8")
        rels = entries[rels_part].decode("utf-8")
        rel = ('<Relationship Id="rIdLinked1" Type="http://schemas.openxmlformats.org/'
               'officeDocument/2006/relationships/image" '
               f'Target="{LINKED_TARGET}" TargetMode="External"/>')
        entries[rels_part] = rels.replace("</Relationships>", rel + "</Relationships>").encode("utf-8")
        return entries

    return _rewrite(data, _t)


def add_orphan_media(data: bytes, payload: bytes) -> bytes:
    """Ajoute un média présent dans ``ppt/media/`` mais référencé par AUCUNE relation."""
    def _t(entries):
        entries["ppt/media/orphan-image.png"] = payload
        return entries

    return _rewrite(data, _t)


def replace_first_media(data: bytes, payload: bytes) -> bytes:
    """Remplace les octets du premier média (nom et type de contenu inchangés)."""
    first = media_parts(data)[0]

    def _t(entries):
        entries[first] = payload
        return entries

    return _rewrite(data, _t)


def make_first_media_vector(data: bytes, *, ext: str = "emf",
                            content_type: str = "image/x-emf") -> bytes:
    """Déclare le premier média comme VECTORIEL (renommage + type de contenu)."""
    first = media_parts(data)[0]
    base = first.rsplit("/", 1)[1]
    new_base = base.rsplit(".", 1)[0] + "." + ext
    new_name = first.rsplit("/", 1)[0] + "/" + new_base

    def _t(entries):
        entries[new_name] = entries.pop(first)
        for name in list(entries):
            if name.startswith("ppt/slides/_rels/"):
                entries[name] = entries[name].replace(
                    f"../media/{base}".encode(), f"../media/{new_base}".encode())
        ct = entries["[Content_Types].xml"].decode("utf-8")
        if f'Extension="{ext}"' not in ct:
            ct = ct.replace("<Default ", f'<Default Extension="{ext}" ContentType="{content_type}"/><Default ', 1)
        entries["[Content_Types].xml"] = ct.encode("utf-8")
        return entries

    return _rewrite(data, _t)


# --------------------------------------------------------------------------- #
# Corpus FIGÉ de la barrière « vision éteinte » (référence capturée AVANT le lot)
# --------------------------------------------------------------------------- #
TITRE_LONG = ("Architecture de la plateforme : ce titre de diapositive porte assez de texte "
              "pour franchir sans ambiguïté le seuil applicatif de caractères utiles.")


def corpus_decks() -> dict:
    """Présentations du corpus figé, par nom. Construction déterministe."""
    principal = build_pptx_with_media(
        [
            {"title": "Texte seul", "bullets": ["Premier point", "Second point"]},
            {"title": None, "pictures": [{"data": PHOTO}]},
            {"title": None, "pictures": [{"data": AUTRE, "alt": "Schéma du réseau électrique"}]},
            {"title": TITRE_LONG, "bullets": ["Un point"], "pictures": [{"data": TROISIEME}]},
            {"title": None, "pictures": [{"data": QUATRIEME, "grouped": True}]},
            {"title": "Tableau et image", "table": [["A", "B"], ["1", "2"]],
             "pictures": [{"data": CINQUIEME, "alt": "Picture 7"}]},
            *({"title": f"Logo {n}", "pictures": [{"data": LOGO}]} for n in range(5)),
            {"title": None},
            {"title": "Icône", "pictures": [{"data": ICONE}]},
        ],
        notes={1: "Notes du présentateur", 4: "Autre note"},
    )
    return {
        "principal": principal,
        "lie": link_first_picture(build_pptx_with_media(
            [{"title": "Image liée", "pictures": [{"data": PHOTO}]}])),
        "orphelin": add_orphan_media(build_pptx_with_media(
            [{"title": "Avec orphelin", "bullets": ["texte"]}]), PHOTO),
        "vectoriel": make_first_media_vector(build_pptx_with_media(
            [{"title": None, "pictures": [{"data": PHOTO}]}])),
        "corrompu": replace_first_media(build_pptx_with_media(
            [{"title": "Image corrompue", "pictures": [{"data": PHOTO}]}]), b"\x89PNG not really"),
    }


def extraction_snapshot(result) -> dict:
    """Projection COMPARABLE d'un ``ExtractionResult`` PPTX (comportement utile)."""
    return {
        "status": result.status.value,
        "partial": bool(result.partial),
        "text": result.text,
        "slides": [{"text": s["text"], "meta": dict(s["meta"])} for s in result.slides],
        "blocks": [{"type": str(b.type), "text": b.text, "meta": dict(b.meta or {})}
                   for b in (result.blocks or [])],
        "tables": list(result.tables or []),
        "warnings": list(result.warnings),
        "metadata": dict(result.metadata),
        "method": result.method,
        "detected_mime": result.detected_mime,
        "visual_units": len(result.visual_units or []),
        "visual_assets": len(result.visual_assets or []),
    }

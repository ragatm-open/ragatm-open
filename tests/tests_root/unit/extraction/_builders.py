"""Constructeurs de fixtures binaires SÛRES et minimales pour les tests d'extraction.

Aucun fichier binaire n'est commité : on génère DOCX (zip) / PDF (reportlab) /
images (Pillow) à la volée.
"""
from __future__ import annotations

import io
import zipfile

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'
)


def make_docx(
    *,
    title: str = "Titre principal",
    paragraphs=("Bonjour le monde.",),
    list_items=("élément 1", "élément 2"),
    table=(("A", "B"), ("1", "2")),
    macro: bool = False,
) -> bytes:
    """Construit un .docx (ou .docm si macro=True) minimal et valide."""
    body = []
    if title:
        body.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>{title}</w:t></w:r></w:p>"
        )
    for p in paragraphs:
        body.append(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>")
    for it in list_items:
        body.append(
            f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
            f"<w:r><w:t>{it}</w:t></w:r></w:p>"
        )
    if table:
        rows = ""
        for row in table:
            cells = "".join(
                f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row
            )
            rows += f"<w:tr>{cells}</w:tr>"
        body.append(f"<w:tbl>{rows}</w:tbl>")

    document_xml = (
        f'<?xml version="1.0"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(body)}</w:body></w:document>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", document_xml)
        if macro:
            z.writestr("word/vbaProject.bin", b"\x00fake-macro\x00")
    return buf.getvalue()


def make_docx_raw(document_xml: str) -> bytes:
    """DOCX dont le `word/document.xml` est fourni TEL QUEL.

    Sert à fabriquer les fixtures hostiles (DOCTYPE / ENTITY / XML malformé) sans
    passer par le générateur nominal. Aucun binaire n'est commité.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def make_docx_with_unsafe_xml(kind: str = "doctype") -> bytes:
    """DOCX porteur d'une déclaration XML interdite (DOCTYPE / ENTITY).

    `billion_laughs` reste VOLONTAIREMENT minuscule : la garde doit refuser sur la
    *syntaxe* de déclaration, jamais après expansion. On ne veut pas d'une fixture
    qui ferait exploser la suite de tests si la garde régressait.
    """
    body = "<w:body><w:p><w:r><w:t>ok</w:t></w:r></w:p></w:body>"
    doc = f'<w:document xmlns:w="{_W}">{body}</w:document>'

    if kind == "doctype":
        prologue = '<!DOCTYPE w:document>'
    elif kind == "entity":
        prologue = '<!DOCTYPE w:document [ <!ENTITY x "boom"> ]>'
    elif kind == "billion_laughs":
        prologue = (
            '<!DOCTYPE w:document [\n'
            '  <!ENTITY a "aaaaaaaaaa">\n'
            '  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
            '  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
            ']>'
        )
    elif kind == "spaced":
        # variantes d'espacement / casse après "<!"
        prologue = '<!  doctype w:document [ <!\n  EnTiTy x "boom"> ]>'
    else:  # pragma: no cover - garde-fou de test
        raise ValueError(f"kind inconnu: {kind}")

    return make_docx_raw(f'<?xml version="1.0"?>{prologue}{doc}')


def make_docx_mentioning_doctype_in_text() -> bytes:
    """DOCX NORMAL dont le TEXTE contient les mots DOCTYPE/ENTITY.

    Garde-fou anti faux-positif : la garde doit matcher la *syntaxe* `<!DOCTYPE`,
    pas le vocabulaire.
    """
    return make_docx(
        title="Le DOCTYPE et les ENTITY en XML",
        paragraphs=("Un DOCTYPE déclare le type ; une ENTITY définit un raccourci.",),
        list_items=(),
        table=(),
    )


def make_zip_bomb_docx(*, entry_uncompressed_mb: int = 500) -> bytes:
    """DOCX valide structurellement mais avec une entrée hautement compressible
    (taille décompressée énorme) pour déclencher la protection zip-bomb."""
    big = b"0" * (entry_uncompressed_mb * 1024 * 1024)
    document_xml = (
        f'<?xml version="1.0"?>'
        f'<w:document xmlns:w="{_W}"><w:body>'
        f"<w:p><w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", document_xml)
        z.writestr("word/bomb.bin", big)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tableurs & Office hérité (lot E0 — sécurité). AUCUNE dépendance : stdlib zipfile,
# exactement comme pour le DOCX. `openpyxl` n'est PAS utilisé (et n'est pas installé).
# ---------------------------------------------------------------------------
_CT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_XLSX_MAIN_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_XLSM_MAIN_CT = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"

_SHEET_XML = (
    '<?xml version="1.0"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Projet</t></is></c></row></sheetData>'
    "</worksheet>"
)
# NB : `xmlns:r` DOIT être déclaré — `r:id` sans son espace de noms rend le XML
# invalide. Le lot E0 ne le voyait pas (le classeur était refusé sans être parsé) ;
# le lot E2 le parse vraiment.
_WORKBOOK_XML = (
    '<?xml version="1.0"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
)


def _xlsx_content_types(main_ct: str) -> str:
    return (
        f'<?xml version="1.0"?><Types xmlns="{_CT_TYPES_NS}">'
        f'<Override PartName="/xl/workbook.xml" ContentType="{main_ct}"/>'
        f"</Types>"
    )


def make_xlsx_zip(
    *,
    macro_binary: bool = False,
    macro_content_type: bool = False,
    workbook_part: bool = True,
    unsafe_xml: bool = False,
) -> bytes:
    """Classeur SpreadsheetML minimal et structurellement valide.

    - `macro_binary`      : ajoute `xl/vbaProject.bin` (signal .xlsm n°1) ;
    - `macro_content_type`: déclare un Content-Type `…macroEnabled…` (signal n°2) ;
    - `workbook_part`     : si False, l'archive a un dossier `xl/` INCOMPLET
                            (pas de `xl/workbook.xml`) -> ne doit PAS passer pour
                            un classeur ;
    - `unsafe_xml`        : injecte un DOCTYPE dans la feuille.

    Aucune macro n'est réelle : `vbaProject.bin` est un octet-stream factice, jamais
    ouvert ni exécuté par le code testé.
    """
    main_ct = _XLSM_MAIN_CT if macro_content_type else _XLSX_MAIN_CT
    sheet = _SHEET_XML
    if unsafe_xml:
        sheet = sheet.replace(
            '<?xml version="1.0"?>',
            '<?xml version="1.0"?><!DOCTYPE worksheet [ <!ENTITY x "boom"> ]>',
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _xlsx_content_types(main_ct))
        if workbook_part:
            z.writestr("xl/workbook.xml", _WORKBOOK_XML)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if macro_binary:
            z.writestr("xl/vbaProject.bin", b"\x00fake-macro-never-executed\x00")
    return buf.getvalue()


def make_xlsm_zip() -> bytes:
    """Classeur macro-enabled : binaire VBA **et** Content-Type macroEnabled."""
    return make_xlsx_zip(macro_binary=True, macro_content_type=True)


def make_odt_zip() -> bytes:
    """Texte OpenDocument minimal — même conteneur, `mimetype` normatif différent."""
    return make_ods_zip(mimetype="application/vnd.oasis.opendocument.text")


def make_ods_zip(*, mimetype: str = "application/vnd.oasis.opendocument.spreadsheet") -> bytes:
    """Classeur OpenDocument minimal (l'entrée `mimetype` est normative, non compressée)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # L'entrée `mimetype` doit être la première et STORED (spec OpenDocument).
        z.writestr(zipfile.ZipInfo("mimetype"), mimetype.encode("ascii"))
        z.writestr(
            "META-INF/manifest.xml",
            '<?xml version="1.0"?><manifest:manifest '
            'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>',
        )
        z.writestr(
            "content.xml",
            '<?xml version="1.0"?><office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>',
        )
    return buf.getvalue()


def make_legacy_ole_header(*, size: int = 1024) -> bytes:
    """En-tête de conteneur OLE2/CFB (famille .xls/.doc/.ppt héritée).

    On ne fabrique pas un vrai .xls : la signature suffit, et c'est bien tout ce que
    le code doit regarder (il ne prétend jamais identifier lequel des formats c'est).
    """
    magic = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    return magic + b"\x00" * max(0, size - len(magic))


def make_generic_zip() -> bytes:
    """Archive ZIP quelconque (ni Office, ni OpenDocument)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("readme.txt", "juste une archive")
        z.writestr("data/a.csv", "a,b\n1,2\n")
    return buf.getvalue()


def make_pptx_zip() -> bytes:
    """Archive PPTX MINIMALE — détection MIME seulement.

    Suffit à `detect_mime` (qui cherche `ppt/presentation.xml` sans le parser), mais
    n'est PAS une présentation ouvrable : `[Content_Types].xml` est vide, `p:presentation`
    n'a aucun namespace déclaré, il n'y a ni diapositive ni relations. Même piège que
    `_WORKBOOK_XML` pour Excel (cf. plus haut) : le lot E0 ne le voyait pas car le fichier
    était refusé AVANT tout parsing.

    Pour tester l'extracteur, utiliser `build_pptx()`.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation/>')
    return buf.getvalue()


def make_pptm_zip() -> bytes:
    """PPTX porteur d'un binaire VBA -> doit être détecté PPTM (macro-enabled)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation/>')
        z.writestr("ppt/vbaProject.bin", b"\x00\x01VBA")
    return buf.getvalue()


def make_pptm_zip_by_content_type() -> bytes:
    """PPTM déclaré par [Content_Types].xml SEUL, sans binaire VBA embarqué.

    Vérifie que la détection ne repose pas uniquement sur la présence de vbaProject.bin.
    """
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CT_TYPES_NS}">'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"/>'
        "</Types>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation/>')
    return buf.getvalue()


def make_ppt_media_only_zip() -> bytes:
    """ZIP portant un dossier `ppt/` INCOMPLET (des médias, pas de présentation).

    Ce n'est pas une présentation : doit rester un ZIP générique, comme un `xl/`
    incomplet reste un ZIP.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("ppt/media/image1.png", b"\x89PNG\r\n\x1a\n")
    return buf.getvalue()


def make_odp_zip() -> bytes:
    """Présentation OpenDocument (.odp) — hors périmètre, doit rester refusée."""
    mimetype = b"application/vnd.oasis.opendocument.presentation"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), mimetype, zipfile.ZIP_STORED)
        z.writestr("content.xml", '<?xml version="1.0"?><office:document-content/>')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Présentations .pptx RÉELLES. Deux voies, comme pour Excel :
#
#   - python-pptx pour les cas NOMINAUX (titres, listes, tableaux, groupes, notes) ;
#   - ZIP/XML stdlib pour ce que python-pptx NE SAIT PAS écrire : archive corrompue,
#     zip-bomb, XML piégé, package chiffré, macro.
# ---------------------------------------------------------------------------
_PPTX_LAYOUT_TITLE_CONTENT = 1
_PPTX_LAYOUT_TITLE_ONLY = 5
_PPTX_LAYOUT_BLANK = 6


def build_pptx(slides, *, notes=None) -> bytes:
    """Présentation nominale via python-pptx.

    `slides` : liste de dicts décrivant chaque diapositive, dans l'ordre :
      - ``title``    : str | None — titre (placeholder de titre)
      - ``bullets``  : liste de ``(texte, niveau)`` ou de str (niveau 0)
      - ``textboxes``: liste de ``(texte, top_emu, left_emu)`` — zones libres,
                       volontairement AJOUTÉES DANS LE DÉSORDRE par les tests d'ordre
      - ``table``    : liste de lignes (listes de cellules)
      - ``group``    : liste de ``(texte, top_emu, left_emu)`` — formes groupées
      - ``picture``  : bool — insère une vraie image PNG
      - ``alt_text`` : str — texte alternatif posé sur l'image

    `notes` : {index_1_based: "texte des notes"}.
    """
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    notes = notes or {}

    for i, spec in enumerate(slides, start=1):
        has_title = spec.get("title") is not None
        has_bullets = bool(spec.get("bullets"))
        if has_title and has_bullets:
            layout = prs.slide_layouts[_PPTX_LAYOUT_TITLE_CONTENT]
        elif has_title:
            layout = prs.slide_layouts[_PPTX_LAYOUT_TITLE_ONLY]
        else:
            layout = prs.slide_layouts[_PPTX_LAYOUT_BLANK]
        slide = prs.slides.add_slide(layout)

        if has_title and slide.shapes.title is not None:
            slide.shapes.title.text = spec["title"]

        if has_bullets:
            body = None
            for ph in slide.placeholders:
                if ph.placeholder_format.idx != 0:
                    body = ph
                    break
            if body is not None:
                tf = body.text_frame
                tf.clear()
                for j, item in enumerate(spec["bullets"]):
                    text, level = item if isinstance(item, tuple) else (item, 0)
                    para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                    para.text = text
                    para.level = level

        for text, top, left in spec.get("textboxes", []) or []:
            box = slide.shapes.add_textbox(Emu(left), Emu(top), Emu(2_000_000), Emu(500_000))
            box.text_frame.text = text

        if spec.get("table"):
            rows = spec["table"]
            n_rows, n_cols = len(rows), max(len(r) for r in rows)
            shape = slide.shapes.add_table(
                n_rows, n_cols, Emu(500_000), Emu(4_000_000), Emu(5_000_000), Emu(1_000_000)
            )
            for r, row in enumerate(rows):
                for c in range(n_cols):
                    shape.table.cell(r, c).text = row[c] if c < len(row) else ""

        if spec.get("group"):
            group = slide.shapes.add_group_shape()
            for text, top, left in spec["group"]:
                box = group.shapes.add_textbox(
                    Emu(left), Emu(top), Emu(2_000_000), Emu(500_000)
                )
                box.text_frame.text = text

        if spec.get("picture"):
            pic = slide.shapes.add_picture(
                io.BytesIO(make_png()), Emu(500_000), Emu(500_000),
                Emu(1_000_000), Emu(1_000_000),
            )
            if spec.get("alt_text"):
                pic._element._nvXxPr.cNvPr.set("descr", spec["alt_text"])

        if i in notes:
            slide.notes_slide.notes_text_frame.text = notes[i]

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _repack_pptx(data: bytes, *, replace=None, extra=None) -> bytes:
    """Réécrit une archive .pptx en remplaçant/ajoutant des entrées.

    Permet de fabriquer des cas hostiles à partir d'une présentation VALIDE, donc de
    n'altérer QUE ce que le test vise.
    """
    replace = replace or {}
    extra = extra or {}
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            payload = replace.get(info.filename, src.read(info.filename))
            out.writestr(info.filename, payload)
        for name, payload in extra.items():
            out.writestr(name, payload)
    src.close()
    return buf.getvalue()


def make_pptx_with_unsafe_xml() -> bytes:
    """Présentation VALIDE dont une diapositive porte un DOCTYPE -> doit être refusée."""
    base = build_pptx([{"title": "Titre", "bullets": ["a"]}])
    src = zipfile.ZipFile(io.BytesIO(base))
    slide_name = next(
        n for n in src.namelist()
        if n.startswith("ppt/slides/") and n.endswith(".xml") and "/_rels/" not in n
    )
    src.close()
    piege = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE p:sld [<!ENTITY lol "lol">]>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    ).encode("utf-8")
    return _repack_pptx(base, replace={slide_name: piege})


def make_pptx_zip_bomb(*, mb=300) -> bytes:
    """Présentation VALIDE + une entrée énorme et ultra-compressible."""
    base = build_pptx([{"title": "Titre", "bullets": ["a"]}])
    return _repack_pptx(base, extra={"ppt/media/bomb.bin": b"\x00" * (mb * 1024 * 1024)})


def make_pptx_encrypted() -> bytes:
    """Archive OOXML dont le paquet est chiffré (DRM/IRM)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation/>')
        z.writestr("EncryptedPackage", b"\x00\x01\x02chiffr\xc3\xa9")
    return buf.getvalue()


def make_pptx_corrupted() -> bytes:
    """En-tête ZIP valide, contenu tronqué -> archive illisible."""
    data = build_pptx([{"title": "Titre", "bullets": ["a"]}])
    return data[: len(data) // 3]


def make_pptx_missing_presentation() -> bytes:
    """Archive OOXML sans `ppt/presentation.xml` (partie canonique absente)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("ppt/slides/slide1.xml", '<?xml version="1.0"?><p:sld/>')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Classeurs .xlsx RÉELS (lot E2). Deux voies, délibérément :
#
#   - openpyxl pour les cas NOMINAUX (types, formats, feuilles, blocs) ;
#   - ZIP/XML stdlib (make_xlsx_zip / _raw_sheet) pour les cas qu'openpyxl NE SAIT
#     PAS écrire : valeur de formule en cache, cellule d'erreur typée, dimension
#     mensongère, sharedStrings volumineux.
#
# Aucun binaire n'est commité : tout est généré en mémoire.
# ---------------------------------------------------------------------------
def build_xlsx(sheets, *, hidden=(), very_hidden=()) -> bytes:
    """Classeur nominal via openpyxl.

    `sheets` : {nom: [[valeurs...], ...]}. Une valeur peut être un tuple
    ``(valeur, number_format)`` pour imposer un format (date, %, devise, durée).
    """
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append([v[0] if isinstance(v, tuple) else v for v in row])
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row, start=1):
                if isinstance(v, tuple):
                    ws.cell(row=r, column=c).number_format = v[1]
        if name in hidden:
            ws.sheet_state = "hidden"
        elif name in very_hidden:
            ws.sheet_state = "veryHidden"

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


_XL_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XL_RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_XL_CT = (
    '<?xml version="1.0"?>'
    f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml"'
    ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    "</Types>"
)
_XL_RELS = (
    '<?xml version="1.0"?>'
    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1"'
    f' Type="{_XL_RNS}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_XL_WB_RELS = (
    '<?xml version="1.0"?>'
    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_XL_RNS}/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)
_XL_WB = (
    '<?xml version="1.0"?>'
    f'<workbook xmlns="{_XL_NS}" xmlns:r="{_XL_RNS}">'
    '<sheets><sheet name="Feuil1" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
)


def raw_sheet(body: str, *, dimension: str | None = None) -> str:
    dim = f'<dimension ref="{dimension}"/>' if dimension else ""
    return (
        '<?xml version="1.0"?>'
        f'<worksheet xmlns="{_XL_NS}">{dim}<sheetData>{body}</sheetData></worksheet>'
    )


def make_raw_xlsx(sheet_xml: str, *, shared_strings: list | None = None,
                  extra: dict | None = None) -> bytes:
    """Classeur .xlsx construit À LA MAIN — contrôle total du XML produit.

    Quand `shared_strings` est fourni, la table est déclarée EN PLUS dans
    `[Content_Types].xml` et dans les relations du classeur : sans ces deux
    déclarations, openpyxl ne trouve pas la table et lève un `IndexError` sur le
    premier renvoi de chaîne. (openpyxl, lui, n'écrit JAMAIS de sharedStrings — il
    encode tout en `inlineStr` — d'où ce builder manuel.)
    """
    ct, wb_rels = _XL_CT, _XL_WB_RELS
    if shared_strings is not None:
        ct = ct.replace(
            "</Types>",
            '<Override PartName="/xl/sharedStrings.xml"'
            ' ContentType="application/vnd.openxmlformats-officedocument'
            '.spreadsheetml.sharedStrings+xml"/></Types>',
        )
        wb_rels = wb_rels.replace(
            "</Relationships>",
            f'<Relationship Id="rId2" Type="{_XL_RNS}/sharedStrings"'
            ' Target="sharedStrings.xml"/></Relationships>',
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", _XL_RELS)
        z.writestr("xl/workbook.xml", _XL_WB)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if shared_strings is not None:
            n = len(shared_strings)
            items = "".join(f"<si><t>{s}</t></si>" for s in shared_strings)
            z.writestr(
                "xl/sharedStrings.xml",
                f'<?xml version="1.0"?><sst xmlns="{_XL_NS}" count="{n}" '
                f'uniqueCount="{n}">{items}</sst>',
            )
        for name, content in (extra or {}).items():
            z.writestr(name, content)
    return buf.getvalue()


def make_xlsx_with_cached_formulas() -> bytes:
    """Formules AVEC et SANS valeur en cache + erreurs Excel typées.

    openpyxl ne sait écrire ni l'un ni l'autre : il n'émet jamais de `<v>` sous un
    `<f>`, et il écrit une erreur comme du texte. D'où le XML à la main.
    """
    body = (
        '<row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Projet</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Budget</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>Realise</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>Ecart</t></is></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2" t="inlineStr"><is><t>Atlas</t></is></c>'
        '<c r="B2"><v>250000</v></c>'
        '<c r="C2"><v>220000</v></c>'
        '<c r="D2"><f>B2-C2</f><v>30000</v></c>'          # AVEC cache
        "</row>"
        '<row r="3">'
        '<c r="A3" t="inlineStr"><is><t>Orion</t></is></c>'
        '<c r="B3"><v>180000</v></c>'
        '<c r="C3"><v>195000</v></c>'
        '<c r="D3"><f>B3-C3</f></c>'                       # SANS cache
        "</row>"
        '<row r="4">'
        '<c r="A4" t="inlineStr"><is><t>Vega</t></is></c>'
        '<c r="B4"><v>100000</v></c>'
        '<c r="C4"><v>0</v></c>'
        '<c r="D4" t="e"><v>#DIV/0!</v></c>'               # erreur typée
        "</row>"
    )
    return make_raw_xlsx(raw_sheet(body, dimension="A1:D4"))


def make_xlsx_lying_dimension() -> bytes:
    """Dimension déclarée `A1:A1`, contenu réel `A1:E10` -> 50 cellules.

    Sans `ws.reset_dimensions()`, openpyxl n'en rend QU'UNE (mesuré en E1).
    C'est la fixture qui verrouille la réserve la plus grave du spike.
    """
    rows = []
    for r in range(1, 11):
        cells = "".join(f'<c r="{chr(64 + c)}{r}"><v>{r * c}</v></c>' for c in range(1, 6))
        rows.append(f'<row r="{r}">{cells}</row>')
    return make_raw_xlsx(raw_sheet("".join(rows), dimension="A1:A1"))


def make_xlsx_huge_shared_strings(*, n: int = 40_000) -> bytes:
    """sharedStrings volumineux — doit être refusé AVANT tout parsing openpyxl."""
    strings = [f"chaine-unique-tres-longue-pour-gonfler-la-table-{i:08d}" for i in range(n)]
    body = "".join(
        f'<row r="{r}">'
        + "".join(
            f'<c r="{chr(64 + c)}{r}" t="s"><v>{(r - 1) * 5 + (c - 1)}</v></c>'
            for c in range(1, 6)
        )
        + "</row>"
        for r in range(1, (n // 5) + 1)
    )
    return make_raw_xlsx(raw_sheet(body), shared_strings=strings)


def make_xlsx_unsafe_xml() -> bytes:
    """Feuille portant une déclaration DOCTYPE/ENTITY -> garde XML du lot E0."""
    body = '<row r="1"><c r="A1"><v>1</v></c></row>'
    sheet = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE worksheet [ <!ENTITY x "boom"> ]>'
        f'<worksheet xmlns="{_XL_NS}"><sheetData>{body}</sheetData></worksheet>'
    )
    return make_raw_xlsx(sheet)


def make_xlsx_zip_bomb(*, mb: int = 300) -> bytes:
    """Classeur structurellement valide + une entrée énorme (garde ZIP du lot E0)."""
    body = '<row r="1"><c r="A1"><v>1</v></c></row>'
    return make_raw_xlsx(
        raw_sheet(body), extra={"xl/bomb.bin": b"0" * (mb * 1024 * 1024)}
    )


def make_pdf_with_text(text: str = "Bonjour ceci est un PDF texte natif.") -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_pdf_pages(texts, *, fontsize: int = 11) -> bytes:
    """PDF texte natif de ``len(texts)`` pages (une ligne par page), sans PyMuPDF."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for text in texts:
        c.setFont("Helvetica", fontsize)
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def make_pdf_image_only() -> bytes:
    """PDF sans texte extractible (uniquement un rectangle) -> candidat OCR."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(72, 600, 200, 120, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_png(width: int = 8, height: int = 8, color=(255, 255, 255)) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DOCX porteurs de MÉDIAS INTÉGRÉS.
#
# `make_docx` n'écrit que `[Content_Types].xml` + `word/document.xml` : il ne sait
# donc produire ni relation ni média, alors que TOUT l'adaptateur visuel DOCX repose
# sur le graphe de relations. D'où ce constructeur, qui reste en stdlib `zipfile`,
# comme le reste du fichier (python-docx n'est pas installé, et ne doit pas l'être).
# ---------------------------------------------------------------------------
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMG_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def _blip_run(rid: str, *, anchored: bool = False, linked: bool = False) -> str:
    """Un run portant une image DrawingML.

    `anchored` : `wp:anchor` (image flottante) au lieu de `wp:inline`.
    `linked`   : `r:link` (image LIÉE, non embarquée) au lieu de `r:embed`.
    """
    frame = "wp:anchor" if anchored else "wp:inline"
    attr = "r:link" if linked else "r:embed"
    return (
        f"<w:r><w:drawing><{frame}>"
        f"<a:graphic><a:graphicData><pic:pic><pic:blipFill>"
        f'<a:blip {attr}="{rid}"/>'
        f"</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
        f"</{frame}></w:drawing></w:r>"
    )


def make_docx_with_media(
    *,
    title: str = "Rapport",
    paragraphs=("Texte du corps.",),
    inline_images=(),
    table_images=(),
    anchored_images=(),
    repeated_image=None,
    linked_image: bool = False,
    external_embed: bool = False,
    traversal_rel: bool = False,
    broken_rel: bool = False,
    malformed_media: bool = False,
    orphan_media=None,
) -> bytes:
    """DOCX minimal valide portant des médias et leurs relations.

    - `inline_images`   : octets -> un paragraphe-image chacun (`wp:inline`) ;
    - `table_images`    : octets -> un tableau 1×1 contenant l'image ;
    - `anchored_images` : octets -> image flottante (`wp:anchor`) ;
    - `repeated_image`  : `(octets, n)` -> n paragraphes réutilisant LE MÊME rId ;
    - `linked_image`    : ajoute un `a:blip/@r:link` + relation `TargetMode="External"` ;
    - `external_embed`  : `@r:embed` pointant une relation externe (cas plus retors) ;
    - `traversal_rel`   : relation dont la cible sort du paquet (`../../secret.png`) ;
    - `broken_rel`      : `@r:embed` vers un rId ABSENT des relations ;
    - `malformed_media` : relation + partie présentes, octets non décodables ;
    - `orphan_media`    : octets déposés dans `word/media/` SANS aucune relation.

    Aucun média n'est commité : tout est généré en mémoire.
    """
    body: list[str] = []
    rels: list[str] = []
    media: dict[str, bytes] = {}
    compteur = {"rel": 0, "img": 0}

    def _ajouter_media(payload: bytes) -> str:
        compteur["rel"] += 1
        compteur["img"] += 1
        rid = f"rId{compteur['rel']}"
        nom = f"image{compteur['img']}.png"
        media[f"word/media/{nom}"] = payload
        rels.append(
            f'<Relationship Id="{rid}" Type="{_IMG_REL_TYPE}" Target="media/{nom}"/>'
        )
        return rid

    if title:
        body.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>{title}</w:t></w:r></w:p>"
        )
    for p in paragraphs:
        body.append(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>")

    for payload in inline_images:
        body.append(f"<w:p>{_blip_run(_ajouter_media(payload))}</w:p>")

    for payload in anchored_images:
        body.append(f"<w:p>{_blip_run(_ajouter_media(payload), anchored=True)}</w:p>")

    for payload in table_images:
        run = _blip_run(_ajouter_media(payload))
        body.append(f"<w:tbl><w:tr><w:tc><w:p>{run}</w:p></w:tc></w:tr></w:tbl>")

    if repeated_image is not None:
        payload, n = repeated_image
        rid = _ajouter_media(payload)
        for _ in range(int(n)):
            body.append(f"<w:p>{_blip_run(rid)}</w:p>")

    if linked_image:
        compteur["rel"] += 1
        rid = f"rId{compteur['rel']}"
        rels.append(
            f'<Relationship Id="{rid}" Type="{_IMG_REL_TYPE}"'
            f' Target="https://exemple.invalid/logo.png" TargetMode="External"/>'
        )
        body.append(f"<w:p>{_blip_run(rid, linked=True)}</w:p>")

    if external_embed:
        compteur["rel"] += 1
        rid = f"rId{compteur['rel']}"
        rels.append(
            f'<Relationship Id="{rid}" Type="{_IMG_REL_TYPE}"'
            f' Target="https://exemple.invalid/embed.png" TargetMode="External"/>'
        )
        body.append(f"<w:p>{_blip_run(rid)}</w:p>")

    if traversal_rel:
        compteur["rel"] += 1
        rid = f"rId{compteur['rel']}"
        rels.append(
            f'<Relationship Id="{rid}" Type="{_IMG_REL_TYPE}"'
            f' Target="../../../secret.png"/>'
        )
        body.append(f"<w:p>{_blip_run(rid)}</w:p>")

    if broken_rel:
        body.append(f"<w:p>{_blip_run('rId9999')}</w:p>")

    if malformed_media:
        compteur["rel"] += 1
        compteur["img"] += 1
        rid = f"rId{compteur['rel']}"
        nom = f"image{compteur['img']}.png"
        media[f"word/media/{nom}"] = b"ceci n'est pas une image"
        rels.append(
            f'<Relationship Id="{rid}" Type="{_IMG_REL_TYPE}" Target="media/{nom}"/>'
        )
        body.append(f"<w:p>{_blip_run(rid)}</w:p>")

    if orphan_media is not None:
        media["word/media/orpheline.png"] = orphan_media

    document_xml = (
        '<?xml version="1.0"?>'
        f'<w:document xmlns:w="{_W}" xmlns:r="{_R_NS}" xmlns:a="{_A_NS}"'
        f' xmlns:wp="{_WP_NS}" xmlns:pic="{_PIC_NS}">'
        f'<w:body>{"".join(body)}</w:body></w:document>'
    )
    rels_xml = (
        '<?xml version="1.0"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">{"".join(rels)}</Relationships>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("word/document.xml", document_xml)
        z.writestr("word/_rels/document.xml.rels", rels_xml)
        for nom, payload in media.items():
            z.writestr(nom, payload)
    return buf.getvalue()

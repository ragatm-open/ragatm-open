"""Fixtures DXF synthétiques, générées par ezdxf en mémoire.

Déterministes à contenu égal ; jamais paramétrer un test avec les OCTETS produits
(les en-têtes DXF portent des horodatages : deux écritures diffèrent, et xdist exige
des identifiants de tests identiques entre workers) — passer des constructeurs.
"""
from __future__ import annotations

import io


def to_bytes(doc, *, fmt: str = "asc") -> bytes:
    if fmt == "bin":
        b = io.BytesIO()
        doc.write(b, fmt="bin")
        return b.getvalue()
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode(doc.encoding if doc.dxfversion < "AC1021" else "utf-8")


def new(version="R2018", *, setup=False):
    import ezdxf

    doc = ezdxf.new(version, setup=setup)
    doc.header["$INSUNITS"] = 4  # millimètres
    return doc


def build_basic() -> bytes:
    """DXF-1 : Model, calque CVC, TEXT « CTA-03 », MTEXT « Débit 12000 m3/h »."""
    doc = new()
    doc.layers.add("CVC")
    msp = doc.modelspace()
    msp.add_text("CTA-03", dxfattribs={"layer": "CVC"})
    msp.add_mtext("Débit 12000 m3/h", dxfattribs={"layer": "CVC"})
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "CVC"})
    return to_bytes(doc)


def build_layouts() -> bytes:
    """DXF-2 : Model / RDC / R+1, plusieurs calques, ordre document contrôlé."""
    doc = new()
    for name in ("CVC", "ELEC_CFO", "STRUCTURE"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_text("Gaine principale", dxfattribs={"layer": "CVC"})
    msp.add_text("Poteau P1", dxfattribs={"layer": "STRUCTURE"})
    msp.add_text("Gaine secondaire", dxfattribs={"layer": "CVC"})
    rdc = doc.layouts.new("RDC")
    rdc.add_text("Tableau TGBT principal", dxfattribs={"layer": "ELEC_CFO"})
    rdc.add_text("Chemin de cable CF01", dxfattribs={"layer": "ELEC_CFO"})
    r1 = doc.layouts.new("R+1")
    r1.add_text("Local technique R+1", dxfattribs={"layer": "CVC"})
    return to_bytes(doc)


def build_block_attribs() -> bytes:
    """DXF-3 : bloc CTA avec attributs REPERE / DEBIT / PUISSANCE."""
    doc = new()
    doc.layers.add("CVC_EQUIPEMENTS")
    blk = doc.blocks.new("CTA")
    blk.add_text("Centrale de traitement d'air")
    blk.add_attdef("REPERE", (0, 0), dxfattribs={"prompt": "Repère"})
    blk.add_attdef("DEBIT", (0, 1))
    blk.add_attdef("PUISSANCE", (0, 2))
    msp = doc.modelspace()
    ins = msp.add_blockref("CTA", (10, 10), dxfattribs={"layer": "CVC_EQUIPEMENTS"})
    ins.add_auto_attribs({"REPERE": "CTA-03", "DEBIT": "12000 m3/h", "PUISSANCE": "45 kW"})
    # Un second bloc sans attributs : compté, jamais une unité.
    doc.blocks.new("PORTE").add_line((0, 0), (1, 0))
    msp.add_blockref("PORTE", (0, 0))
    return to_bytes(doc)


def build_dimension() -> bytes:
    """DXF-4 : cote linéaire 2500 (mm) + cote avec texte de surcharge."""
    doc = new(setup=True)
    doc.layers.add("COTES")
    msp = doc.modelspace()
    msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(2500, 0), dxfattribs={"layer": "COTES"}).render()
    msp.add_linear_dim(base=(0, 15), p1=(0, 10), p2=(1200, 10), text="<> (hors tout)",
                       dxfattribs={"layer": "COTES"}).render()
    msp.add_linear_dim(base=(0, 25), p1=(0, 20), p2=(800, 20), text="VOIR DETAIL",
                       dxfattribs={"layer": "COTES"}).render()
    return to_bytes(doc)


def build_dimension_unset_measurement() -> bytes:
    """Cote dont la mesure déclarée (code 42) vaut la sentinelle -1, comme l'écrit GNU LibreDWG
    0.14 pour 95 % des cotes des plans DWG réels observés (FORMATS-DWG-STAGING-QUALIFICATION)."""
    doc = new(setup=True)
    doc.layers.add("COTES")
    msp = doc.modelspace()
    dim = msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(2100, 0), dxfattribs={"layer": "COTES"})
    dim.render()
    dim.dimension.dxf.actual_measurement = -1.0
    return to_bytes(doc)


def build_xref(path: str = "architecte.dwg", name: str = "ARCHI") -> bytes:
    """DXF-5 : XREF déclarée (jamais ouverte)."""
    doc = new()
    doc.add_xref_def(path, name)
    doc.modelspace().add_text("Plan de base", dxfattribs={"layer": "0"})
    return to_bytes(doc)


def build_geometry_noise(n_lines: int = 3000) -> bytes:
    """DXF-12 : des milliers de LINE et UN texte utile."""
    doc = new()
    doc.layers.add("TRAITS")
    doc.layers.add("NOTES")
    msp = doc.modelspace()
    for i in range(n_lines):
        msp.add_line((i, 0), (i, 1), dxfattribs={"layer": "TRAITS"})
    msp.add_text("Local ventilation zorglub", dxfattribs={"layer": "NOTES"})
    return to_bytes(doc)


def build_long_layer(n_texts: int = 120) -> bytes:
    """DXF-11 : beaucoup d'annotations sur le même layout + calque."""
    doc = new()
    doc.layers.add("ELEC_CFO")
    msp = doc.modelspace()
    for i in range(n_texts):
        msp.add_text(f"Annotation electrique numero {i} armoire AR-{i:03d}", dxfattribs={"layer": "ELEC_CFO"})
    return to_bytes(doc)


def build_entities(n: int) -> bytes:
    """DXF-7 : exactement n entités graphiques (LINE) dans Model, aucun bloc."""
    doc = new()
    msp = doc.modelspace()
    for i in range(n):
        msp.add_line((i, 0), (i, 1))
    return to_bytes(doc)


def build_mtext_formatted() -> bytes:
    """DXF-17 : MTEXT avec codes de formatage (police, gras, retour, symbole)."""
    doc = new()
    doc.modelspace().add_mtext("{\\fArial|b1;Puissance}\\P\\C1;45 kW %%d", dxfattribs={"layer": "0"})
    return to_bytes(doc)


def build_empty() -> bytes:
    """DXF-18 : dessin sans aucune entité."""
    return to_bytes(new())


def build_geometry_only() -> bytes:
    doc = new()
    doc.layers.add("TRAITS")
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "TRAITS"})
    return to_bytes(doc)


def build_layer_states() -> bytes:
    """Calques éteint / gelé : leur contenu reste extrait."""
    doc = new()
    doc.layers.add("VISIBLE")
    doc.layers.add("ETEINT").off()
    doc.layers.add("GELE").freeze()
    msp = doc.modelspace()
    msp.add_text("Texte visible", dxfattribs={"layer": "VISIBLE"})
    msp.add_text("Texte calque eteint", dxfattribs={"layer": "ETEINT"})
    msp.add_text("Texte calque gele", dxfattribs={"layer": "GELE"})
    return to_bytes(doc)


def build_binary() -> bytes:
    """DXF-13 : DXF binaire (« AutoCAD Binary DXF »)."""
    doc = new()
    doc.layers.add("CVC")
    doc.modelspace().add_text("CTA-BIN-07", dxfattribs={"layer": "CVC"})
    return to_bytes(doc, fmt="bin")


def build_r2000_cp1252() -> bytes:
    """Version < R2007 : encodage $DWGCODEPAGE (cp1252), accents non UTF-8."""
    import ezdxf

    doc = ezdxf.new("R2000")
    doc.modelspace().add_text("Débit réglé à 12000", dxfattribs={"layer": "0"})
    return to_bytes(doc)


def build_mleader() -> bytes:
    from ezdxf.math import Vec2

    doc = new(setup=True)
    ml = doc.modelspace().add_multileader_mtext()
    ml.set_content("Note repere ML-1")
    ml.build(insert=Vec2(5, 5))
    return to_bytes(doc)

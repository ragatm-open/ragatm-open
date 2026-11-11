"""Fixtures IFC synthétiques, générées par `ifcopenshell.api` en mémoire.

GlobalId FIXES (jamais `ifcopenshell.guid.new()` dans une paramétrisation xdist) ;
passer des constructeurs aux tests, jamais des octets (l'en-tête porte un horodatage).
IFC2X3 exige un historique de propriétaire : posé via les réglages de l'API.
"""
from __future__ import annotations

GID_DOOR = "2Fabcdefghijklmnopqr0D"
GID_SPACE = "2Sabcdefghijklmnopqr0S"
GID_STOREY = "2Labcdefghijklmnopqr0L"
GID_BUILDING = "2Babcdefghijklmnopqr0B"
GID_SITE = "2Tabcdefghijklmnopqr0T"
GID_PROJECT = "2Pabcdefghijklmnopqr0P"
GID_TERMINAL = "2Vabcdefghijklmnopqr0V"


def _api():
    import ifcopenshell
    import ifcopenshell.api
    import ifcopenshell.api.owner.settings as owner_settings

    return ifcopenshell, ifcopenshell.api.run, owner_settings


def new(schema="IFC4"):
    ifcopenshell, run, owner_settings = _api()
    f = ifcopenshell.file(schema=schema)
    if schema == "IFC2X3":
        person = run("owner.add_person", f)
        org = run("owner.add_organisation", f)
        po = run("owner.add_person_and_organisation", f, person=person, organisation=org)
        app = run("owner.add_application", f)
        owner_settings.get_user = lambda ifc: po
        owner_settings.get_application = lambda ifc: app
    return f


def to_bytes(f) -> bytes:
    return f.to_string().encode("utf-8")


def _hierarchy(f, run, *, with_space=True):
    proj = run("root.create_entity", f, ifc_class="IfcProject", name="Centre hospitalier")
    proj.GlobalId = GID_PROJECT
    run("unit.assign_unit", f)
    site = run("root.create_entity", f, ifc_class="IfcSite", name="Site principal")
    site.GlobalId = GID_SITE
    bld = run("root.create_entity", f, ifc_class="IfcBuilding", name="Building A")
    bld.GlobalId = GID_BUILDING
    sto = run("root.create_entity", f, ifc_class="IfcBuildingStorey", name="Level 02")
    sto.GlobalId = GID_STOREY
    run("aggregate.assign_object", f, relating_object=proj, products=[site])
    run("aggregate.assign_object", f, relating_object=site, products=[bld])
    run("aggregate.assign_object", f, relating_object=bld, products=[sto])
    spc = None
    if with_space:
        spc = run("root.create_entity", f, ifc_class="IfcSpace", name="2.14")
        spc.GlobalId = GID_SPACE
        spc.LongName = "Local technique 2.14"
        run("aggregate.assign_object", f, relating_object=sto, products=[spc])
    return proj, site, bld, sto, spc


def build_full(schema="IFC4") -> bytes:
    """IFC-1/2/3 + 4..9 : hiérarchie complète, porte riche (psets, quantités, type,
    matériau, système, classification), mur pauvre, terminal CVC (IFC4+)."""
    f = new(schema)
    _, run, _ = _api()
    proj, site, bld, sto, spc = _hierarchy(f, run)
    door = run("root.create_entity", f, ifc_class="IfcDoor", name="D-201",
               predefined_type=None if schema == "IFC2X3" else "DOOR")
    door.GlobalId = GID_DOOR
    door.Description = "Porte coupe-feu du local technique"
    door.Tag = "PORTE-201"
    run("spatial.assign_container", f, relating_structure=spc, products=[door])
    pset = run("pset.add_pset", f, product=door, name="Pset_DoorCommon")
    run("pset.edit_pset", f, pset=pset, properties={"FireRating": "EI60", "IsExternal": False})
    qto = run("pset.add_qto", f, product=door, name="Qto_DoorBaseQuantities")
    run("pset.edit_qto", f, qto=qto, properties={"Height": 2100.0, "Width": 900.0, "Area": 1.89})
    dtype = run("root.create_entity", f, ifc_class="IfcDoorStyle" if schema == "IFC2X3" else "IfcDoorType",
                name="Porte EI60 900x2100")
    run("type.assign_type", f, related_objects=[door], relating_type=dtype)
    mat = run("material.add_material", f, name="Acier")
    run("material.assign_material", f, products=[door], material=mat)
    sysm = run("system.add_system", f, ifc_class="IfcSystem")
    sysm.Name = "VENTILATION-CTA03"
    run("system.assign_system", f, products=[door], system=sysm)
    cls = run("classification.add_classification", f, classification="Uniclass 2015")
    run("classification.add_reference", f, products=[door], classification=cls,
        identification="Pr_30_59_98", name="Doors")
    for i in range(3):
        wall = run("root.create_entity", f, ifc_class="IfcWall", name="")
        wall.GlobalId = f"2Wabcdefghijklmnopq{i:03d}"[:22]
        run("spatial.assign_container", f, relating_structure=sto, products=[wall])
    if schema != "IFC2X3":
        term = run("root.create_entity", f, ifc_class="IfcFlowTerminal", name="CTA-03")
        term.GlobalId = GID_TERMINAL
        run("spatial.assign_container", f, relating_structure=spc, products=[term])
        dsys = run("system.add_system", f, ifc_class="IfcDistributionSystem")
        dsys.Name = "CVC-EXTRACTION"
        run("system.assign_system", f, products=[term], system=dsys)
    return to_bytes(f)


def build_external_refs() -> bytes:
    """IFC-71 : références externes (document, classification) — jamais résolues."""
    f = new("IFC4")
    _, run, _ = _api()
    proj, site, bld, sto, spc = _hierarchy(f, run)
    door = run("root.create_entity", f, ifc_class="IfcDoor", name="D-EXT")
    door.GlobalId = GID_DOOR
    run("spatial.assign_container", f, relating_structure=spc, products=[door])
    f.create_entity("IfcDocumentReference", Location="file:///etc/passwd", Name="Notice fabricant")
    cls = run("classification.add_classification", f, classification="Referentiel externe")
    cls.Location = "https://example.invalid/classif"
    run("classification.add_reference", f, products=[door], classification=cls,
        identification="X-01", name="Ref externe")
    ref = door.HasAssociations[0].RelatingClassification
    ref.Location = "https://example.invalid/classif/X-01"
    return to_bytes(f)


def build_uncontained() -> bytes:
    """IFC-49 : élément sans conteneur spatial — extrait quand même."""
    f = new("IFC4")
    _, run, _ = _api()
    _hierarchy(f, run, with_space=False)
    door = run("root.create_entity", f, ifc_class="IfcDoor", name="D-ORPHAN")
    door.GlobalId = GID_DOOR
    door.Description = "Porte hors hiérarchie"
    return to_bytes(f)


def build_long_element(n_props: int = 120) -> bytes:
    """IFC-90 : élément à très nombreuses propriétés textuelles."""
    f = new("IFC4")
    _, run, _ = _api()
    proj, site, bld, sto, spc = _hierarchy(f, run)
    door = run("root.create_entity", f, ifc_class="IfcDoor", name="D-LONG")
    door.GlobalId = GID_DOOR
    run("spatial.assign_container", f, relating_structure=spc, products=[door])
    pset = run("pset.add_pset", f, product=door, name="Pset_Custom")
    run("pset.edit_pset", f, pset=pset,
        properties={f"Propriete_{i:03d}": f"Valeur descriptive numero {i} de la porte longue" for i in range(n_props)})
    return to_bytes(f)


def build_poor_elements(n: int = 12, schema="IFC4") -> bytes:
    """IFC-77 : n murs sans information → un groupe, pas n unités."""
    f = new(schema)
    _, run, _ = _api()
    proj, site, bld, sto, spc = _hierarchy(f, run)
    wtype = run("root.create_entity", f, ifc_class="IfcWallType", name="Mur beton 20 cm")
    for i in range(n):
        wall = run("root.create_entity", f, ifc_class="IfcWall", name="")
        wall.GlobalId = f"2Wabcdefghijklmnopq{i:03d}"[:22]
        run("spatial.assign_container", f, relating_structure=sto, products=[wall])
        run("type.assign_type", f, related_objects=[wall], relating_type=wtype)
    return to_bytes(f)


def build_empty() -> bytes:
    f = new("IFC4")
    return to_bytes(f)


def build_statements(n: int, schema="IFC4") -> bytes:
    """IFC-40 : exactement n déclarations STEP (`#k=…`), sans passer par l'API."""
    lines = ["ISO-10303-21;", "HEADER;", "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');",
             "FILE_NAME('t.ifc','2026-01-01T00:00:00',(''),(''),'','','');", f"FILE_SCHEMA(('{schema}'));",
             "ENDSEC;", "DATA;"]
    for k in range(1, n + 1):
        lines.append(f"#{k}=IFCPROPERTYSINGLEVALUE('P{k}',$,IFCLABEL('v'),$);")
    lines += ["ENDSEC;", "END-ISO-10303-21;", ""]
    return "\n".join(lines).encode()


def build_header_only(schema: str) -> bytes:
    return ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((''),'2;1');\nFILE_NAME('','',(''),(''),'','','');\n"
            f"FILE_SCHEMA(('{schema}'));\nENDSEC;\nDATA;\n#1=IFCPROJECT('2Pabcdefghijklmnopqr0P',$,'P',$,$,$,$,$,$);\n"
            "ENDSEC;\nEND-ISO-10303-21;\n").encode()

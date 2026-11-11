#!/usr/bin/env python3
"""Corpus de qualification interne — ENTIÈREMENT SYNTHÉTIQUE et reproductible.

Univers fictif : « Observatoire Fictif des Lacs d'Altitude » (OFLA). Aucun nom, lieu,
mesure ou document réel. Chaque fait est inventé pour ce corpus ; le manifeste
(`manifest.json`) porte pour chaque fichier : nom, sha256, origine synthétique, version,
questions de référence, passages et pages attendus.

Formats : TXT, Markdown, PDF textuel (reportlab), DOCX (OOXML minimal, stdlib).
Langues : français et anglais. Un document long (PDF 14 pages), plusieurs documents
complémentaires, un document sans réponse à une question prévue, deux documents à
tableaux simples (Markdown et DOCX).

Usage : python generate_corpus.py <dossier_sortie>
"""
from __future__ import annotations

import hashlib, json, sys, zipfile, datetime
from pathlib import Path

VERSION = "1.0.0"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "corpus")
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- textes
DOC_FR_VERMEIL = """OFLA — Fiche descriptive du lac de Vermeil (document synthétique)

1. Situation
Le lac de Vermeil est un lac fictif d'altitude suivi par l'Observatoire Fictif des Lacs
d'Altitude (OFLA). Il est situé dans le massif imaginaire des Trois Cornes, à une altitude
de 2 145 mètres. Sa superficie est de 38 hectares.

2. Bathymétrie
La profondeur maximale du lac de Vermeil est de 47 mètres, relevée au point dit « la Fosse
Bleue ». La profondeur moyenne est de 19 mètres. Le volume estimé est de 7,2 millions de
mètres cubes.

3. Régime thermique
La température de surface la plus élevée enregistrée en 2025 au lac de Vermeil a été de
16,4 °C, le 9 août 2025, à la station Nord. La couche de saut thermique se situe entre
8 et 11 mètres en été.

4. Faune
Le lac abrite une population introduite d'omble chevalier fictif (Salvelinus imaginarius)
et une espèce de copépode endémique inventée, Cyclops vermilionis.

5. Gestion
Le niveau du lac est contrôlé par un seuil naturel. Aucun prélèvement d'eau à usage
industriel n'est autorisé. Le sentier d'accès est fermé du 1er novembre au 31 mai.
"""

DOC_FR_AURORE = """OFLA — Fiche descriptive du lac Aurore (document synthétique)

Le lac Aurore est le second lac fictif suivi par l'OFLA. Il se situe à 2 402 mètres
d'altitude dans le cirque imaginaire de Perle-Haute.

Sa profondeur maximale est de 23 mètres et sa profondeur moyenne de 9 mètres.
La superficie est de 12 hectares.

En 2025, la température de surface maximale du lac Aurore a été de 13,1 °C, mesurée le
21 juillet 2025 à la station Est.

Le lac Aurore gèle en moyenne 182 jours par an. Sa transparence (disque de Secchi) atteint
11 mètres en septembre, la plus forte des lacs suivis.
"""

DOC_EN_SENSORS = """OFLA — Instrumentation note: sensors deployed on the monitored lakes (synthetic document)

This note describes the fictional instrumentation used by the Fictional Observatory of
Alpine Lakes (OFLA). All device names are invented.

Temperature. Water temperature is measured with the Thermion-Alp 220 chain, which carries
twelve thermistors spaced one metre apart from the surface to twelve metres depth. The
chain logs every ten minutes.

Turbidity. The turbidity sensor model is Hydra-TX-9. It uses a 860 nm infrared source and
reports in formazin nephelometric units (FNU). The Hydra-TX-9 is cleaned by a wiper every
six hours.

Dissolved oxygen. Dissolved oxygen is measured with an optical probe named OxyLume S3.
The probe is recalibrated every 90 days against water-saturated air.

Water level. Water level is measured by a pressure transducer named LevelSentinel P40
installed at the natural sill of each lake.

Data transmission. Each station transmits over a fictional low-power radio link named
AlpLink once per hour. Data are stored on the OFLA server named "glacier-01", which is a
fictional hostname used only in this synthetic corpus.
"""

DOC_EN_STATIONS_MD = """# OFLA — Monitoring stations (synthetic document)

The Fictional Observatory of Alpine Lakes operates four monitoring stations. The table
below lists their fictional coordinates and altitudes.

| Station | Lake | Altitude (m) | Latitude (fictional) | Longitude (fictional) |
|---|---|---|---|---|
| North station | Lake Vermeil | 2310 | 45.1234 N | 6.5678 E |
| South station | Lake Vermeil | 2150 | 45.1200 N | 6.5700 E |
| East station | Lake Aurore | 2415 | 45.2001 N | 6.6002 E |
| West station | Lake Aurore | 2408 | 45.2010 N | 6.5950 E |

## Notes

- The North station is the highest station on Lake Vermeil and hosts the Thermion-Alp 220
  reference chain.
- The East station on Lake Aurore is powered by a 120 W solar panel.
- Station identifiers in the data files are N1, S1, E1 and W1 respectively.
"""

DOC_FR_MENU = """OFLA — Menu de la cantine du refuge de la Perle-Haute (document synthétique, sans rapport)

Semaine type (fiction) :
- Lundi : soupe d'orge perlé, gratin de courge, compote de poire.
- Mardi : salade de lentilles, polenta aux champignons, tarte aux myrtilles.
- Mercredi : velouté de carottes, raclette végétale, fruits de saison.
- Jeudi : soupe au pistou, pâtes aux noix, yaourt de brebis.
- Vendredi : salade composée, risotto au safran, gâteau au chocolat.

Le refuge accueille les équipes de l'observatoire pendant la campagne estivale. Les repas
sont servis à 12 h 30 et 19 h 00. Les réservations se font la veille avant 18 h.
"""

# Document LONG (PDF 14 pages) : protocole d'échantillonnage, un fait distinct par page.
PROTO_PAGES = [
 ("1. Objet du protocole",
  "Le présent protocole fictif décrit les règles d'échantillonnage des lacs suivis par l'OFLA. "
  "Il s'applique au lac de Vermeil et au lac Aurore. Il est rédigé pour un corpus synthétique "
  "de qualification et ne décrit aucune procédure réelle. La version en vigueur est la 3.2, "
  "adoptée le 14 mars 2025 par le comité technique fictif."),
 ("2. Périodicité",
  "Les prélèvements ont lieu toutes les deux semaines entre le 15 juin et le 15 octobre. "
  "Hors de cette période, un prélèvement mensuel est réalisé lorsque l'accès est possible. "
  "Chaque campagne dure au plus deux jours consécutifs."),
 ("3. Points de prélèvement",
  "Trois points sont échantillonnés au lac de Vermeil : la Fosse Bleue (point V1), l'exutoire "
  "(point V2) et la baie des Mélèzes (point V3). Deux points sont échantillonnés au lac Aurore : "
  "le centre (point A1) et l'exutoire (point A2)."),
 ("4. Profondeurs de prélèvement",
  "À chaque point, l'eau est prélevée à 0,5 mètre sous la surface, à mi-profondeur et à un mètre "
  "au-dessus du fond. Au point V1, la profondeur de fond retenue est 46 mètres, soit un mètre "
  "au-dessus de la profondeur maximale de 47 mètres."),
 ("5. Conditions de suspension",
  "Les prélèvements sont suspendus lorsque deux conditions se présentent. Première condition : "
  "la vitesse du vent mesurée à la station Nord dépasse 40 kilomètres par heure pendant plus de "
  "dix minutes. Seconde condition : la température de l'air est inférieure à moins cinq degrés "
  "Celsius au départ de la campagne. Une seule de ces conditions suffit à suspendre la campagne."),
 ("6. Matériel",
  "Les prélèvements utilisent une bouteille de type Niskin fictive de 2,5 litres. Le disque de "
  "Secchi a un diamètre de 30 centimètres. La sonde multiparamètre est étalonnée la veille de "
  "chaque campagne selon la procédure OFLA-ETAL-07."),
 ("7. Conservation des échantillons",
  "Les échantillons destinés à l'analyse du phosphore sont acidifiés et conservés à 4 degrés "
  "Celsius. Les échantillons de chlorophylle sont filtrés sur le terrain sur filtre de 0,7 micromètre "
  "et congelés à moins vingt degrés dans les six heures."),
 ("8. Identification",
  "Chaque flacon porte une étiquette au format OFLA-AAAA-MM-JJ-PP-NN où PP est le code du point "
  "et NN le numéro d'ordre. Exemple fictif : OFLA-2025-08-09-V1-03 désigne le troisième flacon du "
  "point V1 prélevé le 9 août 2025."),
 ("9. Transport",
  "Les glacières sont transportées au laboratoire fictif de Val-Sombre dans un délai de 24 heures. "
  "La température des glacières est consignée au départ et à l'arrivée."),
 ("10. Analyses",
  "Le laboratoire réalise les analyses suivantes : phosphore total, azote total, chlorophylle a, "
  "conductivité et pH. Le délai de rendu des résultats est de 21 jours ouvrés."),
 ("11. Contrôle qualité",
  "Un blanc de terrain et un duplicata sont réalisés à chaque campagne. L'écart admissible entre "
  "duplicatas est de 15 pour cent pour le phosphore total et de 10 pour cent pour la conductivité."),
 ("12. Données",
  "Les résultats sont saisis dans la base fictive OFLA-DATA. Chaque saisie est validée par une "
  "seconde personne. Les données validées sont publiées trimestriellement."),
 ("13. Sécurité",
  "Le port du gilet de sauvetage est obligatoire sur l'embarcation. Une radio AlpLink de secours "
  "est emportée à chaque campagne. L'équipe compte au minimum deux personnes."),
 ("14. Révision",
  "Le protocole est révisé tous les deux ans. La prochaine révision fictive est prévue pour mars "
  "2027. Les demandes de modification sont adressées au comité technique."),
]

def pad(text: str, words: int) -> str:
    """Allonge un paragraphe avec du texte de remplissage DIFFÉRENCIÉ (phrases numérotées)."""
    filler = []
    i = 0
    while sum(len(f.split()) for f in filler) < words:
        i += 1
        filler.append(f"Note complémentaire {i} : cette phrase de remplissage synthétique ne porte "
                      f"aucun fait nouveau et sert uniquement à donner au document sa longueur.")
    return text + "\n\n" + " ".join(filler)

# Paragraphe très long (> 1 200 caractères sans saut de ligne) : teste le découpage sous la borne
# d'embeddings du profil fr-en-retrieval (max_input_chars 1200, politique reject).
LONG_PARAGRAPH = ("Le point V3, dit baie des Mélèzes, est le point le plus abrité du lac de Vermeil ; "
                  "il est bordé d'une forêt fictive de mélèzes centenaires dont la litière alimente une "
                  "fraction notable du carbone organique dissous du lac, ce qui explique sa coloration "
                  "légèrement ambrée à la fin de l'été. ") * 8  # ≈ 2 400 caractères, une seule ligne

DOC_FR_LONG_PARA = ("OFLA — Note sur la baie des Mélèzes (document synthétique à paragraphe très long)\n\n"
                    + LONG_PARAGRAPH.strip() + "\n\nFin de la note. Le code du point est V3.\n")

# DOCX à tableau : dates d'étalonnage des capteurs (fiction).
CALIB_ROWS = [("Capteur", "Station", "Dernier étalonnage", "Prochain étalonnage"),
              ("Thermion-Alp 220", "Nord (N1)", "2025-06-02", "2026-06-02"),
              ("Hydra-TX-9", "Nord (N1)", "2025-06-02", "2025-12-02"),
              ("OxyLume S3", "Est (E1)", "2025-07-15", "2025-10-13"),
              ("LevelSentinel P40", "Sud (S1)", "2025-05-20", "2027-05-20")]
DOCX_INTRO = ("OFLA — Registre d'étalonnage des capteurs (document synthétique). Le tableau ci-dessous "
              "récapitule les dates d'étalonnage fictives. La sonde OxyLume S3 de la station Est doit être "
              "réétalonnée le 13 octobre 2025.")

# --------------------------------------------------------------------------- écriture
def write_text(name: str, content: str) -> Path:
    p = OUT / name; p.write_text(content, encoding="utf-8"); return p

def write_pdf(name: str, pages: list[tuple[str, str]]) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    p = OUT / name
    doc = SimpleDocTemplate(str(p), pagesize=A4, title="OFLA — Protocole d'échantillonnage (synthétique)",
                            author="Corpus synthétique RAGatm Open Compact")
    styles = getSampleStyleSheet(); story = []
    story.append(Paragraph("OFLA — Protocole d'échantillonnage des lacs d'altitude (document synthétique, version 3.2)", styles["Title"]))
    story.append(Spacer(1, 12))
    for idx, (title, body) in enumerate(pages):
        story.append(Paragraph(title, styles["Heading2"]))
        for para in pad(body, 260).split("\n\n"):
            story.append(Paragraph(para, styles["BodyText"])); story.append(Spacer(1, 8))
        if idx < len(pages) - 1:
            story.append(PageBreak())
    doc.build(story); return p

def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def write_docx(name: str, intro: str, rows: list[tuple[str, ...]]) -> Path:
    """DOCX minimal (OOXML) écrit avec zipfile : un paragraphe + un tableau."""
    p = OUT / name
    def para(t): return f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(t)}</w:t></w:r></w:p>'
    def cell(t): return f'<w:tc><w:tcPr><w:tcW w:w="2200" w:type="dxa"/></w:tcPr>{para(t)}</w:tc>'
    tbl = ('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
           + "".join(f"<w:tr>{''.join(cell(c) for c in r)}</w:tr>" for r in rows) + "</w:tbl>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{para(intro)}{tbl}{para("Fin du registre synthétique.")}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>')
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in (("[Content_Types].xml", content_types), ("_rels/.rels", rels),
                          ("word/document.xml", document), ("word/_rels/document.xml.rels", doc_rels)):
            zi = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0)); z.writestr(zi, data)
    return p

files = [
    (write_text("fr_lac_vermeil.txt", DOC_FR_VERMEIL), "fr", "txt", ["Fiche lac de Vermeil"]),
    (write_text("fr_lac_aurore.md", "# Lac Aurore\n\n" + DOC_FR_AURORE), "fr", "md", ["Fiche lac Aurore"]),
    (write_text("en_sensors_note.txt", DOC_EN_SENSORS), "en", "txt", ["Instrumentation note"]),
    (write_text("en_stations_table.md", DOC_EN_STATIONS_MD), "en", "md", ["Stations table (Markdown table)"]),
    (write_text("fr_menu_cantine.txt", DOC_FR_MENU), "fr", "txt", ["Document SANS réponse (menu)"]),
    (write_text("fr_note_baie_melezes.txt", DOC_FR_LONG_PARA), "fr", "txt", ["Paragraphe très long (> 1200 car.)"]),
    (write_pdf("fr_protocole_echantillonnage.pdf", PROTO_PAGES), "fr", "pdf", ["Document long, 14 pages"]),
    (write_docx("fr_registre_etalonnage.docx", DOCX_INTRO, CALIB_ROWS), "fr", "docx", ["Tableau DOCX"]),
]

QUESTIONS = [
 {"id": "Q01-factuelle-fr", "kind": "factual", "lang": "fr", "question": "Quelle est la profondeur maximale du lac de Vermeil ?",
  "expected_answer_terms": ["47"], "expected_docs": ["fr_lac_vermeil.txt"], "expected_passage": "profondeur maximale du lac de Vermeil est de 47 mètres", "answerable": True},
 {"id": "Q02-reformulee-fr", "kind": "rephrased", "lang": "fr", "question": "Jusqu'à quelle profondeur le lac de Vermeil descend-il au plus bas ?",
  "expected_answer_terms": ["47"], "expected_docs": ["fr_lac_vermeil.txt"], "expected_passage": "47 mètres", "answerable": True},
 {"id": "Q03-passage-long-fr", "kind": "long_passage", "lang": "fr", "question": "Quel est le code du point de prélèvement de la baie des Mélèzes ?",
  "expected_answer_terms": ["V3"], "expected_docs": ["fr_protocole_echantillonnage.pdf", "fr_note_baie_melezes.txt"], "expected_pages": {"fr_protocole_echantillonnage.pdf": 3}, "answerable": True},
 {"id": "Q04-multi-passages-fr", "kind": "multi_passage", "lang": "fr", "question": "Quelles sont les deux conditions qui suspendent une campagne de prélèvements ?",
  "expected_answer_terms": ["40", "cinq"], "expected_answer_terms_alt": ["40", "-5"], "expected_docs": ["fr_protocole_echantillonnage.pdf"], "expected_pages": {"fr_protocole_echantillonnage.pdf": 5}, "answerable": True},
 {"id": "Q05-multi-documents", "kind": "multi_document", "lang": "fr", "question": "Quelle station a enregistré la température de surface la plus élevée en 2025 et quelle est son altitude ?",
  "expected_answer_terms": ["Nord", "2310"], "expected_answer_terms_alt": ["North", "2 310"], "expected_docs": ["fr_lac_vermeil.txt", "en_stations_table.md"], "answerable": True},
 {"id": "Q06-fr-vers-en", "kind": "cross_lingual_fr_en", "lang": "fr", "question": "Quel est le modèle du capteur de turbidité utilisé par l'observatoire ?",
  "expected_answer_terms": ["Hydra-TX-9"], "expected_docs": ["en_sensors_note.txt"], "expected_passage": "turbidity sensor model is Hydra-TX-9", "answerable": True},
 {"id": "Q07-en-vers-fr", "kind": "cross_lingual_en_fr", "lang": "en", "question": "What is the maximum depth of Lake Vermeil?",
  "expected_answer_terms": ["47"], "expected_docs": ["fr_lac_vermeil.txt"], "answerable": True},
 {"id": "Q08-sans-reponse", "kind": "unanswerable", "lang": "fr", "question": "Quel est le budget annuel de l'observatoire OFLA ?",
  "expected_answer_terms": [], "expected_docs": [], "answerable": False},
 {"id": "Q09-tableau-md", "kind": "table_markdown", "lang": "fr", "question": "Quelle est l'altitude de la station Nord ?",
  "expected_answer_terms": ["2310"], "expected_answer_terms_alt": ["2 310"], "expected_docs": ["en_stations_table.md"], "answerable": True},
 {"id": "Q10-tableau-docx", "kind": "table_docx", "lang": "fr", "question": "Quand la sonde OxyLume S3 doit-elle être réétalonnée ?",
  "expected_answer_terms": ["13 octobre 2025"], "expected_answer_terms_alt": ["2025-10-13"], "expected_docs": ["fr_registre_etalonnage.docx"], "answerable": True},
]

manifest = {
    "corpus": "OFLA — corpus synthétique de qualification RAGatm Open Compact",
    "version": VERSION,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "origin": "synthetic — généré par generate_corpus.py ; aucun document, lieu, mesure ou nom réel",
    "license": "contenu synthétique produit pour le programme RAGatm Open (réutilisation autorisée dans ce programme)",
    "documents": [
        {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size,
         "language": lang, "format": fmt, "origin": "synthetic", "version": VERSION, "notes": notes}
        for p, lang, fmt, notes in files
    ],
    "questions": QUESTIONS,
}
(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
for d in manifest["documents"]:
    print(f"{d['sha256'][:12]}  {d['bytes']:>7}  {d['name']}")
print(f"manifest: {OUT/'manifest.json'} ({len(QUESTIONS)} questions)")

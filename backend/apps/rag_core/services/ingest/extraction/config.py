# backend/apps/rag_core/services/ingest/extraction/config.py
from __future__ import annotations

"""
Lecture centralisée des limites d'extraction (settings Django, surchargeables .env).

Toutes les valeurs ont un défaut raisonnable. Les nouvelles variables `DOCUMENT_*`
priment ; pour la taille de fichier et les pages PDF on retombe sur les variables
historiques `UPLOAD_*` afin de ne PAS dédoubler la configuration existante.
"""

from typing import Optional

from django.conf import settings


def _int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except Exception:
        return default


def _bool(name: str, default: bool = False) -> bool:
    v = getattr(settings, name, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str) -> str:
    v = getattr(settings, name, default)
    return str(v).strip() if v is not None else default


def _list(name: str, default: list[str]) -> list[str]:
    """Réglage de type `list` au registre, normalisé en minuscules.

    Forme CANONIQUE : la liste projetée par `settings/base.py` depuis le registre. La
    forme texte de l'environnement (`"jpeg,png"`) reste acceptée — c'est celle d'un
    `override_settings` ou d'un `.env` lu à la main — et donne le MÊME résultat.
    Jamais `str(liste).split(",")` : une liste y devenait `["['jpeg'", " 'png']"]`,
    et l'allowlist entière était silencieusement vidée.
    """
    v = getattr(settings, name, default)
    if v is None:
        v = default
    items = v.split(",") if isinstance(v, str) else v
    return [str(i).strip().lower() for i in items if str(i).strip()]


# ---------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------
def max_upload_size_bytes() -> int:
    # DOCUMENT_MAX_UPLOAD_SIZE_MB prime, sinon UPLOAD_MAX_FILE_SIZE_MB (historique).
    # Même repli que l'admission HTTP (`limits.max_file_size_mb`) : le défaut du registre.
    from apps.rag_core.services.ingest.limits import max_file_size_mb

    mb = _float("DOCUMENT_MAX_UPLOAD_SIZE_MB", max_file_size_mb())
    return int(mb * 1024 * 1024)


def max_extracted_chars() -> int:
    return _int("DOCUMENT_MAX_EXTRACTED_CHARS", 5_000_000)


def extraction_timeout_s() -> int:
    # Defaut 180s : superieur au budget OCR (DOCUMENT_OCR_TIMEOUT_SECONDS=60) pour ne
    # pas tuer l'OCR/PDF complexe avant la fin (30s etait trop court -> faux timeouts).
    return _int("DOCUMENT_EXTRACTION_TIMEOUT_SECONDS", _int("UPLOAD_EXTRACTION_TIMEOUT_S", 180))


def pdf_max_pages() -> int:
    return _int("DOCUMENT_PDF_MAX_PAGES", _int("UPLOAD_PDF_MAX_PAGES", 500))


# ---------------------------------------------------------------------
# Extraction PDF NATIVE — fallback multi-moteur (P0)
# ---------------------------------------------------------------------
def pdf_native_fallback_enabled() -> bool:
    """Autorise les moteurs d'extraction natifs de secours (pymupdf/pdfminer/…)
    quand pypdf n'extrait pas assez de texte. Local-only, aucun OCR, aucun réseau."""
    return _bool("DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED", True)


def pdf_native_fallback_engines() -> list[str]:
    """Ordre de préférence des moteurs de secours. Les moteurs absents (non
    installés) sont simplement ignorés à l'exécution (import-gardé)."""
    return _list("DOCUMENT_PDF_NATIVE_FALLBACK_ENGINES", ["pymupdf", "pdfminer", "pdfplumber"])


def pdf_min_total_chars() -> int:
    """Seuil de texte natif "utile" (caractères, tout le document) en dessous
    duquel on tente les moteurs de secours avant de conclure à un scan/OCR.

    NB : le seuil PAR PAGE reste ``DOCUMENT_OCR_MIN_CHARS_PER_PAGE`` (24) afin de
    ne pas dupliquer la configuration existante."""
    return _int("DOCUMENT_PDF_MIN_TOTAL_CHARS", 200)


# ---------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------
def ocr_enabled() -> bool:
    return _bool("DOCUMENT_OCR_ENABLED", False)


def ocr_provider() -> str:
    # "tesseract" (local) par défaut ; tout provider externe doit être explicitement
    # configuré ET implémenté. "none"/"disabled" => aucun OCR.
    return _str("DOCUMENT_OCR_PROVIDER", "tesseract").lower()


def ocr_timeout_s() -> int:
    return _int("DOCUMENT_OCR_TIMEOUT_SECONDS", 60)


def ocr_max_pages() -> int:
    return _int("DOCUMENT_OCR_MAX_PAGES", 50)


def ocr_min_chars_per_page() -> int:
    """Seuil sous lequel une page PDF est considérée "sans texte" => OCR candidat."""
    return _int("DOCUMENT_OCR_MIN_CHARS_PER_PAGE", 24)


def ocr_languages() -> str:
    # Format tesseract: "fra+eng".
    return _str("DOCUMENT_OCR_LANGUAGES", "fra+eng")


def ocr_raster_scale() -> float:
    """Échelle de rastérisation PDF -> image avant OCR (pypdfium2 `render(scale=)`).

    scale=2 ≈ 144 DPI (défaut historique). Pour des plans techniques A3 aux
    annotations fines (HA14, e=0.15), 3–4 (≈216–288 DPI) améliore nettement la
    lecture, au prix de plus de CPU/RAM. Surchargeable via DOCUMENT_OCR_RASTER_SCALE."""
    try:
        return max(1.0, _float("DOCUMENT_OCR_RASTER_SCALE", 2.0))
    except Exception:
        return 2.0


def ocr_min_orientation_confidence() -> float:
    """Confiance OSD minimale pour OSER faire pivoter une page.

    Tesseract rend une `orientation_conf` faible sur les pages pauvres en prose (tableaux,
    listes de codes) et s'y trompe : observé sur le corpus terrain, des pages droites
    retournées à 180° et rendues illisibles — un document est passé de 100 % à 91 % de
    concordance avec sa source de référence à cause de cela.

    Le seuil est haut à dessein. Les deux erreurs ne coûtent pas le même prix : une
    rotation MANQUÉE laisse une page pivotée, déjà illisible sans nous ; une rotation À
    TORT détruit une page lisible. Comme la très grande majorité des pages sont droites,
    on n'agit que sur une preuve forte. Une confiance de 5,7 a suffi à produire un
    retournement erroné — les détections fiables sont bien au-delà.
    """
    return _float("DOCUMENT_OCR_MIN_ORIENTATION_CONFIDENCE", 10.0)


def ocr_compute_confidence() -> bool:
    """Calcule-t-on la confiance OCR (second passage Tesseract `image_to_data`) ?

    Le chemin OCR exécute aujourd'hui DEUX reconnaissances complètes sur la même image :
    `image_to_string` produit le texte indexé, `image_to_data` ne sert qu'à la moyenne des
    confiances. Mesuré sur 25 pages du corpus terrain : ce second passage coûte **36,4 %**
    du temps OCR médian, jusqu'à 48 %, et 60,1 s sur les 105,5 s d'une planche A1.

    Le désactiver ne peut pas changer le texte : `text` est affecté avant cette branche et
    n'est jamais réécrit (identité vérifiée sur 5/5 pages par le chemin réel, 36/36 en
    mesure antérieure). Ce qui disparaît alors, et seulement cela : `ocr_confidence`,
    `page_ocr_confidences` et le warning `ocr_low_confidence`.

    Défaut `True` : compatibilité ascendante stricte au premier commit. Aucune confiance
    n'est jamais inventée quand le calcul est éteint — les champs sont simplement absents,
    et `ocr_confidence_computed` dit lequel des deux cas s'applique.
    """
    return _bool("DOCUMENT_OCR_COMPUTE_CONFIDENCE", True)


def ocr_auto_rotate() -> bool:
    """Active la détection/correction d'orientation (OSD tesseract) avant OCR.

    Les plans sont souvent orientés/tournés : on teste l'orientation et on
    redresse l'image avant l'OCR. Désactivable via DOCUMENT_OCR_AUTO_ROTATE=0."""
    return _bool("DOCUMENT_OCR_AUTO_ROTATE", True)


# ---------------------------------------------------------------------
# Images (limites techniques — Lot 1 ingestion image)
# ---------------------------------------------------------------------
def image_max_bytes() -> int:
    """Plafond octets dédié aux images. Repli sur la limite d'upload globale si
    DOCUMENT_IMAGE_MAX_BYTES n'est pas défini (0/absent)."""
    v = _int("DOCUMENT_IMAGE_MAX_BYTES", 0)
    return v if v > 0 else max_upload_size_bytes()


def image_max_pixels() -> int:
    """Nombre max de pixels (largeur*hauteur) — garde anti decompression-bomb.
    Défaut 40 MP (~A3 @ 300 dpi)."""
    return _int("DOCUMENT_IMAGE_MAX_PIXELS", 40_000_000)


def image_preprocess_enabled() -> bool:
    """Active le pré-traitement image (correction d'orientation EXIF) avant OCR."""
    return _bool("DOCUMENT_IMAGE_PREPROCESS_ENABLED", True)


def image_ingest_enabled() -> bool:
    """Porte d'entrée de l'ingestion image (Lot 2). INDÉPENDANTE de
    DOCUMENT_OCR_ENABLED : une image acceptée est OCRisée automatiquement (une
    image n'a aucun chemin texte natif). Désactivable via
    DOCUMENT_IMAGE_INGEST_ENABLED=0 (rollback instantané)."""
    return _bool("DOCUMENT_IMAGE_INGEST_ENABLED", True)


def image_formats() -> list[str]:
    """Allowlist des formats image acceptés (MVP : jpeg/png/tiff/webp).
    GIF/BMP hors MVP par défaut -> refus explicite `image_unsupported_format`."""
    return _list("DOCUMENT_IMAGE_FORMATS", ["jpeg", "png", "tiff", "webp"])


# ---------------------------------------------------------------------
# Analyse visuelle via l'AI Gateway
# ---------------------------------------------------------------------
#: Natures de source que le rail visuel sait RÉELLEMENT traiter.
#: `pdf` depuis une version antérieure, `docx` depuis une version antérieure, `pptx` depuis
#: dans les trois cas les visuels INTÉGRÉS uniquement (aucun
#: rendu, aucun vectoriel).
VISUAL_OPERATIONAL_SOURCES = frozenset({"image", "pdf", "docx", "pptx"})


def visual_analysis_enabled() -> bool:
    """Porte globale de l'analyse visuelle (FERMÉE par défaut)."""
    return _bool("DOCUMENT_VISUAL_ANALYSIS_ENABLED", False)


def visual_analysis_sources() -> frozenset:
    """Natures de source ACTIVES : porte ouverte ∩ allowlist ∩ sources opérationnelles.

    Vide dès que la porte est fermée. Accepte une liste ou une chaîne séparée par des
    virgules, insensible à la casse.
    """
    if not visual_analysis_enabled():
        return frozenset()
    requested = set(_list("DOCUMENT_VISUAL_ANALYSIS_SOURCES", ["image"]))
    return frozenset(requested & VISUAL_OPERATIONAL_SOURCES)


def visual_image_analysis_active() -> bool:
    """Image autonome soumise à l'analyse visuelle (porte de la file `ingest_visual`)."""
    return "image" in visual_analysis_sources()


def visual_pdf_analysis_active() -> bool:
    """PDF soumis à la découverte de visuels intégrés.

    Même porte que l'image : c'est elle qui décide du routage vers `ingest_visual` et
    de l'asynchrone forcé. Fermée, le PDF se comporte EXACTEMENT comme avant le lot.
    """
    return "pdf" in visual_analysis_sources()


def visual_docx_analysis_active() -> bool:
    """DOCX soumis à la découverte de visuels intégrés.

    Fermée, le DOCX se comporte EXACTEMENT comme avant le lot : aucune relation n'est
    lue, aucun média n'est décodé, aucune métadonnée visuelle n'apparaît.
    """
    return "docx" in visual_analysis_sources()


def visual_pptx_analysis_active() -> bool:
    """PPTX soumis à la découverte d'images intégrées.

    Porte générique ET porte du format : une présentation refusée par
    `DOCUMENT_PPTX_ENABLED=0` n'a rien à faire sur la file visuelle. Fermée, le PPTX se
    comporte EXACTEMENT comme avant le lot : aucune image n'est relevée ni décodée,
    aucune métadonnée visuelle n'apparaît.
    """
    return "pptx" in visual_analysis_sources() and pptx_enabled()


# ---------------------------------------------------------------------
# Visuels PDF — bornes génériques
# ---------------------------------------------------------------------
def pdf_visual_max_assets() -> int:
    """Visuels ADMIS au plus par PDF. Borne du coût Gateway, pas de la découverte.

    Volontairement sous le plafond du rail (`MAX_ASSETS_PER_DOCUMENT`) : un PDF
    illustré ne doit pas consommer à lui seul tout le budget d'un document."""
    return _int("DOCUMENT_PDF_VISUAL_MAX_ASSETS", 12)


def pdf_visual_max_assets_per_page() -> int:
    """Visuels admis au plus par PAGE : une page-planche ne monopolise pas le budget."""
    return _int("DOCUMENT_PDF_VISUAL_MAX_PER_PAGE", 4)


def pdf_visual_min_side_px() -> int:
    """Côté minimal (px) d'un visuel admissible. Écarte puces, filets et icônes."""
    return _int("DOCUMENT_PDF_VISUAL_MIN_SIDE_PX", 96)


def pdf_visual_min_pixels() -> int:
    """Aire minimale (px²) d'un visuel admissible."""
    return _int("DOCUMENT_PDF_VISUAL_MIN_PIXELS", 20_000)


def pdf_visual_max_pixels() -> int:
    """Aire maximale (px²) acceptée par le Gateway vision (contrat V1 : 2 Mpx)."""
    return _int("DOCUMENT_PDF_VISUAL_MAX_PIXELS", 2_000_000)


def pdf_visual_max_bytes() -> int:
    """Octets max d'un visuel APRÈS normalisation PNG (borne de charge utile)."""
    return _int("DOCUMENT_PDF_VISUAL_MAX_BYTES", 8_000_000)


def pdf_visual_table_overlap_ratio() -> float:
    """Recouvrement au-delà duquel un visuel est réputé COUVRIR une région tabulaire
    déjà extraite nativement : l'évidence native fait foi, le visuel est écarté."""
    try:
        return float(getattr(settings, "DOCUMENT_PDF_VISUAL_TABLE_OVERLAP_RATIO", 0.6))
    except Exception:
        return 0.6


def pdf_vector_regions_enabled_for(rag_space_id) -> bool:
    """Régions vectorielles rendues pour CE document : drapeau ET espace listé en UUID
    complet. Identité absente, liste vide ou invalide : non."""
    import uuid

    if not _bool("DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED", False):
        return False
    try:
        space = str(uuid.UUID(str(rag_space_id))) if rag_space_id not in (None, "") else None
    except (ValueError, AttributeError, TypeError):
        space = None
    allowed = _uuid_list("DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS")
    return bool(space and allowed and space in allowed)


def pdf_vector_render_dpi() -> int:
    return _int("DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI", 150)


def pdf_vector_max_candidates() -> int:
    return _int("DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES", 24)


def pdf_visual_max_repeats() -> int:
    """Occurrences au-delà desquelles des octets identiques sont réputés DÉCORATIFS
    (logo, bandeau, filigrane) et écartés — signal générique, jamais un nom."""
    return _int("DOCUMENT_PDF_VISUAL_MAX_REPEATS", 3)


# ---------------------------------------------------------------------
# Tableaux structurés (P3) — bornes de sécurité
# ---------------------------------------------------------------------
def table_max_per_doc() -> int:
    """Nombre max de chunks-tableaux créés par document (anti-explosion)."""
    return _int("DOCUMENT_TABLE_MAX_PER_DOC", 50)


def table_max_chars() -> int:
    """Taille max (caractères) du Markdown d'UN tableau avant fragmentation.

    Ce n'est PLUS une borne d'embeddings, et ce n'en a jamais été une : la valeur
    valait 12 000 alors que le modèle gouverné d'un espace peut n'accepter que
    6 000. Le chunk partait quand même et la passerelle refusait le lot entier.
    La borne d'embeddings est désormais le budget de découpage de l'espace, qui
    applique déjà le plafond du modèle ; celle-ci ne borne que le VOLUME d'un
    tableau pris en charge, au-delà duquel on fragmente ce qu'on peut et on le dit.
    """
    return _int("DOCUMENT_TABLE_MAX_CHARS", 100_000)


def table_max_fragments() -> int:
    """Nombre max de fragments produits par UN tableau (anti-explosion).

    `table_max_per_doc` borne le nombre de TABLEAUX ; depuis qu'un tableau peut
    donner plusieurs chunks, il faut aussi borner ce que chacun engendre. Sans
    cette borne, un tableau de plusieurs mégaoctets produirait des milliers de
    fragments — et `table_max_per_doc` ne le verrait pas, puisqu'il ne compte
    qu'un seul tableau.
    """
    return _int("DOCUMENT_TABLE_MAX_FRAGMENTS", 40)


def pdf_generic_tables_enabled() -> bool:
    """Rail tabulaire GÉNÉRIQUE pour les PDF à couche texte (OPT-IN, défaut False).

    Éteint, le comportement PDF est celui de la ligne de base : texte par page, et
    aucun coût d'analyse de mise en page supplémentaire."""
    return _bool("DOCUMENT_PDF_GENERIC_TABLES_ENABLED", False)


#: Raisons de la décision du rail tabulaire générique (cardinalité bornée, aucune donnée de document).
NATIVE_TABLES_GLOBAL_DISABLED = "global_disabled"
NATIVE_TABLES_MISSING_SPACE = "missing_space_identity"
NATIVE_TABLES_ALLOWLIST_INVALID = "allowlist_invalid"
NATIVE_TABLES_SPACE_NOT_ALLOWED = "space_not_allowed"
NATIVE_TABLES_VISUAL_CONFLICT = "visual_rail_conflict"
NATIVE_TABLES_ENABLED = "native_tables_enabled"


def _uuid_list(name: str) -> Optional[frozenset]:
    """UUID canoniques d'un réglage liste ; ``None`` si UNE entrée n'est pas un UUID complet
    (préfixe, nom, motif) — une liste douteuse n'autorise rien."""
    import uuid

    raw = getattr(settings, name, None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    out = set()
    for value in raw:
        text = str(value).strip()
        if not text:
            continue
        try:
            parsed = uuid.UUID(text)
        except (ValueError, AttributeError, TypeError):
            return None
        if str(parsed) != text.lower():
            return None  # forme non canonique (sans tirets, accolades…) : refusée, jamais devinée
        out.add(str(parsed))
    return frozenset(out)


def pdf_generic_tables_decision(rag_space_id) -> tuple:
    """``(appliquer, raison)`` du rail tabulaire générique pour UN document, d'après l'identité de SON
    RagSpace transmise par appel. Échec fermé à chaque étape : drapeau éteint,
    identité absente ou invalide, liste vide ou invalide, espace non listé, espace du rail visuel
    structuré (STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST) — jamais de superposition des deux rails."""
    import uuid

    if not pdf_generic_tables_enabled():
        return False, NATIVE_TABLES_GLOBAL_DISABLED
    try:
        space = str(uuid.UUID(str(rag_space_id))) if rag_space_id not in (None, "") else None
    except (ValueError, AttributeError, TypeError):
        space = None
    if space is None:
        return False, NATIVE_TABLES_MISSING_SPACE
    allowed = _uuid_list("DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS")
    if allowed is None:
        return False, NATIVE_TABLES_ALLOWLIST_INVALID
    if space not in allowed:
        return False, NATIVE_TABLES_SPACE_NOT_ALLOWED
    visual = getattr(settings, "STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST", None) or []
    if isinstance(visual, str):
        visual = visual.split(",")
    if space in {str(v).strip().lower() for v in visual}:
        return False, NATIVE_TABLES_VISUAL_CONFLICT
    return True, NATIVE_TABLES_ENABLED


def table_max_row_chunks() -> int:
    """Borne des fragments (ligne logique x page) du rail générique.

    Distincte de `table_max_per_doc`, qui borne des tableaux ATOMIQUES : appliquer
    cette dernière aux fragments tronquerait un tableau de plusieurs centaines de
    pages dès sa cinquantième ligne."""
    return _int("DOCUMENT_TABLE_MAX_ROW_CHUNKS", 2000)


def table_ocr_enabled() -> bool:
    """Reconstruction de tableaux depuis l'OCR (P3-b) — pages image-only/scannées.

    OPT-IN (défaut False) : fragile (positions de mots) + coûteux (rastérisation
    supplémentaire). Les tableaux ainsi reconstruits sont marqués `confidence=low`
    et `requires_visual_check=True`. N'a d'effet que si l'OCR a effectivement tourné."""
    return _bool("DOCUMENT_TABLE_OCR_ENABLED", False)


def ocr_forces_async() -> bool:
    """Décision produit : l'ingestion DOIT être asynchrone dès que l'OCR est activé.

    L'OCR est coûteux/long : on ne l'exécute jamais dans le cycle d'une requête HTTP.
    """
    return ocr_enabled()


# ---------------------------------------------------------------------
# DOCX (anti zip-bomb)
# ---------------------------------------------------------------------
def docx_max_uncompressed_bytes() -> int:
    return int(_float("DOCUMENT_DOCX_MAX_UNCOMPRESSED_SIZE_MB", 200) * 1024 * 1024)


def docx_max_zip_entries() -> int:
    return _int("DOCUMENT_DOCX_MAX_ZIP_ENTRIES", 2000)


def docx_max_compression_ratio() -> int:
    return _int("DOCUMENT_DOCX_MAX_COMPRESSION_RATIO", 200)


# ---------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------
def csv_max_rows() -> int:
    return _int("DOCUMENT_CSV_MAX_ROWS", 100_000)


def csv_max_columns() -> int:
    return _int("DOCUMENT_CSV_MAX_COLUMNS", 256)


def csv_max_cell_chars() -> int:
    return _int("DOCUMENT_CSV_MAX_CELL_CHARS", 10_000)


def csv_rows_per_block() -> int:
    return _int("DOCUMENT_CSV_ROWS_PER_BLOCK", 1)


# ---------------------------------------------------------------------
# EXCEL (.xlsx) — limites issues des MESURES du spike E1.
# Cf. docs/reports/SPIKE_E1_OPENPYXL_RAGATM.md
# ---------------------------------------------------------------------
def excel_enabled() -> bool:
    """Porte d'entrée de l'extraction Excel (rollback instantané).

    À 0, un `.xlsx` retombe sur le refus historique `unsupported_office_format`.
    """
    return _bool("DOCUMENT_EXCEL_ENABLED", True)


def excel_max_shared_strings_bytes() -> int:
    """LA borne mémoire réelle (E1).

    openpyxl charge INTÉGRALEMENT `xl/sharedStrings.xml` à l'ouverture, même en
    `read_only`. Loi mesurée : ``pic RSS ≈ 45 Mo + 3,5 × taille(sharedStrings.xml)``.
    La taille est DÉCLARÉE dans le répertoire central du ZIP : on la lit sans
    décompresser un seul octet, donc on peut refuser AVANT tout parsing.

    50 Mo ⇒ RSS borné à ~220 Mo. Le plafond de cellules, lui, ne prédit PAS la mémoire.
    """
    return int(_float("DOCUMENT_EXCEL_MAX_SHARED_STRINGS_MB", 50) * 1024 * 1024)


def excel_max_cells() -> int:
    """Pire cas mesuré à 1 M : 16,8 s / 212 Mo (chaînes toutes uniques)."""
    return _int("DOCUMENT_EXCEL_MAX_CELLS", 1_000_000)


def excel_max_sheets() -> int:
    """100 feuilles à contenu constant ne coûtent que +7 % (E1)."""
    return _int("DOCUMENT_EXCEL_MAX_SHEETS", 100)


def excel_max_columns() -> int:
    return _int("DOCUMENT_EXCEL_MAX_COLUMNS", 256)


def excel_max_declared_cols() -> int:
    """Largeur déclarée AU-DELÀ DE LAQUELLE une dimension est jugée FANTÔME.

    La balise `<dimension>` d'un `.xlsx` n'est pas fiable (cf. E1 §10) : un classeur de
    reporting déclare couramment `A1:XFD671` — XFD est la 16384ᵉ et DERNIÈRE colonne
    d'Excel — alors qu'il ne contient qu'une poignée de cellules réelles (mesuré : un
    fichier déclarant 11 M cellules en contenait 5 733, soit 1918× moins).

    Au-delà de ce plafond, la dimension déclarée n'est plus une estimation crédible du
    volume : on ne REFUSE PAS le classeur là-dessus. La vraie protection reste le comptage
    des cellules RÉELLES pendant l'extraction en flux (`state.cells_read > excel_max_cells`),
    qui, lui, ne ment pas. 2048 est déjà absurde pour des colonnes de données réelles
    (les tables de taux les plus larges plafonnent à ~170 colonnes)."""
    return _int("DOCUMENT_EXCEL_MAX_DECLARED_COLS", 2048)


def excel_max_cell_chars() -> int:
    """Contrainte de QUALITÉ de chunk, pas de mémoire (10 k car./cellule = 44 Mo en E1).
    Une cellule trop longue est TRONQUÉE + warning — jamais un rejet du classeur."""
    return _int("DOCUMENT_EXCEL_MAX_CELL_CHARS", 2_000)


def excel_max_uncompressed_bytes() -> int:
    return int(_float("DOCUMENT_EXCEL_MAX_UNCOMPRESSED_MB", 200) * 1024 * 1024)


def excel_max_blocks() -> int:
    return _int("DOCUMENT_EXCEL_MAX_BLOCKS", 200)


def excel_max_chunks() -> int:
    """Plafond embeddings + anti-dilution du retrieval (un classeur ne doit pas
    représenter une part écrasante du corpus)."""
    return _int("DOCUMENT_EXCEL_MAX_CHUNKS", 2_000)


def excel_rows_per_chunk() -> int:
    return _int("DOCUMENT_EXCEL_ROWS_PER_CHUNK", 40)


def excel_chunk_max_chars() -> int:
    """Borne PRIORITAIRE sur le nombre de lignes (une table large sature d'abord)."""
    return _int("DOCUMENT_EXCEL_CHUNK_MAX_CHARS", 4_000)


def excel_max_columns_markdown() -> int:
    """Au-delà, un tableau Markdown devient illisible : repli `col: valeur` par ligne."""
    return _int("DOCUMENT_EXCEL_MAX_COLUMNS_MARKDOWN", 20)


def excel_wide_table_threshold() -> int:
    """Au-delà de N colonnes, un tableau est découpé aussi HORIZONTALEMENT.

    La raison est une pathologie de retrieval, pas d'esthétique. Une feuille à 44
    colonnes répétait ses 44 intitulés sur chacun de ses 50 chunks de lignes : tous
    devenaient lexicalement quasi identiques, `ts_rank` sortait rigoureusement la même
    valeur pour tous, et l'unique chunk qui portait la réponse perdait au tirage. Aucun
    classement ne peut départager une égalité parfaite.
    """
    return _int("DOCUMENT_EXCEL_WIDE_TABLE_THRESHOLD", 16)


def excel_max_columns_per_chunk() -> int:
    """Largeur MAX d'une fenêtre de colonnes. Un groupe plus large est sous-découpé."""
    return _int("DOCUMENT_EXCEL_MAX_COLUMNS_PER_CHUNK", 8)


def excel_max_chunks_per_sheet() -> int:
    """Le découpage bidimensionnel multiplie les chunks : on le borne, et on le DIT."""
    return _int("DOCUMENT_EXCEL_MAX_CHUNKS_PER_SHEET", 500)


def excel_max_header_rows() -> int:
    """Profondeur MAX d'un en-tête multi-lignes (P1.1).

    Un tableau financier empile couramment 2 à 3 niveaux (« Premoney Capitalization »
    au-dessus de « Shares Outstanding »). Au-delà de 4, ce n'est plus un en-tête : on
    tronque aux lignes les plus PROCHES des données et on le signale — jamais on ne
    consomme dix lignes comme en-tête.
    """
    return max(1, _int("DOCUMENT_EXCEL_MAX_HEADER_ROWS", 4))


#: Politiques d'indexation des feuilles masquées.
HIDDEN_POLICY_NONE = "none"            # aucune feuille masquée
HIDDEN_POLICY_HIDDEN_ONLY = "hidden_only"   # `hidden` oui, `veryHidden` non — défaut
HIDDEN_POLICY_ALL = "all"              # tout, y compris `veryHidden`

_HIDDEN_POLICIES = (HIDDEN_POLICY_NONE, HIDDEN_POLICY_HIDDEN_ONLY, HIDDEN_POLICY_ALL)


def excel_hidden_sheets_policy() -> str:
    """Politique effective. Défaut : ``hidden_only``.

    Excel distingue **deux** états de masquage, et ils ne disent pas la même chose :

    - ``hidden`` : l'auteur a rangé une feuille pour aérer son classeur. Le destinataire
      la réaffiche d'un clic droit. C'est une commodité de PRÉSENTATION — pas un
      contrôle d'accès, et sûrement pas un secret. L'exclure faisait disparaître un
      registre financier entier (feuille « Convertibles » du classeur AirWorks) : le
      système répondait « je ne sais pas » sur des données qu'il détenait.

    - ``veryHidden`` : cet état n'est PAS modifiable depuis l'interface d'Excel — il
      exige du VBA ou l'éditeur de macros. C'est une intention technique délibérée. On
      la respecte : exclue.

    L'ancien drapeau booléen ``DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS`` confondait les
    deux. Il reste honoré pour ne casser aucun environnement : à ``1``, il vaut ``all``
    (c'était sa sémantique). La politique explicite, si elle est posée, prime.
    """
    raw = _str("DOCUMENT_EXCEL_HIDDEN_SHEETS_POLICY", "").strip().lower()
    if raw in _HIDDEN_POLICIES:
        return raw
    if _bool("DOCUMENT_EXCEL_INCLUDE_HIDDEN_SHEETS", False):
        return HIDDEN_POLICY_ALL          # compat : l'ancien drapeau incluait TOUT
    return HIDDEN_POLICY_HIDDEN_ONLY


def excel_sheet_is_indexable(state: str) -> bool:
    """Une feuille dans cet état (`visible` | `hidden` | `veryHidden`) est-elle indexée ?"""
    st = (state or "visible").strip()
    if st == "visible":
        return True
    policy = excel_hidden_sheets_policy()
    if policy == HIDDEN_POLICY_ALL:
        return True
    if policy == HIDDEN_POLICY_HIDDEN_ONLY:
        return st == "hidden"
    return False


def excel_include_hidden_sheets() -> bool:
    """Compat : au moins un état masqué est-il indexé ? (l'appelant devrait préférer
    ``excel_sheet_is_indexable``, qui distingue ``hidden`` de ``veryHidden``)."""
    return excel_hidden_sheets_policy() != HIDDEN_POLICY_NONE


def excel_forces_async() -> bool:
    """Décision produit : Excel est TOUJOURS asynchrone, comme l'OCR.

    Même un classeur moyen (50 k cellules) prend ~0,7 s, et 500 k près de 7 s (E1) :
    on ne l'exécute jamais dans le cycle d'une requête HTTP.
    """
    return True


# ---------------------------------------------------------------------
# POWERPOINT (.pptx).
#
# Une présentation est une archive OOXML : les gardes anti zip-bomb sont celles,
# mutualisées, de `zip_security.guard_zip_archive` (mêmes ordres de grandeur que DOCX,
# mais réglables séparément — un deck porte des médias, donc plus d'entrées qu'un .docx).
# `.pptm` / `.ppt` / `.odp` restent refusés. Aucune macro, aucun contenu actif, aucune
# relation externe n'est suivie.
# ---------------------------------------------------------------------
def pptx_enabled() -> bool:
    """Porte globale de l'extraction PowerPoint (cf. `validation.validate_and_classify`)."""
    return _bool("DOCUMENT_PPTX_ENABLED", True)


def pptx_max_uncompressed_bytes() -> int:
    return int(_float("DOCUMENT_PPTX_MAX_UNCOMPRESSED_SIZE_MB", 200) * 1024 * 1024)


def pptx_max_zip_entries() -> int:
    """Plus haut que DOCX (2000) : un deck illustré porte une entrée par média."""
    return _int("DOCUMENT_PPTX_MAX_ZIP_ENTRIES", 5000)


def pptx_max_compression_ratio() -> int:
    return _int("DOCUMENT_PPTX_MAX_COMPRESSION_RATIO", 200)


def pptx_max_slides() -> int:
    return _int("DOCUMENT_PPTX_MAX_SLIDES", 500)


def pptx_max_shapes_per_slide() -> int:
    """Formes ANALYSÉES par diapositive (groupes parcourus récursivement inclus)."""
    return _int("DOCUMENT_PPTX_MAX_SHAPES_PER_SLIDE", 500)


def pptx_max_table_cells() -> int:
    """Cellules de tableau analysées, cumulées sur toute la présentation."""
    return _int("DOCUMENT_PPTX_MAX_TABLE_CELLS", 50_000)


def pptx_max_chars() -> int:
    """Caractères extraits max, cumulés sur toute la présentation."""
    return _int("DOCUMENT_PPTX_MAX_CHARS", 2_000_000)


def pptx_group_max_depth() -> int:
    """Profondeur max de récursion dans les groupes de formes (anti-cycle/anti-pile)."""
    return _int("DOCUMENT_PPTX_GROUP_MAX_DEPTH", 10)


def pptx_include_notes() -> bool:
    """Notes du présentateur — option C de la mission : derrière un flag, OFF par défaut.

    Une note est un commentaire de l'orateur, pas le contenu publié de la diapositive :
    l'inclure par défaut ferait remonter dans les citations un texte que l'auteur n'a
    jamais destiné à être lu. Quand le flag est actif, les notes produisent des chunks
    SÉPARÉS (`kind="pptx_notes"`) — jamais fusionnés avec la diapositive, pour que la
    provenance reste lisible dans la citation.
    """
    return _bool("DOCUMENT_PPTX_INCLUDE_NOTES", False)


def office_chart_data_enabled() -> bool:
    """Graphiques natifs DOCX / PPTX lus depuis leur cache de données. OFF par défaut."""
    return _bool("DOCUMENT_OFFICE_CHART_DATA_ENABLED", False)


def pptx_include_alt_text() -> bool:
    """Texte alternatif des images/formes non textuelles (accessibilité).

    Activé par défaut : c'est du texte SAISI par l'auteur, souvent la seule description
    d'un schéma. Il est marqué comme tel dans le rendu (« [Image : … ] »), jamais présenté
    comme du texte de diapositive.
    """
    return _bool("DOCUMENT_PPTX_INCLUDE_ALT_TEXT", True)


def pptx_forces_async() -> bool:
    """Décision produit : PPTX est TOUJOURS asynchrone, comme Excel et l'OCR.

    Archive compressée, nombre de formes imprévisible, tableaux à aplatir : le coût
    n'est pas bornable a priori depuis la couche HTTP.
    """
    return True


# ---------------------------------------------------------------------
# XML générique (.xml).
#
# Parseur defusedxml (DTD/entités/références externes refusées, aucun réseau ni
# fichier). Porte FERMÉE par défaut (lot neuf). Les limites sont STRUCTURELLES
# (profondeur, éléments), distinctes de la limite d'upload en octets, et ne sont
# jamais réglables par le client.
# ---------------------------------------------------------------------
def xml_enabled() -> bool:
    """Porte globale de l'ingestion XML (cf. `validation.validate_and_classify`).
    À 0, un `.xml` reconnu est refusé (`xml_ingestion_disabled`) — il n'est plus
    indexé comme texte brut."""
    return _bool("DOCUMENT_XML_ENABLED", False)


def xml_max_depth() -> int:
    """Profondeur d'imbrication maximale (racine = 1). Au-delà : `xml_complexity_limit`."""
    return max(1, _int("DOCUMENT_XML_MAX_DEPTH", 64))


def xml_max_nodes() -> int:
    """Nombre maximal d'éléments. Au-delà : `xml_complexity_limit`, arrêt immédiat."""
    return max(1, _int("DOCUMENT_XML_MAX_NODES", 200_000))


# ---------------------------------------------------------------------
# DXF (.dxf).
#
# ezdxf en lecture STRICTE (jamais `recover`), sans rendu ni géométrie : textes, blocs
# et attributs, cotes, calques, layouts, XREF déclarées (jamais ouvertes). Porte FERMÉE
# par défaut ; toujours asynchrone (décision produit, comme Excel/PPTX). Limite
# STRUCTURELLE en entités, jamais réglable par le client.
# ---------------------------------------------------------------------
def dxf_enabled() -> bool:
    """Porte globale de l'ingestion DXF (cf. `validation.validate_and_classify`).
    À 0, un `.dxf` reconnu est refusé (`dxf_ingestion_disabled`)."""
    return _bool("DOCUMENT_DXF_ENABLED", False)


def dxf_max_entities() -> int:
    """Entités maximales (layouts + définitions de blocs). Au-delà : `dxf_complexity_limit`.
    250 000 : la qualification interne situait un dessin important vers 200 000."""
    return max(1, _int("DOCUMENT_DXF_MAX_ENTITIES", 250_000))


def dxf_forces_async() -> bool:
    """Décision produit : DXF est TOUJOURS asynchrone, comme Excel et PPTX.

    Un grand dessin demande plusieurs secondes de parsing (mesuré lors de la qualification) :
    jamais dans le cycle d'une requête HTTP.
    """
    return True


# ---------------------------------------------------------------------
# IFC (.ifc).
#
# IfcOpenShell en extraction SÉMANTIQUE seule (jamais de géométrie), import paresseux
# (~115 Mo RSS). Porte FERMÉE par défaut ; toujours asynchrone, routé vers la file
# dédiée `ingest_ifc` (cf. `ingest.queues`). Limite STRUCTURELLE en entités STEP.
# ---------------------------------------------------------------------
def ifc_enabled() -> bool:
    """Porte globale de l'ingestion IFC. À 0, un `.ifc` reconnu est refusé
    (`ifc_ingestion_disabled`). N'activer que si un worker `--queues=ingest_ifc` tourne."""
    return _bool("DOCUMENT_IFC_ENABLED", False)


def ifc_max_entities() -> int:
    """Entités STEP maximales (pré-scan puis compte du modèle). Au-delà :
    `ifc_complexity_limit` avant tout chargement du modèle."""
    return max(1, _int("DOCUMENT_IFC_MAX_ENTITIES", 250_000))


def ifc_forces_async() -> bool:
    """Décision produit ABSOLUE : IFC est TOUJOURS asynchrone. Aucun client ne peut
    faire parser une maquette dans le cycle d'une requête HTTP."""
    return True


# ---------------------------------------------------------------------
# DWG (.dwg).
#
# Format binaire propriétaire : RagATM ne le parse PAS. Conversion LOCALE par GNU
# LibreDWG 0.14 (`dwg2dxf`, exécutable séparé) vers un DXF ASCII TEMPORAIRE, puis
# extraction par le `DxfExtractor` déjà qualifié. Porte FERMÉE par défaut ; toujours
# asynchrone, routé vers la file dédiée `ingest_dwg` (cf. `ingest.queues`) — le
# convertisseur n'est installé que dans l'image du worker `worker_dwg`.
# ---------------------------------------------------------------------
def dwg_enabled() -> bool:
    """Porte globale de l'ingestion DWG. À 0, un `.dwg` reconnu est refusé
    (`dwg_ingestion_disabled`). N'activer que si un worker `--queues=ingest_dwg`
    portant LibreDWG 0.14 tourne, et seulement après les portes de conformité."""
    return _bool("DOCUMENT_DWG_ENABLED", False)


def dwg_converter_path() -> str:
    """Chemin ABSOLU du convertisseur `dwg2dxf` dans l'image du worker DWG.
    Jamais une donnée de requête : seule la configuration serveur le fixe."""
    return _str("DOCUMENT_DWG_CONVERTER_PATH", "/usr/local/bin/dwg2dxf")


def dwg_conversion_timeout_seconds() -> int:
    """Temps maximal d'une conversion. Au-delà : le groupe de processus est tué et
    l'ingestion échoue (`dwg_conversion_timeout`, NON rejouable). Les conversions
    mesurées en qualification sont bien en deçà de la seconde."""
    return max(1, _int("DOCUMENT_DWG_CONVERSION_TIMEOUT_SECONDS", 60))


def dwg_max_intermediate_dxf_mb() -> int:
    """Plafond du DXF intermédiaire (le DXF pèse jusqu'à ~4,6x le DWG ; source bornée
    par `UPLOAD_MAX_FILE_SIZE_MB`). Au-delà : `dwg_complexity_limit`, avant toute lecture."""
    return max(1, _int("DOCUMENT_DWG_MAX_INTERMEDIATE_DXF_MB", 128))


def dwg_forces_async() -> bool:
    """Décision produit ABSOLUE : DWG est TOUJOURS asynchrone. Un sous-processus natif
    sur une source binaire non fiable n'a pas sa place dans une requête HTTP."""
    return True


# ---------------------------------------------------------------------
# OUTLOOK (.msg) — lot MVP « corps et métadonnées ».
#
# Un .msg est un conteneur CFB non fiable. Le parsing est LOCAL et passif : aucune
# ressource distante n'est chargée, aucune pièce jointe n'est écrite ni ouverte,
# aucun contenu actif n'est exécuté. Les pièces jointes sont INVENTORIÉES (nom, type,
# taille, CID) mais leur contenu n'est PAS ingéré dans ce lot.
#
# Édition Open : le parseur tiers (GPL) a été écarté ; le format `.msg` est refusé quel que
# soit l'état du drapeau, conservé pour la compatibilité des réglages.
# ---------------------------------------------------------------------
def msg_enabled() -> bool:
    """Porte globale de l'ingestion Outlook (cf. `validation.validate_and_classify`).

    DÉFAUT 0, à l'inverse d'Excel/PPTX : lot neuf, activation progressive par
    environnement. À 0, un vrai .msg est refusé avec `msg_ingestion_disabled` — un
    refus EXPLICITE qui nomme Outlook, jamais le message « format Office ancien ».
    """
    return _bool("DOCUMENT_MSG_ENABLED", False)


def msg_max_file_bytes() -> int:
    """Plafond propre au .msg, en plus de la limite d'upload globale.

    25 Mio : le corpus de référence (notes de marché institutionnelles, 16 images
    inline chacune) s'étage de 0,9 à 2,5 Mo. Dix fois le plus gros observé laisse la
    marge des messages à pièces jointes lourdes sans ouvrir la porte à un conteneur
    dont le coût de parsing serait non bornable.
    """
    return int(_float("DOCUMENT_MSG_MAX_FILE_SIZE_MB", 25) * 1024 * 1024)


def msg_max_body_chars() -> int:
    """Plafond du corps APRÈS nettoyage. Au-delà : troncature + statut partiel.

    2 M caractères, aligné sur `html_max_chars` — un corps de mail qui dépasse est
    soit un fil de discussion géant, soit un fichier hostile ; dans les deux cas la
    troncature est préférable au refus (le début du message reste exploitable).
    """
    return _int("DOCUMENT_MSG_MAX_BODY_CHARS", 2_000_000)


def msg_max_recipients() -> int:
    """Nombre de destinataires conservés par champ (To, Cc) dans les métadonnées.

    500 : une liste de diffusion institutionnelle dépasse rarement quelques dizaines
    d'entrées. Au-delà on tronque et on pose `msg_recipients_truncated` — jamais un
    refus : une liste longue n'est pas une anomalie de sécurité. Les destinataires ne
    sont JAMAIS injectés dans le texte des chunks (cf. `msg_extractor`).
    """
    return max(0, _int("DOCUMENT_MSG_MAX_RECIPIENTS", 500))


def msg_max_attachments_inventoried() -> int:
    """Nombre de pièces jointes décrites dans l'inventaire.

    100 : le corpus de référence en porte 16 à 17 (images inline de la maquette
    éditoriale). Au-delà on tronque l'INVENTAIRE et on pose
    `msg_attachments_inventory_truncated` ; le compte total reste exact.
    """
    return max(0, _int("DOCUMENT_MSG_MAX_ATTACHMENTS_INVENTORIED", 100))


def msg_forces_async() -> bool:
    """Décision produit : le .msg est TOUJOURS asynchrone, comme Excel/PPTX/OCR.

    Mesure sur le corpus de référence : 1,1 à 1,4 s de parsing par message, dominées
    par le chargement des pièces jointes inline. Non bornable a priori depuis la
    couche HTTP.
    """
    return True


# ---------------------------------------------------------------------
# Courriels : EML et pièces jointes
#
# Deux portes de FORMAT (MSG historique, EML nouveau) et une porte d'ESPACE commune :
# un courriel n'est admis au dépôt que dans un RagSpace de DOCUMENT_EMAIL_RAGSPACE_IDS.
# Le contenu des pièces jointes exige en plus DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED.
# ---------------------------------------------------------------------
def eml_enabled() -> bool:
    """Porte de format `.eml` (défaut 0). À 0, un `.eml` reconnu est refusé nommément."""
    return _bool("DOCUMENT_EML_ENABLED", False)


def eml_max_file_bytes() -> int:
    return int(_float("DOCUMENT_EML_MAX_FILE_SIZE_MB", 25) * 1024 * 1024)


def email_ragspace_ids() -> Optional[frozenset]:
    """Espaces admettant les courriels ; ``None`` si la liste est invalide (n'autorise rien)."""
    return _uuid_list("DOCUMENT_EMAIL_RAGSPACE_IDS")


def email_global_enabled() -> bool:
    """Disponibilité GÉNÉRALE des courriels : tout RagSpace les admet.

    Ne touche à AUCUN droit : le dépôt exige toujours le droit d'écriture sur l'espace
    (`require_rag_space_access`), les quotas et la validation du fichier. Défaut 0 : un
    déploiement non configuré reste fermé. Jamais déduit d'une liste vide ni d'un joker.
    """
    return _bool("DOCUMENT_EMAIL_GLOBAL_ENABLED", False)


def email_admitted_for(rag_space_id) -> bool:
    """Le RagSpace admet-il les courriels ?

    Identité d'espace OBLIGATOIRE dans les deux modes (le corpus métier, sans espace,
    n'est jamais admis par ce biais). Mode général : tout espace identifié. Sinon : liste
    `DOCUMENT_EMAIL_RAGSPACE_IDS` (vide ou invalide = aucun).
    """
    import uuid

    try:
        space = str(uuid.UUID(str(rag_space_id))) if rag_space_id not in (None, "") else None
    except (ValueError, AttributeError, TypeError):
        space = None
    if not space:
        return False
    if email_global_enabled():
        return True
    allowed = email_ragspace_ids()
    return bool(allowed and space in allowed)


def email_attachment_ingest_enabled() -> bool:
    return _bool("DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED", False)


def email_attachments_enabled_for(rag_space_id) -> bool:
    """Contenu des pièces jointes ingéré pour CE document : drapeau ET espace admis."""
    return email_attachment_ingest_enabled() and email_admitted_for(rag_space_id)


def email_max_attachments() -> int:
    return max(1, min(200, _int("DOCUMENT_EMAIL_MAX_ATTACHMENTS", 20)))


def email_max_attachment_bytes() -> int:
    return int(max(0.1, _float("DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB", 20)) * 1024 * 1024)


def email_max_total_decoded_bytes() -> int:
    return int(max(0.1, _float("DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB", 50)) * 1024 * 1024)


def email_max_nesting_depth() -> int:
    return max(0, min(5, _int("DOCUMENT_EMAIL_MAX_NESTING_DEPTH", 2)))


def email_forces_async(format_key) -> bool:
    """Un courriel est TOUJOURS asynchrone (MSG comme EML) : ses pièces jointes peuvent
    déclencher plusieurs extractions dont le coût n'est pas bornable depuis la couche HTTP."""
    return format_key in ("msg", "eml")


# ---------------------------------------------------------------------
# HTML / Markdown
# ---------------------------------------------------------------------
def html_max_chars() -> int:
    return _int("DOCUMENT_HTML_MAX_CHARS", 2_000_000)


def markdown_max_chars() -> int:
    return _int("DOCUMENT_MARKDOWN_MAX_CHARS", 2_000_000)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def audio_ingest_enabled() -> bool:
    """Porte globale du dépôt audio. À 0, un audio reconnu est refusé au dépôt
    (`audio_ingestion_disabled`). Les audios DÉJÀ transcrits restent indexés et
    réindexables (leur transcription persistée est la source). N'activer que si un
    worker `--queues=ingest_audio` portant ffmpeg/ffprobe tourne."""
    return _bool("DOCUMENT_AUDIO_INGEST_ENABLED", False)


def audio_ragspace_ids() -> Optional[frozenset]:
    """Espaces admettant le dépôt audio ; ``None`` si la liste est invalide (n'autorise rien)."""
    return _uuid_list("DOCUMENT_AUDIO_RAGSPACE_IDS")


def audio_global_enabled() -> bool:
    """Disponibilité GÉNÉRALE du dépôt audio : tout RagSpace l'admet.

    Ne vaut QUE sous `DOCUMENT_AUDIO_INGEST_ENABLED=1`. Ne touche à AUCUN droit : le dépôt
    exige toujours le droit d'écriture sur l'espace (`require_rag_space_access`), les quotas,
    le plafond audio et la validation du fichier. Défaut 0 ; seules les valeurs booléennes
    explicites (`1/true/yes/on`) l'ouvrent — jamais une chaîne arbitraire, une liste vide ou
    un joker.
    """
    return _bool("DOCUMENT_AUDIO_GLOBAL_ENABLED", False)


def audio_admitted_for(rag_space_id) -> bool:
    """Le RagSpace admet-il le dépôt audio ?

    Drapeau `DOCUMENT_AUDIO_INGEST_ENABLED` OBLIGATOIRE, identité d'espace OBLIGATOIRE ;
    puis mode général (`DOCUMENT_AUDIO_GLOBAL_ENABLED` : tout espace identifié, y compris
    créé plus tard) OU espace listé dans `DOCUMENT_AUDIO_RAGSPACE_IDS` (vide = aucun).
    """
    import uuid

    if not audio_ingest_enabled():
        return False
    try:
        space = str(uuid.UUID(str(rag_space_id))) if rag_space_id not in (None, "") else None
    except (ValueError, AttributeError, TypeError):
        space = None
    if not space:
        return False
    if audio_global_enabled():
        return True
    allowed = audio_ragspace_ids()
    return bool(allowed and space in allowed)


def audio_max_upload_bytes() -> int:
    """Plafond de dépôt AUDIO en octets décimaux (défaut 600 000 000 = 600 Mo)."""
    return max(1, _int("DOCUMENT_AUDIO_MAX_UPLOAD_BYTES", 600_000_000))


def audio_max_duration_seconds() -> int:
    return max(60, _int("DOCUMENT_AUDIO_MAX_DURATION_SECONDS", 43_200))


def audio_asr_model_logical() -> str:
    return _str("AUDIO_ASR_MODEL_LOGICAL", "asr-whisper-turbo") or "asr-whisper-turbo"


#: Limites d'UN job ASR du Gateway (`/health/runtime`, relevées le 06/11/2026). Les
#: réglages de partie sont BORNÉS par elles : aucune configuration ne peut soumettre
#: une partie que le Gateway refuserait.
GATEWAY_ASR_MAX_DURATION_SECONDS = 6300
GATEWAY_ASR_MAX_UPLOAD_BYTES = 268_435_456


def audio_part_max_seconds() -> int:
    return max(60, min(GATEWAY_ASR_MAX_DURATION_SECONDS - 300, _int("AUDIO_ASR_PART_MAX_SECONDS", 5400)))


def audio_part_max_bytes() -> int:
    return max(1 << 20, min(GATEWAY_ASR_MAX_UPLOAD_BYTES - (12 << 20), _int("AUDIO_ASR_PART_MAX_BYTES", 192 << 20)))


def audio_poll_interval_seconds() -> int:
    return max(2, min(120, _int("AUDIO_ASR_POLL_INTERVAL_SECONDS", 10)))


def audio_job_deadline_seconds() -> int:
    return max(300, _int("AUDIO_ASR_JOB_DEADLINE_SECONDS", 4800))


def audio_spool_dir() -> str:
    return _str("AUDIO_SPOOL_DIR", "/tmp/ragatm-audio") or "/tmp/ragatm-audio"

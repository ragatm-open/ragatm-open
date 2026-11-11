# backend/apps/rag_core/services/ingest/extraction/base.py
from __future__ import annotations

"""
Socle commun d'extraction documentaire.

Définit :
- les erreurs typées (chacune porte un `code` machine + un `detail` lisible) ;
- le modèle de résultat structuré `ExtractionResult` (consommable par le chunking
  existant via `.text`, enrichi de `blocks`/`metadata`/`warnings`) ;
- l'interface `Extractor` que chaque extracteur de format implémente.

Ce module est volontairement PUR (aucun import Django/ORM) pour rester testable
isolément et réutilisable hors du cycle de requête.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple


# ---------------------------------------------------------------------
# Statuts & types de blocs
# ---------------------------------------------------------------------
class ExtractionStatus(str, Enum):
    """Issue d'une extraction. `str` pour sérialisation directe dans Document.meta."""

    SUCCESS = "success"
    PARTIAL = "partial"   # texte exploitable mais incomplet (timeout OCR, page illisible…)
    EMPTY = "empty"       # parse réussi mais aucun texte exploitable
    FAILED = "failed"     # erreur technique pendant l'extraction
    REJECTED = "rejected"  # refusé avant extraction (MIME/limite/sécurité)


class BlockType(str, Enum):
    TITLE = "title"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CSV_ROW = "csv_row"
    CODE = "code"
    OCR_PAGE = "ocr_page"
    SECTION = "section"
    LINK = "link"


# ---------------------------------------------------------------------
# Erreurs typées
# ---------------------------------------------------------------------
class ExtractionError(Exception):
    """Base : toute erreur d'extraction porte un code machine + un détail lisible.

    `detail` est destiné à l'utilisateur final : il NE DOIT PAS contenir de
    contenu extrait ni de donnée sensible (uniquement des métadonnées techniques).
    """

    code = "extraction_error"

    def __init__(self, detail: str = "", *, code: Optional[str] = None, meta: Optional[dict] = None):
        if code:
            self.code = code
        self.detail = detail or self.code
        # Métadonnées techniques NON sensibles à conserver dans Document.meta même
        # sur un échec (ex. is_image_only_pdf, page_count) — jamais de contenu extrait.
        self.meta = meta if isinstance(meta, dict) else None
        super().__init__(self.detail)


class UnsupportedFormat(ExtractionError):
    code = "unsupported_file_type"


class RejectedFile(ExtractionError):
    """Refus AVANT extraction (allowlist, limite de taille, sécurité)."""

    code = "rejected_file"

    def __init__(self, detail: str = "", *, code: Optional[str] = None, meta: Optional[dict] = None):
        super().__init__(detail, code=code, meta=meta)


class CorruptedFile(ExtractionError):
    code = "corrupted_file"


class ExtractionTimeout(ExtractionError):
    code = "extraction_timeout"


class ExtractionLimitExceeded(RejectedFile):
    """Une limite par format est dépassée (lignes/colonnes/cellule/ratio zip…)."""

    code = "extraction_limit_exceeded"


class ExtractionComplexityExceeded(ExtractionLimitExceeded):
    """Le document dépasse une borne de COMPLEXITÉ structurelle (nombre d'éléments,
    profondeur d'imbrication, entités…) — contrat partagé.

    Échec DÉFINITIF pour ce fichier (``NON_RETRYABLE``, cf. ``retry_strategy``) : rejouer
    ne changera rien tant que le fichier est celui-là. Les codes utilisateur propres à
    chaque format restent à la charge des lots de format (``code=`` explicite).
    """

    code = "extraction_complexity_exceeded"


# ---------------------------------------------------------------------
# Résultat structuré
# ---------------------------------------------------------------------
@dataclass
class Block:
    """Bloc structuré (amélioration progressive ; le chunking se base sur `text`).

    Les quatre derniers champs sont OPTIONNELS et rétrocompatibles : un extracteur qui
    ne les renseigne pas produit exactement le même `as_dict()` qu'avant. Ils existent
    parce que la chaîne ne portait jusqu'ici AUCUNE géométrie — un bloc savait de quelle
    page il venait, jamais d'où sur la page — ce qui interdisait toute réponse ancrée.
    """

    type: BlockType
    text: str
    level: Optional[int] = None          # niveau de titre (1..N) si TITLE/SECTION
    page_number: Optional[int] = None    # page OCR/PDF si applicable
    meta: dict = field(default_factory=dict)
    #: [x0, y0, x1, y1] normalisé dans [0,1], espace MEDIABOX, origine haut-gauche.
    #: Jamais synthétique : renseigné uniquement si le producteur fournit une géométrie.
    bbox: Optional[Tuple[float, float, float, float]] = None
    #: rang de lecture dans la page, tel que déclaré par le producteur.
    reading_order: Optional[int] = None
    #: moteur qui a produit le bloc (« native », « mineru », « tesseract »…).
    source_engine: Optional[str] = None
    #: traçabilité fine du producteur (version, mode, bloc source, artefact brut…).
    provenance: Optional[dict] = None

    def as_dict(self) -> dict:
        d = {"type": self.type.value, "text": self.text}
        if self.level is not None:
            d["level"] = self.level
        if self.page_number is not None:
            d["page_number"] = self.page_number
        if self.bbox is not None:
            d["bbox"] = list(self.bbox)
        if self.reading_order is not None:
            d["reading_order"] = self.reading_order
        if self.source_engine is not None:
            d["source_engine"] = self.source_engine
        if self.provenance:
            d["provenance"] = self.provenance
        if self.meta:
            d["meta"] = self.meta
        return d


#: Clés du repère de provenance générique (``meta.locator``).
#: Chacune est OPTIONNELLE et neutre vis-à-vis du format : ``type`` (nature du repère,
#: ex. « element », « entity », « layer »), ``path`` (chemin lisible dans la structure,
#: ex. un XPath simplifié), ``identifier`` (identifiant stable de l'élément cité).
#: Extensible : un format peut ajouter des clés ; celles-ci sont les seules RENDUES.
LOCATOR_KEYS = ("type", "path", "identifier")


def build_locator(
    *, type: Optional[str] = None, path: Optional[str] = None, identifier: Optional[str] = None,
) -> dict:
    """Construit un ``locator`` JSON-sérialisable ne portant que les clés renseignées.

    Un locator sans aucune clé est un dict vide : l'appelant décide s'il l'omet.
    """
    out: dict = {}
    if type:
        out["type"] = str(type)
    if path:
        out["path"] = str(path)
    if identifier:
        out["identifier"] = str(identifier)
    return out


@dataclass
class StructuredUnit:
    """Unité logique GÉNÉRIQUE d'un document structuré.

    Même modèle que ``slides`` / ``email_sections`` — une unité = un texte + des
    métadonnées JSON-sérialisables — mais typé et indépendant du format : un élément
    XML, une entité IFC, un bloc DXF… L'indexation (``indexing._build_unit_specs``)
    applique le chunker commun À L'INTÉRIEUR de chaque unité, jamais à cheval, avec
    ``page_number=None`` (une unité n'est pas une page) et recopie ``meta`` — dont le
    repère ``meta["locator"]`` (cf. ``LOCATOR_KEYS``) — sur chaque chunk dérivé.
    """

    text: str
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"text": self.text, "meta": dict(self.meta or {})}


def _unit_text(unit) -> str:
    """Texte d'une unité — ``StructuredUnit`` ou dict ``{"text": …}`` (tolérance)."""
    if isinstance(unit, dict):
        return unit.get("text") or ""
    return getattr(unit, "text", None) or ""


def table_is_evidence(table) -> bool:
    """Un fragment tabulaire est-il une ÉVIDENCE indexable ?

    Texte non blanc requis. Pour le rail GÉNÉRIQUE (un fragment = une ligne logique),
    au moins une valeur LUE (``columns_present``) : une ligne dont toutes les cellules
    sont illisibles porte un statut, pas une donnée. Un tableau ATOMIQUE (rail
    spécialisé) est une évidence dès qu'il a du texte.
    """
    if not isinstance(table, dict) or not str(table.get("markdown") or "").strip():
        return False
    meta = table.get("meta") or {}
    if str(meta.get("table_type") or "").startswith("generic"):
        return bool(meta.get("columns_present"))
    return True


@dataclass
class ExtractionResult:
    """
    Résultat d'extraction.

    - `text`     : texte global exploitable par le chunking existant (sortie principale).
    - `pages`    : optionnel, [(page_number, text)] pour préserver la pagination (PDF/OCR).
    - `slides`   : optionnel, unités logiques par diapositive (PPTX).
    - `units`    : optionnel, unités logiques GÉNÉRIQUES des formats structurés (1A).
    - `blocks`   : optionnel, blocs typés (titres, tableaux, lignes CSV, code…).
    - `metadata` : voir clés ci-dessous.
    - `warnings` : messages techniques NON sensibles (ex. "html_inline_neutralized").
    - `status`   : ExtractionStatus.
    - `detected_mime` / `method` : traçabilité (type réel détecté, méthode utilisée).
    - `partial`  : True si l'extraction est incomplète mais exploitable.
    """

    text: str = ""
    pages: Optional[List[Tuple[int, str]]] = None
    blocks: List[Block] = field(default_factory=list)
    # Tableaux structurés (P3) : liste de {"markdown": str, "meta": dict}. Chacun
    # devient un chunk ATOMIQUE non fragmenté à l'indexation.
    tables: List[dict] = field(default_factory=list)
    # Chunks EXCEL déjà fenêtrés par l'extracteur : [{"text": str, "meta": dict}].
    # Voie DISTINCTE de `tables` (cf. indexing._build_excel_specs) : un classeur se
    # fenêtre par lignes (une table PDF, elle, est atomique et seulement tronquée),
    # ses bornes sont propres, et ses valeurs sont EXACTES (requires_visual_check=False,
    # là où `tables` porte la sémantique OCR/plans « à vérifier visuellement »).
    excel_chunks: List[dict] = field(default_factory=list)
    # Unités logiques PPTX : [{"text": str, "meta": dict}], une entrée par diapositive
    # (+ une par bloc de notes si DOCUMENT_PPTX_INCLUDE_NOTES=1). Voie DISTINCTE de
    # `excel_chunks` : ces unités ne sont PAS pré-fenêtrées — le chunker texte générique
    # est appliqué À L'INTÉRIEUR de chaque diapositive (cf. indexing._build_slide_specs),
    # ce qui donne gratuitement la politique de taille/overlap commune tout en garantissant
    # qu'aucun chunk ne franchit une frontière de diapositive.
    #
    # Elle est aussi DISTINCTE de `pages` : une diapositive n'est pas une page. Le rail
    # `pages` renseignerait `Chunk.page_number`, ce qui ferait afficher « p. 4 » et
    # fabriquerait un lien viewer `?page=4` vers un fichier qui n'a pas de pages —
    # exactement la raison pour laquelle un classeur garde `page_number=None`.
    slides: List[dict] = field(default_factory=list)
    # Sections d'un message Outlook : [{"role", "index", "title", "text"}]. Même
    # principe que `slides` — unités logiques NON pré-fenêtrées, le chunker générique
    # s'applique à l'intérieur de chacune (cf. indexing._build_email_specs), et
    # surtout PAS le rail `pages` : un e-mail n'a pas de pages.
    #
    # `role` vaut "header" (objet/expéditeur/date) ou "body". Le boilerplate détecté
    # (signature, disclaimer) n'entre PAS dans ce rail : il est compté dans
    # `metadata["removed_boilerplate_chars"]` mais n'est jamais indexé.
    email_sections: List[dict] = field(default_factory=list)
    # Unités logiques GÉNÉRIQUES : ``[StructuredUnit(text, meta)]``,
    # produites par les extracteurs de formats structurés (XML, DXF, IFC… — aucun n'est
    # activé par ce lot). Voie DISTINCTE de `pages` (une unité n'a pas de page) et
    # PRIORITAIRE sur `text` à l'indexation : quand `units` est non vide, `text` n'est
    # JAMAIS chunké (cf. indexing._prepare_candidate) — il reste un diagnostic
    # (`chars`) et ne double jamais l'index. Vide par défaut : contrat historique intact.
    units: List[StructuredUnit] = field(default_factory=list)
    # Unités VISUELLES : ``[StructuredUnit]`` issues des normaliseurs
    # visuels. Rail ADDITIF, DISTINCT de `units` : `units` est prioritaire et exclusif
    # (il écarte le texte et les tableaux), alors que ces unités S'AJOUTENT au texte et
    # aux tableaux du candidat, avec ``page_number=None``. Les tableaux analysés, eux,
    # entrent dans `tables` via le rail générique. Vide par défaut : contrat intact.
    visual_units: List[StructuredUnit] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    detected_mime: str = ""
    method: str = ""
    partial: bool = False
    # Visuels découverts par l'extracteur : ``[VisualAsset]`` aux
    # octets normalisés, destinés au rail visuel. TRANSITOIRES — jamais sérialisés dans
    # ``metadata``, jamais persistés, absents du ``repr``.
    visual_assets: list = field(default_factory=list, repr=False, compare=False)
    #: Pièces jointes EXTRAITES d'un courriel : ``[(index, ExtractionResult)]``
    #: — ``index`` renvoie à ``metadata["attachments"]``. TRANSITOIRES : consommées par
    #: l'indexation (fragments rattachés au courriel), jamais sérialisées dans ``metadata``.
    email_attachment_results: list = field(default_factory=list, repr=False, compare=False)
    #: Nature de source visuelle reconnue par l'extracteur (``"image"``), transitoire.
    visual_source_kind: str = field(default="", repr=False, compare=False)
    #: Régions tabulaires natives d'un PDF (page -> bboxes) qui ont conditionné la
    #: découverte des visuels (VISUAL-ENRICHMENT-DEFERRED). Persistées avec l'état
    #: d'enrichissement pour que la découverte DIFFÉRÉE retrouve exactement les mêmes
    #: visuels sans refaire l'extraction des tableaux. Transitoire.
    visual_suppressed_regions: dict = field(default_factory=dict, repr=False, compare=False)
    #: Géométrie DÉCLARÉE des pages PDF (CV-3B) : ``[PdfPageGeometry]`` ou ``None`` si
    #: illisible / format sans pages. Matérialisée en ``Page`` par le parent ; jamais
    #: sérialisée dans ``metadata``. Transitoire.
    page_geometry: Optional[list] = field(default=None, repr=False, compare=False)

    # -- helpers -------------------------------------------------------
    def add_warning(self, code: str) -> None:
        if code and code not in self.warnings:
            self.warnings.append(code)

    @property
    def char_count(self) -> int:
        return len(self.text or "")

    def has_units(self) -> bool:
        """Au moins une unité structurée au texte non blanc."""
        return any(_unit_text(u).strip() for u in (self.units or []))

    def has_visual_units(self) -> bool:
        """Au moins une unité visuelle au texte non blanc."""
        return any(_unit_text(u).strip() for u in (self.visual_units or []))

    def is_empty(self) -> bool:
        """Aucun contenu indexable : ni texte global, ni unité structurée ou visuelle non
        blanche, ni fragment tabulaire porteur d'une valeur."""
        return (
            not (self.text or "").strip()
            and not self.has_units()
            and not self.has_visual_units()
            and not any(table_is_evidence(t) for t in (self.tables or []))
        )

    def finalize(self, *, fmt: str, mime: str, method: str) -> "ExtractionResult":
        """Renseigne les métadonnées canoniques et harmonise le statut.

        N'écrase pas un statut REJECTED/FAILED déjà positionné.
        """
        self.detected_mime = mime
        self.method = method
        md = self.metadata
        md.setdefault("format", fmt)
        md.setdefault("mime", mime)
        md["chars"] = self.char_count
        md.setdefault("ocr_used", bool(md.get("ocr_used", False)))
        if self.blocks and "blocks" not in md:
            md["blocks"] = len(self.blocks)
        if self.units and "units" not in md:
            md["units"] = len(self.units)

        if self.status not in (ExtractionStatus.REJECTED, ExtractionStatus.FAILED):
            if self.is_empty():
                self.status = ExtractionStatus.EMPTY
            elif self.partial:
                self.status = ExtractionStatus.PARTIAL
            else:
                self.status = ExtractionStatus.SUCCESS
        md["status"] = self.status.value
        if self.warnings:
            md["warnings"] = list(self.warnings)
        return self


# ---------------------------------------------------------------------
# Contexte d'extraction
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractionContext:
    """Contexte passé à un extracteur. Immuable, sans état applicatif lourd."""

    filename: str = ""
    declared_content_type: str = ""   # Content-Type CLIENT (NON fiable, indicatif)
    # OCR activé pour CETTE extraction (None = lire la configuration globale).
    ocr_enabled: Optional[bool] = None
    # Budget OCR par PAGE pour CETTE extraction, en secondes (None = configuration
    # globale). Même motif que `ocr_enabled` : transporté par appel afin qu'une reprise
    # puisse accorder un budget différent à UN document sans changer celui de tous les
    # autres. Ce budget est TOTAL pour la page — OSD, texte et confiance se le partagent,
    # ils ne le redémarrent pas chacun.
    ocr_page_timeout_s: Optional[int] = None
    # Natures de source dont les visuels sont à DÉCOUVRIR pour le rail visuel
    # Vide = aucun visuel collecté : comportement historique.
    visual_sources: frozenset = frozenset()
    # Visuels PDF admis au plus pour CETTE extraction (None = borne globale
    # DOCUMENT_PDF_VISUAL_MAX_ASSETS). Transporté par appel, comme `ocr_enabled` : seule la
    # borne change, ni les heuristiques ni l'ordre de sélection (STRUCTURED-VISUAL-CAP-32).
    pdf_visual_max_assets: Optional[int] = None
    # Visuels PDF admis au plus PAR PAGE pour CETTE extraction (None = borne globale
    # DOCUMENT_PDF_VISUAL_MAX_PER_PAGE). Même transport, même portée (PER-PAGE-CAP).
    pdf_visual_max_per_page: Optional[int] = None
    # RagSpace propriétaire du document extrait. Transporté PAR APPEL — jamais lu
    # d'un état du processus — jusqu'au processus enfant d'extraction : c'est lui qui décide du rail
    # tabulaire générique (`config.pdf_generic_tables_decision`). None = rail OFF.
    rag_space_id: Optional[str] = None

    @property
    def lower_name(self) -> str:
        return (self.filename or "").lower()


# ---------------------------------------------------------------------
# Interface d'extracteur
# ---------------------------------------------------------------------
class Extractor(Protocol):
    """Contrat commun. Un extracteur reçoit des octets + un contexte et renvoie un
    `ExtractionResult`. Il lève une `ExtractionError` typée en cas d'échec.
    """

    name: str

    def can_handle(self, *, detected_mime: str, ctx: ExtractionContext) -> bool: ...

    def extract(self, data: bytes, ctx: ExtractionContext) -> ExtractionResult: ...


# ---------------------------------------------------------------------
# Utilitaires partagés (texte)
# ---------------------------------------------------------------------
def decode_best_effort(raw: bytes, *, declared: str = "") -> Tuple[str, Optional[str]]:
    """Décode des octets en texte (best-effort).

    Renvoie (texte, warning_code|None). Essaie un BOM/charset déclaré puis des
    encodages courants ; ne lève jamais.
    """
    raw = raw or b""
    if not raw:
        return "", None

    tried: List[str] = []
    if declared:
        tried.append(declared)
    tried += ["utf-8", "utf-8-sig", "cp1252", "latin-1"]

    seen = set()
    for enc in tried:
        enc = (enc or "").strip().lower()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc), None
        except (UnicodeDecodeError, LookupError):
            continue

    # dernier recours : remplacement (jamais d'exception)
    return raw.decode("utf-8", errors="replace"), "encoding_replaced"


def join_blocks_text(blocks: Iterable[Block]) -> str:
    """Concatène les blocs en un texte lisible pour le chunking."""
    parts = [b.text.strip() for b in blocks if (b.text or "").strip()]
    return "\n\n".join(parts).strip()


def pages_to_text(pages: Sequence[Tuple[int, str]]) -> str:
    return "\n\n".join((t or "").strip() for _, t in pages if (t or "").strip()).strip()

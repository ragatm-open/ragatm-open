import type { DocStatus } from "@/components/ui";
import type { RetryStrategy, Retryability } from "@/lib/reindex";
import type { DocumentAudio } from "./audio";

/** Sous-ensemble sûr de meta.extraction exposé par l'API (cf. views_common). */
export type DocumentExtraction = {
  status: string; // success | ready_with_warnings | partial | failed | rejected | ocr_required
  extraction_status?: string;
  /** Code d'erreur STRUCTURÉ (jamais un message humain à parser). */
  error_code?: string;
  /**
   * Ce que le backend juge possible pour ce document. Le frontend l'AFFICHE ; il ne
   * le calcule pas — une seconde table de décision finirait par proposer un OCR que
   * le backend refuserait.
   */
  retryability?: Retryability;
  recommended_strategy?: RetryStrategy | null;
  method?: string;
  ocr_used?: boolean;
  native_fallback_used?: boolean;
  native_fallback_engine?: string | null;
  pdf_native_engine?: string | null;
  pages?: number | null;
  text_pages?: number[];
  empty_pages?: number[];
  image_only_pages?: number[];
  warnings?: string[];
  native_text_chars?: number | null;
  ocr_text_chars?: number | null;
  extraction_quality_score?: number | null;
  // Image (Lot 2/3)
  type?: string; // "image" pour les fichiers image
  // --- Outlook (.msg) ---
  subject?: string | null;
  sender_name?: string | null;
  sender_address?: string | null;
  sent_at?: string | null;
  recipient_count?: number | null;
  body_kind?: string | null;
  removed_boilerplate_chars?: number | null;
  attachments_detected?: number | null;
  attachments_ingested?: number | null;
  attachments_supported_in_this_release?: boolean;
  attachments?: Array<{
    filename: string;
    extension: string;
    mimetype: string;
    size_bytes: number;
    content_id: string;
    is_inline: boolean;
    // statut d'ingestion et provenance de chaque pièce (jamais de contenu).
    index?: number;
    path?: string;
    depth?: number;
    detected_format?: string | null;
    status?: EmailAttachmentStatus;
    reason?: string | null;
    fragment_count?: number;
    same_as?: string | null;
  }>;
  source_format?: string | null;
  attachment_ingestion?: "enabled" | "disabled" | null;
  email_status?: "complete" | "partial" | null;
  requires_visual_check?: boolean;
  image?: {
    format?: string;
    width?: number;
    height?: number;
    mode?: string;
    exif_orientation_applied?: boolean;
    rotation_applied?: number;
  } | null;
};

/** Document tel que renvoyé par GET /api/v1/documents/. */
export type ApiDocument = {
  id: string;
  rag_space_id: string;
  filename: string;
  content_type: string;
  /**
   * Type court décidé par le BACKEND (`PDF`, `DOCX`, `JPEG`…) : exactement la valeur
   * sur laquelle `?ordering=file_type` trie en base. Absent d'un backend antérieur
   * au lot 1 => repli sur l'extension (cf. `documentTypeLabel`).
   */
  file_type?: string;
  size_bytes: number | null;
  sha256: string;
  status: string; // PENDING | INGESTING | READY | FAILED
  error_message: string;
  created_at: string | null;
  chunks_count?: number;
  extraction?: DocumentExtraction;
  /**
   * Enrichissement visuel DIFFÉRÉ (VISUAL-ENRICHMENT-DEFERRED). `status=READY` dit
   * « exploitable par la recherche », pas « vision terminée » : cet état, facultatif,
   * dit où en est l'analyse des visuels (`pending` | `running` | `partial` |
   * `succeeded` | `failed` | `not_applicable`). Absent d'un backend antérieur.
   */
  visual_enrichment_status?: string;
  /**
   * Transcription audio : étape, parties, durée, langue, code
   * d'échec. Présent seulement pour un enregistrement audio ; jamais de texte transcrit.
   */
  audio?: DocumentAudio;
};

/** Libellé d'enrichissement visuel à afficher SOUS le statut principal, ou null. */
export function visualEnrichmentNoticeKey(d: Pick<ApiDocument, "status" | "visual_enrichment_status">):
  | "visualEnrichmentPending"
  | "visualEnrichmentPartial"
  | null {
  if ((d.status || "").toUpperCase() !== "READY") return null;
  const s = d.visual_enrichment_status;
  if (s === "pending" || s === "running") return "visualEnrichmentPending";
  if (s === "partial") return "visualEnrichmentPartial";
  return null;
}

/** Compteurs par onglet, calculés côté serveur sur TOUT le RagSpace. */
export type DocumentCounts = {
  all: number;
  ready: number;
  analyzing: number;
  queued: number;
  error: number;
};

export type DocumentListResponse = {
  items: ApiDocument[];
  /** Total du RagSpace après recherche et filtre de statut — jamais `items.length`. */
  total: number;
  limit: number;
  offset: number;
  has_next?: boolean;
  has_previous?: boolean;
  ordering?: string;
  counts?: DocumentCounts;
};

/** Statut backend (MAJ) → statut UI du badge. */
export function toDocStatus(status: string): DocStatus {
  switch ((status || "").toUpperCase()) {
    case "READY":
      return "ready";
    case "INGESTING":
      return "analyzing";
    case "PENDING":
      return "queued";
    case "FAILED":
      return "error";
    default:
      return "queued";
  }
}

/**
 * Actions de reprise à proposer pour un document en échec, dans l'ordre.
 *
 * Rien n'est déduit du code d'erreur ici : on relaie la décision du backend. C'est
 * ce qui garantit qu'on ne propose JAMAIS « Réessayer avec OCR » sur un document
 * dont l'OCR vient précisément de ne rien produire.
 */
export function retryActionsFor(doc: Pick<ApiDocument, "extraction">): {
  strategies: RetryStrategy[];
  recommended: RetryStrategy | null;
  retryability: Retryability;
} {
  const ext = doc.extraction;
  const retryability = (ext?.retryability ?? "") as Retryability;
  const recommended = (ext?.recommended_strategy ?? null) as RetryStrategy | null;

  if (retryability === "non_retryable" || retryability === "replace_source_recommended") {
    // Aucune reprise ne peut aboutir : ne rien proposer vaut mieux qu'un bouton
    // dont on sait qu'il reproduira l'échec.
    return { strategies: [], recommended: null, retryability };
  }
  if (!retryability) return { strategies: [], recommended: null, retryability };

  // La méthode recommandée d'abord, puis les autres méthodes applicables.
  const ordered: RetryStrategy[] = [];
  if (recommended) ordered.push(recommended);
  if (!ordered.includes("standard")) ordered.push("standard");
  return { strategies: ordered, recommended, retryability };
}

/**
 * Type court AFFICHÉ. Le backend décide (même valeur que la clé de tri SQL) ; le
 * repli sur l'extension ne sert qu'à un backend antérieur au lot 1, jamais à
 * contredire une valeur reçue.
 */
export function documentTypeLabel(d: Pick<ApiDocument, "file_type" | "filename">): string {
  if (typeof d.file_type === "string" && d.file_type) return d.file_type;
  const ext = (d.filename || "").split(".").pop()?.toUpperCase() || "";
  return /^[A-Z0-9]{1,8}$/.test(ext) ? ext : "";
}

/** Couleurs de pastille par type de fichier. */
export const FILE_COLORS: Record<string, string> = {
  PDF: "#C2402F",
  DOCX: "#2A6FDB",
  DOC: "#2A6FDB",
  XLSX: "#1F7A4C",
  PPTX: "#C2622F",
  MD: "#5B53C9",
  MARKDOWN: "#5B53C9",
  CSV: "#1F8A5B",
  HTML: "#C2562F",
  HTM: "#C2562F",
  TXT: "#6B635B",
  // Images (Lot 3)
  JPG: "#C9772F",
  JPEG: "#C9772F",
  PNG: "#2F9C9C",
  TIF: "#7A5BC9",
  TIFF: "#7A5BC9",
  WEBP: "#C95B8E",
  // Audio
  MP3: "#B0458C",
  M4A: "#B0458C",
  WAV: "#B0458C",
  FLAC: "#B0458C",
  OGG: "#B0458C",
  WEBM: "#B0458C",
};

/**
 * Extensions acceptées par l'input upload (PDF/Office/texte + images MVP).
 *
 * SOURCE UNIQUE : `folderImport.ts` en dérive `SUPPORTED_EXTENSIONS`, et le
 * glisser-déposer passe par le même filtre. Un test verrouille cette dérivation.
 *
 * Excel : `.xlsx` UNIQUEMENT. `.xlsm` (macros), `.xls` (format binaire hérité),
 * `.ods` et `.xlsb` restent refusés — le backend les rejette par CONTENU, quelle que
 * soit l'extension, mais autant ne pas les proposer.
 *
 * PowerPoint : `.pptx` UNIQUEMENT. `.pptm` (macros), `.ppt` (binaire hérité)
 * et `.odp` restent refusés, également par CONTENU. Cette liste n'est PAS une barrière
 * de sécurité — c'est une commodité : elle évite à l'utilisateur de sélectionner un
 * fichier qui sera refusé de toute façon.
 *
 * Outlook (`.msg`) N'EST PAS dans cette constante : il dépend d'un flag serveur fermé
 * par défaut. Le proposer en dur promettrait un format que l'environnement refuse.
 *
 * RÔLE EXACT DE CETTE CONSTANTE — ce n'est plus la liste d'admission. C'est le seul
 * REPLI d'affichage, le temps que `/api/v1/capabilities/` réponde au premier rendu.
 * Dès que les capacités sont connues, `uploadAccept()` reconstruit la liste depuis
 * `FORMAT_CATALOG` et `ingest.formats` — y compris pour les extensions ci-dessous,
 * qui ne sont donc PAS garanties déposables. La source de vérité est le serveur.
 */
export const UPLOAD_ACCEPT =
  ".pdf,.docx,.xlsx,.pptx,.csv,.html,.htm,.md,.markdown,.txt,.jpg,.jpeg,.png,.tif,.tiff,.webp";

/** Libellé d'aide des formats acceptés (affiché sous la zone de dépôt). */
export const UPLOAD_FORMATS_LABEL =
  "PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), Markdown, CSV, HTML, TXT, Images (JPG, PNG, TIFF, WEBP)";

/** Capacité `.msg` telle qu'annoncée par le backend. */
export type MsgCapability = {
  format: "msg";
  status: "available" | "disabled_by_config" | "dependency_missing" | "unsupported";
  body_ingestion: boolean;
  attachment_ingestion: boolean;
  inline_image_ocr: boolean;
};

/**
 * Liste de dépôt EFFECTIVE, capacités serveur comprises.
 *
 * Ne jamais utiliser l'attribut HTML `accept` comme validation : c'est un filtre de
 * confort. La décision reste au backend, par CONTENU.
 */
/**
 * CATALOGUE CANONIQUE — clé de format **backend** -> extensions + libellé.
 *
 * Source unique du nommage et du regroupement. Cette table ne DÉCIDE de rien : c'est
 * `ingest.formats` qui dit ce qui est ouvert. Elle ne fait que traduire une clé de
 * format serveur en extensions présentables.
 *
 * L'ancienne forme — une constante statique de seize extensions, plus une chaîne de
 * `if (formats?.includes(...))` par format avancé — avait deux défauts que ce lot
 * ferme :
 *
 *  1. tout nouveau format backend exigeait un patch frontend, alors que le contrat
 *     annoncé était « le serveur décide » ;
 *  2. les seize extensions de base échappaient à ce contrat : fermer
 *     `DOCUMENT_EXCEL_ENABLED` laissait la modale proposer `.xlsx`, que le backend
 *     refusait ensuite fichier par fichier.
 *
 * Une entrée sans clé correspondante dans `ingest.formats` n'affiche rien ; une clé
 * annoncée sans entrée ici reste proposée sous son propre nom (repli ci-dessous) —
 * jamais silencieusement absente.
 *
 * L'ORDRE est celui de l'affichage.
 */
export const FORMAT_CATALOG: readonly {
  key: string;
  label: string;
  extensions: readonly string[];
}[] = Object.freeze([
  { key: "pdf", label: "PDF", extensions: Object.freeze(["pdf"]) },
  { key: "docx", label: "Word (.docx)", extensions: Object.freeze(["docx"]) },
  { key: "excel", label: "Excel (.xlsx)", extensions: Object.freeze(["xlsx"]) },
  { key: "pptx", label: "PowerPoint (.pptx)", extensions: Object.freeze(["pptx"]) },
  { key: "csv", label: "CSV", extensions: Object.freeze(["csv"]) },
  { key: "html", label: "HTML", extensions: Object.freeze(["html", "htm"]) },
  { key: "markdown", label: "Markdown", extensions: Object.freeze(["md", "markdown"]) },
  { key: "txt", label: "TXT", extensions: Object.freeze(["txt"]) },
  // `image` est une FAMILLE : ses extensions viennent d'`ingest.image_formats`.
  { key: "image", label: "Images", extensions: Object.freeze([]) },
  { key: "msg", label: "Outlook (.msg)", extensions: Object.freeze(["msg"]) },
  { key: "eml", label: "Courriel (.eml)", extensions: Object.freeze(["eml"]) },
  { key: "xml", label: "XML (.xml)", extensions: Object.freeze(["xml"]) },
  { key: "dxf", label: "DXF (.dxf)", extensions: Object.freeze(["dxf"]) },
  { key: "ifc", label: "IFC (.ifc)", extensions: Object.freeze(["ifc"]) },
  { key: "dwg", label: "DWG (.dwg)", extensions: Object.freeze(["dwg"]) },
  // `audio` est une FAMILLE : ses extensions viennent
  // d'`ingest.audio.extensions`, et seulement si l'ESPACE courant autorise l'audio.
  // Sa mention d'aide est i18n (page documents), pas ce libellé.
  { key: "audio", label: "Audio", extensions: Object.freeze([]) },
]);

/**
 * Sous-format image annoncé par le backend -> extensions de fichier.
 *
 * `DOCUMENT_IMAGE_FORMATS` nomme des FORMATS (`jpeg`, `tiff`), pas des extensions :
 * un `jpeg` autorisé rend `.jpg` ET `.jpeg` déposables. Retirer `webp` côté serveur
 * doit retirer `.webp` de la modale — c'est tout l'objet de cette traduction.
 */
const IMAGE_EXTENSIONS_BY_FORMAT: Readonly<Record<string, readonly string[]>> = Object.freeze({
  jpeg: Object.freeze(["jpg", "jpeg"]),
  jpg: Object.freeze(["jpg", "jpeg"]),
  png: Object.freeze(["png"]),
  tiff: Object.freeze(["tif", "tiff"]),
  tif: Object.freeze(["tif", "tiff"]),
  webp: Object.freeze(["webp"]),
  gif: Object.freeze(["gif"]),
  bmp: Object.freeze(["bmp"]),
});

/** Extensions de la famille `image` pour les sous-formats annoncés. */
function imageExtensionsOf(imageFormats?: string[] | null): readonly string[] {
  const out: string[] = [];
  for (const fmt of imageFormats ?? []) {
    for (const ext of IMAGE_EXTENSIONS_BY_FORMAT[String(fmt).toLowerCase()] ?? []) {
      if (!out.includes(ext)) out.push(ext);
    }
  }
  return out;
}

/** Extensions d'une clé de format, capacités serveur comprises. */
function extensionsOfFormatKey(
  key: string,
  imageFormats?: string[] | null,
  audioExtensions?: readonly string[] | null
): readonly string[] {
  if (key === "image") return imageExtensionsOf(imageFormats);
  // Audio : extensions fournies par l'appelant (capacité ET espace autorisé), sinon rien.
  if (key === "audio") return audioExtensions ?? [];
  const entry = FORMAT_CATALOG.find((f) => f.key === key);
  // Repli : une clé annoncée que le catalogue ne connaît pas reste déposable sous son
  // propre nom. Mieux vaut une extension approximative qu'un format ouvert et invisible.
  return entry ? entry.extensions : Object.freeze([key]);
}

/**
 * Liste de dépôt EFFECTIVE, dérivée des SEULES capacités serveur.
 *
 * Ne jamais utiliser l'attribut HTML `accept` comme validation : c'est un filtre de
 * confort. La décision reste au backend, par CONTENU.
 *
 * `formats` absent (capacités pas encore chargées) => repli sur le socle statique, le
 * temps du premier rendu. Un tableau VIDE signifie « le serveur n'annonce rien », et
 * non « tout » : on ne propose alors aucun format.
 */
export function uploadAccept(
  msg?: MsgCapability | null,
  formats?: string[] | null,
  imageFormats?: string[] | null,
  /** Extensions audio — à passer SEULEMENT si l'espace courant autorise l'audio. */
  audioExtensions?: readonly string[] | null
): string {
  if (!formats) return UPLOAD_ACCEPT;
  const exts: string[] = [];
  for (const entry of FORMAT_CATALOG) {
    if (!formats.includes(entry.key)) continue;
    for (const ext of extensionsOfFormatKey(entry.key, imageFormats, audioExtensions)) {
      if (!exts.includes(ext)) exts.push(ext);
    }
  }
  // Clés annoncées hors catalogue : jamais perdues.
  for (const key of formats) {
    if (FORMAT_CATALOG.some((f) => f.key === key)) continue;
    for (const ext of extensionsOfFormatKey(key, imageFormats)) {
      if (!exts.includes(ext)) exts.push(ext);
    }
  }
  return exts.map((e) => `.${e}`).join(",");
}

/**
 * Mention d'aide des courriels. Pièces jointes annoncées seulement si le serveur dit les
 * ingérer (`ingest.msg.attachment_ingestion`) ; sinon, le périmètre corps + métadonnées.
 */
export function emailFormatsLabel(formats: readonly string[], msg?: MsgCapability | null): string {
  const kinds = [
    formats.includes("msg") ? "Outlook (.msg)" : null,
    formats.includes("eml") ? "MIME (.eml)" : null,
  ].filter(Boolean).join(" et ");
  if (msg?.attachment_ingestion) {
    return (
      `Courriels ${kinds} : corps, métadonnées et pièces jointes prises en charge ` +
      "(PDF, Word, PowerPoint, Excel, CSV, texte, HTML, images par OCR) ; formats non supportés signalés"
    );
  }
  return `Courriels ${kinds} : corps et métadonnées (pièces jointes inventoriées, non ingérées)`;
}

/** Libellé d'aide correspondant, dérivé du même catalogue. */
export function uploadFormatsLabel(
  msg?: MsgCapability | null,
  formats?: string[] | null,
  imageFormats?: string[] | null
): string {
  if (!formats) return UPLOAD_FORMATS_LABEL;
  const parts: string[] = [];
  for (const entry of FORMAT_CATALOG) {
    if (!formats.includes(entry.key)) continue;
    // Audio : mention i18n dédiée, composée par la page (formats, plafond, transcription).
    if (entry.key === "audio") continue;
    if (entry.key === "image") {
      const exts = imageExtensionsOf(imageFormats);
      if (!exts.length) continue;
      parts.push(`Images (${exts.map((e) => e.toUpperCase()).join(", ")})`);
      continue;
    }
    if (entry.key === "msg" || entry.key === "eml") {
      // Courriels : UNE mention pour .msg et .eml, au périmètre RÉEL annoncé
      // par le serveur. Jamais d'analyse Vision, de GIF, d'antivirus ni de calcul tabulaire promis.
      if (entry.key === "eml" && formats.includes("msg")) continue; // déjà couvert par la mention .msg
      parts.push(emailFormatsLabel(formats, msg));
      continue;
    }
    parts.push(entry.label);
  }
  return parts.join(", ");
}

/**
 * Message d'erreur d'embeddings adapté au provider d'indexation GLOBAL réellement
 * configuré (distinct du provider de génération/chat du RagSpace). Le backend
 * masque volontairement le provider dans `error_message` (anti-fuite), d'où le
 * `provider` passé par l'UI via l'endpoint /api/v1/capabilities/.
 */
function embeddingsErrorMessage(provider?: string): string {
  switch ((provider || "").toLowerCase()) {
    case "ai_gateway":
      return "Pipeline d'embeddings de l'AI Gateway indisponible : document non indexé. Vérifiez que la passerelle IA répond, puis relancez l'analyse.";
    case "ollama":
      return "Service d'embeddings Ollama lent ou indisponible : document non indexé. Démarrez Ollama, vérifiez OLLAMA_BASE_URL, ou changez le provider d'embeddings global, puis relancez l'analyse.";
    default:
      return "Service d'embeddings indisponible : document non indexé. Relancez l'analyse une fois le service d'indexation rétabli.";
  }
}

/**
 * Message d'erreur humain à partir du message brut backend (code: détail).
 * `embeddingsProvider` : provider d'embeddings global (cf. getCapabilities) pour
 * adapter le message d'indisponibilité (ne pas mentionner Ollama pour un pipeline Gateway, etc.).
 */
export function humanError(raw: string | null | undefined, embeddingsProvider?: string): string {
  const s = (raw || "").trim();
  if (!s) return "Échec de l'analyse de ce document.";
  // Codes typés du pipeline d'extraction (cf. docs/DOCUMENT_EXTRACTION.md).
  if (/macro_enabled_document_refused|\.docm/i.test(s))
    return "Document Word à macros (.docm) refusé pour raisons de sécurité.";
  if (/macro_enabled_workbook_refused/i.test(s))
    return "Classeur Excel à macros (.xlsm) refusé pour raisons de sécurité. Enregistrez-le en .xlsx sans macros.";
  if (/executable_refused/i.test(s)) return "Fichier exécutable refusé.";
  // Document Office XML porteur d'une déclaration DOCTYPE/ENTITY (bombe à entités).
  if (/unsafe_xml/i.test(s))
    return "Document refusé : il contient une structure XML non autorisée.";
  // Verrou temporaire Office : le frontend les filtre avant envoi, mais un client tiers
  // (ou un ancien onglet) peut encore en poster un. Le message dit ce que c'est.
  if (/office_temporary_file/i.test(s))
    return "Fichier temporaire créé par Microsoft Office (préfixe « ~$ ») : c'est un fichier de verrouillage, pas un document.";
  if (/macro_enabled_presentation_refused/i.test(s))
    return "Présentation PowerPoint à macros (.pptm) refusée pour raisons de sécurité. Enregistrez-la en .pptx sans macros.";
  if (/msg_ingestion_disabled/i.test(s))
    return "Les fichiers Outlook (.msg) ne sont pas activés sur cet environnement. Exportez le message en .html ou .txt, ou contactez un administrateur.";
  if (/msg_dependency_unavailable/i.test(s))
    return "L'ingestion des fichiers Outlook (.msg) est indisponible sur cet environnement. Contactez un administrateur.";
  if (/msg_file_too_large/i.test(s))
    return "Message Outlook trop volumineux. Réduisez les pièces jointes ou contactez un administrateur.";
  if (/email_ingestion_not_enabled_for_space/i.test(s))
    return "Les courriels (.msg, .eml) ne sont pas activés pour cet espace. Contactez un administrateur.";
  if (/eml_ingestion_disabled/i.test(s))
    return "Les courriels (.eml) ne sont pas activés sur cet environnement. Contactez un administrateur.";
  if (/eml_file_too_large/i.test(s))
    return "Courriel trop volumineux. Réduisez les pièces jointes ou contactez un administrateur.";
  if (/email_parse_failed/i.test(s))
    return "Courriel illisible ou corrompu. Réexportez-le depuis votre messagerie.";
  if (/msg_parse_failed/i.test(s))
    return "Message Outlook illisible ou corrompu. Réexportez-le depuis Outlook, ou enregistrez-le en .html.";
  if (/legacy_office_format/i.test(s))
    return "Format Office ancien (.xls/.doc/.ppt) non supporté. Enregistrez-le dans un format moderne (.docx, .pptx) ou exportez-le en CSV.";
  if (/unsupported_spreadsheet_format/i.test(s))
    return "Classeur OpenDocument (.ods) non supporté. Exportez-le en CSV.";
  if (/unsupported_presentation_format/i.test(s))
    return "Présentation OpenDocument (.odp) non supportée. Enregistrez-la au format .pptx.";
  // `.odt` retombait en « archive ZIP » : le refus était juste, sa raison fausse.
  if (/unsupported_text_document_format/i.test(s))
    return "Document OpenDocument (.odt) non supporté. Enregistrez-le au format .docx.";
  // Excel (lots E2/E4) — placés AVANT les branches génériques.
  if (/excel_not_supported_here/i.test(s))
    return "Les classeurs Excel ne sont pas encore acceptés dans les corpus métier partagés. Utilisez vos documents privés, ou exportez la feuille utile en CSV.";
  if (/excel_shared_strings_too_large/i.test(s))
    return "Classeur trop volumineux : il contient trop de valeurs textuelles distinctes pour être indexé.";
  if (/excel_too_many_cells|excel_too_many_sheets|excel_uncompressed_too_large/i.test(s))
    return "Classeur trop volumineux (cellules, feuilles ou taille au-delà des limites).";
  if (/excel_corrupted|excel_xml_parse_error|excel_missing_workbook_xml/i.test(s))
    return "Classeur Excel illisible ou corrompu.";
  if (/excel_disabled/i.test(s))
    return "L'ingestion des classeurs Excel est désactivée sur cet environnement.";
  // PowerPoint — placés AVANT les branches génériques.
  if (/pptx_not_supported_here/i.test(s))
    return "Les présentations PowerPoint ne sont pas encore acceptées dans les corpus métier partagés. Utilisez vos documents privés, ou exportez la présentation en PDF.";
  if (/pptx_disabled/i.test(s))
    return "L'ingestion des présentations PowerPoint est désactivée sur cet environnement.";
  if (/encrypted_pptx/i.test(s))
    return "Présentation protégée ou chiffrée : retirez la protection puis réimportez le fichier.";
  if (/invalid_pptx_archive/i.test(s))
    return "Présentation PowerPoint illisible ou corrompue.";
  if (/pptx_limit_exceeded|pptx_uncompressed_too_large|pptx_too_many_entries|pptx_compression_ratio/i.test(s))
    return "Présentation trop volumineuse (diapositives ou taille au-delà des limites).";
  if (/pptx_library_missing/i.test(s))
    return "L'extraction des présentations PowerPoint est indisponible sur cet environnement.";
  // Générique : Excel OU PowerPoint désactivé par flag (le refus historique conservé).
  if (/unsupported_office_format/i.test(s))
    return "Format Office non supporté sur cet environnement. Exportez en PDF ou CSV.";
  if (/archive_not_supported/i.test(s)) return "Archive non supportée : envoyez les fichiers un par un.";
  if (/ocr_disabled_for_image/i.test(s)) return "Image reçue : l'OCR n'est pas activé sur cet environnement.";
  // Images (Lot 3) — placés AVANT les branches génériques (too_large/unsupported).
  if (/image_corrupted/i.test(s)) return "Image illisible ou corrompue.";
  if (/image_too_large/i.test(s)) return "Image trop volumineuse ou trop grande en pixels.";
  if (/image_ingest_disabled/i.test(s))
    return "L'ingestion des images est désactivée sur cet environnement.";
  if (/image_unsupported_format/i.test(s))
    return "Format image non supporté. Formats acceptés : JPG, PNG, TIFF, WEBP.";
  // OCR — ordre du plus spécifique au plus générique (messages distincts).
  if (/ocr_engine_missing|rasterizer_missing|ocr_unavailable|ocr_no_provider/i.test(s))
    return "Moteur OCR non installé sur le serveur. Contactez un administrateur (binaire tesseract/OCR requis).";
  if (/ocr_no_text/i.test(s))
    return "L'OCR n'a extrait aucun texte exploitable (document illisible ou délai dépassé).";
  if (/pdf_scanned_requires_ocr/i.test(s) || /scan|texte non/i.test(s))
    return "Aucun texte natif détecté. Lancez « Réindexer avec OCR » pour ce document.";
  if (/csv_too_many_rows|csv_too_many_columns|csv_cell_too_long/i.test(s))
    return "CSV trop volumineux (lignes/colonnes/cellule au-delà des limites).";
  if (/docx_(uncompressed_too_large|compression_ratio|too_many_entries)/i.test(s))
    return "Document Word rejeté (archive suspecte).";
  if (/corrupted_file|docx_corrupted|docx_.*parse|csv_parse_error/i.test(s))
    return "Fichier corrompu ou illisible.";
  if (/extraction_timeout/i.test(s)) return "Délai d'analyse dépassé pour ce document.";
  if (/empty_document/i.test(s)) return "Aucun texte exploitable n'a pu être extrait.";
  if (/unsupported_file_type/i.test(s)) return "Type de fichier non supporté.";
  if (/too.?large|413|file_too_large|size/i.test(s)) return "Fichier trop volumineux pour être analysé.";
  if (/storage_file/i.test(s)) return "Le fichier n'a pas pu être enregistré. Réessayez l'import.";
  // Collision de contenu : un document ACTIF au contenu identique existe déjà dans cet
  // espace ; cette ligne ne peut pas être relancée (distinct d'un échec d'analyse).
  if (/duplicate_content|uniq_rag_document_space_sha256/i.test(s))
    return "Contenu identique déjà présent dans cet espace : ce document est un doublon d'un document déjà disponible, il ne sera pas analysé à nouveau.";
  // Erreur d'INGESTION/embeddings (provider lent/indisponible) — distincte d'un échec d'enqueue.
  // Le provider réel vient de l'UI (capabilities) ; à défaut, on tente de le déduire du message.
  if (/transient_ingest_error|embedding_provider_unavailable|embeddings?|ollama|ai_gateway|provider_unavailable/i.test(s)) {
    const detected = /ai_gateway/i.test(s) ? "ai_gateway" : /ollama/i.test(s) ? "ollama" : "";
    return embeddingsErrorMessage(embeddingsProvider || detected);
  }
  // Erreur d'ENQUEUE (broker/worker indisponible) en mode async réel.
  if (/ingest_enqueue_unavailable|enqueue_failed|\b503\b|service unavailable/i.test(s))
    return "Service d'indexation momentanément indisponible. Réessayez l'import dans quelques instants.";
  return s;
}

/**
 * Notice informative (non bloquante) à afficher pour un document READY dont
 * l'extraction comporte des avertissements (pages graphiques ignorées, texte
 * récupéré via un moteur PDF alternatif). Renvoie "" si rien à signaler.
 */
/** Statut d'une pièce jointe de courriel (contrat backend). */
export type EmailAttachmentStatus =
  | "PENDING"
  | "INDEXED"
  | "PARTIALLY_INDEXED"
  | "UNSUPPORTED"
  | "REJECTED"
  | "FAILED"
  | "SKIPPED_LIMIT"
  | "DUPLICATE_IN_EMAIL"
  | "NOT_INGESTED"
  | "NO_EXTRACTABLE_TEXT";

/**
 * Résumé des pièces jointes d'un courriel : ce qui est INTERROGEABLE, ce qui ne l'est pas,
 * et pourquoi. Une pièce n'est dite interrogeable que si elle a produit des fragments
 * indexés (INDEXED / PARTIALLY_INDEXED) — jamais sur la foi de son seul nom.
 */
export function emailAttachmentSummary(ext: NonNullable<ApiDocument["extraction"]>): string {
  const rows = ext.attachments ?? [];
  if (!rows.length || ext.attachment_ingestion !== "enabled") return "";
  const count = (pred: (s?: string) => boolean) => rows.filter((r) => pred(r.status)).length;
  const indexed = count((s) => s === "INDEXED" || s === "PARTIALLY_INDEXED");
  const unsupported = count((s) => s === "UNSUPPORTED");
  const rejected = count((s) => s === "REJECTED");
  const failed = count((s) => s === "FAILED");
  const skipped = count((s) => s === "SKIPPED_LIMIT");
  // Lue mais sans texte (logo, visuel décoratif) : ni une erreur, ni un contenu indexé (27C).
  const noText = count((s) => s === "NO_EXTRACTABLE_TEXT");
  const parts = [`${indexed} pièce${indexed > 1 ? "s" : ""} jointe${indexed > 1 ? "s" : ""} interrogeable${indexed > 1 ? "s" : ""} sur ${rows.length}`];
  if (noText) parts.push(`${noText} sans texte exploitable`);
  if (unsupported) parts.push(`${unsupported} non prise${unsupported > 1 ? "s" : ""} en charge`);
  if (rejected) parts.push(`${rejected} refusée${rejected > 1 ? "s" : ""} (sécurité ou limite)`);
  if (failed) parts.push(`${failed} en échec d'extraction`);
  if (skipped) parts.push(`${skipped} non traitée${skipped > 1 ? "s" : ""} (limite atteinte)`);
  return parts.join(", ") + ".";
}

export function extractionNotice(doc: Pick<ApiDocument, "status" | "extraction">): string {
  const ext = doc.extraction;
  if (!ext) return "";
  if ((doc.status || "").toUpperCase() !== "READY") return "";

  const parts: string[] = [];
  // Texte issu d'OCR sur un fichier visuel (image, ou PDF OCRisé si le backend
  // pose le drapeau) : invite à vérifier la source avant usage métier.
  if (ext.requires_visual_check) {
    parts.push(
      "Texte OCR issu d'un fichier visuel : vérifiez le fichier source avant usage métier.",
    );
  }
  const imageOnly = ext.image_only_pages ?? ext.empty_pages ?? [];
  if (imageOnly.length > 0) {
    const list = imageOnly.slice(0, 8).join(", ");
    const suffix = imageOnly.length > 8 ? "…" : "";
    parts.push(
      `Certaines pages graphiques n'ont pas été indexées (page${imageOnly.length > 1 ? "s" : ""} ${list}${suffix}).`,
    );
  }
  if (ext.native_fallback_used && ext.native_fallback_engine) {
    parts.push(`Texte récupéré via un moteur PDF alternatif : ${ext.native_fallback_engine}.`);
  }
  // Outlook : dire explicitement que les pièces jointes détectées ne sont PAS
  // interrogeables. Sans cette phrase, un compteur de pièces jointes laisserait
  // croire à l'utilisateur qu'il peut les questionner.
  const attachments = ext.attachments_detected ?? 0;
  const attachmentSummary = emailAttachmentSummary(ext);
  if (attachmentSummary) parts.push(attachmentSummary);
  if (attachments > 0 && ext.attachments_supported_in_this_release === false) {
    parts.push(
      `${attachments} pièce${attachments > 1 ? "s" : ""} jointe${attachments > 1 ? "s" : ""} détectée${attachments > 1 ? "s" : ""} mais non ingérée${attachments > 1 ? "s" : ""} : leur contenu n'est pas interrogeable.`,
    );
  }
  if (ext.body_kind === "plain") {
    parts.push("Corps du message repris en texte brut (aucune version HTML exploitable).");
  }
  return parts.join(" ");
}

/**
 * Message utilisateur pour un échec de suppression (à partir du message brut
 * backend / statut HTTP). 409 = ingestion en cours, 403/404 = impossible/introuvable.
 */
export function deleteErrorMessage(raw: string | null | undefined): string {
  const s = (raw || "").trim();
  if (/document_processing|\b409\b|conflict|en cours de traitement/i.test(s))
    return "Le document est en cours de traitement. Réessayez après la fin de l'analyse.";
  if (/\b403\b|\b404\b|not_found|forbidden|permission/i.test(s))
    return "Suppression impossible ou document introuvable.";
  return s || "Échec de la suppression du document.";
}

/** True si le document est READY mais avec des avertissements d'extraction. */
export function isReadyWithWarnings(doc: Pick<ApiDocument, "status" | "extraction">): boolean {
  return (
    (doc.status || "").toUpperCase() === "READY" &&
    (doc.extraction?.status === "ready_with_warnings" ||
      (doc.extraction?.warnings?.length ?? 0) > 0 ||
      (doc.extraction?.image_only_pages?.length ?? 0) > 0 ||
      (doc.extraction?.empty_pages?.length ?? 0) > 0)
  );
}

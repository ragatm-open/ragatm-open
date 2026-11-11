// frontend/src/app/documents/_lib/folderImport.ts
// Helpers PURS (testables) pour l'import récursif de dossier (webkitdirectory).
import { UPLOAD_ACCEPT } from "./types";

/** Concurrence d'upload bornée (uploads unitaires en parallèle limité). */
export const FOLDER_UPLOAD_CONCURRENCY = 4;
/**
 * Nombre maximum de fichiers compatibles par sélection de dossier.
 *
 * Porté de 500 à 1 000 sans toucher aux autres bornes : la concurrence (4), la
 * cadence de l'ordonnanceur, la taille par fichier et la taille totale du lot
 * restent celles d'avant. Le quota documentaire du workspace (1 000 par défaut)
 * reste l'arbitre serveur, fichier par fichier.
 */
export const MAX_FOLDER_FILES = 1000;
/** Taille totale maximale du lot (garde-fous ergonomique ; le backend borne par fichier). */
export const MAX_FOLDER_TOTAL_MB = 1024;

/**
 * Extensions d'une chaîne `accept` — minuscules, sans point, ordre de déclaration
 * (donc ordre d'affichage déterministe des formats).
 *
 * La chaîne passée est celle d'`uploadAccept(msg, serverFormats)` : la liste STATIQUE
 * `UPLOAD_ACCEPT` n'est que son socle, jamais la vérité de disponibilité. C'est le point
 * du lot : l'import de dossier décidait seul, sur la constante de module, pendant que
 * l'import unitaire et le glisser-déposer suivaient déjà les capacités annoncées par le
 * serveur. Un format ouvert côté serveur était donc compté « format non supporté » par le
 * seul chemin dossier — sans qu'aucun appel API ne soit tenté.
 *
 * Le filtrage frontend reste ergonomique : le backend refait tous les contrôles
 * (format réel par signature, taille, MIME).
 */
export function extensionsOfAccept(accept: string): readonly string[] {
  return Object.freeze(
    (accept || "")
      .split(",")
      .map((e) => e.trim().replace(/^\./, "").toLowerCase())
      .filter(Boolean)
  );
}

/**
 * Socle STATIQUE des formats connus du frontend. Conservé comme catalogue de base et
 * comme repli des appelants purs (tests), jamais comme décision de disponibilité :
 * tout chemin d'import passe par un `FolderFormatCatalog` construit depuis l'`accept`
 * effectif.
 */
export const SUPPORTED_EXTENSIONS_ORDERED: readonly string[] = extensionsOfAccept(UPLOAD_ACCEPT);

export const SUPPORTED_EXTENSIONS: ReadonlySet<string> = new Set(SUPPORTED_EXTENSIONS_ORDERED);

/**
 * Format importable tel que l'utilisateur le choisit : UNE case à cocher par
 * format fonctionnel (« Images »), pas par extension (`.jpg`, `.png`, …).
 */
export type ImportFormat = {
  /** Clé stable, identité de sélection (jamais affichée). */
  key: string;
  /** Libellé affiché (« PowerPoint »). */
  label: string;
  /** Extensions terminales regroupées — minuscules, sans point. */
  extensions: readonly string[];
};

const IMAGES = { key: "images", label: "Images" };
const HTML = { key: "html", label: "HTML" };
const MARKDOWN = { key: "markdown", label: "Markdown" };
const AUDIO = { key: "audio", label: "Audio" };

/**
 * NOMMAGE et REGROUPEMENT des extensions — cette table ne DÉFINIT PAS ce qui est
 * importable : elle ne fait qu'habiller UPLOAD_ACCEPT, qui reste la source unique.
 *
 * Conséquences voulues (verrouillées par test) :
 *  - une extension retirée d'UPLOAD_ACCEPT disparaît de la modale sans toucher ici ;
 *  - une extension ajoutée à UPLOAD_ACCEPT sans entrée ici reste sélectionnable via
 *    le repli ci-dessous — elle n'est JAMAIS silencieusement non importable ;
 *  - une entrée ici sans extension correspondante dans UPLOAD_ACCEPT n'affiche rien
 *    (impossible de proposer un format que le pipeline refuse).
 *
 * `.ppt` / `.pptm` ne figurent pas dans UPLOAD_ACCEPT : « PowerPoint » ne couvre
 * donc que `.pptx`. Idem « Excel » = `.xlsx` seul.
 */
const FORMAT_BY_EXTENSION: Readonly<Record<string, { key: string; label: string }>> = {
  pdf: { key: "pdf", label: "PDF" },
  docx: { key: "word", label: "Word" },
  xlsx: { key: "excel", label: "Excel" },
  pptx: { key: "powerpoint", label: "PowerPoint" },
  csv: { key: "csv", label: "CSV" },
  txt: { key: "txt", label: "Texte" },
  html: HTML,
  htm: HTML,
  md: MARKDOWN,
  markdown: MARKDOWN,
  jpg: IMAGES,
  jpeg: IMAGES,
  png: IMAGES,
  tif: IMAGES,
  tiff: IMAGES,
  webp: IMAGES,
  // Formats sous capacité serveur : nommés ici pour que la modale affiche
  // « Outlook (.msg) » plutôt que le repli « .msg ». Les NOMMER ne les rend pas
  // importables — seul `ingest.formats` en décide.
  msg: { key: "msg", label: "Outlook (.msg)" },
  xml: { key: "xml", label: "XML" },
  dxf: { key: "dxf", label: "DXF" },
  ifc: { key: "ifc", label: "IFC" },
  dwg: { key: "dwg", label: "DWG" },
  // Audio : proposé seulement si l'espace autorise l'audio (accept).
  mp3: AUDIO,
  m4a: AUDIO,
  wav: AUDIO,
  flac: AUDIO,
  ogg: AUDIO,
  webm: AUDIO,
};

/**
 * Clé du pseudo-format des fichiers SANS extension.
 *
 * Un fichier nommé `PLAN` était classé « format non supporté » et n'atteignait jamais
 * l'API — alors que le backend décide par SIGNATURE et l'aurait accepté si c'était un
 * PDF. Le frontend décidait donc d'un format qu'il n'avait pas les moyens de connaître.
 *
 * Ces fichiers sont désormais admissibles, sous un format DISTINCT : on ne prétend pas
 * savoir ce qu'ils contiennent, et on ne les affiche jamais comme « PDF » avant que le
 * serveur ne l'ait établi. L'utilisateur peut les décocher comme n'importe quel format.
 * Ce n'est pas un relâchement : le backend reste seul juge (signature, taille, MIME) et
 * refuse exactement comme avant — la seule différence est qu'il est désormais CONSULTÉ.
 */
export const UNKNOWN_FORMAT_KEY = "unknown";

/** Extension conventionnelle d'un fichier sans suffixe — jamais dans une chaîne `accept`. */
export const NO_EXTENSION = "";

/** Repli : une extension supportée mais non nommée reste proposée telle quelle. */
function formatMetaOf(ext: string): { key: string; label: string } {
  if (ext === NO_EXTENSION) {
    return { key: UNKNOWN_FORMAT_KEY, label: "Sans extension (format reconnu par le serveur)" };
  }
  return FORMAT_BY_EXTENSION[ext] ?? { key: `ext:${ext}`, label: `.${ext}` };
}

/**
 * Catalogue effectif d'un `accept` : extensions retenues + formats regroupés.
 *
 * SOURCE UNIQUE de décision pour les trois chemins d'import. Construit à la volée depuis
 * la chaîne `accept` effective — il n'existe aucune seconde allowlist de runtime.
 */
export type FolderFormatCatalog = {
  /** Extensions effectives, minuscules sans point, ordre de déclaration. */
  readonly extensionsOrdered: readonly string[];
  readonly extensions: ReadonlySet<string>;
  /** Formats fonctionnels regroupés (une case à cocher par format). */
  readonly formats: readonly ImportFormat[];
};

/** Formats fonctionnels d'une liste d'extensions — regroupement et ordre déterministes. */
function buildImportFormats(extensionsOrdered: readonly string[]): readonly ImportFormat[] {
  const byKey = new Map<string, { key: string; label: string; extensions: string[] }>();
  const ordered: { key: string; label: string; extensions: string[] }[] = [];
  for (const ext of extensionsOrdered) {
    const meta = formatMetaOf(ext);
    let fmt = byKey.get(meta.key);
    if (!fmt) {
      fmt = { key: meta.key, label: meta.label, extensions: [] };
      byKey.set(meta.key, fmt);
      ordered.push(fmt);
    }
    fmt.extensions.push(ext);
  }
  return Object.freeze(
    ordered.map((f) => Object.freeze({ ...f, extensions: Object.freeze(f.extensions) }) as ImportFormat)
  );
}

/**
 * Catalogue effectif depuis la chaîne `accept` d'`uploadAccept(msg, serverFormats)`.
 * Appeler avec l'`accept` du moment : un format ouvert côté serveur devient importable
 * par le dossier SANS toucher à la moindre liste du frontend.
 */
export function folderFormatCatalog(accept: string): FolderFormatCatalog {
  const declared = extensionsOfAccept(accept);
  // Aucun format ouvert => aucune admission, pas même « sans extension » : envoyer des
  // octets à un serveur qui n'accepte rien ne produirait que des refus.
  const extensionsOrdered = declared.length
    ? Object.freeze([...declared, NO_EXTENSION])
    : declared;
  return Object.freeze({
    extensionsOrdered,
    extensions: new Set(extensionsOrdered),
    formats: buildImportFormats(extensionsOrdered),
  });
}

/** Catalogue du socle statique — repli des appelants sans capacités serveur. */
export const STATIC_FOLDER_FORMAT_CATALOG: FolderFormatCatalog = folderFormatCatalog(UPLOAD_ACCEPT);

/** @deprecated Catalogue du SOCLE statique. Décider depuis `folderFormatCatalog(accept)`. */
export const IMPORT_FORMATS: readonly ImportFormat[] = STATIC_FOLDER_FORMAT_CATALOG.formats;

/** Format fonctionnel d'un nom de fichier dans CE catalogue (undefined si hors catalogue). */
export function formatOfFilename(
  name: string,
  catalog: FolderFormatCatalog = STATIC_FOLDER_FORMAT_CATALOG
): ImportFormat | undefined {
  const ext = fileExtension(name);
  if (!catalog.extensions.has(ext)) return undefined;
  const key = formatMetaOf(ext).key;
  return catalog.formats.find((f) => f.key === key);
}

/** Un format présent dans le dossier + son nombre de fichiers. */
export type FolderFormatSummary = { format: ImportFormat; count: number };

/**
 * Formats supportés RÉELLEMENT PRÉSENTS dans la sélection, avec leur volumétrie.
 * Une liste courte et utile : les formats absents du dossier ne sont pas proposés.
 * Ordre déterministe (celui d'UPLOAD_ACCEPT). Ne mute pas `files`.
 *
 * Les verrous Office sont exclus du COMPTAGE : un dossier contenant `budget.xlsx` et
 * `~$budget.xlsx` doit annoncer « Excel — 1 fichier », pas 2. Ils ne sont importables
 * par aucun format, donc les compter mentirait sur ce que la case à cocher va faire.
 * Un format qui n'existe QUE sous forme de verrous n'apparaît pas du tout.
 */
export function summarizeFolderFormats(
  files: readonly File[],
  catalog: FolderFormatCatalog = STATIC_FOLDER_FORMAT_CATALOG
): FolderFormatSummary[] {
  const counts = new Map<string, number>();
  for (const file of files) {
    if (isOfficeTemporaryFilename(file.name)) continue;
    const fmt = formatOfFilename(file.name, catalog);
    if (!fmt) continue;
    counts.set(fmt.key, (counts.get(fmt.key) ?? 0) + 1);
  }
  return catalog.formats.filter((f) => counts.has(f.key)).map((format) => ({
    format,
    count: counts.get(format.key) as number,
  }));
}

/** Extensions couvertes par un ensemble de formats sélectionnés. */
export function extensionsOfFormats(
  selectedKeys: ReadonlySet<string>,
  catalog: FolderFormatCatalog = STATIC_FOLDER_FORMAT_CATALOG
): Set<string> {
  const out = new Set<string>();
  for (const fmt of catalog.formats) {
    if (!selectedKeys.has(fmt.key)) continue;
    for (const ext of fmt.extensions) out.add(ext);
  }
  return out;
}

/** Présélection par défaut : TOUS les formats présents dans le dossier. */
export function defaultSelectedFormats(summaries: readonly FolderFormatSummary[]): Set<string> {
  return new Set(summaries.map((s) => s.format.key));
}

export function fileExtension(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

/**
 * Fichier de verrouillage temporaire créé par Microsoft Office (`~$rapport.docx`).
 *
 * Office écrit ce fichier à l'ouverture d'un document et le supprime à la fermeture ;
 * ceux qui subsistent viennent d'un plantage ou d'une fermeture brutale. Ils portent
 * l'extension du document (`.docx`, `.xlsx`, `.pptx`) mais n'en sont PAS : ~160 octets
 * contenant le nom du propriétaire de la session. Le backend les refuse déjà, à raison —
 * mais les envoyer produit une erreur par fichier, pour rien.
 *
 * Ce n'est donc PAS un « format non supporté » : c'est un fichier qui n'a jamais eu
 * vocation à être importé. D'où un motif distinct (`office_temporary_file`).
 *
 * SOURCE UNIQUE de la règle : ne pas réécrire `name.startsWith("~$")` ailleurs.
 *
 * La détection porte sur le **basename**, pas sur le chemin : `sous-dossier/~$a.docx`
 * est temporaire, `rapport~$.docx` ne l'est pas (le marqueur est un PRÉFIXE). Les deux
 * séparateurs sont gérés — `webkitRelativePath` est en `/`, mais un nom saisi ou une
 * plateforme Windows peut porter des `\`.
 */
export function isOfficeTemporaryFilename(name: string): boolean {
  if (!name) return false;
  const basename = name.split(/[\\/]/).pop() ?? name;
  return basename.startsWith("~$");
}

export function isSupportedFilename(
  name: string,
  catalog: FolderFormatCatalog = STATIC_FOLDER_FORMAT_CATALOG
): boolean {
  return catalog.extensions.has(fileExtension(name));
}

/**
 * Sépare `webkitRelativePath` en (sourceFolder = 1er segment, relativePath = reste).
 * Ex. "Ressources humaines/procédures/congés.pdf"
 *   -> { sourceFolder: "Ressources humaines", relativePath: "procédures/congés.pdf" }
 */
export function parseWebkitPath(
  webkitRelativePath: string,
  fallbackName: string
): { sourceFolder: string; relativePath: string } {
  const norm = (webkitRelativePath || "").replace(/\\/g, "/").replace(/^\/+/, "");
  const segments = norm.split("/").filter((s) => s !== "");
  if (segments.length <= 1) {
    // Fichier isolé (pas de dossier racine) : aucune source_folder.
    return { sourceFolder: "", relativePath: segments[0] || fallbackName };
  }
  return { sourceFolder: segments[0], relativePath: segments.slice(1).join("/") };
}

export type PlannedFile = { file: File; sourceFolder: string; relativePath: string };
/**
 * `unsupported` (format hors UPLOAD_ACCEPT) et `format_not_selected` (format
 * supporté mais décoché par l'utilisateur) sont volontairement DISTINCTS : le
 * récapitulatif doit pouvoir expliquer laquelle des deux raisons s'applique.
 */
export type SkipReason =
  | "office_temporary_file"
  | "unsupported"
  | "format_not_selected"
  | "too_large"
  | "empty";
export type SkippedFile = { name: string; relativePath: string; reason: SkipReason };

export type FolderPlan = {
  sourceFolder: string;
  totalFound: number;
  compatible: PlannedFile[];
  skipped: SkippedFile[];
  totalBytes: number;
  /** Trop de fichiers compatibles (> MAX_FOLDER_FILES) : import bloqué. */
  tooManyFiles: boolean;
  /** Taille totale au-delà de MAX_FOLDER_TOTAL_MB : import bloqué. */
  tooLarge: boolean;
};

function webkitPathOf(file: File): string {
  return (file as unknown as { webkitRelativePath?: string }).webkitRelativePath || file.name;
}

/**
 * Construit le récapitulatif d'import à partir des fichiers sélectionnés :
 * écarte les formats non supportés / non sélectionnés / vides / trop gros,
 * conserve les chemins relatifs, calcule la taille totale et les garde-fous.
 *
 * `selectedExtensions` = extensions retenues par l'utilisateur (cf.
 * `extensionsOfFormats`). Omis => tous les formats supportés sont retenus
 * (glisser-déposer et upload unitaire, où il n'y a pas de choix de formats).
 * Un ensemble VIDE signifie « aucun format retenu » — et non « tous ».
 *
 * Ne mute ni `files` ni `opts`.
 */
export function buildFolderPlan(
  files: readonly File[],
  opts: {
    maxSizeBytes: number;
    /**
     * Plafond PAR FICHIER, quand il dépend du format (audio : 600 Mo, cf. `audio.ts`).
     * Omis => `maxSizeBytes` pour tous.
     */
    maxSizeBytesOf?: (name: string) => number;
    selectedExtensions?: ReadonlySet<string>;
    /** Catalogue effectif (capacités serveur). Omis => socle statique. */
    catalog?: FolderFormatCatalog;
  }
): FolderPlan {
  const catalog = opts.catalog ?? STATIC_FOLDER_FORMAT_CATALOG;
  const compatible: PlannedFile[] = [];
  const skipped: SkippedFile[] = [];
  let sourceFolder = "";
  let totalBytes = 0;

  for (const file of files) {
    const { sourceFolder: sf, relativePath } = parseWebkitPath(webkitPathOf(file), file.name);
    if (sf && !sourceFolder) sourceFolder = sf;

    // Verrou Office EN PREMIER : `~$budget.xlsx` porte une extension supportée, donc
    // le contrôle d'extension le laisserait passer et il partirait au serveur pour se
    // faire refuser. Le classer « format non supporté » serait faux, et « décoché »
    // encore plus : il n'a jamais été proposé.
    if (isOfficeTemporaryFilename(file.name)) {
      skipped.push({ name: file.name, relativePath, reason: "office_temporary_file" });
      continue;
    }
    if (!isSupportedFilename(file.name, catalog)) {
      skipped.push({ name: file.name, relativePath, reason: "unsupported" });
      continue;
    }
    // Décoché par l'utilisateur : écarté AVANT les contrôles de taille — c'est
    // l'intention explicite, elle prime sur un motif technique secondaire.
    if (opts.selectedExtensions && !opts.selectedExtensions.has(fileExtension(file.name))) {
      skipped.push({ name: file.name, relativePath, reason: "format_not_selected" });
      continue;
    }
    if (file.size === 0) {
      skipped.push({ name: file.name, relativePath, reason: "empty" });
      continue;
    }
    if (file.size > (opts.maxSizeBytesOf ? opts.maxSizeBytesOf(file.name) : opts.maxSizeBytes)) {
      skipped.push({ name: file.name, relativePath, reason: "too_large" });
      continue;
    }
    compatible.push({ file, sourceFolder: sf, relativePath });
    totalBytes += file.size;
  }

  return {
    sourceFolder,
    totalFound: files.length,
    compatible,
    skipped,
    totalBytes,
    tooManyFiles: compatible.length > MAX_FOLDER_FILES,
    tooLarge: totalBytes > MAX_FOLDER_TOTAL_MB * 1024 * 1024,
  };
}

/** Construit le FormData d'upload d'UN fichier, avec provenance d'import optionnelle. */
export function buildUploadForm(
  file: File,
  opts: { ragSpaceId: string; relativePath?: string; sourceFolder?: string }
): FormData {
  const form = new FormData();
  form.append("rag_space_id", opts.ragSpaceId);
  form.append("file", file);
  if (opts.relativePath) form.append("relative_path", opts.relativePath);
  if (opts.sourceFolder) form.append("source_folder", opts.sourceFolder);
  return form;
}

export function humanBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 o";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / Math.pow(1024, i);
  // 1 décimale sous 10, arrondi entier au-delà ; sans zéro décimal superflu.
  const rounded = v >= 10 || i === 0 ? Math.round(v) : Math.round(v * 10) / 10;
  return `${rounded} ${units[i]}`;
}

/** Détection de support de la sélection de dossier (webkitdirectory). */
export function folderInputSupported(): boolean {
  if (typeof document === "undefined") return false;
  try {
    return "webkitdirectory" in document.createElement("input");
  } catch {
    return false;
  }
}

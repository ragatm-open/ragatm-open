// Aides PURES (testables) de l'écran « Capacités documentaires ».
// Aucune valeur métier n'est codée en dur : tout est LU dans l'instantané backend.
import type {
  DocumentCapability,
  DocumentFormat,
  MatrixRow,
} from "@/lib/coreConfig";
import { isActiveState } from "./status";

// --- Familles & filtres -------------------------------------------------------

export const FAMILY_FILTERS = [
  "all",
  "pdf",
  "office",
  "images",
  "email",
  "audio",
  "structured",
  "text",
  "refused",
] as const;

export type FamilyFilter = (typeof FAMILY_FILTERS)[number];

/** Formats couverts par les capacités transverses `format_id: "visual"`. */
export const VISUAL_FORMATS: ReadonlySet<string> = new Set(["pdf", "docx", "pptx", "image"]);

function normalize(s: string | null | undefined): string {
  return (s ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

/**
 * Famille de filtre d'un format, déduite du libellé `family` fourni par le backend
 * (et du statut pour les refusés). Un format refusé appartient à « Refusés ».
 */
export function familyOf(format: Pick<DocumentFormat, "family" | "status">): Exclude<FamilyFilter, "all"> {
  const fam = normalize(format.family);
  if (format.status === "UNSUPPORTED" || fam.startsWith("refus")) return "refused";
  if (fam.includes("pdf")) return "pdf";
  if (fam.includes("office")) return "office";
  if (fam.includes("image")) return "images";
  if (fam.includes("courriel") || fam.includes("email") || fam.includes("mail")) return "email";
  if (fam.includes("audio")) return "audio";
  if (fam.includes("structur") || fam.includes("cao") || fam.includes("bim")) return "structured";
  return "text";
}

/** Capacités d'un format : les siennes + « visual » (pdf/docx/pptx/image) + « common ». */
export function capabilitiesForFormat(caps: DocumentCapability[], formatId: string): DocumentCapability[] {
  const own = caps.filter((c) => c.format_id === formatId);
  const visual = VISUAL_FORMATS.has(formatId) ? caps.filter((c) => c.format_id === "visual") : [];
  const common = caps.filter((c) => c.format_id === "common");
  return [...own, ...visual, ...common];
}

/** Vrai si au moins une capacité PROPRE ou visuelle ACTIVE du format produit du contenu recherchable. */
export function producesSearchableContent(caps: DocumentCapability[], formatId: string): boolean {
  return capabilitiesForFormat(caps, formatId)
    .filter((c) => c.format_id !== "common")
    .some((c) => c.output_searchable && isActiveState(c.state));
}

export type FilterState = {
  family: FamilyFilter;
  hideUnsupported: boolean;
  searchableOnly: boolean;
};

export const DEFAULT_FILTERS: FilterState = { family: "all", hideUnsupported: false, searchableOnly: false };

export function formatMatchesFilters(
  format: Pick<DocumentFormat, "format_id" | "family" | "status">,
  caps: DocumentCapability[],
  filters: FilterState
): boolean {
  if (filters.family !== "all" && familyOf(format) !== filters.family) return false;
  if (filters.hideUnsupported && format.status === "UNSUPPORTED") return false;
  if (filters.searchableOnly && !producesSearchableContent(caps, format.format_id)) return false;
  return true;
}

export function filterFormats(
  formats: DocumentFormat[],
  caps: DocumentCapability[],
  filters: FilterState
): DocumentFormat[] {
  return formats.filter((f) => formatMatchesFilters(f, caps, filters));
}

export function filterMatrix(
  matrix: MatrixRow[],
  formats: DocumentFormat[],
  caps: DocumentCapability[],
  filters: FilterState
): MatrixRow[] {
  const byId = new Map(formats.map((f) => [f.format_id, f]));
  return matrix.filter((row) => {
    const fmt = byId.get(row.format_id) ?? {
      format_id: row.format_id,
      family: row.label,
      status: row.format_status,
    };
    return formatMatchesFilters(fmt, caps, filters);
  });
}

/** Regroupe les formats par libellé de famille (ordre d'apparition backend conservé). */
export function groupFormatsByFamily(formats: DocumentFormat[]): { family: string; formats: DocumentFormat[] }[] {
  const groups: { family: string; formats: DocumentFormat[] }[] = [];
  const index = new Map<string, number>();
  for (const f of formats) {
    const key = f.family || "—";
    if (!index.has(key)) {
      index.set(key, groups.length);
      groups.push({ family: key, formats: [] });
    }
    groups[index.get(key)!].formats.push(f);
  }
  return groups;
}

// --- Matrice ------------------------------------------------------------------

export function cellCounts(items: { state: string }[]): { active: number; total: number } {
  return { active: items.filter((i) => isActiveState(i.state)).length, total: items.length };
}

// --- Portée -------------------------------------------------------------------

export type ScopeView =
  | { kind: "global" }
  | { kind: "spaces"; count: number }
  | { kind: "inoperative" }
  | { kind: "none" }
  | { kind: "unknown" };

/** Portée lisible d'une capacité. INCONSISTENT + 0 espace admis → « inopérant ». */
export function scopeView(cap: Pick<DocumentCapability, "state" | "activation_scope" | "scope">): ScopeView {
  if (cap.state === "INCONSISTENT" && cap.scope && cap.scope.admitted_spaces === 0) return { kind: "inoperative" };
  if (cap.scope?.global_mode) return { kind: "global" };
  switch (cap.activation_scope) {
    case "GLOBAL":
      return { kind: "global" };
    case "SCOPED":
      return { kind: "spaces", count: cap.scope?.admitted_spaces ?? 0 };
    case "NONE":
      return { kind: "none" };
    default:
      return { kind: "unknown" };
  }
}

// --- Images intégrées ---------------------------------------------------------

export const INLINE_IMAGE_FORMATS = ["pdf", "docx", "pptx", "xlsx", "msg", "eml", "image"] as const;

export type InlineImageRow = {
  formatId: string;
  detection: DocumentCapability | null;
  ocr: DocumentCapability | null;
  vision: DocumentCapability | null;
  structured: DocumentCapability | null;
  /** `yes` : une capacité active indexe ; `inactive` : indexerait mais inactive ; `no` : rien. */
  indexed: "yes" | "inactive" | "no";
  searchable: "yes" | "inactive" | "no";
};

function firstCap(byId: Map<string, DocumentCapability>, ids: string[]): DocumentCapability | null {
  for (const id of ids) {
    const c = byId.get(id);
    if (c) return c;
  }
  return null;
}

function tri(caps: (DocumentCapability | null)[], test: (c: DocumentCapability) => boolean): "yes" | "inactive" | "no" {
  const matching = caps.filter((c): c is DocumentCapability => !!c && test(c));
  if (matching.some((c) => isActiveState(c.state))) return "yes";
  return matching.length ? "inactive" : "no";
}

/**
 * Tableau « Images intégrées », CALCULÉ depuis les identifiants de capacités
 * (jamais d'état codé en dur). Une ligne absente du catalogue est omise.
 */
export function inlineImageRows(caps: DocumentCapability[], formatIds: readonly string[] = INLINE_IMAGE_FORMATS): InlineImageRow[] {
  const byId = new Map(caps.map((c) => [c.id, c]));
  const rows: InlineImageRow[] = [];
  for (const f of formatIds) {
    const detection = firstCap(byId, [`${f}.inline_image.detection`, `${f}.images`]);
    const ocr = firstCap(byId, [`${f}.inline_image.ocr`, ...(f === "image" ? ["image.ocr"] : [])]);
    const vision = firstCap(byId, [`${f}.inline_image.vision`, ...(f === "image" ? ["image.vision"] : [])]);
    const structured = VISUAL_FORMATS.has(f) ? firstCap(byId, ["visual.structx.table"]) : null;
    if (!detection && !ocr && !vision && !structured) continue;
    const contentCaps = [ocr, vision, structured];
    rows.push({
      formatId: f,
      detection,
      ocr,
      vision,
      structured,
      indexed: tri(contentCaps, (c) => c.output_indexed === "INDEXED"),
      searchable: tri(contentCaps, (c) => c.output_searchable),
    });
  }
  return rows;
}

// --- Formatage ----------------------------------------------------------------

/**
 * Taille lisible. Les limites exprimées en multiples exacts de 10^6 (ex. audio :
 * 600 000 000 octets) sont rendues en DÉCIMAL (Mo/Go) ; les autres en binaire
 * (Mio/Gio), pour refléter la valeur configurée sans arrondi trompeur.
 */
export function formatBytes(bytes: number | null | undefined, locale = "fr"): string | null {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  const nf = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const decimal = bytes >= 1_000_000 && bytes % 1_000_000 === 0;
  const fr = locale.startsWith("fr");
  const units = decimal
    ? fr
      ? ["octets", "ko", "Mo", "Go", "To"]
      : ["bytes", "kB", "MB", "GB", "TB"]
    : fr
      ? ["octets", "Kio", "Mio", "Gio", "Tio"]
      : ["bytes", "KiB", "MiB", "GiB", "TiB"];
  const base = decimal ? 1000 : 1024;
  let value = bytes;
  let unit = 0;
  while (value >= base && unit < units.length - 1) {
    value /= base;
    unit += 1;
  }
  return `${nf.format(value)} ${units[unit]}`;
}

/** Nombre d'octets exact, groupé selon la locale (ex. « 600 000 000 »). */
export function formatExactBytes(bytes: number, locale = "fr"): string {
  return new Intl.NumberFormat(locale).format(bytes);
}

export function shortSha(sha: string | null | undefined): string | null {
  const s = (sha ?? "").trim();
  return s ? s.slice(0, 8) : null;
}

/** Epoch (secondes) → date/heure locale ; `null` si absent ou invalide. */
export function formatEpoch(seconds: number | null | undefined, locale = "fr"): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "medium" }).format(new Date(seconds * 1000));
}

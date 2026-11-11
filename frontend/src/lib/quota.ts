// Conversions d'unités pour les quotas de stockage documentaire.
//
// La valeur CANONIQUE échangée avec le backend est TOUJOURS en OCTETS (entier).
// L'UI propose des unités binaires (Mo/Go/To = 1024^n) ; on convertit au dernier
// moment avant l'envoi et jamais l'inverse comme source de vérité. On n'expose
// jamais une valeur flottante comme quantité canonique (arrondi à l'entier).

// Snapshot quota + usage (identique à la sortie du service backend get_usage).
// Défini ici (module sans dépendance) pour être partagé par api.ts ET
// platformAdmin.ts sans import circulaire.
export type QuotaUsage = {
  max_document_count: number;
  used_document_count: number;
  remaining_document_count: number;
  max_document_bytes: number;
  used_document_bytes: number;
  remaining_document_bytes: number;
  count_usage_percent: number;
  size_usage_percent: number;
  is_count_exceeded: boolean;
  is_size_exceeded: boolean;
  is_exceeded: boolean;
  can_upload: boolean;
  enforced: boolean;
};

export type WorkspaceQuotaDefaults = {
  default_max_document_count: number;
  default_max_document_bytes: number;
  hard_max_document_count: number;
  hard_max_document_bytes: number;
};

export type WorkspaceQuotaInput = {
  max_document_count: number;
  max_document_bytes: number;
};

export type SizeUnit = "MB" | "GB" | "TB";

export const SIZE_UNITS: readonly SizeUnit[] = ["MB", "GB", "TB"] as const;

const FACTOR: Record<SizeUnit, number> = {
  MB: 1024 ** 2,
  GB: 1024 ** 3,
  TB: 1024 ** 4,
};

/** Valeur (dans `unit`) → octets entiers. */
export function unitToBytes(value: number, unit: SizeUnit): number {
  return Math.round(value * FACTOR[unit]);
}

/** Octets → valeur exprimée dans `unit` (peut être fractionnaire). */
export function bytesToUnit(bytes: number, unit: SizeUnit): number {
  return bytes / FACTOR[unit];
}

/** Unité d'affichage la plus naturelle pour un nombre d'octets donné. */
export function pickUnit(bytes: number): SizeUnit {
  if (bytes >= FACTOR.TB && bytes % FACTOR.TB === 0) return "TB";
  if (bytes >= FACTOR.GB) return "GB";
  return "MB";
}

/** Octets → chaîne d'édition dans `unit` (entier si exact, sinon 2 décimales). */
export function bytesToInputValue(bytes: number, unit: SizeUnit): string {
  const v = bytesToUnit(bytes, unit);
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

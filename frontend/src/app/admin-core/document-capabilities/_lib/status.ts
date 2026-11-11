// Correspondances PURES statut → ton / icône / clé de libellé.
//
// Règle d'accessibilité : un statut n'est JAMAIS porté par la couleur seule. Chaque
// entrée fournit une icône et une clé de libellé (namespace `documentCapabilities`) ;
// la couleur n'est qu'un renfort. Les dimensions restent DISTINCTES : état
// d'activation, disponibilité runtime, indexation, preuve chiffrée, qualification.
import type {
  CapabilityState,
  FormatStatus,
  GatewayServiceStatus,
  MatrixCellState,
  OutputIndexed,
  RuntimeAvailability,
  RuntimeConsistencyStatus,
} from "@/lib/coreConfig";

export type Tone = "success" | "info" | "scoped" | "warning" | "danger" | "neutral" | "muted";

export type StatusMeta = { tone: Tone; icon: string; key: string };

/** Couleurs contrastées (texte ≥ 4.5:1 sur le fond associé, AA). */
export const TONE_COLORS: Record<Tone, { color: string; bg: string; border: string }> = {
  success: { color: "#166534", bg: "#E7F4EC", border: "#B7DEC6" },
  info: { color: "#1E4FA3", bg: "#E6EEFB", border: "#BCD0F2" },
  scoped: { color: "#5B3FB0", bg: "#EEE9FB", border: "#D2C6F3" },
  warning: { color: "#8A4B06", bg: "#FBF0DB", border: "#EED4A4" },
  danger: { color: "#A12F20", bg: "#FBE7E3", border: "#F0C4BC" },
  neutral: { color: "#4F4740", bg: "#F1ECE6", border: "#DDD5CB" },
  muted: { color: "#5E5650", bg: "#F6F3EF", border: "#ECE6DF" },
};

export const ACTIVE_STATES: ReadonlySet<string> = new Set(["ACTIVE_GLOBAL", "ACTIVE_SCOPED", "ACTIVE_CONDITIONAL"]);

export function isActiveState(state: string | null | undefined): boolean {
  return !!state && ACTIVE_STATES.has(state);
}

/** Ordre d'affichage de la légende (états d'activation). */
export const CAPABILITY_STATES: CapabilityState[] = [
  "ACTIVE_GLOBAL",
  "ACTIVE_SCOPED",
  "ACTIVE_CONDITIONAL",
  "DISABLED",
  "UNSUPPORTED",
  "UNAVAILABLE",
  "INCONSISTENT",
  "UNKNOWN",
];

const CAPABILITY_STATE_META: Record<MatrixCellState, StatusMeta> = {
  ACTIVE_GLOBAL: { tone: "success", icon: "checkCircle", key: "state_ACTIVE_GLOBAL" },
  ACTIVE_SCOPED: { tone: "scoped", icon: "filter", key: "state_ACTIVE_SCOPED" },
  ACTIVE_CONDITIONAL: { tone: "info", icon: "spark", key: "state_ACTIVE_CONDITIONAL" },
  DISABLED: { tone: "neutral", icon: "stop", key: "state_DISABLED" },
  UNSUPPORTED: { tone: "muted", icon: "close", key: "state_UNSUPPORTED" },
  UNAVAILABLE: { tone: "danger", icon: "alert", key: "state_UNAVAILABLE" },
  INCONSISTENT: { tone: "warning", icon: "alert", key: "state_INCONSISTENT" },
  UNKNOWN: { tone: "neutral", icon: "clock", key: "state_UNKNOWN" },
  NOT_APPLICABLE: { tone: "muted", icon: "dots", key: "state_NOT_APPLICABLE" },
};

/** État d'activation d'une capacité (ou d'une cellule de matrice). Inconnu → UNKNOWN. */
export function capabilityStateMeta(state: string | null | undefined): StatusMeta {
  return CAPABILITY_STATE_META[(state ?? "") as MatrixCellState] ?? CAPABILITY_STATE_META.UNKNOWN;
}

const FORMAT_STATUS_META: Record<FormatStatus, StatusMeta> = {
  SUPPORTED: { tone: "success", icon: "checkCircle", key: "fmt_SUPPORTED" },
  CONDITIONALLY_SUPPORTED: { tone: "info", icon: "spark", key: "fmt_CONDITIONALLY_SUPPORTED" },
  DISABLED: { tone: "neutral", icon: "stop", key: "fmt_DISABLED" },
  UNSUPPORTED: { tone: "danger", icon: "close", key: "fmt_UNSUPPORTED" },
  TEXT_FALLBACK: { tone: "warning", icon: "doc", key: "fmt_TEXT_FALLBACK" },
  UNKNOWN: { tone: "neutral", icon: "clock", key: "fmt_UNKNOWN" },
  UNAVAILABLE: { tone: "danger", icon: "alert", key: "fmt_UNAVAILABLE" },
  INCONSISTENT: { tone: "warning", icon: "alert", key: "fmt_INCONSISTENT" },
};

export function formatStatusMeta(status: string | null | undefined): StatusMeta {
  return FORMAT_STATUS_META[(status ?? "") as FormatStatus] ?? FORMAT_STATUS_META.UNKNOWN;
}

const SERVICE_STATUS_META: Record<GatewayServiceStatus, StatusMeta> = {
  AVAILABLE: { tone: "success", icon: "checkCircle", key: "svc_AVAILABLE" },
  // Publié par un inventaire, jamais testé par une inférence : pas de voyant vert.
  DECLARED: { tone: "scoped", icon: "dots", key: "svc_DECLARED" },
  DEGRADED: { tone: "warning", icon: "alert", key: "svc_DEGRADED" },
  UNAVAILABLE: { tone: "danger", icon: "alert", key: "svc_UNAVAILABLE" },
  UNKNOWN: { tone: "neutral", icon: "clock", key: "svc_UNKNOWN" },
};

/** Disponibilité runtime / statut de service Gateway. `null`/inconnu → « Inconnu ». */
export function serviceStatusMeta(status: string | null | undefined): StatusMeta {
  return SERVICE_STATUS_META[(status ?? "") as GatewayServiceStatus] ?? SERVICE_STATUS_META.UNKNOWN;
}

export function runtimeMeta(status: RuntimeAvailability | string | null | undefined): StatusMeta {
  return serviceStatusMeta(status);
}

const INDEXED_META: Record<OutputIndexed, StatusMeta> = {
  INDEXED: { tone: "success", icon: "search", key: "idx_INDEXED" },
  INTERNAL_ONLY: { tone: "neutral", icon: "lock", key: "idx_INTERNAL_ONLY" },
  NONE: { tone: "muted", icon: "dots", key: "idx_NONE" },
};

export function indexedMeta(value: string | null | undefined): StatusMeta {
  return INDEXED_META[(value ?? "") as OutputIndexed] ?? INDEXED_META.NONE;
}

/** Badge de qualification. Les statuts inconnus retombent sur « Non mesuré ». */
const QUALIFICATION_META: Record<string, StatusMeta> = {
  REAL_DOCUMENT_QUALIFIED: { tone: "success", icon: "shield", key: "qual_REAL_DOCUMENT_QUALIFIED" },
  SYNTHETIC_QUALIFIED: { tone: "info", icon: "shield", key: "qual_SYNTHETIC_QUALIFIED" },
  PARTIAL: { tone: "warning", icon: "spark", key: "qual_PARTIAL" },
  NOT_QUALIFIED: { tone: "danger", icon: "alert", key: "qual_NOT_QUALIFIED" },
  EXPERIMENTAL: { tone: "scoped", icon: "sparkles", key: "qual_EXPERIMENTAL" },
  NOT_MEASURED: { tone: "neutral", icon: "clock", key: "qual_NOT_MEASURED" },
  NOT_APPLICABLE: { tone: "muted", icon: "dots", key: "qual_NOT_APPLICABLE" },
};

export function qualificationMeta(status: string | null | undefined): StatusMeta {
  return QUALIFICATION_META[status ?? ""] ?? QUALIFICATION_META.NOT_MEASURED;
}

/** Preuve chiffrée : `false` → « Non probant » (badge d'alerte), `true` → fiable, `null` → sans objet. */
export function numericEvidenceMeta(reliable: boolean | null | undefined): StatusMeta {
  if (reliable === false) return { tone: "warning", icon: "alert", key: "num_UNRELIABLE" };
  if (reliable === true) return { tone: "success", icon: "check", key: "num_RELIABLE" };
  return { tone: "muted", icon: "dots", key: "num_NA" };
}

const CONSISTENCY_META: Record<RuntimeConsistencyStatus, StatusMeta> = {
  OK: { tone: "success", icon: "checkCircle", key: "cons_OK" },
  PARTIAL: { tone: "warning", icon: "alert", key: "cons_PARTIAL" },
  MISMATCH: { tone: "danger", icon: "alert", key: "cons_MISMATCH" },
  UNKNOWN: { tone: "neutral", icon: "clock", key: "cons_UNKNOWN" },
};

export function consistencyMeta(status: string | null | undefined): StatusMeta {
  return CONSISTENCY_META[(status ?? "") as RuntimeConsistencyStatus] ?? CONSISTENCY_META.UNKNOWN;
}

/** Codes d'avertissement traduits ; tout code inconnu retombe sur UNKNOWN. */
export const WARNING_CODES = [
  "SCOPE_EMPTY",
  "SCOPE_STALE_ENTRY",
  "CONFIGURATION_MISMATCH",
  "WORKER_STALE",
  "DEPLOYMENT_SHA_MISMATCH",
  "SERVICE_UNAVAILABLE",
  "SERVICE_DEGRADED",
  "UNKNOWN",
] as const;

export type WarningCode = (typeof WARNING_CODES)[number];

export function warningKey(code: string | null | undefined): `warn_${WarningCode}` {
  const known = (WARNING_CODES as readonly string[]).includes(code ?? "") ? (code as WarningCode) : "UNKNOWN";
  return `warn_${known}`;
}

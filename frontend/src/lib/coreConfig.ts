// frontend/src/lib/coreConfig.ts
// Client admin RagATM_Core. Historiquement LECTURE SEULE ; l'écriture d'override
// (PR-F) est réservée au superuser ET soumise au gate serveur (prod interdit +
// CORE_CONFIG_EDIT_ENABLED). Seul le sous-ensemble « défauts de quota workspace »
// est exposé côté frontend.
import { apiGet, apiFetch } from "./api";

export type CoreConfigEntry = {
  key: string;
  effective_value: boolean | number | string;
  source: string;
  editable: boolean;
  sensitive: boolean;
  redacted: boolean;
  description?: string;
};

export type CoreConfigEffective = {
  environment: string;
  read_only: boolean;
  config: CoreConfigEntry[];
  // Garde-fou PR-D : l'overlay DB est diagnostic uniquement (runtime = settings).
  diagnostic_overlay_only?: boolean;
  db_overlay_runtime_active?: boolean;
  runtime_source_of_truth?: string;
  warning?: string;
};

/** Statut d'un service AI Gateway (sondes GET /health/*). */
/** DECLARED = publié par un inventaire non inférentiel (/v1/models, /health/runtime) : inférence NON vérifiée. */
export type GatewayServiceStatus = "AVAILABLE" | "DECLARED" | "DEGRADED" | "UNAVAILABLE" | "UNKNOWN";

/** Santé RÉELLE des services Gateway : jamais « ok » sans preuve. */
export type GatewayHealthSummary = {
  status: GatewayServiceStatus | null;
  services: Record<string, GatewayServiceStatus | null>;
  probed_at?: number | null;
  cached?: boolean | null;
};

export type CoreHealth = {
  /** `ok` vaut `null` et `verified` `false` tant que le backend ne vérifie rien réellement. */
  core_health: { ok: boolean | null; verified?: boolean; embedding_dim: number | null };
  gateway_configured: boolean;
  gateway_enabled: boolean;
  gateway_health: null | GatewayHealthSummary;
  fallback_enabled: boolean;
};

export type CoreAuditEvent = {
  id: string;
  created_at: string;
  user: string | null;
  user_id: number | null;
  workspace_id: number | null;
  rag_space_id: string | null;
  chat: { old_provider: string; old_model: string; new_provider: string; new_model: string };
  embeddings: { old_provider: string; old_model: string; new_provider: string; new_model: string };
};

export type CoreAuditPage = {
  count: number;
  limit: number;
  offset: number;
  results: CoreAuditEvent[];
};

export function getEffectiveConfig(): Promise<CoreConfigEffective> {
  return apiGet<CoreConfigEffective>("/api/v1/admin/core-config/effective/");
}

export function getCoreHealth(): Promise<CoreHealth> {
  return apiGet<CoreHealth>("/api/v1/admin/core-config/health/");
}

export function getCoreAudit(opts: { limit?: number; offset?: number } = {}): Promise<CoreAuditPage> {
  const qs = new URLSearchParams();
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiGet<CoreAuditPage>(`/api/v1/admin/core-config/audit/${suffix}`);
}

// --- Capacités documentaires (LECTURE SEULE) --------------
// GET /api/v1/admin/core-config/document-capabilities/[?ragspace_id=<uuid>][&refresh=1]
// Superuser seulement (403 sinon). Le backend fait autorité : aucune valeur n'est
// déduite ni codée en dur côté client (environnement, états, qualification…).

export type CapabilityState =
  | "ACTIVE_GLOBAL"
  | "ACTIVE_SCOPED"
  | "ACTIVE_CONDITIONAL"
  | "DISABLED"
  | "UNSUPPORTED"
  | "UNAVAILABLE"
  | "INCONSISTENT"
  | "UNKNOWN";

/** État d'une cellule de matrice : un état de capacité, ou « non applicable ». */
export type MatrixCellState = CapabilityState | "NOT_APPLICABLE";

export type FormatStatus =
  | "SUPPORTED"
  | "CONDITIONALLY_SUPPORTED"
  | "DISABLED"
  | "UNSUPPORTED"
  | "TEXT_FALLBACK"
  | "UNKNOWN"
  | "UNAVAILABLE"
  | "INCONSISTENT";

export type ActivationScope = "GLOBAL" | "SCOPED" | "NONE" | "UNKNOWN";
export type RuntimeAvailability = "AVAILABLE" | "DEGRADED" | "UNAVAILABLE" | "UNKNOWN";
export type OutputIndexed = "INDEXED" | "INTERNAL_ONLY" | "NONE";
export type QualificationStatus =
  | "REAL_DOCUMENT_QUALIFIED"
  | "SYNTHETIC_QUALIFIED"
  | "PARTIAL"
  | "NOT_QUALIFIED"
  | "EXPERIMENTAL"
  | "NOT_MEASURED"
  | "NOT_APPLICABLE";

export type CapabilityCategory =
  | "text"
  | "ocr"
  | "images"
  | "vision"
  | "tables"
  | "charts"
  | "audio"
  | "upload"
  | "email"
  | "structured"
  | "common";

export type CapabilityScope = {
  admitted_spaces: number;
  listed: number;
  existing: number;
  stale_entries: number;
  global_mode: boolean;
  list_setting: string;
};

export type GatewayDependency =
  | {
      uses_ai_gateway: true;
      gateway_service: string;
      gateway_route: string | null;
      gateway_call_type: string;
      stage: string | null;
      gateway_model_setting?: string | null;
      model_logical: string | null;
      model_concrete: string | null;
      service_status: GatewayServiceStatus | null;
    }
  | {
      uses_ai_gateway: false;
      gateway_call_type: string;
    };

export type DocumentCapability = {
  id: string;
  /** Format propriétaire ; « visual » = pdf/docx/pptx/image, « common » = tous. */
  format_id: string;
  category: CapabilityCategory | string;
  label: string;
  description: string;
  content_type: string;
  implemented: "YES" | "NO" | "PARTIAL" | string;
  configured_enabled: boolean;
  configuration_error?: string;
  state: CapabilityState;
  activation_scope: ActivationScope;
  scope: CapabilityScope | null;
  content_trigger: string;
  content_trigger_conditional: boolean;
  runtime_available: RuntimeAvailability;
  runtime_reasons: string[];
  executing_service: string;
  consumers: string[];
  gateway_dependency: GatewayDependency;
  output_type: string;
  output_indexed: OutputIndexed;
  output_searchable: boolean;
  citation_support: string;
  numeric_evidence_class: string;
  numeric_evidence_reliable: boolean | null;
  qualification_status: QualificationStatus | string;
  qualification_evidence: string;
  limitations: string[];
  flags: string[];
  space_applicable: boolean | null;
};

export type DocumentFormat = {
  format_id: string;
  label: string;
  family: string;
  extensions: string[];
  status: FormatStatus;
  upload_enabled: boolean;
  extractor: string | null;
  registry_key?: string | null;
  executing_service: string | null;
  refusal_code: string;
  max_upload_bytes: number | null;
  scope: CapabilityScope | null;
  notes: string[];
  in_matrix: boolean;
};

export type MatrixColumn = "text" | "ocr" | "images" | "vision" | "tables" | "charts" | "audio";

export type MatrixCell = {
  state: MatrixCellState;
  items: { id: string; label: string; state: CapabilityState }[];
};

export type MatrixRow = {
  format_id: string;
  label: string;
  format_status: FormatStatus;
  cells: Record<MatrixColumn, MatrixCell> & {
    gateway: { state: "USED" | "NOT_USED"; services: string[] };
  };
};

export type GatewayServiceInfo = {
  service: string;
  status: GatewayServiceStatus;
  reason?: string;
  environment?: string | null;
  build?: string | null;
  model_logical?: string | null;
  model_concrete?: string | null;
  model_revision?: string | null;
  tasks?: string[];
  capabilities?: string[];
  describe_available?: boolean;
  semantic_enabled?: boolean;
  contract?: string | null;
  experimental?: boolean;
  qualified_for_production?: boolean;
  task_qualification?: Record<string, string>;
  qualification_protocol?: string | null;
  models?: {
    model_logical: string | null;
    model_concrete: string | null;
    dimension?: number | null;
    identity_verified?: boolean;
    ready?: boolean;
  }[];
  default_model?: string | null;
};

export type ScopeSummaryEntry = {
  scope: string;
  list_setting: string;
  global_mode: boolean;
  global_mode_error?: string;
  listed: number;
  invalid_entries: number;
  existing: number;
  stale_entries: number;
  admitted_spaces: number;
  total_spaces: number;
};

export type EmbeddingsProviderRow = {
  provider: string;
  model: string;
  spaces: number;
  call_type: string;
  service_status: GatewayServiceStatus | null;
};

export type EmbeddingsSummary = {
  total_spaces: number;
  ai_gateway_spaces: number;
  ollama_direct_spaces: number;
  other_provider_spaces: number;
  unknown_provider_spaces: number;
  providers: EmbeddingsProviderRow[];
  gateway_fallback_enabled: boolean;
  default_for_new_spaces: { provider: string; model: string };
  selected_space?: { provider: string; model: string; via_ai_gateway: boolean } | null;
};

export type RuntimeConsistencyStatus = "OK" | "PARTIAL" | "MISMATCH" | "UNKNOWN";

export type RuntimeConsistency = {
  status: RuntimeConsistencyStatus;
  reason?: string;
  roles: Record<
    string,
    { state: "FRESH" | "STALE"; instances?: number; deployment_sha?: string | null; age_seconds?: number | null }
  >;
  flags: Record<
    string,
    { status: "OK" | "UNKNOWN" | "CONFIGURATION_MISMATCH"; consumers?: string[]; unobserved_consumers?: string[] }
  >;
  mismatches: { flag: string; roles: string[] }[];
  ignored_non_consumer_differences: number;
  deployment_sha_mismatches: string[];
};

export type CapabilityWarning = {
  code: string;
  capability_id?: string;
  flag?: string;
  scope?: string;
  service?: string;
  detail?: string;
};

export type DocumentCapabilitiesSnapshot = {
  schema_version: number;
  /** Environnement INTERROGÉ, fourni par le backend — jamais codé en dur. */
  environment: string;
  deployment_sha: string;
  /** Epoch en secondes. */
  generated_at: number;
  data_freshness: {
    gateway_probed_at: number | null;
    gateway_cached: boolean | null;
    gateway_cache_ttl_seconds: number;
    worker_stale_after_seconds: number;
  };
  summary: {
    formats: number;
    extensions: number;
    capabilities: number;
    states: Partial<Record<CapabilityState, number>>;
    warnings: number;
  };
  formats: DocumentFormat[];
  capabilities: DocumentCapability[];
  matrix: MatrixRow[];
  gateway_services: GatewayServiceInfo[];
  scope_summary: Record<string, ScopeSummaryEntry>;
  embeddings: EmbeddingsSummary;
  runtime_consistency: RuntimeConsistency;
  warnings: CapabilityWarning[];
  ragspace_view: { id: string; name: string } | null;
  read_only: true;
};

export function getDocumentCapabilities(
  opts: { ragspaceId?: string | null; refresh?: boolean } = {}
): Promise<DocumentCapabilitiesSnapshot> {
  const qs = new URLSearchParams();
  const space = (opts.ragspaceId ?? "").trim();
  if (space) qs.set("ragspace_id", space);
  if (opts.refresh) qs.set("refresh", "1");
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiGet<DocumentCapabilitiesSnapshot>(`/api/v1/admin/core-config/document-capabilities/${suffix}`);
}

// --- Écriture d'override GLOBAL (défauts de quota workspace) -----------------
// PUT /api/v1/admin/core-config/override/. Le gate serveur (prod interdit +
// CORE_CONFIG_EDIT_ENABLED) peut renvoyer 403/409 : l'appelant gère l'erreur.
export type CoreConfigWriteResult = { ok: boolean; action: string; event_id: string };

export function setGlobalConfigOverride(
  key: string,
  value: number | string | boolean,
  reason = ""
): Promise<CoreConfigWriteResult> {
  return apiFetch<CoreConfigWriteResult>("/api/v1/admin/core-config/override/", {
    method: "PUT",
    body: JSON.stringify({ scope_type: "global", scope_ref: "", key, value, reason }),
  });
}

// frontend/src/lib/reindex.ts
//
// Client de la réindexation DURABLE d'un RagSpace (jobs asynchrones).
// Le POST répond 202 avec un job_id ; le suivi se fait par polling du job, jamais
// en gardant la requête POST ouverte. Voir docs/03-backend/reindexation-jobs.md.
import { apiFetch, apiFetchRaw } from "@/lib/api";

export type ReindexJobStatus =
  | "pending"
  | "queuing"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelled"
  | "interrupted";

export const TERMINAL_JOB_STATUSES: ReindexJobStatus[] = [
  "completed",
  "completed_with_errors",
  "failed",
  "cancelled",
  "interrupted",
];

export type ReindexFailedItem = {
  document_id: string;
  filename: string;
  status: string;
  error_code: string | null;
  attempt_count: number;
};

/**
 * Portée d'un job. Le traitement d'un document est identique dans les deux cas ;
 * seule diffère la façon dont la liste de départ a été constituée.
 */
export type ReindexJobKind = "full_reindex" | "failed_documents_retry";

/** Trace descriptive de la sélection (compteurs, mode). Jamais une liste d'ids. */
export type ReindexJobSelection = {
  mode?: "explicit" | "filter" | "retry_failed";
  search?: string;
  requested_count?: number;
  eligible_count?: number;
  excluded_count?: number;
  parent_job_id?: string;
};

export type ReindexJob = {
  job_id: string;
  rag_space_id: string;
  status: ReindexJobStatus;
  kind?: ReindexJobKind;
  selection?: ReindexJobSelection;
  requested_strategy?: RetryStrategy;
  /** Avancement PAR stratégie effective — un job `auto` en mélange plusieurs. */
  strategy_breakdown?: Partial<
    Record<RetryStrategy, { total: number; succeeded: number; failed: number }>
  >;
  skipped?: number;
  /** Raisons structurées des documents volontairement non traités. */
  skip_reasons?: Record<string, number>;
  total: number;
  pending: number;
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  interrupted: number;
  progress_percent: number;
  parent_job_id: string | null;
  error_code: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  failed_items?: ReindexFailedItem[];
};

export type StartReindexResult = {
  status: "accepted" | "already_running";
  job_id: string;
  rag_space_id?: string;
  kind?: ReindexJobKind;
  requested_strategy?: RetryStrategy;
  strategy_breakdown?: Partial<Record<RetryStrategy, number>>;
  skip_reasons?: Record<string, number>;
  total_documents?: number;
  /** Documents demandés mais écartés (déjà prêts, supprimés, sans fichier source…). */
  skipped_documents?: number;
  already_running: boolean;
  job?: ReindexJob;
};

/**
 * Sélection d'une reprise ciblée. Deux formes, jamais mélangées :
 *
 * - `explicit` : la liste des documents cochés ;
 * - `filter` : « tous les résultats du filtre », éventuellement moins quelques
 *   exclusions. Le serveur reconstruit le périmètre — le navigateur n'a jamais à
 *   transporter des centaines d'identifiants pour dire « tout ».
 */
export type RetrySelection =
  | { mode: "explicit"; document_ids: string[] }
  | { mode: "filter"; search?: string; excluded_document_ids?: string[] };

/**
 * Méthode de reprise. `auto` est la seule qui tienne compte de la CAUSE de l'échec ;
 * les autres sont des choix explicites. Le backend est la source de vérité : cette
 * union n'est qu'un miroir de son allowlist, jamais une seconde table de décision.
 */
export type RetryStrategy = "auto" | "standard" | "force_ocr" | "extended_timeout";

export const RETRY_STRATEGIES: readonly RetryStrategy[] = [
  "auto",
  "standard",
  "force_ocr",
  "extended_timeout",
];

/** Ce que le backend recommande pour UN document en échec (jamais déduit ici). */
export type Retryability =
  | "retryable"
  | "retryable_with_strategy"
  | "non_retryable"
  | "replace_source_recommended"
  | "";

/** Prévision d'une campagne : ce qui se passerait, sans que rien ne se passe. */
export type RetryPreview = {
  requested_strategy: RetryStrategy;
  total_documents: number;
  actionable_documents: number;
  skipped_documents: number;
  strategy_breakdown: Partial<Record<RetryStrategy, number>>;
  skip_reasons: Record<string, number>;
  extended_timeout_seconds: number | null;
};

export function isTerminal(status: ReindexJobStatus): boolean {
  return TERMINAL_JOB_STATUSES.includes(status);
}

/** POST le lancement. Renvoie le job (202) OU le job actif existant (409). Lève sur 5xx. */
export async function startReindex(ragSpaceId: string): Promise<StartReindexResult> {
  const res = await apiFetchRaw(`/api/v1/rag-spaces/${ragSpaceId}/reindex/`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  // 202 = lancé, 409 = un job actif existe déjà (on l'adopte pour le suivi).
  if (res.status === 202 || res.status === 409) {
    return body as StartReindexResult;
  }
  const detail = body?.detail || `${res.status}`;
  throw new Error(detail);
}

/**
 * Lance une reprise ciblée des documents en ERREUR.
 *
 * Même contrat que `startReindex` (202 = lancé, 409 = un job est déjà actif sur
 * l'espace, quel que soit son type) : le suivi se fait par polling du job, jamais
 * en gardant le POST ouvert. N'effectue AUCUN upload et ne crée aucun document.
 */
export async function startFailedDocumentsRetry(
  ragSpaceId: string,
  selection: RetrySelection,
  strategy: RetryStrategy = "auto"
): Promise<StartReindexResult> {
  const res = await apiFetchRaw(`/api/v1/rag-spaces/${ragSpaceId}/retry-failed-documents/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selection, strategy }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 202 || res.status === 409) {
    // 409 recouvre deux cas distincts : un job déjà actif (à SUIVRE) et un refus
    // métier (« plus rien à reprendre »), qui doit remonter comme une erreur.
    if (res.status === 409 && !body?.job_id) throw new Error(body?.detail || "conflict");
    return body as StartReindexResult;
  }
  throw new Error(body?.detail || `${res.status}`);
}

export function getReindexJob(jobId: string): Promise<ReindexJob> {
  return apiFetch<ReindexJob>(`/api/v1/reindex-jobs/${jobId}/`);
}

/** `init.signal` permet d'annuler la requête en vol (boucle de statut de SpacesTab). */
export function getActiveReindexJob(
  ragSpaceId: string,
  init: RequestInit = {}
): Promise<{ active: boolean; job: ReindexJob | null }> {
  return apiFetch(`/api/v1/rag-spaces/${ragSpaceId}/active-reindex-job/`, init);
}

/**
 * Prévisualise une campagne de reprise : combien de documents relèvent de l'OCR, du
 * délai étendu, ou d'aucune reprise. N'écrit rien, ne lit aucun fichier.
 */
export function previewFailedDocumentsRetry(
  ragSpaceId: string,
  selection: RetrySelection,
  strategy: RetryStrategy = "auto"
): Promise<RetryPreview> {
  return apiFetch<RetryPreview>(
    `/api/v1/rag-spaces/${ragSpaceId}/retry-failed-documents/preview/`,
    { method: "POST", body: JSON.stringify({ selection, strategy }) }
  );
}

/**
 * Relance les documents en échec : nouveau job lié.
 *
 * `strategy` omis => la méthode du job parent est CONSERVÉE (rejouer à l'identique).
 * `"auto"` la fait recalculer sur les causes d'échec les plus récentes.
 */
export async function retryFailed(
  jobId: string,
  strategy?: RetryStrategy
): Promise<StartReindexResult> {
  const res = await apiFetchRaw(`/api/v1/reindex-jobs/${jobId}/retry-failed/`, {
    method: "POST",
    ...(strategy
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ strategy }) }
      : {}),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 202 || res.status === 409) {
    return body as StartReindexResult;
  }
  throw new Error(body?.detail || `${res.status}`);
}

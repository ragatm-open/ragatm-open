// frontend/src/lib/api.ts
import { getCsrfToken } from "./auth";
import { ApiError, parseRetryAfterMs } from "./apiError";
import type { Coverage } from "@/components/shell/WorkspaceContext";
import type { PermissionLevel, SpaceRole } from "./ragspacePermissions";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

/** Ajoute le header CSRF (double-submit) sur les méthodes mutantes. */
function applyCsrfHeader(headers: Headers, method: string) {
  if (SAFE_METHODS.has(method.toUpperCase())) return;
  const csrf = getCsrfToken();
  if (csrf && !headers.has("X-CSRFToken")) headers.set("X-CSRFToken", csrf);
}

function ensureTrailingSlashOnPath(path: string): string {
  if (!path.startsWith("/api/")) return path;

  const [pathname, query] = path.split("?", 2);

  let normalized = pathname;
  if (normalized.length > 1 && !normalized.endsWith("/")) normalized += "/";

  return query ? `${normalized}?${query}` : normalized;
}

function isJsonContentType(contentType: string): boolean {
  return contentType.toLowerCase().includes("application/json");
}

function isFormDataBody(body: any): body is FormData {
  return typeof FormData !== "undefined" && body instanceof FormData;
}

/**
 * API fetch unifiée via le proxy Next (/api/..)
 * - Auth par cookie HttpOnly : credentials "include" (aucun token en JS)
 * - Ajoute le header CSRF (X-CSRFToken) sur les méthodes mutantes
 * - Ajoute trailing slash sur les routes internes /api/*
 * - Met Content-Type JSON seulement si on envoie un body JSON (pas FormData)
 * - Gère 204 No Content (retourne null)
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();

  applyCsrfHeader(headers, method);

  const hasBody = init.body !== undefined && init.body !== null;

  // Met JSON par défaut seulement si body existe ET n'est pas FormData ET pas déjà défini
  if (hasBody && !headers.has("Content-Type") && !isFormDataBody(init.body)) {
    headers.set("Content-Type", "application/json");
  }

  const p = ensureTrailingSlashOnPath(path);
  const res = await fetch(p, { ...init, headers, credentials: "include" });

  if (res.status === 204 || res.status === 205) {
    if (!res.ok) throw new ApiError(res.status, res.statusText, null, `API ${res.status} ${res.statusText}`);
    return null as T;
  }

  const contentType = res.headers.get("content-type") || "";
  const body = isJsonContentType(contentType)
    ? await res.json().catch(() => null)
    : await res.text().catch(() => "");

  if (!res.ok) {
    const msg =
      typeof body === "string"
        ? body
        : body?.detail || body?.non_field_errors?.[0] || JSON.stringify(body);
    throw new ApiError(
      res.status,
      res.statusText,
      body,
      `API ${res.status} ${res.statusText}: ${msg}`,
      parseRetryAfterMs(res.headers.get("Retry-After")),
    );
  }

  return body as T;
}

// Helpers optionnels
export function apiGet<T>(path: string, init: RequestInit = {}): Promise<T> {
  return apiFetch<T>(path, { ...init, method: "GET" });
}

export function apiPost<T>(path: string, data?: unknown, init: RequestInit = {}): Promise<T> {
  const hasBody = data !== undefined;
  return apiFetch<T>(path, {
    ...init,
    method: "POST",
    body: hasBody ? JSON.stringify(data) : undefined,
  });
}

export function apiPatch<T>(path: string, data?: unknown, init: RequestInit = {}): Promise<T> {
  const hasBody = data !== undefined;
  return apiFetch<T>(path, {
    ...init,
    method: "PATCH",
    body: hasBody ? JSON.stringify(data) : undefined,
  });
}

export function apiDelete<T>(path: string, init: RequestInit = {}): Promise<T> {
  return apiFetch<T>(path, { ...init, method: "DELETE" });
}

export function apiPostForm<T>(path: string, form: FormData, init: RequestInit = {}): Promise<T> {
  return apiFetch<T>(path, {
    ...init,
    method: "POST",
    body: form,
  });
}

export type UploadProgress = { loaded: number; total: number; percent: number };

/**
 * Upload multipart avec progression RÉELLE de l'envoi réseau (XMLHttpRequest).
 *
 * `fetch` n'expose pas la progression d'upload : on passe donc par XHR pour
 * câbler `upload.onprogress`. On reproduit le comportement de `apiFetch` :
 * cookie HttpOnly (`withCredentials`), header CSRF sur méthode mutante, trailing
 * slash sur /api/*, parsing JSON et message d'erreur normalisé. On ne fixe PAS
 * Content-Type : le navigateur pose lui-même le boundary multipart.
 */
export function apiUploadForm<T>(
  path: string,
  form: FormData,
  opts: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal } = {}
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const p = ensureTrailingSlashOnPath(path);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", p, true);
    xhr.withCredentials = true;

    const csrf = getCsrfToken();
    if (csrf) xhr.setRequestHeader("X-CSRFToken", csrf);

    if (opts.onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          opts.onProgress!({
            loaded: e.loaded,
            total: e.total,
            percent: e.total > 0 ? Math.round((e.loaded / e.total) * 100) : 0,
          });
        }
      };
    }

    xhr.onload = () => {
      const ct = (xhr.getResponseHeader("content-type") || "").toLowerCase();
      const raw = xhr.responseText;
      let body: any = raw;
      if (ct.includes("application/json")) {
        try {
          body = JSON.parse(raw);
        } catch {
          body = null;
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const msg =
        typeof body === "string"
          ? body
          : body?.detail || body?.non_field_errors?.[0] || JSON.stringify(body);
      // `Retry-After` est relevé AVANT de construire l'erreur : une fois l'ApiError
      // créée, l'objet XHR n'est plus à portée de l'appelant, et l'information que
      // le serveur a explicitement donnée serait perdue.
      reject(
        new ApiError(
          xhr.status,
          xhr.statusText,
          body,
          `API ${xhr.status} ${xhr.statusText}: ${msg}`,
          parseRetryAfterMs(xhr.getResponseHeader("Retry-After")),
        ),
      );
    };

    xhr.onerror = () => reject(new Error("network_error: échec réseau pendant l'envoi du fichier."));
    xhr.ontimeout = () => reject(new Error("upload_timeout: délai d'envoi dépassé."));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));

    if (opts.signal) {
      if (opts.signal.aborted) {
        xhr.abort();
        return;
      }
      opts.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }

    xhr.send(form);
  });
}

// ---------- Capacités globales (non sensibles) ----------

export type EmbeddingsInfo = {
  configurable?: boolean;
  provider: string; // "ai_gateway" | "stub"
  model: string;
  scope?: string;
  warning?: string;
};

export type ProviderCapability = {
  available: boolean;
  configured: boolean;
  key_present?: boolean;
  default_model: string;
  default_embedding_model?: string;
};

export type ProvidersCapabilities = {
  ollama: ProviderCapability;
  ai_gateway?: ProviderCapability;
};

/**
 * GET /api/v1/capabilities/
 * Expose les capacités des providers (chat/embeddings, sans secret) et le réglage
 * d'embeddings GLOBAL (repli d'affichage). Sans secret, lisible par tout
 * utilisateur authentifié.
 */
import type { MsgCapability } from "@/app/documents/_lib/types";
import type { AudioCapability } from "@/app/documents/_lib/audio";

export type IngestCapabilities = {
  /** Formats réellement exploitables sur CET environnement (flags compris). */
  formats?: string[];
  /**
   * Sous-formats de la famille `image` (`jpeg`, `png`, `tiff`, `webp`…). `formats`
   * annonce la FAMILLE, celle-ci dit LESQUELS : sans elle, le frontend devait deviner
   * les extensions image et en proposait que le backend refusait ensuite.
   */
  image_formats?: string[];
  /** Détail du format sous flag `.msg` (cf. documents/_lib/types MsgCapability). */
  msg?: MsgCapability;
  /** Dépôt audio : extensions, plafond décimal, chemin dédié. */
  audio?: AudioCapability;
};

export function getCapabilities(): Promise<{
  providers?: ProvidersCapabilities;
  embeddings: EmbeddingsInfo;
  ingest?: IngestCapabilities;
}> {
  return apiGet<{
    providers?: ProvidersCapabilities;
    embeddings: EmbeddingsInfo;
    ingest?: IngestCapabilities;
  }>("/api/v1/capabilities/");
}

// ---------- Document summary ----------

export type SummaryMode = "short" | "detailed" | "bullets" | "executive";
export type SummaryScope = "document" | "page";
export type SummaryStatus = "ready" | "pending" | "failed" | "none";

export type DocumentSummaryResponse = {
  ok: boolean;
  document_id: string;
  status: SummaryStatus;
  filename?: string;
  summary_id?: string | null;
  summary?: string;
  error?: string;
  mode?: SummaryMode;
  scope?: SummaryScope;
  page?: number | null;
  language?: string;
  truncated?: boolean;
  provider?: string | null;
  model?: string | null;
  tokens?: number;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  cost_usd?: number | null;
  cached?: boolean;
  generated_at?: string | null;
  log_id?: string | null;
  /**
   * Renseignés uniquement pour le parcours chat (thread_id + request_id fournis).
   * Permettent de réconcilier le message optimiste avec la ligne persistée.
   */
  message_id?: string | null;
  request_id?: string | null;
  /** Provenance du document résumé. Absent = réponse de l'API privée (historique). */
  source_type?: "workspace_document" | "domain_corpus";
  corpus_id?: string | null;
  corpus_name?: string | null;
};

type SummaryOpts = { mode?: SummaryMode; scope?: SummaryScope; page?: number; language?: string };

/**
 * Rattachement au fil de discussion. Les deux champs vont ENSEMBLE : le backend
 * refuse un `threadId` sans `requestId`, car un retry réseau créerait sinon un
 * second message. Absents = résumé hors chat (modale Documents), non persisté
 * dans un fil.
 */
export type SummaryThreadOpts = { threadId?: string; requestId?: string };

/**
 * Résume un document à partir de ses chunks indexés.
 * Lecture seule côté backend (les viewers peuvent résumer). Le provider LLM
 * utilisé est celui configuré sur le RagSpace (modèle local via l'AI Gateway).
 * `scope: "page"` (avec `page`) restreint le résumé à une page précise.
 */
export function summarizeDocument(
  documentId: string,
  opts: SummaryOpts & { refresh?: boolean } & SummaryThreadOpts = {}
): Promise<DocumentSummaryResponse> {
  return apiPost<DocumentSummaryResponse>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/summary/`,
    {
      mode: opts.mode ?? "bullets",
      scope: opts.scope ?? "document",
      page: opts.page,
      // `auto` : le backend résout d'après les préférences du compte. Un défaut
      // `fr` ici forçait le français pour TOUS les appelants, y compris ceux qui
      // n'exprimaient aucune langue — c'était la seconde cause de l'incident
      // « résumés en français malgré une interface anglaise ».
      language: opts.language ?? "auto",
      refresh: opts.refresh ?? false,
      // Omis (et non `undefined` explicite) hors parcours chat : le backend
      // distingue « pas de fil » de « fil invalide ».
      ...(opts.threadId ? { thread_id: opts.threadId, request_id: opts.requestId } : {}),
    }
  );
}

/** Lecture seule de l'état d'un résumé (polling des documents longs). */
export function getDocumentSummary(documentId: string, opts: SummaryOpts = {}): Promise<DocumentSummaryResponse> {
  const qs = new URLSearchParams({
    mode: opts.mode ?? "bullets",
    scope: opts.scope ?? "document",
    // Doit rester STRICTEMENT aligné sur le défaut du POST : POST et polling
    // ciblent la même variante de cache (`mode:scope:page:langue`).
    language: opts.language ?? "auto",
  });
  if (opts.scope === "page" && opts.page != null) qs.set("page", String(opts.page));
  return apiGet<DocumentSummaryResponse>(
    `/api/v1/documents/${encodeURIComponent(documentId)}/summary/?${qs.toString()}`
  );
}

/**
 * Lance un résumé et, si le document est long (génération asynchrone côté
 * serveur → ``status: "pending"``), poll l'état jusqu'à `ready`/`failed`.
 */
export async function summarizeDocumentAwait(
  documentId: string,
  opts: SummaryOpts & { refresh?: boolean } & SummaryThreadOpts = {},
  hooks: { onPending?: (r: DocumentSummaryResponse) => void; intervalMs?: number; timeoutMs?: number } = {}
): Promise<DocumentSummaryResponse> {
  const first = await summarizeDocument(documentId, opts);
  if (first.status !== "pending") return first;
  hooks.onPending?.(first);

  // Le POST renvoie la langue CONCRÈTE (fr|en) qu'il a résolue et avec laquelle il a
  // construit la clé de variante. On poll avec cette valeur plutôt qu'avec la
  // préférence d'origine : `auto` resterait ambigu, et le polling interrogerait une
  // autre variante si la résolution changeait entre les deux appels.
  const pollOpts: SummaryOpts = first.language ? { ...opts, language: first.language } : opts;

  const intervalMs = hooks.intervalMs ?? 2500;
  const timeoutMs = hooks.timeoutMs ?? 180000;
  const start = Date.now();
  for (;;) {
    await new Promise((r) => setTimeout(r, intervalMs));
    const r = await getDocumentSummary(documentId, pollOpts);
    if (r.status === "ready" || r.status === "failed") return r;
    if (Date.now() - start > timeoutMs) throw new Error("summary_timeout");
  }
}

/* ---------- Résumé par SOURCE (dispatch discriminé) ----------
 *
 * Le choix de l'endpoint dépend de la PROVENANCE de la source, jamais de la forme
 * de l'identifiant : les documents privés et ceux du corpus métier ont deux
 * espaces d'UUID disjoints. C'est ce dispatch qui remplace l'ancien envoi
 * systématique vers `/api/v1/documents/<id>/summary/`.
 *
 * Un booléen `isCorpus` a été écarté volontairement : il n'aurait pas de troisième
 * état pour `unknown` (sources antérieures à la v2 du contrat), et le prochain
 * type de source aurait imposé de le remplacer partout. Le discriminant est donc
 * le `sourceType` lui-même.
 */
export type SummarySourceRef = {
  sourceType: "workspace_document" | "domain_corpus" | "unknown";
  documentId: string;
  /** Espace CONSOMMATEUR. Requis pour le corpus métier : il porte la politique LLM. */
  ragSpaceId?: string | null;
  corpusId?: string | null;
};

/** URL de résumé d'une source, selon sa provenance. */
export function summaryEndpointFor(ref: SummarySourceRef): string {
  const id = encodeURIComponent(ref.documentId);
  // `unknown` -> endpoint PRIVÉ : une source sans provenance date d'avant la v2 du
  // contrat, époque où seul le privé existait. C'est le comportement historique,
  // conservé délibérément (cf. docs/03-backend/resume-corpus-metier.md).
  return ref.sourceType === "domain_corpus"
    ? `/api/v1/domain-corpus-documents/${id}/summary/`
    : `/api/v1/documents/${id}/summary/`;
}

function summaryBody(
  ref: SummarySourceRef,
  opts: SummaryOpts & { refresh?: boolean } & SummaryThreadOpts
): Record<string, unknown> {
  return {
    mode: opts.mode ?? "bullets",
    scope: opts.scope ?? "document",
    page: opts.page,
    language: opts.language ?? "auto",
    refresh: opts.refresh ?? false,
    ...(opts.threadId ? { thread_id: opts.threadId, request_id: opts.requestId } : {}),
    // Envoyé pour le corpus uniquement : le backend le confronte à la garde
    // d'accès (il ne le croit pas sur parole) et en dérive le workspace.
    ...(ref.sourceType === "domain_corpus" && ref.ragSpaceId
      ? { rag_space_id: ref.ragSpaceId }
      : {}),
  };
}

/** Résume une source, en choisissant l'endpoint d'après sa provenance. */
export function summarizeSourceDocument(
  ref: SummarySourceRef,
  opts: SummaryOpts & { refresh?: boolean } & SummaryThreadOpts = {}
): Promise<DocumentSummaryResponse> {
  return apiPost<DocumentSummaryResponse>(summaryEndpointFor(ref), summaryBody(ref, opts));
}

/** Lecture seule de l'état d'un résumé de source (polling), même dispatch. */
export function getSourceDocumentSummary(
  ref: SummarySourceRef,
  opts: SummaryOpts = {}
): Promise<DocumentSummaryResponse> {
  const qs = new URLSearchParams({
    mode: opts.mode ?? "bullets",
    scope: opts.scope ?? "document",
    language: opts.language ?? "auto",
  });
  if (opts.scope === "page" && opts.page != null) qs.set("page", String(opts.page));
  if (ref.sourceType === "domain_corpus" && ref.ragSpaceId) {
    qs.set("rag_space_id", ref.ragSpaceId);
  }
  return apiGet<DocumentSummaryResponse>(`${summaryEndpointFor(ref)}?${qs.toString()}`);
}

/**
 * Lance le résumé d'une source et poll jusqu'à `ready`/`failed` si le backend a
 * répondu `pending` (document long → génération asynchrone).
 *
 * Même boucle que `summarizeDocumentAwait`, mais sur l'endpoint résolu par la
 * provenance : POST et polling visent ainsi toujours la même variante de cache.
 */
export async function summarizeSourceDocumentAwait(
  ref: SummarySourceRef,
  opts: SummaryOpts & { refresh?: boolean } & SummaryThreadOpts = {},
  hooks: { onPending?: (r: DocumentSummaryResponse) => void; intervalMs?: number; timeoutMs?: number } = {}
): Promise<DocumentSummaryResponse> {
  const first = await summarizeSourceDocument(ref, opts);
  if (first.status !== "pending") return first;
  hooks.onPending?.(first);

  const pollOpts: SummaryOpts = first.language ? { ...opts, language: first.language } : opts;
  const intervalMs = hooks.intervalMs ?? 2500;
  const timeoutMs = hooks.timeoutMs ?? 180000;
  const start = Date.now();
  for (;;) {
    await new Promise((r) => setTimeout(r, intervalMs));
    const r = await getSourceDocumentSummary(ref, pollOpts);
    if (r.status === "ready" || r.status === "failed") return r;
    if (Date.now() - start > timeoutMs) throw new Error("summary_timeout");
  }
}

/**
 * Traduit un code d'erreur backend de résumé en CLÉ du namespace i18n `errors`.
 *
 * Renvoyait auparavant du français en dur : un utilisateur en interface anglaise
 * voyait donc un message français sous un résumé anglais. La fonction ne rend plus
 * de texte — l'appelant traduit avec `useTranslations("errors")`, et la langue suit
 * l'interface comme pour tout autre message applicatif.
 */
export function summaryErrorKey(msg: string): string {
  const m = (msg || "").toLowerCase();
  if (m.includes("document_not_ready")) return "SUMMARY_NOT_READY";
  if (m.includes("document_no_content")) return "SUMMARY_NO_CONTENT";
  if (m.includes("summary_provider_unavailable")) return "SUMMARY_PROVIDER_UNAVAILABLE";
  if (m.includes("summary_timeout")) return "SUMMARY_TIMEOUT";
  // Avant `not_found` : le corps du 409 contient `request_id_conflict`, et la
  // recherche de sous-chaîne prendrait sinon le premier motif rencontré.
  if (m.includes("request_id_conflict")) return "SUMMARY_REQUEST_CONFLICT";
  if (m.includes("not_found")) return "SUMMARY_NOT_FOUND";
  return "SUMMARY_FAILED";
}

// ---------- Documents (liste légère, pour résolution de nom) ----------

export type DocumentLite = { id: string; filename: string; status: string };

/** Liste des documents d'un espace (sous-ensemble de champs). */
export function listDocuments(
  ragSpaceId: string,
  opts: { limit?: number } = {}
): Promise<{ items: DocumentLite[]; total: number }> {
  const qs = new URLSearchParams({ rag_space_id: ragSpaceId, limit: String(opts.limit ?? 200) });
  return apiGet<{ items: DocumentLite[]; total: number }>(`/api/v1/documents/?${qs.toString()}`);
}

export type DeleteDocumentResponse = {
  ok: boolean;
  deleted?: boolean;
  document_id?: string;
  chunks_deleted?: number;
  file_deleted?: boolean;
  warnings?: string[];
};

/** Suppression DÉFINITIVE d'un document (document + chunks + résumés + fichier). */
export function deleteDocument(documentId: string): Promise<DeleteDocumentResponse> {
  return apiDelete<DeleteDocumentResponse>(`/api/v1/documents/${documentId}/delete/`);
}

export type RephrasingSuggestionsResponse = {
  status: "none" | "pending" | "ready" | "failed";
  suggestions: string[];
};

/** Récupère les suggestions de reformulation (générées de façon asynchrone, F2). */
export function getRephrasingSuggestions(
  threadId: string,
  requestId?: string
): Promise<RephrasingSuggestionsResponse> {
  const qs = requestId ? `?request_id=${encodeURIComponent(requestId)}` : "";
  return apiGet<RephrasingSuggestionsResponse>(
    `/api/v1/chat/threads/${encodeURIComponent(threadId)}/rephrasing-suggestions/${qs}`
  );
}

// ---------- Authentification (MFA obligatoire) ----------
//
// Le mot de passe seul n'ouvre JAMAIS de session : il produit un challenge (cookie
// HttpOnly posé par le serveur, invisible du JS) que seuls les endpoints
// /api/v1/auth/mfa/* acceptent. Le cookie d'auth n'est posé qu'après le code TOTP.
// Aucune donnée MFA n'est conservée côté navigateur (ni localStorage, ni
// sessionStorage) : en cas de rafraîchissement, GET /api/v1/auth/mfa/status/ redonne
// l'étape. La page de connexion appelle ces endpoints via `apiFetch`.

export type MfaStep = "mfa" | "mfa_setup";

export type LoginStepResponse = {
  ok: boolean;
  mfa_required: true;
  next: MfaStep;
  expires_in: number;
};

export type MfaStatusResponse = { pending: boolean; next: MfaStep | null; expires_in: number };

export type MfaSetupResponse = { method: "totp"; provisioning_uri: string; secret: string };

export type AuthIdentity = { ok: boolean; id: string; email: string; username: string };

// ---------- Bootstrap ----------

export type BootstrapResponse = {
  created: boolean;
  workspace: { id: number; name: string; role: string };
  ragspace: { id: string; name: string };
  /** Langue d'interface persistée (fr | en). Sert à synchroniser le cookie de locale. */
  preferred_locale: string;
};

export function bootstrap(): Promise<BootstrapResponse> {
  return apiPost<BootstrapResponse>("/api/v1/bootstrap/", {});
}

/** Préférence de LANGUE DE RÉPONSE IA (distincte de la locale d'interface). */
export type AnswerLanguage = "auto" | "fr" | "en";

export type UserPreferences = { preferred_locale: string; preferred_answer_language: AnswerLanguage };

export function getPreferences(): Promise<UserPreferences> {
  return apiGet<UserPreferences>("/api/v1/me/preferences/");
}

export function updateAnswerLanguage(pref: AnswerLanguage): Promise<UserPreferences> {
  return apiPatch<UserPreferences>("/api/v1/me/preferences/", { preferred_answer_language: pref });
}

// ---------- Workspace / RagSpaces loaders (for unified selectors) ----------

export type WorkspaceListItem = { id: number; name: string; role: string };

export type RagSpaceListItem = {
  id: string;
  name: string;
  workspace_id: number;
  llm_provider?: string;
  llm_model?: string;
  // Rôle effectif (lecture seule, décidé par le backend). Voir ragspacePermissions.ts.
  permission_level?: PermissionLevel | null;
  space_role?: SpaceRole | null;
  is_owner_implicit?: boolean;
};

/** GET /api/v1/workspaces/ */
export function listWorkspaces(): Promise<WorkspaceListItem[]> {
  return apiGet<WorkspaceListItem[]>("/api/v1/workspaces/");
}

/**
 * GET /api/v1/workspaces/<id>/quota/ — quota + usage (owner/admin, lecture seule).
 * La MODIFICATION est exclusivement superuser (console plateforme).
 */
export function getWorkspaceQuota(workspaceId: number): Promise<import("./quota").QuotaUsage> {
  return apiGet<import("./quota").QuotaUsage>(`/api/v1/workspaces/${workspaceId}/quota/`);
}

/**
 * GET /api/v1/rag-spaces/
 * - if workspaceId provided, backend supports ?workspace_id=<id>
 * - otherwise returns global list (after your backend fix)
 */
export function listRagSpaces(opts: { workspaceId?: number | null } = {}): Promise<RagSpaceListItem[]> {
  const qs = opts.workspaceId ? `?workspace_id=${encodeURIComponent(String(opts.workspaceId))}` : "";
  return apiGet<RagSpaceListItem[]>(`/api/v1/rag-spaces/${qs}`);
}

// --- Binary helpers (PDF, etc.)

export async function apiFetchRaw(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();
  applyCsrfHeader(headers, method);

  const p = ensureTrailingSlashOnPath(path);
  const res = await fetch(p, { ...init, headers, credentials: "include" });
  return res;
}

export async function apiFetchBlobUrl(
  path: string,
  init: RequestInit = {}
): Promise<{ url: string; revoke: () => void; contentType: string }> {
  const res = await apiFetchRaw(path, init);

  if (!res.ok) {
    const t = await res.text().catch(() => "");
    throw new ApiError(res.status, res.statusText, t, t || `Download failed: ${res.status} ${res.statusText}`);
  }

  const contentType = res.headers.get("content-type") || "";
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);

  return {
    url,
    contentType,
    revoke: () => URL.revokeObjectURL(url),
  };
}

/* ============================================================================
 * Analyses globales de corpus (L2) — client API typé.
 *
 * Aucun hook, aucune UI : ce lot fournit les FONDATIONS. L'option « Analyse
 * complète » du chat reste explicitement indisponible (cf. L3) — elle sera
 * activée en L4, quand un moteur d'analyse globale réel existera.
 * ==========================================================================*/

export type AnalysisStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelled";

/** Statuts après lesquels plus rien ne bouge : un poller doit s'y arrêter. */
export const TERMINAL_ANALYSIS_STATUSES: readonly AnalysisStatus[] = [
  "succeeded",
  "partial",
  "failed",
  "cancelled",
] as const;

export function isTerminalAnalysisStatus(status: AnalysisStatus | string): boolean {
  return (TERMINAL_ANALYSIS_STATUSES as readonly string[]).includes(status);
}

export type AnalysisProgress = {
  phase: string;
  current: number;
  total: number;
  percent: number;
};

export type AnalysisPreflight = {
  documents_total: number;
  documents_ready: number;
  documents_failed: number;
  documents_without_chunks: number;
  chunks_total: number;
  pages_numbered_total: number;
  characters_total: number;
  estimated_tokens_total: number;
  corpus_fingerprint: string;
  within_limits: boolean;
  limits_exceeded: { limit: string; value: number; max: number }[];
};

export type CorpusAnalysis = {
  analysis_id: string;
  rag_space_id: string;
  analysis_type: string;
  parameters: Record<string, unknown>;
  status: AnalysisStatus;
  progress: AnalysisProgress;
  preflight: AnalysisPreflight;
  coverage: Coverage | null;
  result: Record<string, unknown> | null;
  error_code: string | null;
  /** Message SÛR pour l'utilisateur. Jamais une trace d'exception. */
  error_message: string | null;
  corpus_fingerprint: string;
  /** `null` tant que le job n'est pas terminal (aucun recalcul en polling). */
  is_stale: boolean | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number | null;
  /** Présents à la création seulement. */
  reused?: boolean;
  joined_active?: boolean;
};

export function createCorpusAnalysis(
  ragSpaceId: string,
  body: { analysis_type: string; parameters?: Record<string, unknown>; request_id?: string },
): Promise<CorpusAnalysis> {
  return apiFetch<CorpusAnalysis>(
    `/api/v1/rag-spaces/${encodeURIComponent(ragSpaceId)}/analyses/`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function getCorpusAnalysis(analysisId: string): Promise<CorpusAnalysis> {
  return apiFetch<CorpusAnalysis>(`/api/v1/analyses/${encodeURIComponent(analysisId)}/`);
}

export function cancelCorpusAnalysis(analysisId: string): Promise<CorpusAnalysis> {
  return apiFetch<CorpusAnalysis>(
    `/api/v1/analyses/${encodeURIComponent(analysisId)}/cancel/`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

/* ---- L4 : recherche globale (corpus.global_search) ---- */

export type GlobalSearchSource = {
  chunk_id: string;
  document_id: string;
  page: number | null;
  /**
   * Nom du document, figé à l'analyse. Optionnel : les résultats produits avant
   * l'introduction du champ n'en ont pas, et un document supprimé donne `null`.
   */
  filename?: string | null;
};

export type GlobalSearchItem = {
  id: string;
  statement: string;
  sources: GlobalSearchSource[];
  confidence: number | null;
};

/** Métriques du préfiltre. `chunks_scanned` (tout le périmètre) est distinct de
 *  `candidate_chunks` (retenus) et de `processed_chunks` (traités). */
export type GlobalSearchMetrics = {
  chunks_scanned: number;
  documents_text_scanned: number;
  candidate_chunks: number;
  candidate_documents: number;
  processed_chunks: number;
  processed_documents: number;
  batches_total: number;
  batches_failed: number;
  fts_configuration?: string;
};

export type GlobalSearchResult = {
  result_version: string;
  query: string;
  summary_markdown: string;
  items: GlobalSearchItem[];
  search: GlobalSearchMetrics;
};

/** Analyses lancées depuis un fil — permet la reprise après rechargement. */
export function getThreadAnalyses(threadId: string): Promise<{ analyses: CorpusAnalysis[] }> {
  return apiFetch<{ analyses: CorpusAnalysis[] }>(
    `/api/v1/chat/threads/${encodeURIComponent(threadId)}/analyses/`,
  );
}

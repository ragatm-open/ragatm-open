// AUTO-GENERATED FROM shared/contracts/openapi.yaml
// Suggested location: frontend/src/types/openapi.ts
// (You can rename/move as needed.)

export interface Error {
  detail: string;
}

export interface Ok {
  ok: boolean;
}

export interface EmailLoginRequest {
  email: string;
  password: string;
}

export interface EmailLoginResponse {
  token: string;
}

export interface AuthMeResponse {
  id: number;
  email: string;
}

export interface PasswordResetRequest {
  email: string;
}

export interface PasswordResetConfirm {
  uid: string;
  token: string;
  new_password: string;
}

export interface BootstrapWorkspace {
  id: number;
  name: string;
  role: string;
}

export interface BootstrapRagSpace {
  id: string;
  name: string;
}

export interface BootstrapResponse {
  created: boolean;
  workspace: BootstrapWorkspace;
  ragspace: BootstrapRagSpace;
}

export interface WorkspaceListItem {
  id: number;
  name: string;
  role: string;
}

export interface WorkspaceMember {
  id: number;
  user_id: number;
  email: string;
  role: string;
  created_at: string;
}

export interface WorkspaceMemberAddRequest {
  email: string;
  role?: (string) | null;
}

export interface WorkspaceMemberPatchRequest {
  role: string;
}

export interface WorkspaceRagSpaceCreateRequest {
  name: string;
}

export interface RagSpaceListItem {
  id: string;
  name: string;
  llm_provider: string;
  llm_model: string;
}

export interface RagSpacePatchRequest {
  name?: string;
  llm_provider?: "ollama";
  llm_model?: string;
}

export interface PromptTemplate {
  id: string;
  workspace: number;
  name: string;
  version: number;
  content: string;
  is_active: boolean;
  created_at: string;
}

export interface PromptTemplateCreateRequest {
  workspace_id?: (number) | null;
  workspace?: (number) | null;
  name: string;
  content: string;
  activate?: boolean;
}

export interface DocumentListItem {
  id: string;
  rag_space_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  error_message: string;
  created_at: (string) | null;
}

export interface DocumentListResponse {
  items: DocumentListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface DocumentIngestRequest {
  rag_space_id: string;
  filename?: (string) | null;
  text: string;
}

export interface DocumentUploadMultipart {
  rag_space_id: string;
  file: string;
}

export interface DocumentIngestResult {
  document_id: string;
  status: string;
  chunks_created: number;
}

export interface RagSourceV1 {
  schema_version: number;
  rank: number;
  document_id: any;
  chunk_id: any;
  score: number;
  filename: any;
  page_number: any;
  snippet: string;
}

export interface ChatRequest {
  rag_space_id: string;
  question: string;
  thread_id?: (string) | null;
}

export interface ChatMeta {
  request_id?: (string) | null;
  rag_space_id?: (string) | null;
  thread_id?: (string) | null;
  policy?: (string) | null;
  latency_ms?: (number) | null;
  sources_count?: (number) | null;
}

export interface ChatResponse {
  status: "ok" | "no_answer" | "error";
  answer: string;
  sources: RagSourceV1[];
  meta: ChatMeta;
  // Reformulations proposées uniquement sur status "no_answer" (0 à 3).
  // Optionnel/rétrocompatible : absent pour les réponses valides et erreurs.
  suggested_rephrasings?: string[];
}

export interface ChatThreadCreateRequest {
  rag_space_id: string;
  title?: (string) | null;
}

export interface ChatThread {
  id: string;
  rag_space_id: string;
  title: (string) | null;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  thread_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  sources?: (Record<string, any>) | null;
  request_id?: (string) | null;
  rag_query_log_id?: (string) | null;
  created_at: string;
}

export interface AuditLogListItem {
  id: string;
  created_at: (string) | null;
  user_id: (number) | null;
  rag_space_id: (string) | null;
  status: (string) | null;
  latency_ms: (number) | null;
  policy: (string) | null;
  error_stage: (string) | null;
  request_id: (string) | null;
  question: (string) | null;
  sources_count: number;
}

export interface AuditLogDetail {
  id: string;
  created_at: (string) | null;
  status: (string) | null;
  error_stage: (string) | null;
  latency_ms: (number) | null;
  question: (string) | null;
  answer: (string) | null;
  workspace_id: number;
  rag_space_id: (string) | null;
  user_id: (number) | null;
  prompt_hash: (string) | null;
  sources_schema_version: (string) | null;
  sources: RagSourceV1[];
  llm_provider: (string) | null;
  llm_model: (string) | null;
  llm_prompt_tokens: (number) | null;
  llm_completion_tokens: (number) | null;
  llm_total_tokens: (number) | null;
}

// ---------- Convenience API shapes ----------
export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type DocumentsListResponse = DocumentListResponse;
export type DocumentsIngestResponse = DocumentIngestResult;
export type ChatNonStreamingResponse = ChatResponse;
export type AuditListResponse = AuditLogListItem[];
export type AuditDetailResponse = AuditLogDetail;

export type WorkspaceListItem = { id: number; name: string; role: string };

export type Member = {
  id: number;
  user_id: number;
  email: string;
  role: string;
  created_at: string;
};

export type InvitationStatus = "pending" | "expired" | "accepted" | "revoked";

export type Invitation = {
  id: string;
  email: string;
  role: string;
  status: InvitationStatus;
  generation: number;
  created_at: string;
  expires_at: string;
  can_regenerate: boolean;
  can_revoke: boolean;
};

// Réponse one-shot de création / régénération : le lien n'est renvoyé QU'ICI.
export type InvitationCreated = {
  id: string;
  email?: string;
  role?: string;
  status?: string;
  generation: number;
  expires_at: string;
  email_sent?: boolean;
  invitation_url: string;
};

export type RagSpace = {
  id: string;
  name: string;
  llm_provider?: "ollama";
  llm_model?: string;
  embedding_provider?: "ai_gateway";
  embedding_model?: string;
};

export type LlmProviderKey = "ollama" | "ai_gateway";

export type LlmProviderCapability = {
  available: boolean;
  configured: boolean;
  key_present?: boolean;
  default_model: string;
  default_embedding_model?: string;
};

export type LlmSettings = {
  llm_provider: LlmProviderKey;
  llm_model: string;
  embedding_provider: LlmProviderKey;
  embedding_model: string;
  providers: {
    ollama: LlmProviderCapability;
    ai_gateway?: LlmProviderCapability;
  };
  embeddings: {
    configurable: boolean;
    provider: string;
    model: string;
    warning: string;
  };
};

export type LlmTestResult = {
  ok: boolean;
  code?: string;
  message?: string;
  provider?: string;
};

export type RagQueryLogItem = {
  id: number;
  created_at: string;
  user_id: number | null;
  rag_space_id: string | null;
  status: string;
  latency_ms: number | null;
  policy: string | null;
  error_stage: string | null;
  request_id: string | null;
  question: string | null;
  sources_count: number;
};

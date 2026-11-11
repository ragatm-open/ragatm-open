// frontend/src/lib/platformAdmin.ts
//
// Client typé de la console d'ADMINISTRATION PLATEFORME (superuser only).
//
// Le backend est l'autorité : ces fonctions ne font qu'appeler /api/v1/platform/*.
// Le frontend ne recalcule AUCUNE règle (owner-scope, D1, invariants) et n'affiche
// jamais de contenu documentaire. La garde superuser est appliquée côté serveur.

import { apiGet, apiPost, apiPatch, apiDelete } from "./api";
import { isApiError } from "./apiError";
import type { QuotaUsage, WorkspaceQuotaDefaults, WorkspaceQuotaInput } from "./quota";

export type { QuotaUsage, WorkspaceQuotaDefaults, WorkspaceQuotaInput };

const BASE = "/api/v1/platform";

// ---------- Types ----------
export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

export type PlatformUserRow = {
  id: number;
  username: string;
  email: string;
  display_name: string;
  is_active: boolean;
  is_staff: boolean;
  is_superuser: boolean;
  date_joined: string | null;
  last_login: string | null;
  memberships_count: number;
  owned_workspaces_count: number;
};

export type PlatformUserMembership = {
  membership_id: number;
  workspace_id: number;
  workspace_name: string;
  workspace_is_active: boolean;
  role: string;
  owner_scope_membership_id: number | null;
  effective_owner_scope_pk: number | null;
  owner_membership: { membership_id: number; user_id: number; display_name: string } | null;
};

export type PlatformUserDetail = PlatformUserRow & {
  memberships: PlatformUserMembership[];
  owned_workspaces: { workspace_id: number; workspace_name: string }[];
  active_ragspace_accesses_count: number;
};

export type PlatformOwnerRef = {
  membership_id: number;
  user_id?: number;
  username?: string;
  email?: string;
  display_name?: string;
};

export type PlatformWorkspaceRow = {
  id: number;
  name: string;
  is_active: boolean;
  created_at: string | null;
  owners: PlatformOwnerRef[];
  members_count: number;
  ragspaces_count: number;
  active_ragspaces_count: number;
  documents_count: number;
  has_integrity_violation: boolean;
  migratable_automatically: boolean;
  anomaly_codes: string[];
  suggested_action: string;
};

export type PlatformAnomaly = { code: string; [k: string]: unknown };

export type PlatformWorkspaceDetail = {
  id: number;
  name: string;
  is_active: boolean;
  created_at: string | null;
  owners: { membership_id: number; user: PlatformUserRow }[];
  memberships: {
    membership_id: number;
    user: PlatformUserRow;
    role: string;
    owner_scope_membership_id: number | null;
    effective_owner_scope_pk: number | null;
    created_at: string | null;
  }[];
  owner_scopes: { membership_id: number; user: PlatformUserRow; scoped_memberships_count: number }[];
  ragspaces: {
    id: string;
    name: string;
    is_active: boolean;
    deleted_at: string | null;
    owner_scope_id: number | null;
    created_by_id: number | null;
    documents_count: number;
  }[];
  counts: Record<string, number>;
  quota: QuotaUsage;
  diagnostic: {
    anomalies: PlatformAnomaly[];
    has_integrity_violation: boolean;
    migratable_automatically: boolean;
    suggested_action: string;
  };
};

export type PlatformDashboard = {
  users_total: number;
  users_active: number;
  users_superuser: number;
  workspaces_total: number;
  workspaces_active: number;
  workspaces_disabled: number;
  workspaces_without_owner: number;
  ragspaces_without_owner_scope: number;
  integrity_violations: number;
};

export type PlatformDiagnostics = {
  workspaces: (PlatformWorkspaceRow & { anomalies: PlatformAnomaly[] })[];
  summary: {
    total: number;
    has_integrity: boolean;
    has_decision: boolean;
    integrity_violations: number;
    without_owner: number;
    multiple_owners: number;
    migratable_automatically: number;
    exit_code: number;
    strict_exit_code: number;
  };
};

export type TransferPreview = {
  workspace_id: number;
  source_owner: { membership_id: number; user: PlatformUserRow };
  target_owner: { membership_id: number; user: PlatformUserRow };
  affected_memberships: { membership_id: number; user: PlatformUserRow; role: string }[];
  affected_ragspaces: { id: string; name: string; owner_scope_id: number | null }[];
  ragspace_accesses_valid: { grant_id: string; rag_space_name: string; space_role: string }[];
  ragspace_accesses_invalid: { grant_id: string; rag_space_name: string; space_role: string }[];
  documents_in_progress: number;
  active_reindex_jobs: number;
  integrity_anomalies: PlatformAnomaly[];
  allowed: boolean;
  blockers: string[];
  preview_hash: string;
  apply_supported: boolean;
};

// ---------- Read ----------
export function getPlatformDashboard(): Promise<PlatformDashboard> {
  return apiGet<PlatformDashboard>(`${BASE}/dashboard/`);
}

export type UserFilters = {
  search?: string;
  is_active?: boolean;
  is_superuser?: boolean;
  workspace_id?: number;
  role?: string;
  limit?: number;
  offset?: number;
};

function qs(params: Record<string, unknown>): string {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    s.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  const str = s.toString();
  return str ? `?${str}` : "";
}

export function listPlatformUsers(f: UserFilters = {}): Promise<Page<PlatformUserRow>> {
  return apiGet<Page<PlatformUserRow>>(`${BASE}/users/${qs(f)}`);
}
export function getPlatformUser(id: number): Promise<PlatformUserDetail> {
  return apiGet<PlatformUserDetail>(`${BASE}/users/${id}/`);
}

export type WorkspaceFilters = {
  search?: string;
  is_active?: boolean;
  without_owner?: boolean;
  with_anomalies?: boolean;
  limit?: number;
  offset?: number;
};

export function listPlatformWorkspaces(f: WorkspaceFilters = {}): Promise<Page<PlatformWorkspaceRow>> {
  return apiGet<Page<PlatformWorkspaceRow>>(`${BASE}/workspaces/${qs(f)}`);
}
export function getPlatformWorkspace(id: number): Promise<PlatformWorkspaceDetail> {
  return apiGet<PlatformWorkspaceDetail>(`${BASE}/workspaces/${id}/`);
}
export function getPlatformDiagnostics(workspaceId?: number): Promise<PlatformDiagnostics> {
  return apiGet<PlatformDiagnostics>(`${BASE}/owner-scope-diagnostics/${qs({ workspace_id: workspaceId })}`);
}
export function getWorkspaceQuotaDefaults(): Promise<WorkspaceQuotaDefaults> {
  return apiGet<WorkspaceQuotaDefaults>(`${BASE}/workspace-quota-defaults/`);
}

// ---------- Mutations ----------
export function createWorkspace(name: string, ownerUserId: number, quota?: WorkspaceQuotaInput) {
  return apiPost<{
    code: string;
    workspace: { id: number; name: string; is_active: boolean };
    owner: { membership_id: number; user: PlatformUserRow };
    quota: WorkspaceQuotaInput;
  }>(
    `${BASE}/workspaces/`,
    // `quota` OPTIONNEL : absent => défauts de plateforme (compat clients existants).
    quota ? { name, owner_user_id: ownerUserId, quota } : { name, owner_user_id: ownerUserId }
  );
}
/** Modifie les quotas d'un workspace (superuser). PATCH partiel possible. */
export function setWorkspaceQuota(
  workspaceId: number,
  body: { max_document_count?: number; max_document_bytes?: number; reason?: string }
) {
  return apiPatch<{ code: string; quota: QuotaUsage; warning?: string }>(
    `${BASE}/workspaces/${workspaceId}/quota/`,
    body
  );
}
export function assignWorkspaceOwner(
  workspaceId: number,
  userId: number,
  opts: { allow_additional?: boolean; promote?: boolean } = {}
) {
  return apiPost<{ code: string; membership_id?: number }>(
    `${BASE}/workspaces/${workspaceId}/owner/`,
    { user_id: userId, ...opts }
  );
}
export function addMembership(
  workspaceId: number,
  body: { user_id: number; role: string; owner_membership_id?: number }
) {
  return apiPost<{ code: string; membership_id?: number }>(
    `${BASE}/workspaces/${workspaceId}/memberships/`,
    body
  );
}
export function changeMembershipOwnerScope(
  workspaceId: number,
  membershipId: number,
  body: { owner_membership_id: number; revoke_incompatible?: boolean }
) {
  return apiPatch<{
    code: string;
    owner_scope_membership_id?: number;
    revoked_grants?: string[];
    incompatible_grants?: { grant_id: string; rag_space_name: string; space_role: string }[];
  }>(`${BASE}/workspaces/${workspaceId}/memberships/${membershipId}/owner-scope/`, body);
}
export function disableUser(userId: number, reason: string, confirmSelf = false) {
  return apiPost<{ code: string }>(`${BASE}/users/${userId}/disable/`, { reason, confirm_self: confirmSelf });
}
export function enableUser(userId: number, reason: string) {
  return apiPost<{ code: string }>(`${BASE}/users/${userId}/enable/`, { reason });
}
export function changeMembershipRole(
  workspaceId: number,
  membershipId: number,
  body: { role: string; owner_membership_id?: number; revoke_incompatible?: boolean }
) {
  return apiPatch<{
    code: string;
    role?: string;
    revoked_grants?: string[];
    incompatible_grants?: { grant_id: string; rag_space_name: string; space_role: string }[];
  }>(`${BASE}/workspaces/${workspaceId}/memberships/${membershipId}/`, body);
}
export function removeMembership(workspaceId: number, membershipId: number, revokeGrants = false) {
  return apiDelete<{
    code: string;
    revoked_grants?: string[];
    active_grants?: { grant_id: string; rag_space_name: string; space_role: string }[];
  }>(
    `${BASE}/workspaces/${workspaceId}/memberships/${membershipId}/`,
    revokeGrants ? { body: JSON.stringify({ revoke_grants: true }) } : {}
  );
}
export function disableWorkspace(workspaceId: number, reason: string) {
  return apiPost<{ code: string }>(`${BASE}/workspaces/${workspaceId}/disable/`, { reason });
}
export function enableWorkspace(workspaceId: number, reason?: string) {
  return apiPost<{ code: string }>(`${BASE}/workspaces/${workspaceId}/enable/`, { reason: reason || "" });
}
export function previewOwnerScopeTransfer(body: {
  workspace_id: number;
  source_owner_membership_id: number;
  target_owner_membership_id: number;
}): Promise<TransferPreview> {
  return apiPost<TransferPreview>(`${BASE}/owner-scope-transfers/preview/`, body);
}

// ---------- Traduction NEUTRE des codes (§18) ----------
// Ne jamais afficher un code interne brut. `null` => l'appelant garde son message.
const CODE_MESSAGES: Record<string, string> = {
  cross_workspace: "Cette opération créerait une incohérence entre workspaces.",
  owner_membership_invalid: "L'owner sélectionné n'est pas valide pour ce workspace.",
  workspace_has_owner: "Ce workspace possède déjà un owner.",
  member_exists: "Cet utilisateur est déjà membre de ce workspace.",
  already_member: "Cet utilisateur est déjà membre de ce workspace.",
  owner_scope_required: "Un périmètre owner doit être choisi pour un membre non-owner.",
  grants_incompatible: "Certains accès doivent être corrigés avant de poursuivre.",
  self_reference: "Un membre ne peut pas être son propre périmètre.",
  cannot_scope_owner: "Un owner ne peut pas être rattaché à un périmètre.",
  reason_required: "Un motif est requis pour cette opération.",
  user_inactive: "Ce compte utilisateur est désactivé.",
  user_not_found: "Utilisateur introuvable.",
  workspace_not_found: "Workspace introuvable.",
  membership_not_found: "Ce membership n'existe plus.",
  name_required: "Un nom de workspace est requis.",
  source_equals_target: "La source et la cible doivent être différentes.",
  transfer_apply_not_implemented: "L'application d'un transfert n'est pas disponible.",
  invalid_role: "Ce rôle n'est pas valide.",
  // P1/P2
  last_active_superuser: "Le dernier superuser actif ne peut pas être désactivé.",
  self_disable_requires_confirm: "Confirmez explicitement la désactivation de votre propre compte.",
  already_user_disabled: "Ce compte est déjà désactivé.",
  already_user_enabled: "Ce compte est déjà actif.",
  owner_promotion_requires_workflow: "La promotion en owner passe par « Attribuer un owner ».",
  last_owner: "Le dernier owner du workspace ne peut pas être retiré ou rétrogradé.",
  owner_scope_not_empty: "Cet owner possède encore des membres ou espaces dans son périmètre.",
  grants_exceed_role: "Certains accès dépassent le nouveau rôle et doivent être corrigés.",
  active_grants: "Ce membership possède encore des accès actifs.",
  active_jobs: "Une tâche est en cours sur ce périmètre. Réessayez plus tard.",
  workspace_without_owner: "Ce workspace doit recevoir un owner avant sa réactivation.",
};

/** Extrait le code applicatif d'une ApiError (body.detail / body.code). */
export function platformErrorCode(e: unknown): string | null {
  if (!isApiError(e)) return null;
  const body = e.body as { detail?: string; code?: string } | null;
  return (body?.code || body?.detail || e.code) ?? null;
}

/** Message FR neutre pour un échec de mutation plateforme. */
export function humanPlatformError(e: unknown, fallback = "L'opération n'a pas pu aboutir."): string {
  const code = platformErrorCode(e);
  if (code && CODE_MESSAGES[code]) return CODE_MESSAGES[code];
  if (isApiError(e) && e.status === 409) return "L'état a changé depuis le chargement. Rechargez les données.";
  return fallback;
}

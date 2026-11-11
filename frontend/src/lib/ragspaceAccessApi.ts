// frontend/src/lib/ragspaceAccessApi.ts
//
// Client typé de l'administration des accès RagSpaceAccess (niveau « manage »).
//
// Le backend est l'autorité : ces fonctions ne font qu'appeler les endpoints
// platform. La révocation renseigne `revoked_at` côté serveur (jamais de
// suppression physique). Aucune règle d'autorisation n'est dupliquée ici.

import { apiGet, apiPost, apiPatch, apiDelete } from "./api";
import type { SpaceRole } from "./ragspacePermissions";

/** Utilisateur minimal exposé par l'API (jamais de champ sensible). */
export type AccessUser = { id: number; username: string; email: string };

/** Un octroi RagSpaceAccess actif ou révoqué. */
export type RagSpaceGrant = {
  id: string;
  user_id: number;
  user: AccessUser | null;
  space_role: SpaceRole;
  assigned_by: number | null;
  created_at: string | null;
  /** Non-null => grant révoqué (historique conservé). */
  revoked_at: string | null;
};

/** Membre du périmètre octroyable, avec les rôles autorisés (plafond D1). */
export type EligibleUser = {
  user: AccessUser;
  workspace_role: string;
  allowed_space_roles: SpaceRole[];
};

/** Codes applicatifs stables renvoyés par les mutations (usage interne). */
export type GrantCode =
  | "granted"
  | "already_granted"
  | "role_changed"
  | "conflict"
  | "owner_implicit"
  | "revoked"
  | "grant_not_found"
  | "ragspace_deleted"
  | "invalid";

/** Résultat d'une mutation (grant / change / revoke). */
export type GrantResult = {
  code: GrantCode;
  grant_id?: string | null;
  user_id?: number | null;
  space_role?: SpaceRole | null;
  previous_role?: SpaceRole | null;
  current_role?: SpaceRole | null;
  detail?: string | null;
};

const base = (ragSpaceId: string) =>
  `/api/v1/rag-spaces/${encodeURIComponent(ragSpaceId)}/access`;

/** Liste des octrois de l'espace. `includeRevoked` ajoute l'historique révoqué. */
export function listRagSpaceGrants(
  ragSpaceId: string,
  opts: { includeRevoked?: boolean } = {}
): Promise<RagSpaceGrant[]> {
  const qs = opts.includeRevoked ? "?include_revoked=1" : "";
  return apiGet<RagSpaceGrant[]>(`${base(ragSpaceId)}/${qs}`);
}

/** Utilisateurs du périmètre octroyables (sans grant actif, owner implicite exclu). */
export function listEligibleUsers(ragSpaceId: string): Promise<EligibleUser[]> {
  return apiGet<EligibleUser[]>(`${base(ragSpaceId)}/eligible/`);
}

/** Octroie un rôle d'espace à un utilisateur. */
export function grantRagSpaceAccess(
  ragSpaceId: string,
  userId: number,
  spaceRole: SpaceRole
): Promise<GrantResult> {
  return apiPost<GrantResult>(`${base(ragSpaceId)}/`, { user_id: userId, space_role: spaceRole });
}

/** Change le rôle d'un octroi existant. */
export function changeRagSpaceAccessRole(
  ragSpaceId: string,
  grantId: string,
  spaceRole: SpaceRole
): Promise<GrantResult> {
  return apiPatch<GrantResult>(`${base(ragSpaceId)}/${encodeURIComponent(grantId)}/`, {
    space_role: spaceRole,
  });
}

/** Révoque un octroi (renseigne `revoked_at` côté serveur ; pas de suppression). */
export function revokeRagSpaceAccess(ragSpaceId: string, grantId: string): Promise<GrantResult> {
  return apiDelete<GrantResult>(`${base(ragSpaceId)}/${encodeURIComponent(grantId)}/`);
}

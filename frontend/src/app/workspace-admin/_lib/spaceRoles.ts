// Vocabulaire produit des rôles d'ESPACE (RagSpaceAccess). Distinct des rôles
// workspace (_lib/roles.ts). Purement présentationnel — la politique (plafond D1,
// owner implicite) reste décidée par le backend.
import type { SpaceRole } from "@/lib/ragspacePermissions";

// Libellés/descriptions = CLÉS i18n (namespace `workspaces`), traduites au rendu.
export const SPACE_ROLES: Array<{ key: SpaceRole; labelKey: string; descKey: string; color: string }> = [
  { key: "manager", labelKey: "manager", descKey: "managerDesc", color: "#9A3D17" },
  { key: "editor", labelKey: "contributor", descKey: "editorDesc", color: "#0E7C86" },
  { key: "viewer", labelKey: "viewer", descKey: "spaceViewerDesc", color: "#5B53D9" },
];

const BY_KEY = Object.fromEntries(SPACE_ROLES.map((r) => [r.key, r]));

export function spaceRoleInfo(key: string) {
  return BY_KEY[(key || "").toLowerCase()] || SPACE_ROLES[2];
}

/** Statut + CLÉ i18n (namespace `workspaceAdmin`) associés à un code de mutation
 *  RagSpaceAccess. Le libellé est traduit au rendu par le composant client. */
export function grantCodeMessage(code: string): { ok: boolean; key: string } {
  switch (code) {
    case "granted":
      return { ok: true, key: "grantGranted" };
    case "role_changed":
      return { ok: true, key: "grantRoleChanged" };
    case "revoked":
      return { ok: true, key: "grantRevoked" };
    case "already_granted":
      return { ok: true, key: "grantAlreadyGranted" };
    case "owner_implicit":
      return { ok: false, key: "grantOwnerImplicit" };
    case "conflict":
      return { ok: false, key: "grantConflict" };
    case "invalid":
      return { ok: false, key: "grantInvalid" };
    case "grant_not_found":
      return { ok: false, key: "grantNotFound" };
    case "ragspace_deleted":
      return { ok: false, key: "grantRagspaceDeleted" };
    default:
      return { ok: false, key: "grantFailed" };
  }
}

// Rôles d'appartenance au workspace (valeurs backend) + présentation produit.
// Les libellés/descriptions sont des CLÉS i18n (namespace `workspaces`), traduites
// au rendu par les composants clients qui consomment WS_ROLES.
export type WsRoleKey = "owner" | "admin" | "member" | "viewer";

export const WS_ROLES: Array<{ key: WsRoleKey; labelKey: string; descKey: string; color: string }> = [
  { key: "owner", labelKey: "owner", descKey: "ownerDesc", color: "#9A3D17" },
  { key: "admin", labelKey: "administrator", descKey: "administratorDesc", color: "#C2562F" },
  { key: "member", labelKey: "contributor", descKey: "contributorDesc", color: "#0E7C86" },
  { key: "viewer", labelKey: "viewer", descKey: "viewerDesc", color: "#5B53D9" },
];

// Rôles attribuables par invitation : owner exclu (jamais attribuable via ce flux).
export const INVITE_ROLES = WS_ROLES.filter((r) => r.key !== "owner");

const BY_KEY = Object.fromEntries(WS_ROLES.map((r) => [r.key, r]));

export function roleInfo(key: string) {
  return BY_KEY[(key || "").toLowerCase()] || WS_ROLES[3];
}

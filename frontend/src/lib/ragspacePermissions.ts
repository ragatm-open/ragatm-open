// frontend/src/lib/ragspacePermissions.ts
//
// Rôle EFFECTIF d'un RagSpace + helpers de gating UI.
//
// PRINCIPE CARDINAL : le frontend NE RECALCULE JAMAIS la politique d'autorisation.
// Le backend (garde canonique, flag-aware) est la SEULE autorité : il expose par
// espace `permission_level` / `space_role` / `is_owner_implicit`. Ces helpers ne
// font que LIRE cette valeur pour piloter l'UX (afficher / masquer / désactiver).
//
// Règle de sécurité : RESTRICTIF quand l'info est inconnue. Un rôle effectif absent
// (null / undefined) => aucune capacité (read/write/manage = false). On ne suppose
// jamais un accès à partir du seul rôle workspace.

/** Niveau d'accès effectif renvoyé par le backend. `null` = aucun accès. */
export type PermissionLevel = "read" | "write" | "manage";

/** Rôle d'espace explicite (grant RagSpaceAccess). Distinct de l'owner implicite. */
export type SpaceRole = "manager" | "editor" | "viewer";

/** Forme normalisée (camelCase) consommée par l'UI. */
export type EffectiveRagSpaceAccess = {
  permissionLevel: PermissionLevel | null;
  spaceRole: SpaceRole | null;
  /** True = owner du périmètre → manage implicite, sans grant (non modifiable). */
  isOwnerImplicit: boolean;
};

/** Forme brute (snake_case) telle qu'exposée par l'API sur chaque item d'espace. */
export type EffectiveRagSpaceAccessWire = {
  permission_level?: PermissionLevel | null;
  space_role?: SpaceRole | null;
  is_owner_implicit?: boolean;
};

const VALID_LEVELS: readonly PermissionLevel[] = ["read", "write", "manage"];
const VALID_ROLES: readonly SpaceRole[] = ["manager", "editor", "viewer"];

/** Accès « inconnu » — posture restrictive par défaut (aucune capacité). */
export const UNKNOWN_ACCESS: EffectiveRagSpaceAccess = {
  permissionLevel: null,
  spaceRole: null,
  isOwnerImplicit: false,
};

/**
 * Normalise la forme brute de l'API vers `EffectiveRagSpaceAccess`. Toute valeur
 * non reconnue est ramenée à `null` (restrictif). Ne dérive rien : simple lecture.
 */
export function toEffectiveAccess(
  raw: EffectiveRagSpaceAccessWire | null | undefined
): EffectiveRagSpaceAccess {
  if (!raw) return UNKNOWN_ACCESS;
  const level = VALID_LEVELS.includes(raw.permission_level as PermissionLevel)
    ? (raw.permission_level as PermissionLevel)
    : null;
  const role = VALID_ROLES.includes(raw.space_role as SpaceRole)
    ? (raw.space_role as SpaceRole)
    : null;
  return {
    permissionLevel: level,
    spaceRole: role,
    isOwnerImplicit: raw.is_owner_implicit === true,
  };
}

// Ordre des niveaux : read < write < manage. Sert UNIQUEMENT à comparer un niveau
// DÉJÀ décidé par le backend — jamais à décider un accès.
const LEVEL_RANK: Record<PermissionLevel, number> = { read: 1, write: 2, manage: 3 };

function atLeast(access: EffectiveRagSpaceAccess | null | undefined, min: PermissionLevel): boolean {
  const level = access?.permissionLevel;
  if (!level) return false; // inconnu => restrictif
  return LEVEL_RANK[level] >= LEVEL_RANK[min];
}

/** Peut consulter l'espace (lecture, sources, questions). */
export function canReadRagSpace(access: EffectiveRagSpaceAccess | null | undefined): boolean {
  return atLeast(access, "read");
}

/** Peut modifier le contenu (import, suppression de documents, réindexation). */
export function canWriteRagSpace(access: EffectiveRagSpaceAccess | null | undefined): boolean {
  return atLeast(access, "write");
}

/** Peut gouverner l'espace (réglages, gestion des accès RagSpaceAccess). */
export function canManageRagSpace(access: EffectiveRagSpaceAccess | null | undefined): boolean {
  return atLeast(access, "manage");
}

/**
 * Libellé FR du rôle effectif, pour badge / en-tête. Purement présentationnel :
 * traduit ce que le backend a décidé, sans jamais l'inférer. `null` (accès inconnu)
 * => libellé neutre restrictif.
 */
export function getRagSpaceRoleLabel(access: EffectiveRagSpaceAccess | null | undefined): string {
  const level = access?.permissionLevel ?? null;
  if (level === "manage") {
    if (access?.isOwnerImplicit) return "Propriétaire";
    if (access?.spaceRole === "manager") return "Gestionnaire";
    return "Gestion";
  }
  if (level === "write") return "Éditeur";
  if (level === "read") return "Lecteur";
  return "Accès restreint";
}

/**
 * Variante i18n : renvoie la CLÉ de traduction (namespace `workspaces`) du rôle
 * effectif plutôt qu'un libellé figé. Miroir exact de `getRagSpaceRoleLabel`,
 * utilisée par les composants (qui traduisent), sans casser le contrat testé de
 * la fonction historique.
 */
export function getRagSpaceRoleLabelKey(access: EffectiveRagSpaceAccess | null | undefined): string {
  const level = access?.permissionLevel ?? null;
  if (level === "manage") {
    if (access?.isOwnerImplicit) return "owner";
    if (access?.spaceRole === "manager") return "manager";
    return "roleManagement";
  }
  if (level === "write") return "roleEditor";
  if (level === "read") return "viewer";
  return "restrictedAccess";
}

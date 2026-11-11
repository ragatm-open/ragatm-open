import { describe, it, expect } from "vitest";
import {
  toEffectiveAccess,
  canReadRagSpace,
  canWriteRagSpace,
  canManageRagSpace,
  getRagSpaceRoleLabel,
  UNKNOWN_ACCESS,
} from "@/lib/ragspacePermissions";

describe("ragspacePermissions — restrictif quand inconnu", () => {
  it("null / undefined => aucune capacité", () => {
    for (const a of [null, undefined, UNKNOWN_ACCESS]) {
      expect(canReadRagSpace(a)).toBe(false);
      expect(canWriteRagSpace(a)).toBe(false);
      expect(canManageRagSpace(a)).toBe(false);
    }
  });

  it("toEffectiveAccess ramène toute valeur non reconnue à null", () => {
    const a = toEffectiveAccess({ permission_level: "bogus" as any, space_role: "x" as any });
    expect(a.permissionLevel).toBeNull();
    expect(a.spaceRole).toBeNull();
    expect(a.isOwnerImplicit).toBe(false);
  });

  it("toEffectiveAccess normalise la forme brute valide", () => {
    const a = toEffectiveAccess({ permission_level: "manage", space_role: "manager", is_owner_implicit: true });
    expect(a).toEqual({ permissionLevel: "manage", spaceRole: "manager", isOwnerImplicit: true });
  });
});

describe("ragspacePermissions — hiérarchie read < write < manage", () => {
  const read = toEffectiveAccess({ permission_level: "read" });
  const write = toEffectiveAccess({ permission_level: "write" });
  const manage = toEffectiveAccess({ permission_level: "manage" });

  it("read n'autorise que la lecture", () => {
    expect(canReadRagSpace(read)).toBe(true);
    expect(canWriteRagSpace(read)).toBe(false);
    expect(canManageRagSpace(read)).toBe(false);
  });
  it("write autorise lecture + écriture, pas la gestion", () => {
    expect(canReadRagSpace(write)).toBe(true);
    expect(canWriteRagSpace(write)).toBe(true);
    expect(canManageRagSpace(write)).toBe(false);
  });
  it("manage autorise tout", () => {
    expect(canReadRagSpace(manage)).toBe(true);
    expect(canWriteRagSpace(manage)).toBe(true);
    expect(canManageRagSpace(manage)).toBe(true);
  });
});

describe("getRagSpaceRoleLabel", () => {
  it("distingue propriétaire, gestionnaire et gestion générique", () => {
    expect(getRagSpaceRoleLabel(toEffectiveAccess({ permission_level: "manage", is_owner_implicit: true }))).toBe("Propriétaire");
    expect(getRagSpaceRoleLabel(toEffectiveAccess({ permission_level: "manage", space_role: "manager" }))).toBe("Gestionnaire");
    expect(getRagSpaceRoleLabel(toEffectiveAccess({ permission_level: "manage" }))).toBe("Gestion");
  });
  it("write => Éditeur, read => Lecteur", () => {
    expect(getRagSpaceRoleLabel(toEffectiveAccess({ permission_level: "write" }))).toBe("Éditeur");
    expect(getRagSpaceRoleLabel(toEffectiveAccess({ permission_level: "read" }))).toBe("Lecteur");
  });
  it("inconnu => Accès restreint (posture restrictive)", () => {
    expect(getRagSpaceRoleLabel(null)).toBe("Accès restreint");
    expect(getRagSpaceRoleLabel(UNKNOWN_ACCESS)).toBe("Accès restreint");
  });
});

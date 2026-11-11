/**
 * Écran d'administration des accès RagSpaceAccess.
 *
 * Le composant n'est qu'une façade des endpoints backend : on vérifie qu'il lit
 * (grants + éligibles), qu'il octroie / révoque via les bonnes fonctions, qu'il
 * rappelle l'owner implicite non modifiable, et qu'il ne montre jamais un code interne.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listRagSpaceGrants = vi.fn();
const listEligibleUsers = vi.fn();
const grantRagSpaceAccess = vi.fn();
const changeRagSpaceAccessRole = vi.fn();
const revokeRagSpaceAccess = vi.fn();

vi.mock("@/lib/ragspaceAccessApi", () => ({
  listRagSpaceGrants: (...a: any[]) => listRagSpaceGrants(...a),
  listEligibleUsers: (...a: any[]) => listEligibleUsers(...a),
  grantRagSpaceAccess: (...a: any[]) => grantRagSpaceAccess(...a),
  changeRagSpaceAccessRole: (...a: any[]) => changeRagSpaceAccessRole(...a),
  revokeRagSpaceAccess: (...a: any[]) => revokeRagSpaceAccess(...a),
}));

import { AccessManagerModal } from "@/app/workspace-admin/_components/AccessManagerModal";

const GRANT = {
  id: "grant-1",
  user_id: 7,
  user: { id: 7, username: "alice", email: "alice@t.local" },
  space_role: "editor",
  assigned_by: 1,
  created_at: "2026-01-01T00:00:00Z",
  revoked_at: null,
};
const ELIGIBLE = {
  user: { id: 9, username: "bob", email: "bob@t.local" },
  workspace_role: "member",
  allowed_space_roles: ["editor", "viewer"],
};

function renderModal() {
  render(<AccessManagerModal ragSpaceId="space-1" ragSpaceName="RH" onClose={vi.fn()} />);
}

describe("AccessManagerModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listRagSpaceGrants.mockResolvedValue([GRANT]);
    listEligibleUsers.mockResolvedValue([ELIGIBLE]);
    grantRagSpaceAccess.mockResolvedValue({ code: "granted" });
    revokeRagSpaceAccess.mockResolvedValue({ code: "revoked" });
  });

  it("charge et affiche les accès actifs + rappelle l'owner implicite", async () => {
    renderModal();
    expect(await screen.findByText("alice@t.local")).toBeTruthy();
    expect(screen.getByText(/propriétaire/i)).toBeTruthy();
    expect(screen.getByText(/implicite/i)).toBeTruthy();
    expect(listRagSpaceGrants).toHaveBeenCalledWith("space-1");
    expect(listEligibleUsers).toHaveBeenCalledWith("space-1");
  });

  it("octroie un accès à un éligible via grantRagSpaceAccess", async () => {
    renderModal();
    await screen.findByText("alice@t.local");
    fireEvent.change(screen.getByLabelText(/utilisateur à qui accorder/i), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: /accorder/i }));
    await waitFor(() => expect(grantRagSpaceAccess).toHaveBeenCalledWith("space-1", 9, expect.any(String)));
  });

  it("révoque après confirmation via revokeRagSpaceAccess", async () => {
    renderModal();
    await screen.findByText("alice@t.local");
    fireEvent.click(screen.getByRole("button", { name: /révoquer l'accès de alice/i }));
    // Confirmation
    fireEvent.click(screen.getByRole("button", { name: /^révoquer l'accès$/i }));
    await waitFor(() => expect(revokeRagSpaceAccess).toHaveBeenCalledWith("space-1", "grant-1"));
  });

  it("un code non-ok (owner_implicit) affiche un message neutre sans code interne", async () => {
    grantRagSpaceAccess.mockResolvedValue({ code: "owner_implicit" });
    renderModal();
    await screen.findByText("alice@t.local");
    fireEvent.change(screen.getByLabelText(/utilisateur à qui accorder/i), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: /accorder/i }));
    expect(await screen.findByText(/accès complet est implicite/i)).toBeTruthy();
    expect(screen.queryByText(/owner_implicit/i)).toBeNull();
  });
});

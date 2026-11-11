/**
 * P0 — sélecteur de workspace : un utilisateur multi-workspace peut basculer.
 * La bascule délègue à WorkspaceContext.setWorkspaceId (qui recharge/vide les caches).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const setWorkspaceId = vi.fn();
const state = vi.hoisted(() => ({
  workspaces: [
    { id: 2, name: "Initial workspace", role: "owner" },
    { id: 5, name: "FOLDER-LIVE", role: "admin" },
  ] as any[],
  workspaceId: 2,
}));
vi.mock("@/components/shell/WorkspaceContext", () => ({
  useWorkspace: () => ({
    workspaces: state.workspaces,
    workspaceId: state.workspaceId,
    setWorkspaceId,
  }),
}));

import { WorkspaceSwitcher } from "@/components/shell/WorkspaceSwitcher";

describe("WorkspaceSwitcher", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.workspaces = [
      { id: 2, name: "Initial workspace", role: "owner" },
      { id: 5, name: "FOLDER-LIVE", role: "admin" },
    ];
    state.workspaceId = 2;
  });

  it("liste tous les workspaces et bascule via setWorkspaceId", () => {
    render(<WorkspaceSwitcher />);
    // Le workspace courant est affiché sur le bouton.
    expect(screen.getByLabelText("Changer d'espace de travail")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Changer d'espace de travail"));
    // Les deux workspaces sont proposés.
    expect(screen.getByRole("option", { name: /FOLDER-LIVE/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("option", { name: /FOLDER-LIVE/ }));
    expect(setWorkspaceId).toHaveBeenCalledWith(5);
  });

  it("ne re-sélectionne pas le workspace déjà courant", () => {
    render(<WorkspaceSwitcher />);
    fireEvent.click(screen.getByLabelText("Changer d'espace de travail"));
    fireEvent.click(screen.getByRole("option", { name: /Initial workspace/ }));
    expect(setWorkspaceId).not.toHaveBeenCalled();
  });

  it("est masqué avec un seul workspace (aucun choix)", () => {
    state.workspaces = [{ id: 2, name: "Solo", role: "owner" }];
    const { container } = render(<WorkspaceSwitcher />);
    expect(container.firstChild).toBeNull();
  });
});

/**
 * Annuaire utilisateurs de la console plateforme.
 * Sécurité : la liste ne montre jamais de secret/contenu ; le rôle effectif et les
 * décisions viennent du backend. On vérifie l'affichage + l'absence de contenu.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const listPlatformUsers = vi.fn();
const getPlatformUser = vi.fn();
vi.mock("@/lib/platformAdmin", () => ({
  listPlatformUsers: (...a: any[]) => listPlatformUsers(...a),
  getPlatformUser: (...a: any[]) => getPlatformUser(...a),
}));

import { UsersTab } from "@/app/platform-admin/_components/UsersTab";

const USER = {
  id: 7,
  username: "alice",
  email: "alice@t.local",
  display_name: "Alice",
  is_active: true,
  is_staff: false,
  is_superuser: false,
  date_joined: "2026-01-01T00:00:00Z",
  last_login: null,
  memberships_count: 2,
  owned_workspaces_count: 1,
};

describe("UsersTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listPlatformUsers.mockResolvedValue({ items: [USER], total: 1, limit: 25, offset: 0 });
  });

  it("charge et affiche l'annuaire (sans secret)", async () => {
    render(<UsersTab />);
    expect(await screen.findByText("Alice")).toBeTruthy();
    expect(screen.getByText("alice@t.local")).toBeTruthy();
    expect(listPlatformUsers).toHaveBeenCalled();
    // aucun mot de passe / token affiché
    expect(screen.queryByText(/password|token|secret/i)).toBeNull();
  });

  it("affiche un état d'erreur neutre en cas d'échec", async () => {
    listPlatformUsers.mockRejectedValueOnce(new Error("boom"));
    render(<UsersTab />);
    await waitFor(() => expect(screen.getByText(/Erreur de chargement des utilisateurs/i)).toBeTruthy());
  });
});

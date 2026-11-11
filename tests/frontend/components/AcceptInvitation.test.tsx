import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

/**
 * Page publique d'acceptation d'invitation : orientation du flux selon l'état du
 * token (nouveau compte → mot de passe ; compte actif → connexion, JAMAIS de
 * champ mot de passe).
 */
const push = vi.fn();
const searchParams = new URLSearchParams("token=RAW-TOKEN-123");

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => searchParams,
}));

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

import AcceptInvitationClient from "@/app/accept-invitation/AcceptInvitationClient";

beforeEach(() => {
  apiFetch.mockReset();
  push.mockReset();
  window.history.replaceState({}, "", "/accept-invitation?token=RAW-TOKEN-123");
});

describe("AcceptInvitationClient", () => {
  it("nouveau compte : affiche le formulaire mot de passe et accepte", async () => {
    apiFetch
      .mockResolvedValueOnce({
        status: "pending",
        workspace_name: "Espace Démo",
        role: "viewer",
        email_masked: "n***w@ex.com",
        account_active: false,
      })
      .mockResolvedValueOnce({ ok: true });

    render(<AcceptInvitationClient />);

    await waitFor(() => expect(screen.getByText(/Rejoindre/)).toBeTruthy());
    expect(screen.getByText(/Espace Démo/)).toBeTruthy();
    const inputs = document.querySelectorAll('input[type="password"]');
    expect(inputs.length).toBe(2);

    fireEvent.change(inputs[0], { target: { value: "Corr3ct-Horse-88" } });
    fireEvent.change(inputs[1], { target: { value: "Corr3ct-Horse-88" } });
    fireEvent.click(screen.getByText(/Activer mon compte/));

    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/v1/invitations/accept/",
        expect.objectContaining({ method: "POST" })
      )
    );
    // Le token est envoyé dans le corps, jamais le rôle.
    const body = JSON.parse((apiFetch.mock.calls[1][1] as any).body);
    expect(body).toEqual({
      token: "RAW-TOKEN-123",
      password: "Corr3ct-Horse-88",
      password_confirmation: "Corr3ct-Horse-88",
    });
    expect(body.role).toBeUndefined();
  });

  it("compte actif : aucun champ mot de passe, invite à se connecter", async () => {
    apiFetch.mockResolvedValueOnce({
      status: "pending",
      workspace_name: "Espace Démo",
      role: "admin",
      email_masked: "a***e@ex.com",
      account_active: true,
    });

    render(<AcceptInvitationClient />);

    await waitFor(() => expect(screen.getByText(/Ce compte existe déjà/)).toBeTruthy());
    expect(document.querySelectorAll('input[type="password"]').length).toBe(0);
    expect(screen.getByText(/Se connecter/)).toBeTruthy();
  });

  it("token révoqué : message dédié, pas de formulaire", async () => {
    apiFetch.mockResolvedValueOnce({
      status: "revoked",
      workspace_name: "X",
      role: "viewer",
      email_masked: "x***@ex.com",
      account_active: false,
    });
    render(<AcceptInvitationClient />);
    await waitFor(() => expect(screen.getByText(/Invitation révoquée/)).toBeTruthy());
    expect(document.querySelectorAll('input[type="password"]').length).toBe(0);
  });

  it("nettoie le token de l'URL après capture", async () => {
    apiFetch.mockResolvedValueOnce({
      status: "pending",
      workspace_name: "X",
      role: "viewer",
      email_masked: "x***@ex.com",
      account_active: false,
    });
    render(<AcceptInvitationClient />);
    await waitFor(() => expect(window.location.search).toBe(""));
  });
});

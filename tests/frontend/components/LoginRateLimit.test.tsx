/**
 * Page de connexion — 429 explicite.
 *
 * Incident staging du 08/10/2026 : après un renvoi à tort vers /login, le login
 * soumis avec un cookie encore valide comptait dans le seau DRF `user` épuisé et
 * affichait le texte brut « API 429 : Requête ralentie. Expected available in … ».
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ApiError } from "@/lib/apiError";

const apiFetch = vi.fn();
const bootstrap = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...a: any[]) => apiFetch(...a),
  bootstrap: (...a: any[]) => bootstrap(...a),
}));
const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/login",
}));
vi.mock("next/link", () => ({
  default: ({ children, href }: any) => <a href={href}>{children}</a>,
}));
vi.mock("@/components/LanguageSelector", () => ({ LanguageSelector: () => null }));

import LoginPage from "@/app/login/page";

const INVALID = /Identifiants invalides/;
const apiErr = (status: number, detail: string, retryAfterMs: number | null = null) =>
  new ApiError(status, "", { detail }, `API ${status} : ${detail}`, retryAfterMs);

const loginCalls = () => apiFetch.mock.calls.filter(([u]) => String(u).startsWith("/api/v1/auth/login"));

// MFA obligatoire : après le mot de passe, la page demande le code TOTP.
async function submitCode() {
  const input = await screen.findByLabelText(/Code de vérification/i);
  fireEvent.change(input, { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: /Vérifier et accéder/ }));
}

function submit() {
  fireEvent.change(screen.getByLabelText(/Adresse email/i), { target: { value: "u@x.com" } });
  fireEvent.change(screen.getByLabelText(/Mot de passe/i, { selector: "input" }), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: /Accéder à mon espace/ }));
}

beforeEach(() => {
  vi.clearAllMocks();
  bootstrap.mockResolvedValue({
    preferred_locale: "fr",
    workspace: { id: 1 },
    ragspace: { id: "A" },
  });
});

describe("Login — 429", () => {
  it("W10 — 429 avec Retry-After 17 s : message d'attente, pas « identifiants invalides », pas de réessai", async () => {
    apiFetch.mockRejectedValue(
      apiErr(429, "Requête ralentie. Expected available in 17 seconds.", 17_000)
    );
    render(<LoginPage />);
    submit();

    await waitFor(() => expect(screen.getByText(/Trop de requêtes/)).toBeInTheDocument());
    expect(screen.getByText(/environ 17 secondes/)).toBeInTheDocument();
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
    expect(screen.queryByText(/API 429/)).not.toBeInTheDocument();
    // L'utilisateur reste sur la page, champs conservés, aucun réessai automatique.
    expect(replace).not.toHaveBeenCalled();
    expect((screen.getByLabelText(/Adresse email/i) as HTMLInputElement).value).toBe("u@x.com");
    await new Promise((r) => setTimeout(r, 50));
    expect(loginCalls()).toHaveLength(1);
    // Le bouton redevient utilisable (pas d'état « Connexion… » figé).
    expect(screen.getByRole("button", { name: /Accéder à mon espace/ })).not.toBeDisabled();
  });

  it("429 sans Retry-After : message d'attente générique", async () => {
    apiFetch.mockRejectedValue(apiErr(429, "Requête ralentie."));
    render(<LoginPage />);
    submit();
    await waitFor(() => expect(screen.getByText(/Réessayez dans quelques instants/)).toBeInTheDocument());
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });

  it("429 au bootstrap qui suit un login réussi : même message d'attente", async () => {
    apiFetch.mockResolvedValue({ ok: true, next: "mfa" });
    bootstrap.mockRejectedValue(apiErr(429, "Requête ralentie.", 5_000));
    render(<LoginPage />);
    submit();
    await submitCode();
    await waitFor(() => expect(screen.getByText(/environ 5 secondes/)).toBeInTheDocument());
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("Login — non-régression hors 429", () => {
  it("200 : second facteur, bootstrap puis /chat", async () => {
    apiFetch.mockResolvedValue({ ok: true, next: "mfa" });
    render(<LoginPage />);
    submit();
    await submitCode();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/chat"));
    expect(loginCalls()).toHaveLength(1);
  });

  it.each([400, 401])("%i : « identifiants invalides »", async (status) => {
    apiFetch.mockRejectedValue(apiErr(status, "Invalid"));
    render(<LoginPage />);
    submit();
    await waitFor(() => expect(screen.getByText(INVALID)).toBeInTheDocument());
    expect(replace).not.toHaveBeenCalled();
  });

  it("500 : message technique conservé (comportement existant)", async () => {
    apiFetch.mockRejectedValue(apiErr(500, "Server Error"));
    render(<LoginPage />);
    submit();
    await waitFor(() => expect(screen.getByText(/API 500/)).toBeInTheDocument());
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });
});

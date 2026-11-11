/**
 * Page de connexion — second facteur obligatoire (AUTH-MFA-01).
 *
 * Invariant : un mot de passe valide n'ouvre AUCUNE session. Le bootstrap et la
 * redirection vers /chat n'ont lieu qu'après le code TOTP accepté par le serveur.
 * Aucune donnée MFA (code, secret, URI) n'est écrite dans le stockage navigateur.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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
const toDataURL = vi.fn();
vi.mock("qrcode", () => ({ default: { toDataURL: (...a: any[]) => toDataURL(...a) } }));

import LoginPage from "@/app/login/page";

const SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
const URI = `otpauth://totp/RAGatm:u?secret=${SECRET}&issuer=RAGatm`;
const apiErr = (status: number, code: string) =>
  new ApiError(status, "", { detail: code, code }, `API ${status} : ${code}`, null);

type Routes = Record<string, (init?: any) => any>;

function route(routes: Routes) {
  apiFetch.mockImplementation(async (path: string, init?: any) => {
    const key = `${(init?.method || "GET").toUpperCase()} ${path.replace(/\/$/, "")}`;
    const handler = routes[key];
    if (!handler) throw new Error(`route non simulée : ${key}`);
    return handler(init);
  });
}

const calls = (fragment: string) => apiFetch.mock.calls.filter(([u]) => String(u).includes(fragment));

function submitPassword() {
  fireEvent.change(screen.getByLabelText(/Adresse email/i), { target: { value: "u@x.com" } });
  fireEvent.change(screen.getByLabelText(/Mot de passe/i, { selector: "input" }), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: /Accéder à mon espace/ }));
}

async function submitCode(value = "123456", button = /Vérifier et accéder/) {
  fireEvent.change(await screen.findByLabelText(/Code de vérification/i), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: button }));
}

const NO_CHALLENGE = () => ({ pending: false, next: null, expires_in: 0 });

let setItem: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  bootstrap.mockResolvedValue({ preferred_locale: "fr", workspace: { id: 1 }, ragspace: { id: "A" } });
  toDataURL.mockResolvedValue("data:image/png;base64,QR");
  setItem = vi.spyOn(Storage.prototype, "setItem");
});

afterEach(() => {
  // Aucune donnée MFA dans localStorage/sessionStorage, quel que soit le scénario.
  for (const [k, v] of setItem.mock.calls) {
    expect(["selectedWorkspaceId", "selectedRagSpaceId", "bootstrapped"]).toContain(k);
    expect(String(v)).not.toContain(SECRET);
    expect(String(v)).not.toContain("123456");
  }
  setItem.mockRestore();
});

describe("Login — second facteur", () => {
  it("mot de passe valide : écran MFA, AUCUN bootstrap, aucune redirection", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, mfa_required: true, next: "mfa", expires_in: 600 }),
    });
    render(<LoginPage />);
    submitPassword();
    expect(await screen.findByText("Vérification en deux étapes")).toBeInTheDocument();
    expect(bootstrap).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
    const input = screen.getByLabelText(/Code de vérification/i);
    expect(input).toHaveAttribute("autocomplete", "one-time-code");
    expect(input).toHaveAttribute("inputmode", "numeric");
  });

  it("code accepté : bootstrap puis /chat", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, next: "mfa" }),
      "POST /api/v1/auth/mfa/verify": () => ({ ok: true, id: "1", email: "u@x.com", username: "u" }),
    });
    render(<LoginPage />);
    submitPassword();
    await submitCode(" 123 456 ");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/chat"));
    expect(JSON.parse(calls("mfa/verify")[0][1].body)).toEqual({ token: "123456" });
    // Le bootstrap suit TOUJOURS la vérification, jamais le mot de passe.
    expect(bootstrap.mock.invocationCallOrder[0]).toBeGreaterThan(apiFetch.mock.invocationCallOrder.at(-1)!);
  });

  it("code faux : message, reste sur l'écran MFA, pas de bootstrap", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, next: "mfa" }),
      "POST /api/v1/auth/mfa/verify": () => {
        throw apiErr(400, "invalid_mfa_token");
      },
    });
    render(<LoginPage />);
    submitPassword();
    await submitCode("000000");
    expect(await screen.findByText(/Code invalide/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Code de vérification/i)).toBeInTheDocument();
    expect(bootstrap).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("challenge expiré : retour à l'écran d'identifiants avec explication", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, next: "mfa" }),
      "POST /api/v1/auth/mfa/verify": () => {
        throw apiErr(401, "mfa_challenge_invalid");
      },
    });
    render(<LoginPage />);
    submitPassword();
    await submitCode();
    expect(await screen.findByText(/vérification a expiré/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Adresse email/i)).toBeInTheDocument();
    expect(bootstrap).not.toHaveBeenCalled();
  });

  it("rafraîchissement pendant le challenge : l'étape est reprise depuis le serveur", async () => {
    route({ "GET /api/v1/auth/mfa/status": () => ({ pending: true, next: "mfa", expires_in: 420 }) });
    render(<LoginPage />);
    expect(await screen.findByLabelText(/Code de vérification/i)).toBeInTheDocument();
    expect(calls("auth/login")).toHaveLength(0);
  });

  it("« utiliser un autre compte » revient aux identifiants", async () => {
    route({ "GET /api/v1/auth/mfa/status": () => ({ pending: true, next: "mfa", expires_in: 420 }) });
    render(<LoginPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Utiliser un autre compte/ }));
    expect(screen.getByLabelText(/Adresse email/i)).toBeInTheDocument();
  });

  it("compte verrouillé : message dédié", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => {
        throw apiErr(403, "account_locked");
      },
    });
    render(<LoginPage />);
    submitPassword();
    expect(await screen.findByText(/temporairement verrouillé/)).toBeInTheDocument();
  });
});

describe("Login — premier enrôlement", () => {
  it("QR + clé manuelle, confirmation, puis bootstrap et /chat", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, next: "mfa_setup" }),
      "POST /api/v1/auth/mfa/setup": () => ({ method: "totp", provisioning_uri: URI, secret: SECRET }),
      "POST /api/v1/auth/mfa/setup/confirm": () => ({ ok: true, id: "1", email: "u@x.com", username: "u" }),
    });
    render(<LoginPage />);
    submitPassword();
    expect(await screen.findByText("Activez la vérification en deux étapes")).toBeInTheDocument();
    expect(await screen.findByAltText(/QR code de configuration/)).toHaveAttribute("src", "data:image/png;base64,QR");
    expect(toDataURL).toHaveBeenCalledWith(URI, expect.anything());
    expect(screen.getByTestId("mfa-manual-key").textContent?.replace(/\s/g, "")).toBe(SECRET);
    expect(bootstrap).not.toHaveBeenCalled();

    await submitCode("654321", /Activer et accéder/);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/chat"));
    expect(JSON.parse(calls("mfa/setup/confirm")[0][1].body)).toEqual({ token: "654321" });
  });

  it("serveur répond 409 (aucun facteur) à la vérification : bascule vers l'enrôlement", async () => {
    route({
      "GET /api/v1/auth/mfa/status": NO_CHALLENGE,
      "POST /api/v1/auth/login": () => ({ ok: true, next: "mfa" }),
      "POST /api/v1/auth/mfa/verify": () => {
        throw apiErr(409, "mfa_device_required");
      },
      "POST /api/v1/auth/mfa/setup": () => ({ method: "totp", provisioning_uri: URI, secret: SECRET }),
    });
    render(<LoginPage />);
    submitPassword();
    await submitCode();
    expect(await screen.findByText("Activez la vérification en deux étapes")).toBeInTheDocument();
  });
});

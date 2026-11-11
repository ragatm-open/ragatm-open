/**
 * Palette de recherche globale montée par la VRAIE
 * coquille : raccourcis, calque modal, debounce, annulation, états, liens réels.
 * Le contexte d'espace est doublé ; l'API aussi (aucun réseau).
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/home",
  useRouter: () => ({ refresh: vi.fn(), replace: nav.replace, push: nav.push }),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={typeof href === "string" ? href : "#"} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/components/shell/SpaceSwitcher", () => ({ SpaceSwitcher: () => null }));
vi.mock("@/components/shell/WorkspaceSwitcher", () => ({ WorkspaceSwitcher: () => null }));

const ctx = vi.hoisted(() => ({
  openTarget: vi.fn(() => true),
  loadAllRagSpaces: vi.fn(async () => []),
  logout: vi.fn(),
}));
vi.mock("@/components/shell/WorkspaceContext", () => ({
  WorkspaceProvider: ({ children }: any) => <>{children}</>,
  useWorkspace: () => ({
    me: { id: "u1", username: "u", email: "u@e.com" },
    role: "admin",
    isAdmin: false,
    isGlobalOwner: false,
    isSuperuser: false,
    logout: ctx.logout,
    workspaces: [
      { id: 1, name: "Alpha WS", role: "owner" },
      { id: 2, name: "Beta WS", role: "member" },
    ],
    workspaceId: 1,
    ragSpaces: [{ id: "S1", name: "Dépenses publiques", workspace_id: 1 }],
    ragSpaceId: "S1",
    favorites: ["S2"],
    recents: ["S1"],
    openTarget: ctx.openTarget,
    allRagSpaces: [
      { id: "S1", name: "Dépenses publiques", workspace_id: 1 },
      { id: "S2", name: "Marché Nordlicht", workspace_id: 2 },
    ],
    loadAllRagSpaces: ctx.loadAllRagSpaces,
  }),
  ROLE_INFO: { admin: { labelKey: "administrator", descKey: "administratorDesc", color: "#000" } },
}));

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...a: any[]) => apiFetch(...a) }));

import { ApiError } from "@/lib/apiError";
import { AppShell } from "@/components/shell/AppShell";

const RESULT = {
  id: "8f0c8a1e-2b0d-4f8e-9a3b-1c2d3e4f5a6b",
  title: "Analyse des dépenses",
  rag_space_id: "S2",
  rag_space_name: "Marché Nordlicht",
  workspace_id: 2,
  workspace_name: "Beta WS",
  updated_at: "2026-10-30T10:00:00Z",
  match: "content",
  snippet: "… les <b>dépenses</b> d'intérêts …",
};

function mount() {
  return render(
    <AppShell>
      <button data-testid="before">avant</button>
      <textarea data-testid="composer" />
    </AppShell>
  );
}

const palette = () => screen.queryByTestId("command-palette");
const input = () => screen.getByRole("combobox", { name: "Rechercher dans RAGatm" });
const searchCalls = () => apiFetch.mock.calls.filter(([u]) => String(u).startsWith("/api/v1/chat/threads/search/"));

describe("Palette — ouverture et fermeture", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ results: [], next: null });
  });
  afterEach(() => {
    document.documentElement.style.overflow = "";
  });

  it("Ctrl+K ouvre la palette, l'action native est empêchée", () => {
    mount();
    const ev = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(ev);
    });
    expect(ev.defaultPrevented).toBe(true);
    expect(palette()).toBeInTheDocument();
    expect(input()).toHaveFocus();
  });

  it("⌘K ouvre la palette", () => {
    mount();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(palette()).toBeInTheDocument();
  });

  it("Ctrl+K fonctionne aussi depuis le composer", () => {
    mount();
    fireEvent.keyDown(screen.getByTestId("composer"), { key: "K", ctrlKey: true });
    expect(palette()).toBeInTheDocument();
  });

  it("« / » est ignoré dans un champ de saisie, actif ailleurs", () => {
    mount();
    fireEvent.keyDown(screen.getByTestId("composer"), { key: "/" });
    expect(palette()).not.toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "/" });
    expect(palette()).toBeInTheDocument();
  });

  it("composition IME : aucun raccourci", () => {
    mount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true, isComposing: true });
    expect(palette()).not.toBeInTheDocument();
  });

  it("le bouton visible « Rechercher… » ouvre la palette", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Ouvrir la recherche (espaces et conversations)" }));
    expect(palette()).toBeInTheDocument();
  });

  it("Échap ferme et restitue le focus à l'élément d'origine", async () => {
    mount();
    const before = screen.getByTestId("before");
    before.focus();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(input()).toHaveFocus();
    fireEvent.keyDown(input(), { key: "Escape" });
    await waitFor(() => expect(palette()).not.toBeInTheDocument());
    expect(before).toHaveFocus();
  });

  it("le bouton ✕ ferme la palette (mobile : pas d'Échap au doigt)", async () => {
    mount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.click(screen.getByRole("button", { name: "Fermer la recherche" }));
    await waitFor(() => expect(palette()).not.toBeInTheDocument());
  });

  it("sans saisie : favoris et récents multi-workspace, aucun appel de recherche", () => {
    mount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(screen.getByRole("group", { name: "Favoris" })).toHaveTextContent("Marché Nordlicht");
    expect(screen.getByRole("group", { name: "Favoris" })).toHaveTextContent("Beta WS");
    expect(screen.getByRole("group", { name: "Récents" })).toHaveTextContent("Dépenses publiques");
    expect(searchCalls()).toHaveLength(0);
    expect(ctx.loadAllRagSpaces).toHaveBeenCalled();
  });

  it("ouvrir un espace d'un autre workspace passe par openTarget", async () => {
    mount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.change(input(), { target: { value: "marché" } });
    fireEvent.click(screen.getByRole("tab", { name: "Espaces" }));
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(ctx.openTarget).toHaveBeenCalledWith({ workspaceId: 2, ragSpaceId: "S2" });
    await waitFor(() => expect(palette()).not.toBeInTheDocument());
  });
});

describe("Palette — recherche de conversations", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiFetch.mockReset();
    nav.push.mockReset();
    nav.replace.mockReset();
    ctx.logout.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
    document.documentElement.style.overflow = "";
  });

  function openWith(q: string) {
    mount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    fireEvent.change(input(), { target: { value: q } });
  }

  it("moins de 2 caractères : aucun appel réseau", () => {
    apiFetch.mockResolvedValue({ results: [], next: null });
    openWith("d");
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(searchCalls()).toHaveLength(0);
  });

  it("debounce de 250 ms, puis un appel ; état de chargement affiché", async () => {
    let resolve!: (v: unknown) => void;
    apiFetch.mockImplementation(() => new Promise((r) => (resolve = r)));
    openWith("dépenses");
    act(() => {
      vi.advanceTimersByTime(249);
    });
    expect(searchCalls()).toHaveLength(0);
    expect(screen.getByRole("status")).toHaveTextContent("Recherche en cours");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(searchCalls()).toHaveLength(1);
    expect(String(searchCalls()[0][0])).toContain("q=d%C3%A9penses");
    await act(async () => {
      resolve({ results: [RESULT], next: null });
    });
    const link = screen.getByRole("link", { name: /Analyse des dépenses/ });
    expect(link).toHaveAttribute("href", `/chat?thread=${RESULT.id}`);
    // Extrait affiché en TEXTE (jamais interprété comme du HTML).
    expect(screen.getByText("… les <b>dépenses</b> d'intérêts …")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("une nouvelle saisie annule la requête précédente", () => {
    apiFetch.mockImplementation(() => new Promise(() => {}));
    openWith("dep");
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(searchCalls()).toHaveLength(1);
    const firstSignal = searchCalls()[0][1].signal as AbortSignal;
    fireEvent.change(input(), { target: { value: "depe" } });
    expect(firstSignal.aborted).toBe(true);
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(searchCalls()).toHaveLength(2);
  });

  it("clic simple : navigation applicative ; Ctrl+clic : comportement natif", async () => {
    apiFetch.mockResolvedValue({ results: [RESULT], next: null });
    openWith("analyse");
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    const link = screen.getByRole("link", { name: /Analyse des dépenses/ });
    const ctrlClick = new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true, button: 0 });
    link.dispatchEvent(ctrlClick);
    expect(ctrlClick.defaultPrevented).toBe(false);
    expect(nav.push).not.toHaveBeenCalled();
    fireEvent.click(link);
    expect(nav.push).toHaveBeenCalledWith(`/chat?thread=${RESULT.id}`);
  });

  it("Entrée sur une conversation navigue vers son lien direct", async () => {
    apiFetch.mockResolvedValue({ results: [RESULT], next: null });
    openWith("analyse");
    fireEvent.click(screen.getByRole("tab", { name: "Conversations" }));
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(nav.push).toHaveBeenCalledWith(`/chat?thread=${RESULT.id}`);
  });

  it("« Afficher plus » suit le curseur opaque, sans doublon", async () => {
    const second = { ...RESULT, id: "9f0c8a1e-2b0d-4f8e-9a3b-1c2d3e4f5a6b", title: "Deuxième" };
    apiFetch
      .mockResolvedValueOnce({ results: [RESULT], next: "CUR1" })
      .mockResolvedValueOnce({ results: [RESULT, second], next: null });
    openWith("analyse");
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Afficher plus de conversations" }));
    });
    expect(String(searchCalls()[1][0])).toContain("cursor=CUR1");
    expect(within(palette()!).getAllByRole("link")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Afficher plus de conversations" })).not.toBeInTheDocument();
  });

  it("aucun résultat : état vide", async () => {
    apiFetch.mockResolvedValue({ results: [], next: null });
    openWith("introuvable");
    fireEvent.click(screen.getByRole("tab", { name: "Conversations" }));
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Aucun résultat pour « introuvable »");
  });

  it("erreur : message et bouton Réessayer", async () => {
    apiFetch.mockRejectedValueOnce(new ApiError(500, "err", null, "boom")).mockResolvedValue({ results: [RESULT], next: null });
    openWith("analyse");
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByRole("status")).toHaveTextContent("La recherche a échoué.");
    fireEvent.click(screen.getByRole("button", { name: "Réessayer" }));
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByRole("link", { name: /Analyse des dépenses/ })).toBeInTheDocument();
  });

  it("429 : état dédié, aucune déconnexion, aucune redirection, aucun nouvel essai", async () => {
    apiFetch.mockRejectedValue(new ApiError(429, "Too Many Requests", { detail: "throttled" }, "429", 10_000));
    openWith("analyse");
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Trop de recherches");
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    expect(searchCalls()).toHaveLength(1);
    expect(ctx.logout).not.toHaveBeenCalled();
    expect(nav.replace).not.toHaveBeenCalled();
    expect(apiFetch.mock.calls.some(([u]) => String(u).includes("/auth/"))).toBe(false);
    expect(palette()).toBeInTheDocument();
  });

  it("503 search_timeout : message dédié", async () => {
    apiFetch.mockRejectedValue(new ApiError(503, "err", { detail: "search_timeout" }, "503"));
    openWith("analyse");
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByRole("status")).toHaveTextContent("trop de temps");
  });
});

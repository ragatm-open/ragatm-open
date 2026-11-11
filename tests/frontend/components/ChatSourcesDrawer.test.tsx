/**
 * Page chat : panneau Sources en tiroir sous 1200 px.
 *
 * Vérifie l'ÉTAT (ouvert/fermé, source sélectionnée, inert, focus) — jsdom ne
 * mesure rien. Le desktop doit rester strictement inchangé : une citation y
 * bascule la surbrillance et n'ouvre aucun tiroir.
 */
import React from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: any) => <div>{children}</div>,
  Topbar: ({ actions }: any) => <div>{actions}</div>,
  useWorkspace: () => ({
    ragSpace: { id: "rs-1", name: "Espace" },
    ragSpaceId: "rs-1",
    currentStats: { documents: { total: 3 }, indexing: { in_progress: false } },
  }),
}));

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({
  apiFetch: (...args: any[]) => apiFetch(...args),
  listDocuments: vi.fn(async () => ({ items: [] })),
  summarizeDocumentAwait: vi.fn(),
  summaryErrorKey: (s: string) => s,
  getRephrasingSuggestions: vi.fn(async () => []),
  cancelCorpusAnalysis: vi.fn(),
  getCorpusAnalysis: vi.fn(),
  getThreadAnalyses: vi.fn(async () => ({ analyses: [] })),
  isTerminalAnalysisStatus: () => true,
  getPreferences: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
  updateAnswerLanguage: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
}));
vi.mock("@/lib/auth", () => ({ getCsrfToken: () => null }));

import ChatPage from "@/app/chat/page";

const SOURCES = [1, 2, 3].map((rank) => ({ schema_version: 3, rank, filename: `doc-${rank}.pdf`, snippet: "…" }));

function mockHistory() {
  apiFetch.mockImplementation(async (url: string) => {
    if (typeof url !== "string") return [];
    if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-1", title: "Fil" }];
    if (/\/messages\/$/.test(url)) {
      return [
        { id: "m-1", role: "user", content: "Question ?" },
        { id: "m-2", role: "assistant", kind: "message", content: "Réponse [2].\n\nSources: [1], [2], [3]", sources: SOURCES },
      ];
    }
    return [];
  });
}

/** `compact` = largeur < 1200 px (tablette ou mobile). */
function stubViewport(compact: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: compact && /max-width:\s*1199px/.test(query),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as any;
}

const panel = () => document.getElementById("chat-sources") as HTMLElement;
const sourcesButton = () => document.querySelector('[aria-controls="chat-sources"]') as HTMLButtonElement;
const chip = (n: number) => screen.getByRole("button", { name: `Mettre en évidence la source ${n}` });
const card = (n: number) => document.getElementById(`src-${n}`) as HTMLElement;
const isHighlighted = (el: HTMLElement) => el.style.border.includes("var(--accent)");

async function mountChat() {
  render(<ChatPage />);
  await screen.findByRole("button", { name: "Mettre en évidence la source 2" });
}

describe("Chat — tiroir Sources (tablette / mobile)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    mockHistory();
  });
  afterEach(() => {
    delete (window as any).matchMedia;
    document.documentElement.style.overflow = "";
  });

  it("le panneau porte un nom accessible et le bouton Sources le contrôle", async () => {
    stubViewport(true);
    await mountChat();
    expect(screen.getByRole("complementary", { name: "Sources" })).toBe(panel());
    expect(sourcesButton()).toHaveAttribute("aria-expanded", "false");
    expect(panel()).toHaveAttribute("data-open", "false");
  });

  it("une citation [n] sélectionne la source, ouvre le tiroir et la montre", async () => {
    stubViewport(true);
    await mountChat();
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    chip(3).focus();
    fireEvent.click(chip(3));
    expect(panel()).toHaveAttribute("data-open", "true");
    expect(sourcesButton()).toHaveAttribute("aria-expanded", "true");
    expect(isHighlighted(card(3))).toBe(true);
    expect(scroll.mock.instances).toContain(card(3));
    // Le reste de la page est inerte, le focus est dans le tiroir.
    expect(screen.getByRole("textbox").closest("[inert]")).not.toBeNull();
    expect(panel().contains(document.activeElement)).toBe(true);
    scroll.mockRestore();
  });

  it("recliquer la même citation garde le tiroir ouvert sur la source (pas de bascule)", async () => {
    stubViewport(true);
    await mountChat();
    fireEvent.click(chip(2));
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(chip(2));
    expect(panel()).toHaveAttribute("data-open", "true");
    expect(isHighlighted(card(2))).toBe(true);
  });

  it("Échap ferme et rend le focus à la citation", async () => {
    stubViewport(true);
    await mountChat();
    chip(1).focus();
    fireEvent.click(chip(1));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(panel()).toHaveAttribute("data-open", "false");
    expect(document.activeElement).toBe(chip(1));
    expect(document.querySelectorAll("[inert]")).toHaveLength(0);
  });

  it("bouton Sources : ouvre ; « Fermer les sources » ferme et rend le focus", async () => {
    stubViewport(true);
    await mountChat();
    sourcesButton().focus();
    fireEvent.click(sourcesButton());
    expect(panel()).toHaveAttribute("data-open", "true");
    fireEvent.click(within(panel()).getByRole("button", { name: "Fermer les sources" }));
    expect(panel()).toHaveAttribute("data-open", "false");
    expect(document.activeElement).toBe(sourcesButton());
  });

  it("le voile ferme le tiroir", async () => {
    stubViewport(true);
    await mountChat();
    fireEvent.click(sourcesButton());
    const veil = panel().previousElementSibling as HTMLElement;
    expect(veil).toHaveAttribute("aria-hidden", "true");
    expect(veil).not.toHaveAttribute("inert");
    fireEvent.click(veil);
    expect(panel()).toHaveAttribute("data-open", "false");
  });
});

describe("Chat — desktop inchangé", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    mockHistory();
  });

  it("une citation bascule la surbrillance sans ouvrir de tiroir ni rendre la page inerte", async () => {
    // Pas de matchMedia (jsdom) : comportement desktop.
    await mountChat();
    fireEvent.click(chip(2));
    expect(isHighlighted(card(2))).toBe(true);
    expect(panel()).toHaveAttribute("data-open", "false");
    expect(document.querySelectorAll("[inert]")).toHaveLength(0);
    await act(async () => {
      fireEvent.click(chip(2));
    });
    expect(isHighlighted(card(2))).toBe(false);
  });
});

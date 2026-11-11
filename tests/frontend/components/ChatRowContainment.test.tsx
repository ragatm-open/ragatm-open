/**
 * Confinement des lignes de message.
 *
 * Chaque ligne `Message` porte la classe `messageRow` (`ChatLayout.module.css`),
 * frontière `contain: layout style` : sur un long fil, le navigateur ne reparcourt
 * plus les lignes hors écran à chaque image (PrePaint).
 *
 * Garde de source : le confinement de PEINTURE et `content-visibility` sont exclus.
 * Le premier rogne l'anneau de focus des actions au bord de la ligne ; le second
 * retire le contenu hors écran de l'arbre d'accessibilité et fausse la géométrie
 * du fil. jsdom ne calcule ni layout ni
 * peinture : ces effets se vérifient au banc navigateur, pas ici.
 */
import React from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/shell", () => ({
  AppShell: ({ children }: any) => <div>{children}</div>,
  Topbar: ({ actions }: any) => <div>{actions}</div>,
  useWorkspace: () => ({
    ragSpace: { id: "rs-1", name: "Espace" },
    ragSpaceId: "rs-1",
    currentStats: { documents: { total: 3 }, indexing: { in_progress: false } },
  }),
}));

const apiFetch = vi.fn(async (url: string) => {
  if (typeof url !== "string") return [];
  if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-1", title: "Fil" }];
  if (/\/messages\/$/.test(url)) {
    return [
      { id: "m-1", role: "user", content: "Question utilisateur" },
      { id: "m-2", role: "assistant", content: "Réponse assistant [1].", sources: [{ schema_version: 3, rank: 1, filename: "a.pdf", snippet: "…" }] },
    ];
  }
  return [];
});
vi.mock("@/lib/api", () => ({
  apiFetch: (...args: any[]) => (apiFetch as any)(...args),
  listDocuments: vi.fn(async () => ({ items: [] })),
  summarizeSourceDocumentAwait: vi.fn(),
  summaryErrorKey: (s: string) => s,
  getRephrasingSuggestions: vi.fn(async () => ({ status: "none", suggestions: [] })),
  cancelCorpusAnalysis: vi.fn(),
  getCorpusAnalysis: vi.fn(),
  getThreadAnalyses: vi.fn(async () => ({ analyses: [] })),
  isTerminalAnalysisStatus: () => true,
  getPreferences: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
  updateAnswerLanguage: vi.fn(async () => ({ preferred_locale: "fr", preferred_answer_language: "auto" })),
}));
vi.mock("@/lib/auth", () => ({ getCsrfToken: () => null }));

import ChatPage from "@/app/chat/page";

const CSS_PATH = path.resolve(__dirname, "../../../frontend/src/app/chat/_components/ChatLayout.module.css");

/** Racine de ligne : l'ancêtre du texte qui est enfant direct de la liste des messages. */
function rowOf(el: HTMLElement): HTMLElement {
  let cur: HTMLElement | null = el;
  while (cur && !cur.parentElement?.className.includes("threadInner")) cur = cur.parentElement;
  return cur as HTMLElement;
}

describe("Chat — confinement des lignes de message", () => {
  it("chaque ligne de message (utilisateur et assistant) porte la classe de confinement", async () => {
    render(<ChatPage />);
    const user = rowOf(await screen.findByText("Question utilisateur"));
    const assistant = rowOf(await screen.findByText(/Réponse assistant/));
    expect(user).not.toBeNull();
    expect(assistant).not.toBeNull();
    expect(user).not.toBe(assistant);
    expect(user.className).toContain("messageRow");
    expect(assistant.className).toContain("messageRow");
  });

  it("la règle est `contain: layout style` — ni peinture, ni content-visibility", () => {
    // Déclarations seulement : les commentaires peuvent citer ce qui est exclu.
    const css = readFileSync(CSS_PATH, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = css.match(/\.messageRow\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule![1].replace(/\s+/g, " ").trim()).toBe("contain: layout style;");
    expect(css).not.toMatch(/content-visibility/);
    expect(css).not.toMatch(/contain:\s*[^;]*\b(paint|content|strict)\b/);
    expect(css).not.toMatch(/overflow-clip-margin/);
  });
});

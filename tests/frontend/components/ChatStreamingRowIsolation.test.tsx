/**
 * Lignes de message pendant le streaming.
 *
 * Défaut corrigé : chaque jeton crée un nouveau tableau `messages` ; `Message`
 * n'étant pas mémoïsé (et recevant des closures recréées à chaque rendu), TOUTES
 * les lignes de l'historique se rerendaient — ~45 ms par jeton sur 450 messages.
 *
 * Instrumentation de TEST uniquement : les messages d'historique sont enveloppés
 * dans un Proxy qui compte les lectures de `text`. Pendant un flux, seule une ligne
 * `Message` lit le texte d'un message d'historique (utilisateur comme assistant) :
 * le compteur mesure donc les rendus de ces lignes, sans toucher au code de production.
 *
 * Les callbacks des lignes sont désormais stables ; les tests de fraîcheur
 * vérifient qu'ils agissent sur l'état COURANT (index après ajout, mode compact,
 * fil courant), pas sur celui du rendu où ils ont été créés.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* -------------------------------------------------------------- mocks ---- */

const textReads = new Map<string, number>();
const totalHistoryReads = () => [...textReads.values()].reduce((a, b) => a + b, 0);

vi.mock("@/app/chat/_lib/chatMessages", async () => {
  const actual = await vi.importActual<typeof import("@/app/chat/_lib/chatMessages")>("@/app/chat/_lib/chatMessages");
  return {
    ...actual,
    mapServerMessages: (list: any) =>
      actual.mapServerMessages(list).map(
        (m) =>
          new Proxy(m, {
            get(target, prop, receiver) {
              if (prop === "text" && target.id) textReads.set(target.id, (textReads.get(target.id) || 0) + 1);
              return Reflect.get(target, prop, receiver);
            },
          }),
      ),
  };
});

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

/* ------------------------------------------------------------ fixtures --- */

const src = (prefix: string) => [1, 2, 3].map((rank) => ({ schema_version: 3, rank, filename: `${prefix}-${rank}.pdf`, snippet: "…" }));

/** 20 messages d'historique (10 échanges), chacun avec sources et citations. */
function history(prefix: string, exchanges = 10) {
  const out: any[] = [];
  for (let i = 0; i < exchanges; i++) {
    out.push({ id: `${prefix}-u${i}`, role: "user", content: `Question ${prefix} ${i}` });
    out.push({ id: `${prefix}-a${i}`, role: "assistant", content: `Réponse ${prefix} ${i} [1] [2].\n\nSources: [1], [2], [3]`, sources: src(`${prefix}${i}`) });
  }
  return out;
}

let HISTORY: Record<string, any[]> = {};
/** Une fois un flux lancé, les rechargements d'historique restent en vol (pas de serveur réel). */
let holdHistory = false;
function mockApi() {
  apiFetch.mockImplementation(async (url: string, init?: RequestInit) => {
    if (typeof url !== "string") return [];
    if (url.startsWith("/api/v1/chat/threads/?")) return [{ id: "t-a", title: "Fil A" }, { id: "t-b", title: "Fil B" }];
    if (url === "/api/v1/chat/threads/" && init?.method === "POST") return { id: "t-new", title: null };
    const m = url.match(/\/chat\/threads\/([^/]+)\/messages\/$/);
    if (m) return holdHistory ? new Promise(() => {}) : HISTORY[m[1]] ?? [];
    return [];
  });
}

function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Flux contrôlé : chaque trame n'est libérée qu'à la demande du test. */
function controlledStream() {
  const enc = new TextEncoder();
  const queue: string[] = [];
  let wake: (() => void) | null = null;
  let closed = false;
  const response = {
    ok: true,
    status: 200,
    statusText: "OK",
    body: {
      getReader: () => ({
        read: async () => {
          while (!queue.length && !closed) await new Promise<void>((r) => (wake = r));
          if (!queue.length) return { value: undefined, done: true };
          return { value: enc.encode(queue.shift()!), done: false };
        },
        cancel: async () => {},
        releaseLock: () => {},
      }),
    },
  } as unknown as Response;
  const push = (frame: string) => { queue.push(frame); wake?.(); wake = null; };
  const close = () => { closed = true; wake?.(); wake = null; };
  return { response, push, close };
}

const settle = (ms = 30) => act(async () => void (await new Promise((r) => setTimeout(r, ms))));
const composer = () => screen.getByLabelText("Votre question") as HTMLTextAreaElement;
const card = (n: number) => document.getElementById(`src-${n}`) as HTMLElement;
const isHighlighted = (el: HTMLElement | null) => !!el && el.style.border.includes("var(--accent)");
const panel = () => document.getElementById("chat-sources") as HTMLElement;
const chipsOf = (n: number) => screen.getAllByRole("button", { name: `Mettre en évidence la source ${n}` });

/** matchMedia pilotable : `setCompact` notifie les abonnés comme un vrai redimensionnement. */
function controllableViewport(initialCompact: boolean) {
  let compact = initialCompact;
  const listeners = new Set<() => void>();
  window.matchMedia = vi.fn((query: string) => ({
    get matches() { return compact && /max-width:\s*1199px/.test(query); },
    media: query,
    addEventListener: (_: string, l: () => void) => listeners.add(l),
    removeEventListener: (_: string, l: () => void) => listeners.delete(l),
  })) as any;
  return { setCompact: (v: boolean) => act(() => { compact = v; listeners.forEach((l) => l()); }) };
}

async function mountChat(lastText: string) {
  render(<ChatPage />);
  await screen.findByText(lastText, { exact: false });
  await settle();
}

async function startStream(question = "Nouvelle question") {
  const stream = controlledStream();
  const fetchSpy = vi.fn(async () => stream.response);
  vi.stubGlobal("fetch", fetchSpy);
  holdHistory = true;
  fireEvent.change(composer(), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /Envoyer/ }));
  stream.push(sseFrame("meta", { request_id: "r-1", thread_id: "t-a", log_id: "log-1" }));
  stream.push(sseFrame("sources", { sources: src("new") }));
  await settle();
  return { ...stream, fetchSpy };
}

/* ---------------------------------------------------------------- tests -- */

describe("Chat — lignes d'historique pendant le streaming", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    textReads.clear();
    holdHistory = false;
    HISTORY = { "t-a": history("a"), "t-b": history("b", 2) };
    mockApi();
  });
  afterEach(() => {
    delete (window as any).matchMedia;
    vi.unstubAllGlobals();
  });

  it("20 jetons ne rerendent aucune ligne d'historique, le message courant s'affiche à chaque jeton", async () => {
    await mountChat("Réponse a 9");
    const stream = await startStream();
    expect(totalHistoryReads()).toBeGreaterThan(0); // la sentinelle compte bien les rendus

    const before = new Map(textReads);
    let shown = "";
    for (let k = 0; k < 20; k++) {
      const token = ` mot${k}`;
      shown += token;
      stream.push(sseFrame("token", { delta: token }));
      await settle(5);
      // chaque jeton est visible dès sa mise à jour
      expect(document.body.textContent).toContain(shown.trim());
    }
    // aucune ligne d'historique (utilisateur ou assistant) n'a été rerendue par le flux
    for (const [id, n] of textReads) expect({ id, reads: n }).toEqual({ id, reads: before.get(id) ?? 0 });

    stream.close();
    await settle();
  });

  it("citation : le callback stable vise la ligne cliquée, y compris après l'ajout d'un échange", async () => {
    controllableViewport(false); // desktop : bascule de surbrillance
    await mountChat("Réponse a 9");

    // Avant ajout : la dernière réponse (index 19) est celle du panneau.
    fireEvent.click(chipsOf(2).at(-1)!);
    expect(isHighlighted(card(2))).toBe(true);
    fireEvent.click(chipsOf(2).at(-1)!); // bascule : désélection
    expect(isHighlighted(card(2))).toBe(false);

    // Un nouvel échange est ajouté : la ligne nouvelle a l'index 21, le panneau suit.
    const stream = await startStream();
    stream.push(sseFrame("token", { delta: "Nouvelle réponse [1] [3]." }));
    stream.push(sseFrame("done", { request_id: "r-1", thread_id: "t-a", log_id: "log-1", status: "ok" }));
    stream.close();
    await settle(60);

    const newCard = card(3);
    expect(newCard.textContent).toContain("new-3.pdf");
    fireEvent.click(chipsOf(3).at(-1)!); // pastille [3] de la NOUVELLE réponse
    expect(isHighlighted(card(3))).toBe(true);
  });

  it("citation : le passage en mode compact est pris en compte par une ligne non rerendue", async () => {
    const vp = controllableViewport(false);
    await mountChat("Réponse a 9");
    expect(panel()).toHaveAttribute("data-open", "false");

    // Le viewport passe sous 1200 px : la ligne historique n'a pas à se rerendre,
    // mais son clic doit désormais ouvrir le tiroir sur la source.
    await vp.setCompact(true);
    fireEvent.click(chipsOf(1)[0]); // pastille [1] de la PREMIÈRE réponse (index 1)
    expect(panel()).toHaveAttribute("data-open", "true");
    expect(card(1).textContent).toContain("a0-1.pdf");
    expect(isHighlighted(card(1))).toBe(true);
  });

  it("changement de fil : une citation du fil B agit sur le message de B", async () => {
    vi.stubGlobal("fetch", vi.fn());
    controllableViewport(true);
    await mountChat("Réponse a 9");

    fireEvent.click(screen.getByRole("button", { name: /Fil A/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Fil B/ }));
    await screen.findByText("Réponse b 1", { exact: false });
    await settle();

    fireEvent.click(chipsOf(2)[0]); // première réponse du fil B
    expect(panel()).toHaveAttribute("data-open", "true");
    expect(card(2).textContent).toContain("b0-2.pdf");
    expect(isHighlighted(card(2))).toBe(true);
  });

  it("choix de portée : le callback stable envoie le choix pour le fil COURANT", async () => {
    HISTORY["t-b"] = [
      { id: "c-u", role: "user", content: "Analyse tout" },
      { id: "c-a", role: "assistant", content: "Deux modes possibles.", awaiting_corpus_choice: true, global_analysis_available: true },
    ];
    await mountChat("Réponse a 9");
    fireEvent.click(screen.getByRole("button", { name: /Fil A/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Fil B/ }));
    await screen.findByText("Deux modes possibles.");
    await settle();

    const stream = controlledStream();
    const fetchSpy = vi.fn(async () => stream.response);
    vi.stubGlobal("fetch", fetchSpy);
    fireEvent.click(screen.getByRole("button", { name: /Réponse rapide/ }));
    await settle();
    stream.close();
    await settle();

    const bodies = fetchSpy.mock.calls.map(([, init]) => JSON.parse(String((init as RequestInit).body)));
    expect(bodies).toHaveLength(1);
    expect(bodies[0].thread_id).toBe("t-b");
    expect(bodies[0].corpus_choice).toBeTruthy();
  });

  it("une ligne d'historique se rerend quand SA sélection change (pas d'interface figée)", async () => {
    controllableViewport(false);
    await mountChat("Réponse a 9");
    const before = textReads.get("a-a9") ?? 0;
    const others = totalHistoryReads() - before;
    fireEvent.click(chipsOf(1).at(-1)!);
    await settle();
    expect(textReads.get("a-a9")).toBeGreaterThan(before); // la ligne sélectionnée se rerend
    expect(totalHistoryReads() - (textReads.get("a-a9") ?? 0)).toBe(others); // les autres non
  });
});
